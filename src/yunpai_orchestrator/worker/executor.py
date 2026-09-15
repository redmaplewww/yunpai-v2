"""Worker 执行节点（书二 §5.2）：装配 → ToolRegistry/SkillRegistry 调用 → Goal 信封。

异常映射按信封 errors[].code 分类，不做工具名硬编码（治痛点 6）。
"""
from __future__ import annotations

from typing import Any, Callable

from ..contracts import normalize_contract_result
from ..registry import ToolHTTPError
from ..state import RunStateV2, StepRecordV2, now_iso, summarize
from .assembler import Assembler, blocked_envelope


def tool_context(state: RunStateV2, tool: str) -> dict[str, Any]:
    request = state.get("request", {})
    principal = request.get("principal") or {}
    actor_user = principal.get("user") or request.get("actor_user") or "agent"
    context = {
        "task_id": state.get("task_id", ""),
        "run_id": state.get("run_id", ""),
        "tenant_id": state.get("tenant_id", "default"),
        "trace_id": state.get("run_id", ""),
        "idempotency_key": f"{state.get('task_id', 'task')}:{tool}",
        "actor_user": actor_user,
        "actor_role": principal.get("role") or request.get("actor_role") or "",
        # 键名映射别名（INFRA-DECISIONS §1.2）：迁移进来的本地 handler 与 V2 自己的
        # workers.py:183/187/766/772 都读 ``actor``，而本函数从未提供该键，导致
        # 它们静默退化为 "operator"。此处只做同源别名，不引入第二事实源。
        "actor": actor_user,
    }
    # M6 uses an isolated ledger path during local execution. Preserve it in
    # the shared context when the request supplies one; do not add an empty
    # key so the existing context contract remains stable for other modules.
    m6_db_path = request.get("m6_db_path")
    if m6_db_path:
        context["m6_db_path"] = str(m6_db_path)
    return context


def _error_envelope(tool: str, code: str, message: str) -> dict[str, Any]:
    return {
        "success": False, "status": "failed", "code": code,
        "errors": [{"code": code, "message": message[:500], "tool": tool}],
        "data": {}, "invoked_tools": [tool],
    }


def _classify_exception(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ToolHTTPError):
        return str(exc.code), str(exc)
    if isinstance(exc, ValueError):
        return "INVALID_INPUT", str(exc)
    if isinstance(exc, KeyError):
        return "UNKNOWN_TOOL", str(exc)
    if isinstance(exc, RuntimeError):
        return "UPSTREAM_UNAVAILABLE", str(exc)
    return "TOOL_ERROR", f"{type(exc).__name__}: {exc}"


def make_worker_execute(deps) -> Callable:
    """deps: GraphDeps（见 graph.py）。返回 LangGraph 节点函数。"""
    async def worker_execute(state: RunStateV2) -> dict[str, Any]:
        step: StepRecordV2 = dict(state.get("current_step") or {})  # type: ignore[assignment]
        tool = str(step.get("tool") or step.get("name") or "")
        kind = str(step.get("kind") or "tool")
        started = now_iso()
        trace = list(state.get("trace") or [])
        trace.append({"event": "react.action", "agent": "worker", "tool": tool,
                      "kind": kind, "at": started})
        result: dict[str, Any]
        failed = False
        try:
            if kind == "skill":
                payload = dict(state.get("request", {}).get(tool) or {})
                result = await deps.skills.call(tool, payload, tool_context(state, tool))
            else:
                assembled = await deps.assembler.assemble(tool, state)
                if assembled.blocked:
                    result = assembled.payload
                    trace.append({"event": "bridge.blocked_input", "tool": tool,
                                  "missing": [m.get("field") for m in assembled.missing], "at": now_iso()})
                else:
                    result = await deps.registry.call(tool, assembled.payload, tool_context(state, tool))
        except Exception as exc:  # noqa: BLE001 —— 工具失败必须成为步骤结果而非击穿图
            code, message = _classify_exception(exc)
            result = _error_envelope(tool, code, message)
            failed = True

        code = str(result.get("code") or "")
        if failed:
            status = "failed"
        elif code == "BLOCKED_INPUT":
            status = "blocked"
        elif result.get("success") is False:
            status = "failed"  # 非阻塞失败信封（success=False）同样不得当成功
        else:
            status = "completed"
        trace.append({"event": "react.observation", "agent": "worker", "tool": tool,
                      "status": status, "at": now_iso()})
        step = {**step,
                "status": status,
                "input_summary": summarize(result.get("data") or {}),
                "output_summary": summarize(result),
                "error": result.get("errors")[0] if result.get("errors") else None,
                "finished_at": now_iso(), "started_at": started}
        outputs = {**state.get("outputs", {}), tool: result}
        errors = list(state.get("errors") or [])
        if failed:
            errors.append({"tool": tool, "code": (result.get("errors") or [{}])[0].get("code"),
                           "message": (result.get("errors") or [{}])[0].get("message")})
        return {"current_step": step, "outputs": outputs, "steps": [step],
                "errors": errors, "trace": trace}

    return worker_execute
