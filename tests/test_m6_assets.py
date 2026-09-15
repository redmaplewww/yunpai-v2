"""B4：资产台账（`get_asset_ledger` / `upsert_asset_ledger` / `compute_asset_benefit`）。

要点：

1. **修订模型**：台账按 `asset_code + revision` 存多版，**生效行 = revision 最大且已
   confirmed**。propose 只**追加** trial 修订——所以"先批准后落库"对台账也成立：
   未批准的改动**不会改写账上已确认的原值**（本文件对此有专门断言）；
2. **三段式**：`upsert_asset_ledger` 挂 finance 门，approve 后由
   `_apply_m6_asset_commit` 翻 confirmed；reject 后草稿留着但**不生效**；
3. **草稿不叠**：已有待确认修订时拒绝再叠一版（`ASSET_PENDING_EXISTS`）；
4. **不编造成本**：效益分摊的资产成本取"显式 > 台账生效原值"，两处都没有就
   `missing_asset_cost` + `cost_incomplete`；口径未给标 `assumed=true`。
"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from test_graph_smoke import _deps

from yunpai_orchestrator.graph import build_graph
from yunpai_orchestrator.m6_store import M6Store, STATUS_CONFIRMED, STATUS_TRIAL
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.reviewer import rules
from yunpai_orchestrator.state import new_state_v2

FINANCE_ROLES = ("finance-officer", "admin")


def _ctx(tmp_path, **extra):
    return {"tenant_id": "default", "task_id": "task-m6-asset",
            "m6_db_path": str(tmp_path / "m6.sqlite"), **extra}


@pytest.fixture
def registry():
    return build_default_registry()


def _store(ctx) -> M6Store:
    return M6Store(ctx["m6_db_path"])


# ---------------------------------------------------------------------------
# 修订模型：propose 不覆盖已确认原值
# ---------------------------------------------------------------------------

async def test_upsert_appends_revision_and_never_overwrites_effective(registry, tmp_path):
    ctx = _ctx(tmp_path)
    first = await registry.call("upsert_asset_ledger", {
        "asset_code": "MOLD-01", "asset_name": "一号模具", "category": "mold",
        "acquisition_cost": 10000.0, "acquired_at": "2026-01-05",
        "useful_life_months": 24}, ctx)
    assert first["success"] is True, first.get("errors")
    assert first["data"]["status"] == STATUS_TRIAL          # propose：草稿
    assert first["data"]["pending_asset_commit"] is True
    assert first["data"]["revision"] == 1
    assert first["data"]["effective_revision"] is None      # 还没批，账上无生效值

    store = _store(ctx)
    store.confirm_asset(first["data"]["asset_id"], actor="fin-01")
    assert store.effective_asset("MOLD-01")["acquisition_cost"] == 10000.0

    # 第二次 upsert：新增 v2 草稿，**不动** v1 的已确认原值
    second = await registry.call("upsert_asset_ledger", {
        "asset_code": "MOLD-01", "asset_name": "一号模具（改）",
        "acquisition_cost": 12000.0}, ctx)
    assert second["data"]["revision"] == 2
    assert second["data"]["effective_revision"] == 1
    assert second["data"]["effective_acquisition_cost"] == 10000.0
    assert store.effective_asset("MOLD-01")["acquisition_cost"] == 10000.0   # 未被改写
    assert store.pending_asset("MOLD-01")["revision"] == 2

    # 已有待确认修订 → 不再叠一版
    blocked = await registry.call("upsert_asset_ledger",
                                  {"asset_code": "MOLD-01", "acquisition_cost": 13000.0}, ctx)
    assert blocked["success"] is False and blocked["code"] == "ASSET_PENDING_EXISTS"
    assert blocked["data"]["pending_asset_id"] == second["data"]["asset_id"]


async def test_get_asset_ledger_separates_effective_and_pending(registry, tmp_path):
    ctx = _ctx(tmp_path)
    saved = (await registry.call("upsert_asset_ledger", {
        "asset_code": "MOLD-01", "acquisition_cost": 10000.0}, ctx))["data"]
    _store(ctx).confirm_asset(saved["asset_id"], actor="fin-01")
    await registry.call("upsert_asset_ledger",
                        {"asset_code": "MOLD-01", "acquisition_cost": 12000.0}, ctx)

    detail = await registry.call("get_asset_ledger", {"asset_code": "MOLD-01"}, ctx)
    data = detail["data"]
    assert data["effective"]["acquisition_cost"] == 10000.0     # 账上事实
    assert data["effective"]["status"] == STATUS_CONFIRMED
    assert data["pending"]["acquisition_cost"] == 12000.0       # 等人批的草稿
    assert data["pending"]["status"] == STATUS_TRIAL
    assert [row["revision"] for row in data["revisions"]] == [1, 2]

    listed = await registry.call("get_asset_ledger", {}, ctx)
    assert listed["data"]["count"] == 1
    assert listed["data"]["assets"][0]["asset_code"] == "MOLD-01"

    assert (await registry.call("get_asset_ledger",
                                {"asset_code": "NOPE"}, ctx))["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# compute_asset_benefit：成本来源与口径
# ---------------------------------------------------------------------------

async def test_benefit_uses_ledger_effective_cost(registry, tmp_path):
    ctx = _ctx(tmp_path)
    effective = (await registry.call("upsert_asset_ledger", {
        "asset_code": "MOLD-01", "acquisition_cost": 10000.0}, ctx))["data"]
    _store(ctx).confirm_asset(effective["asset_id"], actor="fin-01")
    # 未批准的 v2 不得参与分摊（否则等于用未批的原值算成本）
    await registry.call("upsert_asset_ledger",
                        {"asset_code": "MOLD-01", "acquisition_cost": 99999.0}, ctx)

    result = await registry.call("compute_asset_benefit", {"asset_usage": [
        {"asset_code": "MOLD-01", "product_code": "P-1", "quantity": 30},
        {"asset_code": "MOLD-01", "product_code": "P-2", "quantity": 10},
    ]}, ctx)
    data = result["data"]
    assert data["asset_cost_source"] == "ledger"
    assert data["asset_cost"] == {"MOLD-01": 10000.0}       # 取生效原值，不是 99999 草稿
    allocations = {(row["product_code"]): row["allocated_cost"] for row in data["allocations"]}
    assert allocations == {"P-1": 7500.0, "P-2": 2500.0}
    assert data["cost_incomplete"] is False
    assert data["assumptions"]["allocation_basis"] == "quantity"
    assert data["assumptions"]["allocation_basis_assumed"] is True
    assert data["assumptions"]["pending_finance_confirmation"]
    assert _store(ctx).list_documents() == [] and not _store(ctx).list_snapshots()


async def test_benefit_explicit_cost_wins_and_missing_is_reported(registry, tmp_path):
    ctx = _ctx(tmp_path)
    explicit = await registry.call("compute_asset_benefit", {
        "asset_cost": {"MOLD-X": 500.0},
        "asset_usage": [{"asset_code": "MOLD-X", "product_code": "P-1", "quantity": 1},
                        {"asset_code": "MOLD-X", "product_code": "P-2", "quantity": 1},
                        {"asset_code": "MOLD-MISSING", "product_code": "P-1", "quantity": 1}],
    }, ctx)
    data = explicit["data"]
    assert data["asset_cost_source"] == "explicit"
    assert data["cost_incomplete"] is True                  # 缺成本的资产如实标出
    assert data["missing"] == [{"asset_code": "MOLD-MISSING",
                               "reason": "missing_asset_cost"}]
    assert [row["allocated_cost"] for row in data["allocations"]] == [250.0, 250.0]

    # 台账为空、也没给显式成本 → 来源 missing（不编造）
    empty = await registry.call("compute_asset_benefit", {
        "asset_usage": [{"asset_code": "MOLD-Y", "product_code": "P-1", "quantity": 1}]},
        ctx)
    assert empty["data"]["asset_cost_source"] == "missing"
    assert empty["data"]["cost_incomplete"] is True

    # 工时口径
    hours = await registry.call("compute_asset_benefit", {
        "allocation_basis": "labor_hours",
        "asset_cost": {"MOLD-X": 300.0},
        "asset_usage": [{"asset_code": "MOLD-X", "product_code": "P-1", "labor_hours": 2},
                        {"asset_code": "MOLD-X", "product_code": "P-2", "labor_hours": 1}],
    }, ctx)
    assert [row["allocated_cost"] for row in hours["data"]["allocations"]] == [200.0, 100.0]
    assert hours["data"]["assumptions"]["allocation_basis_assumed"] is False


# ---------------------------------------------------------------------------
# 三段式端到端：propose → finance 门 → commit / reject 不生效
# ---------------------------------------------------------------------------

def _graph_state(tmp_path, monkeypatch, **payload_extra):
    """台账是**叶子写工具**（没有上游事实要装配）：载荷走装配器的显式参数通道
    `request["upsert_asset_ledger"]`（EXPLICIT_ONLY 语义，与 M4 各写工具同口径）。"""
    monkeypatch.setenv("YUNPAI_M6_DB", str(tmp_path / "m6.sqlite"))
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0-absent.sqlite"))
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "m4b-absent.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)
    graph = build_graph(_deps(), checkpointer=MemorySaver())
    payload = {"asset_code": "MOLD-01", "asset_name": "一号模具",
               "acquisition_cost": 10000.0, "useful_life_months": 24, **payload_extra}
    state = new_state_v2({"message": "登记资产台账", "tools": ["upsert_asset_ledger"],
                          "upsert_asset_ledger": payload})
    return graph, state, {"configurable": {"thread_id": state["thread_id"]},
                          "recursion_limit": 64}


def _pending(out: dict) -> dict:
    assert out.get("__interrupt__"), "应挂起在人工门"
    info = out["__interrupt__"][0]
    return info.value if hasattr(info, "value") else info


def test_asset_upsert_propose_then_finance_commit(tmp_path, monkeypatch):
    graph, state, config = _graph_state(tmp_path, monkeypatch)
    db = tmp_path / "m6.sqlite"

    out = asyncio.run(graph.ainvoke(state, config))
    saved = out["outputs"]["upsert_asset_ledger"]["data"]
    assert saved["status"] == STATUS_TRIAL                  # propose：草稿
    assert _pending(out)["gate"]["type"] == "finance"
    assert _pending(out)["gate"]["review"]["asset_code"] == "MOLD-01"
    assert _pending(out)["gate"]["review"]["original_value"] == 10000.0
    assert M6Store(str(db)).effective_asset("MOLD-01") is None    # 未批准 → 账上无生效值

    resumed = asyncio.run(graph.ainvoke(
        Command(resume={"decision": "approve", "actor": "fin-01",
                        "roles": list(FINANCE_ROLES)}), config))
    assert resumed["status"] == "completed", f"errors={resumed.get('errors')}"
    effective = M6Store(str(db)).effective_asset("MOLD-01")
    assert effective["acquisition_cost"] == 10000.0         # 批准后才是账上事实
    assert effective["confirmed_by"] == "fin-01"
    assert effective["useful_life_months"] == 24
    assert resumed["outputs"]["upsert_asset_ledger"]["data"]["pending_asset_commit"] is False
    assert resumed["outputs"]["upsert_asset_ledger"]["result"] == resumed["outputs"]["upsert_asset_ledger"]["data"]
    assert any(e.get("event") == "m6.asset_confirmed" for e in resumed.get("trace") or [])
    assert [row["action"] for row in resumed.get("review_applied") or []] == ["commit"]


def test_asset_upsert_reject_keeps_draft_without_effect(tmp_path, monkeypatch):
    graph, state, config = _graph_state(tmp_path, monkeypatch)
    db = tmp_path / "m6.sqlite"

    out = asyncio.run(graph.ainvoke(state, config))
    asset_id = out["outputs"]["upsert_asset_ledger"]["data"]["asset_id"]
    resumed = asyncio.run(graph.ainvoke(
        Command(resume={"decision": "reject", "actor": "fin-01",
                        "roles": list(FINANCE_ROLES), "note": "原值待核"}),
        config))
    assert resumed["status"] == "failed"
    store = M6Store(str(db))
    assert store.effective_asset("MOLD-01") is None          # reject：账上仍无生效值
    assert store.get_asset(asset_id)["status"] == STATUS_TRIAL   # 草稿留着可追溯
    assert not resumed.get("review_applied")


# ---------------------------------------------------------------------------
# 契约 ↔ 规则
# ---------------------------------------------------------------------------

def test_asset_contracts_and_rules_agree(registry):
    expected = {
        "get_asset_ledger": ("none", "none", ""),
        "upsert_asset_ledger": ("local_write", "finance", "finance"),
        "compute_asset_benefit": ("none", "none", ""),
    }
    for tool, (side_effect, review_gate, gate) in expected.items():
        spec = registry.specs[tool]
        assert spec.module == "m6", tool
        assert spec.side_effect == side_effect, tool
        assert spec.review_gate == review_gate, tool
        assert rules.gate_type_for(tool, spec) == gate, tool
    assert [f["gate"] for f in rules.evaluate(
        "upsert_asset_ledger", {"success": True,
                                "data": {"pending_asset_commit": True}})] == ["finance"]
    assert rules.evaluate("upsert_asset_ledger",
                          {"success": True, "data": {"pending_asset_commit": False}}) == []
    assert rules.AUTO_APPROVE_ALLOWED is False
