"""LangGraph 主图（书二 §3）——**唯一执行路径**（治痛点 1）。

与旧实现的根本区别：不存在第二套手写执行循环；Studio 与生产共用本图编译结果。
人工 Gate 用 interrupt() 挂起（checkpointer 持久化），resume 用 Command 恢复；
挂起前经 side-effect 把 waiting_human 状态写入 RunRepository（API/前端可见）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .llm import QwenRouter
from .orchestrator.knowledge_injection import attach_knowledge
from .orchestrator.planner import Planner
from .orchestrator.router import Router
from .orchestrator.workflow_engine import WorkflowEngine
from .registry import ToolRegistry
from .reviewer import rules
from .reviewer import gates as gate_mod
from .reviewer import usage_feedback
from .skills import SkillRegistry
from .models import summarize
from .state import RunStateV2, now_iso, public_state
from .worker.assembler import Assembler
from .worker.executor import make_worker_execute


@dataclass
class GraphDeps:
    registry: ToolRegistry
    skills: SkillRegistry
    router: Router
    planner: Planner
    engine: WorkflowEngine
    assembler: Assembler
    config: Any = None
    evolution: Any = None            # EvolutionRepository | None
    repository: Any = None           # RunRepository | None（Gate 挂起镜像）
    max_step_retries: int = 2
    knowledge_top_k: int = 5


def default_deps(config: Any = None, *, registry: ToolRegistry | None = None,
                 skills: SkillRegistry | None = None, evolution: Any = None,
                 repository: Any = None) -> GraphDeps:
    from .config import OrchestratorConfig
    from .registry import build_default_registry
    from .skills import build_default_skill_registry

    cfg = config or OrchestratorConfig()
    reg = registry or build_default_registry()
    sk = skills or build_default_skill_registry(reg)
    # Skill 内部派发必须走同一个 ToolRegistry（见 INFRA-DECISIONS §1）；
    # 显式传入的 SkillRegistry 若未注入，则在此补上，避免 Skill 路径静默不可用。
    if getattr(sk, "tool_registry", None) is None:
        sk.tool_registry = reg
    sk.validate_tools(reg.specs)
    engine = WorkflowEngine()
    return GraphDeps(
        registry=reg, skills=sk,
        router=Router(QwenRouter(), reg, sk, engine),
        planner=Planner(engine), engine=engine,
        assembler=Assembler(reg), config=cfg,
        evolution=evolution, repository=repository,
        max_step_retries=cfg.max_step_retries, knowledge_top_k=cfg.knowledge_top_k,
    )


def compute_recursion_limit(plan_len: int, *, base: int = 16, per_step: int = 5) -> int:
    """图循环上限：每步骤（分发+执行+审查）+ 重试余量。"""
    return base + per_step * max(plan_len, 1) * 2




def _jsonable_state(state: dict) -> dict:
    """落库前把 LangGraph message 对象转为可 JSON 序列化的 dict。"""
    out = dict(state)
    messages = []
    for m in out.get("messages") or []:
        if isinstance(m, dict):
            messages.append(m)
        else:
            messages.append({"role": getattr(m, "type", "unknown"),
                             "content": getattr(m, "content", str(m))})
    out["messages"] = messages
    return out

# ── 节点实现 ────────────────────────────────────────────────────

def orchestrator_plan_node(deps: GraphDeps) -> Callable:
    async def _node(state: RunStateV2) -> dict[str, Any]:
        knowledge = attach_knowledge(state, deps.evolution, deps.knowledge_top_k) if deps.evolution else []
        decision = await deps.router.decide({**state, "knowledge_context": knowledge})
        plan = deps.planner.build(decision, state)
        trace = list(state.get("trace") or [])
        trace.append({"event": "agent.route", "agent": "planner", "route": decision.route,
                      "source": decision.source, "selected_tools": [s.get("tool", "") for s in plan],
                      "reason": decision.reason, "at": now_iso()})
        route_decision = decision.as_dict()
        if decision.answer:
            route_decision["answer"] = decision.answer
        return {
            "route": decision.route, "route_decision": route_decision,
            "intent": {"intent": decision.intent, "source": decision.source},
            "workflow_id": decision.workflow_id or "",
            "plan": plan, "knowledge_context": knowledge,
            "knowledge_consumed": bool(knowledge) and decision.source == "llm",
            "messages": [{"role": "user", "content": state.get("message", "")}],
            "status": "running", "trace": trace,
        }
    return _node


def orchestrator_dispatch_node(deps: GraphDeps) -> Callable:
    async def _node(state: RunStateV2) -> dict[str, Any]:
        plan = list(state.get("plan") or [])
        engine = deps.engine
        ready = engine.ready(plan)
        trace = list(state.get("trace") or [])
        if ready:
            step_id = ready[0]
            step = next(s for s in plan if s["step_id"] == step_id)
            step = {**step, "status": "running", "started_at": now_iso()}
            plan = engine.mark(plan, step_id, "running")
            trace.append({"event": "react.thought", "agent": "orchestrator",
                          "reason": f"分发步骤 {step_id}", "at": now_iso()})
            return {"plan": plan, "current_step": step, "status": "running", "trace": trace}
        stats = engine.stats(plan)
        if stats.get("pending", 0) or stats.get("blocked", 0):
            trace.append({"event": "run.blocked", "stats": stats, "at": now_iso()})
            return {"current_step": {}, "status": "blocked", "trace": trace,
                    "summary": {"steps": stats, "blocked": True}}
        trace.append({"event": "run.dispatch_done", "stats": stats, "at": now_iso()})
        return {"current_step": {}, "trace": trace}
    return _node


def chat_answer_node(deps: GraphDeps) -> Callable:
    async def _node(state: RunStateV2) -> dict[str, Any]:
        answer = str((state.get("route_decision") or {}).get("answer")
                     or state.get("route_decision", {}).get("reason") or "")
        if not answer:
            answer = "已收到。该请求被路由为对话模式；如需执行业务链请上传业务文件或显式指定工具。"
        trace = list(state.get("trace") or [])
        trace.append({"event": "chat.answered", "at": now_iso()})
        return {"response": answer, "status": "completed",
                "summary": {"mode": "chat"}, "trace": trace,
                "messages": [{"role": "assistant", "content": answer}]}
    return _node


def _stamp_engineering_approval(state: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any] | None:
    """工程门批准 → 在该工具产出的 bom/sop generation 上写入 approval_status=approved。

    bridge.read_approved_bom/read_approved_route 只消费已批准 BOM/route，
    此处是「批准」动作落到 outputs 的唯一写入点；非 engineering 门不改 outputs。
    """
    if str(gate.get("type")) != "engineering":
        return None
    tool = str(gate.get("tool") or "")
    outputs = dict(state.get("outputs") or {})
    envelope = outputs.get(tool)
    if not isinstance(envelope, dict):
        return None
    envelope = dict(envelope)
    holders = [envelope]
    if isinstance(envelope.get("data"), dict):
        data = dict(envelope["data"])
        envelope["data"] = data
        holders.append(data)
    changed = False
    for holder in holders:
        for section in ("bom_generation", "sop_generation"):
            block = holder.get(section)
            if isinstance(block, dict):
                holder[section] = {**block, "approval_status": "approved"}
                changed = True
    if not changed:
        return None
    outputs[tool] = envelope
    return outputs


def _apply_m5_release(state: dict[str, Any], gate: dict[str, Any],
                      actor: str = "") -> dict[str, Any] | None:
    """apply 门批准 → M5 draft 计划真实发布（approved→released + head CAS）。

    书二 §6.2「apply 释放走 M5 head CAS〔迁〕`_apply_m5_release` 语义」。
    仅当 M5 库可用（``request.m5_db_path`` 或 ``YUNPAI_M5_DB``）且该工具产出里
    带 ``plan_version``/``scenario_id`` 时生效；缺库或缺版本时**不触碰库、也不把
    RunState 标成 released**（preview/未持久化求解的真实状态）。
    发布冲突不抛异常（抛错会把 resume 值固化进 checkpoint，Gate 通道会卡死）：
    改为把冲突写回 ``pending_gate``，保持人工可恢复。
    """
    if str(gate.get("type")) != "apply":
        return None
    import os

    tool = str(gate.get("tool") or "")
    outputs = dict(state.get("outputs") or {})
    envelope = outputs.get(tool)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), dict):
        return None
    data = dict(envelope["data"])
    plan_version = str(data.get("plan_version") or "")
    schedule = data.get("schedule") if isinstance(data.get("schedule"), dict) else {}
    result_block = data.get("result") if isinstance(data.get("result"), dict) else {}
    nested_schedule = result_block.get("schedule") if isinstance(result_block.get("schedule"), dict) else {}
    scenario_id = str(
        data.get("scenario_id") or schedule.get("scenario_id")
        or nested_schedule.get("scenario_id")
        or (state.get("request") or {}).get("scenario_id") or ""
    )
    db_path = str((state.get("request") or {}).get("m5_db_path") or os.getenv("YUNPAI_M5_DB") or "")
    if not plan_version or not scenario_id or not db_path:
        return None
    from .m5_repository import M5Repository, M5RepositoryError

    repo = M5Repository(db_path)
    expected = (state.get("request") or {}).get("expected_head_revision")
    expected_revision = int(expected) if expected is not None and str(expected).isdigit() else None
    readback: dict[str, Any] = {}
    try:
        applied = repo.apply_release(
            plan_version, scenario_id,
            tenant_id=str(state.get("tenant_id") or "default"), gate="apply",
            actor=str(actor or ""), task_id=str(state.get("task_id") or ""),
            trace_id=f"{state.get('task_id', 'task')}:apply",
            expected_head_revision=expected_revision,
        )
        readback = repo.readback_after_release(plan_version, scenario_id)
    except M5RepositoryError as exc:
        existing = repo.get_plan(plan_version)
        head = repo.get_head(scenario_id)
        if (exc.code == "PLAN_PROTECTED" and existing
                and existing.get("lifecycle_status") == "released"
                and (head or {}).get("head_plan_version") == plan_version):
            # 幂等重放：同计划已 released 且 head 指向它 → 视为已应用
            readback = repo.readback_after_release(plan_version, scenario_id)
            applied = {"head_revision": readback.get("head_revision")}
        else:
            trace = list(state.get("trace") or [])
            trace.append({"event": "m5.release_conflict", "code": exc.code,
                          "message": exc.message, "at": now_iso()})
            # 冲突不得静默走 completed：由调用方撤回本次授权并把步骤退回 pending，
            # 让 apply 门重新打开（人工可恢复）。
            return {"release_conflict": {"code": exc.code, "message": exc.message},
                    "trace": trace}
    data.update({
        "lifecycle_status": "released",
        "head_revision": applied.get("head_revision") or readback.get("head_revision"),
        "released_at": (repo.get_plan(plan_version) or {}).get("released_at"),
    })
    evidence = list(envelope.get("evidence") or [])
    evidence.append({"module": "m5", "source_ref": plan_version,
                     "evidence_ref": f"m5:{plan_version}:release",
                     "detail": f"Apply Gate 真实发布：lifecycle=released, "
                               f"head revision={data['head_revision']}"})
    trace = list(state.get("trace") or [])
    trace.append({"event": "m5.released", "plan_version": plan_version,
                  "scenario_id": scenario_id, "head_revision": data["head_revision"],
                  "at": now_iso()})
    return {"outputs": {**outputs, tool: {**envelope, "data": data, "evidence": evidence}},
            "trace": trace}


# ── M6 财务：三段式的 commit 段（书二 §6.2.1 / D-005 / F-008）─────────────────
#
# 为什么在这里、而不是在工具里：v2 图内唯一开门点是 `reviewer_check_node`，且在
# `worker_execute` 之后——"工具自己翻状态"就等于"生效写在人工批准之前"（B-001 的
# 原病）。因此 M6 写工具分成两半：工具只做 **propose**（写 trial 草稿 / 回报待确认的
# 事实），**生效**（trial→confirmed、月账冻结、单据生效）只发生在本节的钩子，即
# `approve` 分支的 commit 段。照 `_apply_m5_release` 的既有经验：
#
# - **冲突不抛异常**（抛错会把 resume 值固化进 checkpoint、Gate 通道卡死）——改为返回
#   `m6_conflict`，由调用方撤回本次授权并把步骤退回 pending，保持人工可恢复；
# - 钩子只回自己拥有的增量（`outputs` + `trace_events`），**不整份覆盖 trace**，
#   因此 approve 分支已有的 react.review 等留痕不会丢。
#
# 库路径与 handler 同构（`m6_store.store_path`：request.m6_db_path → `YUNPAI_M6_DB`
# → `runtime/yunpai-m6.sqlite`）；与 M5 同一取舍：进程内嵌入用环境变量指定库。

def _m6_gate_envelope(state: dict[str, Any], gate: dict[str, Any],
                      tool: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """取该 finance 门对应的工具输出信封（门型/工具名不符或产出缺失时返回 None）。"""
    if str(gate.get("type")) != "finance" or str(gate.get("tool") or "") != tool:
        return None
    envelope = (state.get("outputs") or {}).get(tool)
    if not isinstance(envelope, dict):
        return None
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    return envelope, dict(data)


def _m6_store(state: dict[str, Any]):
    from .m6_store import M6Store, store_path

    return M6Store(store_path(state.get("request") or {}))


def _apply_m6_costing_confirm(state: dict[str, Any], gate: dict[str, Any],
                              *, actor: str = "") -> dict[str, Any] | None:
    """finance 门批准 → 成本快照 ``trial → confirmed``（"正式成本"落账）。

    只对 `confirm_costing_snapshot` 生效（幂等：已 confirmed 时 `changed=False`）。
    失败（快照消失/期间已冻结）走冲突路径，不把「已授权」写成既成事实。
    """
    got = _m6_gate_envelope(state, gate, "confirm_costing_snapshot")
    if got is None:
        return None
    envelope, data = got
    snapshot_id = str(data.get("snapshot_id") or "")
    if not snapshot_id:
        return None
    tenant_id = str(state.get("tenant_id") or "default")
    result = _m6_store(state).confirm_snapshot(snapshot_id, actor=actor, tenant_id=tenant_id)
    if not result.get("success"):
        code = str(result.get("code") or "CONFIRM_FAILED")
        return {"m6_conflict": {"code": code, "snapshot_id": snapshot_id, "tool": "confirm_costing_snapshot"},
                "trace_events": [{"event": "m6.costing_confirm_conflict", "code": code,
                                  "snapshot_id": snapshot_id, "at": now_iso()}]}
    stamped = {**data, "status": "confirmed", "changed": bool(result.get("changed")),
               "confirmed_at": result.get("confirmed_at"), "confirmed_by": actor,
               "pending_confirmation": False, "committed_by": "finance_gate"}
    evidence = list(envelope.get("evidence") or []) + [{
        "module": "m6", "source_ref": f"snapshot:{snapshot_id}",
        "evidence_ref": f"m6:{snapshot_id}:confirm",
        "detail": f"Finance Gate 批准 → trial→confirmed（actor={actor or 'human'}），正式成本落账"}]
    return {"outputs": {"confirm_costing_snapshot": {**envelope, "data": stamped,
                                                    "result": stamped, "evidence": evidence}},
            "trace_events": [{"event": "m6.costing_confirmed", "snapshot_id": snapshot_id,
                              "period": result.get("period"), "changed": bool(result.get("changed")),
                              "at": now_iso()}]}


def _apply_m6_close_month(state: dict[str, Any], gate: dict[str, Any],
                          *, actor: str = "") -> dict[str, Any] | None:
    """finance 门批准 → 冻结月账（月末结账）。

    冻结的是**被审阅的那组合计**（propose 段回报、人工据此批准），不是在 commit 时
    重算——审阅什么就冻结什么，避免"批了 A 冻了 B"。
    """
    got = _m6_gate_envelope(state, gate, "close_month_costing")
    if got is None:
        return None
    envelope, data = got
    period = str(data.get("period") or "")
    summary = data.get("month_summary") if isinstance(data.get("month_summary"), dict) else {}
    if not period:
        return None
    totals = {
        "total_cost": summary.get("total_cost", 0.0),
        "by_order": summary.get("by_order", []),
        "trial_count": summary.get("trial_count", 0),
        "trial_excluded": True,
    }
    tenant_id = str(state.get("tenant_id") or "default")
    result = _m6_store(state).close_month(
        period=period, totals=totals, snapshot_count=int(summary.get("snapshot_count") or 0),
        actor=actor, tenant_id=tenant_id)
    if not result.get("success"):
        code = str(result.get("code") or "CLOSE_FAILED")
        return {"m6_conflict": {"code": code, "period": period, "tool": "close_month_costing"},
                "trace_events": [{"event": "m6.month_close_conflict", "code": code,
                                  "period": period, "at": now_iso()}]}
    stamped = {**data, "pending_close": False, "frozen": True,
               "closed_at": result.get("closed_at"), "closed_by": actor,
               "snapshot_count": int(result.get("snapshot_count") or 0), "totals": totals,
               "committed_by": "finance_gate"}
    evidence = list(envelope.get("evidence") or []) + [{
        "module": "m6", "source_ref": f"month:{period}",
        "evidence_ref": f"m6:{period}:close",
        "detail": f"Finance Gate 批准 → 月账冻结（confirmed {result.get('snapshot_count')} 版，"
                  f"合计 {totals['total_cost']}；actor={actor or 'human'}）"}]
    return {"outputs": {"close_month_costing": {**envelope, "data": stamped,
                                                  "result": stamped, "evidence": evidence}},
            "trace_events": [{"event": "m6.month_closed", "period": period,
                              "closed_at": result.get("closed_at"), "at": now_iso()}]}


def _apply_m6_document_commit(state: dict[str, Any], gate: dict[str, Any],
                              *, actor: str = "") -> dict[str, Any] | None:
    """finance 门批准 → 单据 ``trial → confirmed``（生效凭据）。

    B1 第二批还没有单据工具（报价单/对账单台账在 B2、送货单在 B3），故本钩子按
    **约定**分派：M6 单据写工具的产出带 ``data.pending_document_commit=True`` 且给出
    ``doc_id``（与 `confirm_costing_snapshot` 的 ``pending_confirmation`` 同一写法）。
    B2/B3 的工具照此声明即可复用本钩子，无需再动 `reviewer_check_node`。
    """
    if str(gate.get("type")) != "finance":
        return None
    tool = str(gate.get("tool") or "")
    envelope = (state.get("outputs") or {}).get(tool)
    if not isinstance(envelope, dict):
        return None
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    if not data.get("pending_document_commit"):
        return None
    doc_id = str(data.get("doc_id") or "")
    if not doc_id:
        return None
    tenant_id = str(state.get("tenant_id") or "default")
    result = _m6_store(state).confirm_document(doc_id, actor=actor, tenant_id=tenant_id)
    if not result.get("success"):
        code = str(result.get("code") or "CONFIRM_FAILED")
        return {"m6_conflict": {"code": code, "doc_id": doc_id, "tool": tool},
                "trace_events": [{"event": "m6.document_confirm_conflict", "code": code,
                                  "doc_id": doc_id, "at": now_iso()}]}
    stamped = {**data, "status": "confirmed", "pending_document_commit": False,
               "confirmed_by": actor, "committed_by": "finance_gate"}
    evidence = list(envelope.get("evidence") or []) + [{
        "module": "m6", "source_ref": f"document:{doc_id}",
        "evidence_ref": f"m6:{doc_id}:confirm",
        "detail": f"Finance Gate 批准 → 单据 trial→confirmed（actor={actor or 'human'}）"}]
    return {"outputs": {tool: {**envelope, "data": stamped,
                               "result": stamped, "evidence": evidence}},
            "trace_events": [{"event": "m6.document_confirmed", "doc_id": doc_id,
                              "at": now_iso()}]}


def _apply_m6_asset_commit(state: dict[str, Any], gate: dict[str, Any],
                           *, actor: str = "") -> dict[str, Any] | None:
    """finance 门批准 → 资产台账修订 ``trial → confirmed``（B4）。

    台账按 `asset_code + revision` 存多版，本钩子只翻**被审阅的那一条修订**
    （`asset_id`）——propose 从不覆盖已确认原值，所以"先批准后落库"对台账也成立。
    """
    if str(gate.get("type")) != "finance":
        return None
    tool = str(gate.get("tool") or "")
    envelope = (state.get("outputs") or {}).get(tool)
    if not isinstance(envelope, dict):
        return None
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    if not data.get("pending_asset_commit"):
        return None
    asset_id = str(data.get("asset_id") or "")
    if not asset_id:
        return None
    tenant_id = str(state.get("tenant_id") or "default")
    result = _m6_store(state).confirm_asset(asset_id, actor=actor, tenant_id=tenant_id)
    if not result.get("success"):
        code = str(result.get("code") or "CONFIRM_FAILED")
        return {"m6_conflict": {"code": code, "asset_id": asset_id, "tool": tool},
                "trace_events": [{"event": "m6.asset_confirm_conflict", "code": code,
                                  "asset_id": asset_id, "at": now_iso()}]}
    stamped = {**data, "status": "confirmed", "pending_asset_commit": False,
               "confirmed_by": actor, "committed_by": "finance_gate",
               "effective_revision": result.get("revision")}
    evidence = list(envelope.get("evidence") or []) + [{
        "module": "m6", "source_ref": f"asset:{asset_id}",
        "evidence_ref": f"m6:{asset_id}:confirm",
        "detail": f"Finance Gate 批准 → 资产台账修订 v{result.get('revision')} "
                  f"trial→confirmed（actor={actor or 'human'}）"}]
    return {"outputs": {tool: {**envelope, "data": stamped,
                               "result": stamped, "evidence": evidence}},
            "trace_events": [{"event": "m6.asset_confirmed", "asset_id": asset_id,
                              "revision": result.get("revision"), "at": now_iso()}]}


def _apply_candidate_approval(state: dict[str, Any], gate: dict[str, Any], *, actor: str) -> dict[str, Any] | None:
    """M0 candidate 门批准的副作用：把该批次未裁决候选落为 approved（人工裁决落地）。

    与 INT `graph.py:646-670` 同语义：候选门批准就是人工裁决结论，若不落地，
    下游 `data_import_commit` 会因 PENDING_REVIEW 死锁（W913 基线实测）。
    只对 `data_import_run` 生效（批次候选面）；`ingest_canonical` / facade / resolve /
    rollback 的 candidate 门只记录批准，不改写已发布事实。
    """
    if str(gate.get("type")) != "candidate" or str(gate.get("tool") or "") != "data_import_run":
        return None
    outputs = dict(state.get("outputs") or {})
    envelope = outputs.get("data_import_run")
    if not isinstance(envelope, dict):
        return None
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    batch_id = str(envelope.get("batch_id") or envelope.get("id")
                   or data.get("batch_id") or data.get("id") or "")
    if not batch_id:
        return None
    import os

    try:
        db_path = os.getenv("YUNPAI_M0_DB")
        if db_path:
            from .m0_import_store import CanonicalImportStore

            store = CanonicalImportStore(db_path)
        else:
            from .m0_sandbox import M0SandboxStore

            store = M0SandboxStore(os.getenv("YUNPAI_M0_SANDBOX_DB") or "runtime/yunpai-m0-sandbox.sqlite")
        resolved = 0
        for doc in (store.preview(batch_id).get("documents") or []):
            if str(doc.get("review_status") or "") in ("approved", "rejected", "published"):
                continue
            store.resolve(batch_id=batch_id, candidate_id=doc.get("candidate_id"),
                          action="approve", actor=actor)
            resolved += 1
    except Exception:  # noqa: BLE001 —— 本地无该批次时保持批准结论，不击穿图
        return None
    if not resolved:
        return None
    stamped = dict(envelope)
    stamped["candidate_approval"] = {"batch_id": batch_id, "resolved": resolved, "actor": actor}
    outputs["data_import_run"] = stamped
    return outputs


def reviewer_check_node(deps: GraphDeps) -> Callable:
    async def _node(state: RunStateV2) -> dict[str, Any]:
        engine = deps.engine
        step = dict(state.get("current_step") or {})
        tool = str(step.get("tool") or step.get("name") or "")
        step_id = str(step.get("step_id") or "")
        plan = list(state.get("plan") or [])
        result = (state.get("outputs") or {}).get(tool) or {}
        spec = deps.registry.specs.get(tool)
        findings = rules.evaluate(tool, result, spec)
        review_findings = list(state.get("review_findings") or []) + [
            {"tool": tool, "step_id": step_id, **f, "at": now_iso()} for f in findings]
        trace = list(state.get("trace") or [])
        trace.append({"event": "react.review", "agent": "reviewer", "tool": tool,
                      "findings": [f.get("action") for f in findings], "at": now_iso()})

        base: dict[str, Any] = {"review_findings": review_findings, "trace": trace}

        gate_finding = next((f for f in findings if f.get("gate")), None)
        authorized = list(state.get("authorized_steps") or [])
        if gate_finding and tool not in authorized:
            gate = gate_mod.make_gate(gate_finding["gate"], tool, gate_finding["reason"],
                                      step_id=step_id,
                                      payload_digest=str(summarize(result))[:200],
                                      code=str(gate_finding.get("code") or ""),
                                      message=str(gate_finding.get("message") or ""),
                                      missing_fields=list(gate_finding.get("missing_fields") or []))
            # 挂起前把 waiting_human 镜像写入 RunRepository（前端/GET 可见；节点返回前的 side-effect）。
            # checkpointer 与 runs 表同库（sqlite），写锁竞争会让单次 save 偶发失败且被吞，
            # 造成 run 记录丢失（GET 404/resume 不可用）——重试+退避后再放弃并告警。
            if deps.repository is not None:
                import logging
                import time as _time
                mirrored = {**state, "pending_gate": gate, "status": "waiting_human"}
                for attempt in range(3):
                    try:
                        deps.repository.save(_jsonable_state(mirrored))
                        break
                    except Exception:
                        if attempt == 2:
                            logging.getLogger("yunpai.agent").warning(
                                "gate mirror save failed after retries: run=%s gate=%s",
                                state.get("run_id"), gate.get("type"), exc_info=True)
                        else:
                            _time.sleep(0.25)
            decision = interrupt({"type": "gate_pending", "gate": gate})  # ← 挂起点
            # 非法决策/角色不足不得抛异常：节点在 resume 后抛错会把该 resume 值固化进
            # checkpoint，之后任何 resume 都不再送达（langgraph 实测，Gate 通道被卡死）；
            # 因此改为再次挂起并附错误提示，等待下一次合法决策。
            while True:
                try:
                    updates = gate_mod.apply_decision(state, gate, dict(decision or {}))
                    break
                except gate_mod.GateError as exc:
                    decision = interrupt({"type": "gate_invalid", "gate": gate, "error": str(exc)})
            usage_feedback.on_gate_decision(deps.evolution, state, gate, dict(decision or {}))
            normalized = gate_mod.validate_resume_decision(str(gate.get("type")),
                                                           str((decision or {}).get("decision") or ""))
            if normalized == "approve":
                plan = engine.mark(plan, step_id, "completed")
                step = {**step, "status": "completed", "finished_at": now_iso()}
                updates = {**base, **updates, "plan": plan, "current_step": step}
                stamped = _stamp_engineering_approval(state, gate)
                if stamped is not None:
                    updates["outputs"] = stamped
                # M0 candidate 门批准 → 该批次未裁决候选落为 approved（解 commit 死锁）
                candidate_stamped = _apply_candidate_approval(
                    state, gate, actor=str((decision or {}).get("actor") or "operator"))
                if candidate_stamped is not None:
                    updates["outputs"] = candidate_stamped
                # M5 apply 门批准 → draft 计划真实发布（head CAS）。顺序：先 candidate 落库
                # 再 apply 发布；release 分支把自身 outputs 合并在 updates["outputs"] 之上，
                # 因此两者产出（data_import_run / M5 工具）互不覆盖。
                released = _apply_m5_release(state, gate,
                                             actor=str((decision or {}).get("actor") or ""))
                if released is not None:
                    conflict = released.pop("release_conflict", None)
                    if conflict:
                        # head CAS 冲突：撤回本次授权 + 步骤退回 pending，
                        # 重派发后 apply 门会重新打开（不把冲突吞成 completed）
                        plan = engine.mark(plan, step_id, "pending")
                        step = {**step, "status": "pending"}
                        updates = {**updates, **released,
                                   "plan": plan, "current_step": step,
                                   "pending_gate": {**gate, "conflict": conflict},
                                   "authorized_steps": [
                                       s for s in (state.get("authorized_steps") or [])
                                       if s != tool]}
                    else:
                        if "outputs" in released and "outputs" in updates:
                            released["outputs"] = {**updates["outputs"], **released["outputs"]}
                        updates = {**updates, **released}
                # M6 财务 finance 门批准 → 三段式的 **commit 段**（书二 §6.2.1 / D-005）：
                # 成本快照 trial→confirmed / 月账冻结 / 单据生效。三个钩子互斥（各自认
                # 门型 + 工具名/约定字段），命中即按 M5 同款语义落地：
                # 冲突（期间已冻结、快照消失）不抛异常——撤回本次授权 + 步骤退回 pending，
                # finance 门重新打开，人工可恢复（抛错会把 resume 值固化进 checkpoint）。
                for _m6_hook in (_apply_m6_costing_confirm, _apply_m6_close_month,
                                 _apply_m6_document_commit, _apply_m6_asset_commit):
                    applied = _m6_hook(state, gate,
                                       actor=str((decision or {}).get("actor") or ""))
                    if applied is None:
                        continue
                    conflict = applied.pop("m6_conflict", None)
                    events = list(applied.pop("trace_events", []) or [])
                    if events:
                        updates["trace"] = list(updates.get("trace") or []) + events
                    if conflict:
                        plan = engine.mark(plan, step_id, "pending")
                        step = {**step, "status": "pending"}
                        updates = {**updates, "plan": plan, "current_step": step,
                                   "pending_gate": {**gate, "conflict": conflict},
                                   "authorized_steps": [
                                       s for s in (state.get("authorized_steps") or [])
                                       if s != tool]}
                    else:
                        merged = dict(updates.get("outputs") or {})
                        merged.update(applied.get("outputs") or {})
                        applied.pop("outputs", None)
                        updates = {**updates, **applied, "outputs": merged,
                                   "review_applied": [
                                       *(state.get("review_applied") or []),
                                       {"gate_type": str(gate.get("type") or ""), "tool": tool,
                                        "action": "commit", "actor": str((decision or {}).get("actor") or ""),
                                        "events": [e.get("event") for e in events],
                                        "at": now_iso()}]}
                    break
                return updates
            if normalized == "reject":
                plan = engine.mark(plan, step_id, "skipped")
                step = {**step, "status": "skipped", "finished_at": now_iso()}
                return {**base, **updates, "plan": plan, "current_step": step,
                        "status": "failed"}
            # retry / supplement / supplier_by_material：补数已并入 request，重装配重试
            plan = engine.mark(plan, step_id, "pending")
            step = {**step, "status": "pending"}
            return {**base, **updates, "plan": plan, "current_step": step}

        fail_finding = next((f for f in findings if f.get("action") == "fail"), None)
        if fail_finding and str(result.get("code") or "") != "BLOCKED_INPUT":
            retries = dict(state.get("retry_counts") or {})
            used = int(retries.get(step_id, 0))
            if used < deps.max_step_retries:
                retries[step_id] = used + 1
                plan = engine.mark(plan, step_id, "pending")
                step = {**step, "status": "pending"}
                trace.append({"event": "step.retry", "tool": tool, "attempt": used + 1, "at": now_iso()})
                return {**base, "plan": plan, "current_step": step, "retry_counts": retries}
            plan = engine.mark(plan, step_id, "failed")
            step = {**step, "status": "failed", "finished_at": now_iso()}
            return {**base, "plan": plan, "current_step": step, "status": "failed"}

        # 工具异常（executor 已标 failed，未命中 fail 规则）：重试余量内重试，否则失败。
        # 不得静默标 completed——否则 TOOL_ERROR 会被当成功吞掉、整链误报「完成」（实测复现）。
        if step.get("status") == "failed":
            retries = dict(state.get("retry_counts") or {})
            used = int(retries.get(step_id, 0))
            if used < deps.max_step_retries:
                retries[step_id] = used + 1
                plan = engine.mark(plan, step_id, "pending")
                step = {**step, "status": "pending"}
                trace.append({"event": "step.retry", "tool": tool, "attempt": used + 1,
                              "reason": "tool_error", "at": now_iso()})
                return {**base, "plan": plan, "current_step": step, "retry_counts": retries}
            plan = engine.mark(plan, step_id, "failed")
            step = {**step, "finished_at": now_iso()}
            return {**base, "plan": plan, "current_step": step, "status": "failed"}

        # 通过（含 BLOCKED_INPUT 步骤：保持 blocked 状态等待 data Gate 决策后的重试路径）
        status = step.get("status") or "completed"
        if status != "blocked":
            plan = engine.mark(plan, step_id, "completed")
            status = "completed"
        evidence = list(state.get("evidence") or []) + [
            {**e, "tool": tool} for e in (result.get("evidence") or []) if isinstance(e, dict)]
        step = {**step, "status": status, "finished_at": now_iso()}
        return {**base, "plan": plan, "current_step": step, "evidence": evidence}
    return _node


def finalize_node(deps: GraphDeps) -> Callable:
    async def _node(state: RunStateV2) -> dict[str, Any]:
        engine = deps.engine
        plan = list(state.get("plan") or [])
        stats = engine.stats(plan)
        failed = stats.get("failed", 0) + stats.get("skipped", 0)
        if state.get("status") == "failed":
            status = "failed"
        elif failed:
            status = "failed"
        elif stats.get("pending", 0) or stats.get("blocked", 0) or stats.get("running", 0):
            status = "blocked"
        else:
            status = "completed"
        completed = stats.get("completed", 0)
        response = state.get("response") or (
            f"业务链执行完成：{completed}/{len(plan)} 步，Gate 审批 {len(state.get('approvals') or [])} 次"
            if status == "completed" and plan else "")
        summary = {"steps": stats, "gates": len(state.get("approvals") or []),
                   "route": state.get("route"), "workflow_id": state.get("workflow_id", "")}
        trace = list(state.get("trace") or [])
        trace.append({"event": "run.completed" if status == "completed" else "run.failed",
                      "stats": stats, "at": now_iso()})
        updates: dict[str, Any] = {"status": status, "summary": summary,
                                   "response": response, "trace": trace,
                                   "messages": [{"role": "assistant", "content": response}] if response else []}
        if deps.repository is not None:
            try:
                deps.repository.save(_jsonable_state({**state, **updates}))
            except Exception:
                pass
        usage_feedback.on_finalize(deps.evolution, {**state, **updates}, status == "completed")
        return updates
    return _node


def evolution_observe_node(deps: GraphDeps) -> Callable:
    async def _node(state: RunStateV2) -> dict[str, Any]:
        if deps.evolution is not None:
            try:
                from .evolution.signals import observe_run
                observe_run(deps.evolution, state)
            except Exception:
                pass  # 进化观察永不破坏主流程（legacy graph.py:109-117 语义）
        if deps.repository is not None:
            try:
                deps.repository.save(_jsonable_state(state))  # 终态统一落库（chat 路径也可见于 GET /runs）
            except Exception:
                pass
        return {}
    return _node


# ── 条件路由 ────────────────────────────────────────────────────

def route_after_plan(state: RunStateV2) -> str:
    return "chat" if state.get("route") == "chat" else "execute"


def route_after_dispatch(state: RunStateV2) -> str:
    return "worker" if (state.get("current_step") or {}).get("step_id") else "finalize"


def route_after_review(deps: GraphDeps) -> Callable:
    engine = deps.engine

    def _route(state: RunStateV2) -> str:
        step = state.get("current_step") or {}
        if step.get("status") in ("skipped", "failed"):
            return "finalize"
        stats = engine.stats(list(state.get("plan") or []))
        if stats.get("pending", 0) or stats.get("running", 0):
            return "next"
        return "finalize"
    return _route


# ── 组装 ────────────────────────────────────────────────────────

def build_graph(deps: GraphDeps, *, checkpointer: Any = None):
    g: StateGraph = StateGraph(RunStateV2)
    g.add_node("orchestrator_plan", orchestrator_plan_node(deps))
    g.add_node("orchestrator_dispatch", orchestrator_dispatch_node(deps))
    g.add_node("worker_execute", make_worker_execute(deps))
    g.add_node("reviewer_check", reviewer_check_node(deps))
    g.add_node("chat_answer", chat_answer_node(deps))
    g.add_node("finalize", finalize_node(deps))
    g.add_node("evolution_observe", evolution_observe_node(deps))

    g.add_edge(START, "orchestrator_plan")
    g.add_conditional_edges("orchestrator_plan", route_after_plan,
                            {"execute": "orchestrator_dispatch", "chat": "chat_answer",
                             "finalize": "finalize"})
    g.add_edge("orchestrator_dispatch", "worker_execute")
    g.add_edge("worker_execute", "reviewer_check")
    g.add_conditional_edges("reviewer_check", route_after_review(deps),
                            {"next": "orchestrator_dispatch", "finalize": "finalize"})
    g.add_edge("chat_answer", "evolution_observe")
    g.add_edge("finalize", "evolution_observe")
    g.add_edge("evolution_observe", END)
    return g.compile(checkpointer=checkpointer)


def invoke(graph, state: RunStateV2, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """同步入口（兼容旧 tests 习惯）；生产走 API 层的 ainvoke/astream。"""
    import asyncio
    return asyncio.run(graph.ainvoke(state, config or {}))
