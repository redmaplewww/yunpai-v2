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
    return {
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


#: 派发 Skill 时从**顶层** request 并入载荷的键（G2，INT2 第五轮）。
#: 调用方显式写进 ``request[技能名]`` 的值永远优先（只在缺键时并入）。
_SKILL_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "message", "product_code", "product_name", "attachments", "documents",
)


def skill_payload(state: RunStateV2, tool: str) -> dict[str, Any]:
    """Skill 载荷 = ``request[tool]``（显式优先）+ 顶层同义键（仅缺键时并入）。

    G2（INT2 第五轮）背景：V2 只把 ``request[技能名]`` 当载荷（原 ``executor.py:68``），
    而前端「基础资料识别落库」面板把产品编码/文件放在**顶层** ``message``/``documents``
    里 → ``identify_business_data`` 的正则取不到产品编码、也看不到文件（实测 records=0）。
    这里只做「缺键才并入」，不覆盖调用方显式值；tool 路径完全不变。
    """
    request = state.get("request", {})
    payload = dict(request.get(tool) or {})
    for key in _SKILL_TOP_LEVEL_KEYS:
        if key not in payload and request.get(key) not in (None, ""):
            payload[key] = request[key]
    # 文件别名：``identify_business_data`` 读 ``files``（legacy ``_payload_for`` 同口径
    # ``files = request.documents or request.attachments``）。仅当调用方未显式给
    # ``files`` 时补上，否则整批附件会因键名不同被当成「没有文件」。
    if not payload.get("files"):
        files = request.get("documents") or request.get("attachments")
        if isinstance(files, list) and files:
            payload["files"] = files
    return payload


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
                payload = skill_payload(state, tool)
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
