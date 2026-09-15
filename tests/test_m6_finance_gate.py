"""M6 三段式端到端验收（书二 §6.2.1 / 账本 D-005 / 说明一 §1.7 的 5 条断言）。

为什么必须端到端而不是只测工具：三段式的关键在于**「生效」发生在人工门批准之后**，
而 v2 图内唯一开门点是 `reviewer_check_node`、它在 `worker_execute` **之后**。因此
"工具有没有偷偷把状态翻正" 只有把图跑起来（interrupt → resume）才看得见：

1. `save_costing_snapshot` 产物 `status == "trial"`，且**不进**月末汇总；
2. 开 `finance` 门 → `interrupt` 挂起；`approve` → `_apply_m6_costing_confirm` 生效，
   `status == "confirmed"`；
3. `reject` 路径**不产生** `confirmed` 行（断言库内 confirmed 计数为 0）；
4. `finance` 门角色越权被拒（非 `finance-officer`/`admin` 被 `authorize()` 拦）；
5. `test_no_auto_approve` 仍绿（红线不回归）。

另加两条本批必须的护栏：commit 冲突**不抛异常**（否则 resume 值会被固化进
checkpoint、Gate 通道卡死），以及月结冻结的是**被审阅的那组数**。
"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from test_graph_smoke import _deps

from yunpai_orchestrator.graph import build_graph
from yunpai_orchestrator.m6_store import M6Store, STATUS_CONFIRMED, STATUS_TRIAL
from yunpai_orchestrator.repository import InMemoryRunRepository
from yunpai_orchestrator.reviewer import rules
from yunpai_orchestrator.state import new_state_v2

PERIOD = "2026-09"
FINANCE_ROLES = ("finance-officer", "admin")

#: 成本 = 5*2*1.1（材料，有库存走 BOM 价）+ 1h*50（人工）+ 1h*20（制费）= 81 元/台。
#: 与 M3/M5 同口径：权威事实放在 request **顶层**（桥接层读，`assembler` 再叠加显式参数）——
#: 这样本用例同时验证 R1a 的装配接线，而不是绕过装配直接喂工具。
FACTS = {
    "period": PERIOD, "order_id": "SO-M6-1", "product_code": "W-H913", "batch_no": "B1",
    "quantity": 10,
    "bom_lines": [{"material_code": "MAT-A", "qty_per": 2, "unit_price": 5.0, "loss_rate": 0.1}],
    "routing_steps": [{"operation_id": "OP-10", "standard_minutes": 60}],
    "inventory": [{"material_code": "MAT-A", "available_qty": 100, "stock_class": "raw"}],
    "hour_rate": 50, "overhead_rate": 20,
}


@pytest.fixture
def m6_db(tmp_path, monkeypatch):
    """M6 库落 tmp；同时把 M0/M4B 指到不存在的库——让装配读空、用例自洽（不读真库）。"""
    db = tmp_path / "m6.sqlite"
    monkeypatch.setenv("YUNPAI_M6_DB", str(db))
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0-absent.sqlite"))
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "m4b-absent.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)
    return db


def _state(tools: list[str], **request_extra) -> dict:
    request = {"message": "算成本并按财务口径落账", "tools": tools, **FACTS, **request_extra}
    return new_state_v2(request)


def _run(graph, payload, *, limit: int = 96):
    """跑一轮（resume 必须回同一条 thread：MemorySaver 按 thread_id 恢复）。"""
    thread_id = (payload.get("thread_id") if isinstance(payload, dict) else None) \
        or _THREADS[id(graph)]
    return asyncio.run(graph.ainvoke(
        payload, {"configurable": {"thread_id": thread_id}, "recursion_limit": limit}))


#: run 与 thread 的对应（`_graph_and_state` 建图时登记）。
_THREADS: dict[int, str] = {}


def _graph_and_state(repository=None, tools=("save_costing_snapshot",
                                             "confirm_costing_snapshot")):
    """建图 + 建 state：成本事实给在 request 顶层，由桥接装配（不过桥接就等于没测接线）。"""
    graph = build_graph(_deps(repository), checkpointer=MemorySaver())
    state = _state(list(tools))
    _THREADS[id(graph)] = state["thread_id"]
    return graph, state


def _resume(decision: str, *, roles=FINANCE_ROLES, actor: str = "fin-01", note: str = ""):
    return Command(resume={"decision": decision, "actor": actor, "roles": list(roles), "note": note})


def _pending(out: dict) -> dict:
    assert out.get("__interrupt__"), "应挂起在人工门（interrupt）"
    info = out["__interrupt__"][0]
    return info.value if hasattr(info, "value") else info


def _confirmed_rows(db) -> list[dict]:
    return M6Store(str(db)).list_snapshots(period=PERIOD, status=STATUS_CONFIRMED)


# ---------------------------------------------------------------------------
# 断言 1 + 2：试算不进账 → finance 门 → approve → 生效
# ---------------------------------------------------------------------------

def test_save_trial_then_confirm_through_finance_gate(m6_db):
    repo = InMemoryRunRepository()
    graph, state = _graph_and_state(repository=repo)

    out = _run(graph, state)
    # 断言 1：propose 段只写 trial，且不进月末汇总
    saved = out["outputs"]["save_costing_snapshot"]["data"]
    assert saved["status"] == STATUS_TRIAL
    snapshot_id = saved["snapshot_id"]
    store = M6Store(str(m6_db))
    assert store.get_snapshot(snapshot_id)["status"] == STATUS_TRIAL
    assert store.month_summary(PERIOD)["snapshot_count"] == 0
    assert store.month_summary(PERIOD)["trial_count"] == 1

    # 挂起在 finance 门（不是 authorization）；挂起时库里**仍**是 trial
    gate = _pending(out)["gate"]
    assert gate["type"] == "finance" and gate["tool"] == "confirm_costing_snapshot"
    assert gate["allowed_roles"] == list(FINANCE_ROLES)
    assert gate["review"]["total_cost"] == saved["total_cost"] == 810.0
    assert gate["review"]["cost_incomplete"] is False
    assert store.get_snapshot(snapshot_id)["status"] == STATUS_TRIAL
    assert _confirmed_rows(m6_db) == []
    # 挂起被镜像到 RunRepository（前端/GET 可见）
    mirrored = repo.get(state["run_id"])
    assert mirrored and mirrored["pending_gate"]["type"] == "finance"

    # 断言 2：approve → commit 钩子生效（trial→confirmed 真的落库）
    resumed = _run(graph, _resume("approve"))
    assert resumed["status"] == "completed", f"errors={resumed.get('errors')}"
    snapshot = store.get_snapshot(snapshot_id)
    assert snapshot["status"] == STATUS_CONFIRMED
    assert snapshot["confirmed_by"] == "fin-01"
    assert snapshot["confirmed_at"]
    # 汇总只认 confirmed：现在才进账
    assert store.month_summary(PERIOD)["snapshot_count"] == 1
    # 生效留痕：trace 事件 + review_applied + 工具产出的证据链
    assert any(e.get("event") == "m6.costing_confirmed" for e in resumed.get("trace") or [])
    assert [row["action"] for row in resumed.get("review_applied") or []] == ["commit"]
    assert resumed["review_applied"][0]["tool"] == "confirm_costing_snapshot"
    confirmed_env = resumed["outputs"]["confirm_costing_snapshot"]
    assert confirmed_env["data"]["pending_confirmation"] is False
    # 提交钩子更新后的信封两条镜像必须同时刷新，避免 result 保留旧 trial 状态。
    assert confirmed_env["result"] == confirmed_env["data"]
    assert any("trial→confirmed" in str(item.get("detail", ""))
               for item in confirmed_env["evidence"])


# ---------------------------------------------------------------------------
# 断言 3：reject 路径不产生 confirmed 行
# ---------------------------------------------------------------------------

def test_reject_path_leaves_no_confirmed_row(m6_db):
    graph, state = _graph_and_state()
    out = _run(graph, state)
    snapshot_id = out["outputs"]["save_costing_snapshot"]["data"]["snapshot_id"]
    assert _pending(out)["gate"]["type"] == "finance"

    resumed = _run(graph, _resume("reject", note="口径待财务确认"))
    assert resumed["status"] == "failed"                     # 步骤 skipped → 整轮失败
    assert _confirmed_rows(m6_db) == []                      # 断言：库内 confirmed 计数为 0
    assert M6Store(str(m6_db)).get_snapshot(snapshot_id)["status"] == STATUS_TRIAL
    assert not resumed.get("review_applied")                 # reject 不写生效留痕
    assert not any(e.get("event") == "m6.costing_confirmed" for e in resumed.get("trace") or [])


# ---------------------------------------------------------------------------
# 断言 4：finance 门角色越权被拒，且门保持可恢复
# ---------------------------------------------------------------------------

def test_finance_gate_rejects_non_finance_roles(m6_db):
    graph, state = _graph_and_state()
    out = _run(graph, state)
    snapshot_id = out["outputs"]["save_costing_snapshot"]["data"]["snapshot_id"]

    denied = _run(graph, _resume("approve", roles=["operator"], actor="op-01"))
    refused = _pending(denied)
    assert refused["type"] == "gate_invalid"                 # 非法角色不得抛异常卡死通道
    assert "finance-officer" in str(refused.get("error") or "")
    assert _confirmed_rows(m6_db) == []                      # 越权没有产生任何生效写

    allowed = _run(graph, _resume("approve"))
    assert allowed["status"] == "completed"
    assert M6Store(str(m6_db)).get_snapshot(snapshot_id)["status"] == STATUS_CONFIRMED


# ---------------------------------------------------------------------------
# 断言 5：红线不回归
# ---------------------------------------------------------------------------

def test_no_auto_approve_is_still_false():
    """审查永不自动放行人工门（旧审计 P0 教训）——M6 不得破坏这条。"""
    assert rules.AUTO_APPROVE_ALLOWED is False
    assert rules.default_gate_for_authorized("confirm_costing_snapshot", None, []) is False
    assert rules.default_gate_for_authorized("close_month_costing", None, []) is False


# ---------------------------------------------------------------------------
# 月结：冻结被审阅的那组数
# ---------------------------------------------------------------------------

def test_close_month_freezes_reviewed_totals(m6_db):
    graph, state = _graph_and_state(
        tools=["save_costing_snapshot", "confirm_costing_snapshot", "close_month_costing"])

    confirmed = _run(graph, state)
    assert _pending(confirmed)["gate"]["tool"] == "confirm_costing_snapshot"
    out = _run(graph, _resume("approve"))
    # 确认生效后继续走到月结：第二次挂起应在 close_month_costing 上
    gate = _pending(out)["gate"]
    assert gate["tool"] == "close_month_costing" and gate["type"] == "finance"
    assert gate["review"]["period"] == PERIOD
    assert gate["review"]["total_cost"] == 810.0
    assert gate["review"]["trial_excluded"] is True
    store = M6Store(str(m6_db))
    assert store.get_month_close(PERIOD) is None             # 未批准前不得冻结
    reviewed = out["outputs"]["close_month_costing"]["data"]
    assert reviewed["pending_close"] is True
    assert reviewed["snapshot_count"] == 1

    frozen = _run(graph, _resume("approve"))
    assert frozen["status"] == "completed", f"errors={frozen.get('errors')}"
    close_row = store.get_month_close(PERIOD)
    assert close_row is not None and close_row["closed_by"] == "fin-01"
    summary = store.month_summary(PERIOD)
    assert summary["frozen"] is True
    # 冻结的就是被审阅的那组数（不随 commit 时重算漂移）
    assert summary["totals"]["total_cost"] == reviewed["total_cost"] == 810.0
    assert summary["snapshot_count"] == reviewed["snapshot_count"] == 1
    assert frozen["outputs"]["close_month_costing"]["result"] == frozen["outputs"]["close_month_costing"]["data"]
    assert any(e.get("event") == "m6.month_closed" for e in frozen.get("trace") or [])


# ---------------------------------------------------------------------------
# commit 冲突不得抛异常（M5 `_apply_m5_release` 的既有教训）
# ---------------------------------------------------------------------------

def test_confirm_conflict_does_not_raise_and_writes_nothing(m6_db):
    graph, state = _graph_and_state()
    out = _run(graph, state)
    snapshot_id = out["outputs"]["save_costing_snapshot"]["data"]["snapshot_id"]

    # 批准前别人把该期间月账冻结了（或快照被清理）→ commit 段失败
    M6Store(str(m6_db)).close_month(period=PERIOD, totals={}, snapshot_count=0,
                                    actor="finance-officer")
    resumed = _run(graph, _resume("approve"))
    assert resumed["status"] != "completed"                  # 冲突不得被吞成"已完成"
    assert _confirmed_rows(m6_db) == []                      # 也不得写入任何生效行
    assert M6Store(str(m6_db)).get_snapshot(snapshot_id)["status"] == STATUS_TRIAL
    assert any(e.get("event") == "m6.costing_confirm_conflict" for e in resumed.get("trace") or [])


# ---------------------------------------------------------------------------
# 第三个 commit 钩子（B2 单据落库的挂点）——现在无工具，先锁约定
# ---------------------------------------------------------------------------

def test_document_commit_hook_confirms_saved_document(m6_db):
    """`_apply_m6_document_commit` 按约定分派：产出带 ``pending_document_commit`` + ``doc_id``。

    B2 的报价单/对账单、B3 的送货单工具照此声明即可复用，无需再动 `reviewer_check_node`。
    """
    from yunpai_orchestrator.graph import _apply_m6_document_commit

    store = M6Store(str(m6_db))
    store.save_document(doc_id="DOC-1", doc_no="QT-001", doc_type="quotation",
                        counterparty_code="CUST-01", amount=1000.0)
    state = {
        "tenant_id": "default", "request": {"m6_db_path": str(m6_db)},
        "outputs": {"save_quotation": {"data": {"doc_id": "DOC-1", "pending_document_commit": True,
                                                "status": STATUS_TRIAL}, "evidence": []}},
    }
    gate = {"type": "finance", "tool": "save_quotation"}
    applied = _apply_m6_document_commit(state, gate, actor="fin-01")
    assert applied is not None
    assert applied["outputs"]["save_quotation"]["data"]["status"] == STATUS_CONFIRMED
    assert applied["outputs"]["save_quotation"]["data"]["pending_document_commit"] is False
    assert store.list_documents(doc_type="quotation")[0]["status"] == STATUS_CONFIRMED

    # 非 finance 门 / 无待提交标记 → 一律不动作（钩子必须窄）
    assert _apply_m6_document_commit({**state, "outputs": {}}, {"type": "apply", "tool": "x"}) is None
    assert _apply_m6_document_commit({"tenant_id": "default", "outputs": {
        "save_quotation": {"data": {"doc_id": "DOC-1"}}}}, gate) is None
