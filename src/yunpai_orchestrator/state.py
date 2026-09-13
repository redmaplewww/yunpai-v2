"""RunState v2（书二 §2）——旧 RunState 的超集，字段名向后兼容随迁组件。

关键差异（相对旧 models.RunState）：
- ``messages`` 真正读写（add_messages reducer），跨 run 由 ``thread_id`` 关联（痛点 11）；
- 删除 ``next_step_index``（调度改由 DAG ready 计算，痛点 10）；
- 新增 ``thread_id / retry_counts / review_findings / knowledge_consumed / summary``；
- ``outputs`` 沿用旧约定：**以工具名为键**（bridge 的 output_data 按工具名回读）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypedDict
from uuid import uuid4

from langgraph.graph.message import add_messages

from .models import summarize  # 兼容随迁组件的 summarize 引用

Status = Literal["queued", "running", "waiting_human", "completed", "failed", "blocked", "cancelled"]
StepStatus = Literal["pending", "ready", "running", "completed", "failed", "blocked", "skipped", "stale"]
Route = Literal["workflow", "free", "chat"]


class StepRecordV2(TypedDict, total=False):
    step_id: str
    kind: Literal["tool", "skill"]
    module: str
    tool: str                 # 兼容旧 plan step 结构（bridge/进化信号按 tool 读）
    name: str                 # skill 名（kind=skill 时）
    status: StepStatus
    depends_on: list[str]
    attempt: int
    input_summary: dict[str, Any]
    output_summary: dict[str, Any]
    evidence: list[dict[str, Any]]
    error: dict[str, Any] | None
    started_at: str
    finished_at: str


class RunStateV2(TypedDict, total=False):
    # ── 标识 ──
    run_id: str
    task_id: str
    tenant_id: str
    thread_id: str            # 新增：跨 run 会话
    # ── 输入（沿旧键名）──
    request: dict[str, Any]
    attachments: list[dict[str, Any]]
    message: str
    # ── 会话记忆（新增激活）──
    messages: Annotated[list[Any], add_messages]
    # ── 路由与计划 ──
    intent: dict[str, Any]
    route: Route
    route_decision: dict[str, Any]
    workflow_id: str
    workflow_version: str
    plan: list[StepRecordV2]
    current_step: dict[str, Any]
    # ── 执行 ──
    outputs: dict[str, Any]   # 工具名 → Goal 信封
    evidence: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    retry_counts: dict[str, int]      # 新增：step_id → 已重试
    # ── 审查与门 ──
    pending_gate: dict[str, Any] | None
    approvals: list[dict[str, Any]]
    authorized_steps: list[str]
    review_findings: list[dict[str, Any]]  # 新增：规则命中记录（可审计）
    # 新增（书二 §6.2.1 commit 段）：`_apply_*` 钩子「已生效」的留痕——与 trace 不同，
    # 这里只记**真的落了库的生效动作**（翻正／冻结／发布）；reject 路径永不写入。
    review_applied: list[dict[str, Any]]
    # ── 知识（进化闭环）──
    knowledge_context: list[dict[str, Any]]
    knowledge_consumed: bool             # 新增：统筹是否真实消费（缺口②留痕）
    # ── 终态 ──
    status: Status
    response: str
    summary: dict[str, Any]              # 新增
    trace: list[dict[str, Any]]
    model: dict[str, Any]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_state_v2(
    request: dict[str, Any],
    *,
    tenant_id: str = "default",
    thread_id: str | None = None,
) -> RunStateV2:
    message = str(request.get("message") or request.get("task") or "")
    return RunStateV2(
        run_id=f"run-{uuid4().hex}",
        task_id=f"task-{uuid4().hex}",
        tenant_id=str(request.get("tenant_id") or tenant_id),
        thread_id=thread_id or f"thread-{uuid4().hex[:12]}",
        request=request,
        attachments=list(request.get("attachments") or request.get("documents") or []),
        message=message,
        messages=[],
        intent={},
        route="chat",
        route_decision={},
        workflow_id="",
        workflow_version="",
        plan=[],
        current_step={},
        outputs={},
        evidence=[],
        steps=[],
        errors=[],
        retry_counts={},
        pending_gate=None,
        approvals=[],
        authorized_steps=[],
        review_findings=[],
        knowledge_context=[],
        knowledge_consumed=False,
        status="queued",
        response="",
        summary={},
        trace=[{"event": "run.created", "at": now_iso()}],
        model={},
    )


def public_state(state: RunStateV2) -> dict[str, Any]:
    """API 对外视图：沿旧 ``_public_state`` 语义，快照 content_b64 脱敏。"""
    def _clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: ("[omitted]" if k == "content_b64" and isinstance(v, str) and len(v) > 64 else _clean(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [_clean(v) for v in value]
        return value

    keys = (
        "run_id", "task_id", "tenant_id", "thread_id", "status", "route", "route_decision",
        "workflow_id", "plan", "outputs", "steps", "evidence", "pending_gate", "approvals",
        "authorized_steps", "errors", "response", "intent", "model", "summary", "retry_counts",
    )
    return {k: _clean(state.get(k)) for k in keys if k in state}
