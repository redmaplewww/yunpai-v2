"""B1 第三批：M6 内核读工具（`get_product_cost` / `audit_order_cost` / `allocate_expenses`）。

这三件是**纯算数读工具**——不落库、无副作用、无门（契约 `side_effect` 不声明）。
本文件锁住四条：

1. **与落库快照同口径**：`get_product_cost`（D8 的当场算/报价预览）与
   `save_costing_snapshot` 共用 `_cost_breakdown` → 同一套事实算出的单位成本**必须相等**
   （否则"预览 100、落库 105"这类漂移没人能发现）；
2. **缺数不编造**：缺 BOM/工艺/费率/单价一律 `cost_incomplete` + `missing`，且
   `audit_order_cost` 的「算不全」**绝不能**被判成 `passed`（不能把缺数当盈利）；
3. **只读**：三件跑完 M6 库里**没有任何新行**（快照/明细/单据计数不变）；
4. **装配来源可溯**：费用事实来自 canonical（拆信封）、基准来源以
   `basis_source`（explicit/canonical/missing）明示。
"""

from __future__ import annotations

import pytest

from yunpai_orchestrator.m6_store import M6Store
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.reviewer import rules

PERIOD = "2026-09"

#: 与 `test_m6_finance_gate.py` 同一组事实：材料 5*2*1.1 + 采购 8.5*1 + 采购 3.2*4 = 32.3，
#: 人工 1h*50 + 制费 1h*20 = 70 → 单台 102.3。
BOM_LINES = [
    {"material_code": "MAT-A", "qty_per": 2, "unit_price": 5.0, "loss_rate": 0.1},
    {"material_code": "MAT-B", "qty_per": 1, "unit_price": 8.0, "loss_rate": 0.0},
    {"material_code": "MAT-C", "qty_per": 4, "unit_price": 3.0, "loss_rate": 0.0},
]
ROUTING_STEPS = [{"operation_id": "OP-10", "standard_minutes": 60}]
INVENTORY = [{"material_code": "MAT-A", "available_qty": 100, "stock_class": "raw"}]
TRACKING_ROWS = [
    {"id": 7, "purchase_order_no": "PO-1", "purchase_order_item_id": 21,
     "supplier_name": "供应商一", "promised_date": "2026-09-01", "unit_price": "8.5",
     "currency": "CNY"},
    {"id": 8, "purchase_order_no": "PO-2", "purchase_order_item_id": 22,
     "promised_date": "2026-09-02", "unit_price": "3.2", "currency": "CNY"},
]
ORDER_ITEMS = [{"id": 21, "item_code": "MAT-B", "internal_material_no": "IM-B"},
               {"id": 22, "item_code": "MAT-C", "internal_material_no": "IM-C"}]


def _ctx(tmp_path, **extra):
    return {"tenant_id": "default", "task_id": "task-m6-c", 
            "m6_db_path": str(tmp_path / "m6.sqlite"), **extra}


def _cost_payload(**overrides):
    payload = {"product_code": "W-H913", "bom_lines": BOM_LINES,
               "routing_steps": ROUTING_STEPS, "inventory": INVENTORY,
               "hour_rate": 50, "overhead_rate": 20,
               "purchase_tracking_rows": TRACKING_ROWS,
               "purchase_order_items": ORDER_ITEMS}
    payload.update(overrides)
    return payload


@pytest.fixture
def registry():
    return build_default_registry()


def _digest(ctx) -> list[tuple]:
    """M6 库内容的公开读口摘要（快照号/状态/总额 + 明细行数）——用于证明读工具没写库。"""
    store = M6Store(ctx["m6_db_path"])
    rows = store.list_snapshots()
    return [(row["snapshot_id"], row["status"], row["total_cost"],
             len(store.get_snapshot(row["snapshot_id"])["lines"])) for row in rows]


# ---------------------------------------------------------------------------
# get_product_cost：当场算（D8 的试算/报价预览）
# ---------------------------------------------------------------------------

async def test_product_cost_matches_snapshot_same_facts(registry, tmp_path):
    """同一套事实：当场算的单位成本 == 试算快照的单位成本（共用 _cost_breakdown）。"""
    ctx = _ctx(tmp_path)
    preview = await registry.call("get_product_cost", _cost_payload(), ctx)
    assert preview["success"] is True, preview.get("errors")
    assert preview["data"]["unit_cost"] == 102.3
    assert preview["data"]["basis"] == "mixed"          # A 走库存、B/C 走采购
    assert preview["data"]["cost_incomplete"] is False
    assert preview["data"]["missing"] == []
    # 行级价格来源（哪行走库存、哪行走采购）逐行给出
    kinds = {row["material_code"]: row["source_kind"] for row in preview["data"]["lines"]}
    assert kinds == {"MAT-A": "stock", "MAT-B": "purchase", "MAT-C": "purchase"}

    before = _digest(ctx)
    saved = await registry.call("save_costing_snapshot",
                                {**_cost_payload(), "period": PERIOD, "quantity": 1}, ctx)
    assert saved["data"]["unit_cost"] == preview["data"]["unit_cost"]      # 同口径同结果
    assert _digest(ctx) == [*before, (saved["data"]["snapshot_id"], "trial", 102.3, 5)]
    # 预览本身不写库：再跑一次预览，库内容逐条不变（快照/明细都不动）
    await registry.call("get_product_cost", _cost_payload(), ctx)
    assert _digest(ctx) == [*before, (saved["data"]["snapshot_id"], "trial", 102.3, 5)]


async def test_product_cost_reports_missing_without_inventing(registry, tmp_path):
    ctx = _ctx(tmp_path)
    # 缺费率 + 缺 BOM 价 + 缺采购价 → 全部如实报缺
    result = await registry.call("get_product_cost",
                                 {"product_code": "W-X", "bom_lines": BOM_LINES[:1]}, ctx)
    data = result["data"]
    assert data["cost_incomplete"] is True
    assert data["unit_cost"] == 0.0
    reasons = {item["reason"] for item in data["missing_inputs"]} | \
        {item["reason"] for item in data["missing"]}
    assert reasons == {"missing_routing_steps", "missing_purchase_price"}
    # 费率的口径痕：没给就是 assumed=true（不编造默认费率）
    assert data["assumptions"]["hour_rate_assumed"] is True
    assert data["assumptions"]["pending_finance_confirmation"]
    assert _digest(ctx) == []                    # 一行都没写


async def test_product_cost_requires_product_code(registry, tmp_path):
    """契约层就拦（图里由 assembler 的 required 校验先开 BLOCKED_INPUT 门）。"""
    with pytest.raises(ValueError, match="product_code"):
        await registry.call("get_product_cost", {}, _ctx(tmp_path))


# ---------------------------------------------------------------------------
# audit_order_cost：订单成本审计与毛利
# ---------------------------------------------------------------------------

async def test_audit_rolls_unit_cost_from_product_facts(registry, tmp_path):
    ctx = _ctx(tmp_path)
    result = await registry.call("audit_order_cost", {
        "order_id": "SO-1",
        "order_lines": [{"product_code": "W-H913", "qty": 10, "unit_price": 150.0}],
        "products": {"W-H913": _cost_payload()},
    }, ctx)
    data = result["data"]
    assert result["success"] is True
    assert data["order_id"] == "SO-1"
    assert data["revenue"] == 1500.0
    assert data["total_cost"] == 1023.0                  # 102.3 × 10
    assert data["gross_profit"] == 477.0
    assert data["gross_margin"] == 0.318
    assert data["status"] == "passed"                    # ≥ 0.15 门槛
    assert data["rolled_products"] == ["W-H913"]         # 单位成本是当场滚动出来的
    assert data["cost_incomplete"] is False
    assert _digest(ctx) == []


async def test_audit_uses_explicit_unit_costs_when_given(registry, tmp_path):
    ctx = _ctx(tmp_path)
    result = await registry.call("audit_order_cost", {
        "order_id": "SO-2",
        "order_lines": [{"product_code": "W-H913", "qty": 2, "unit_price": 100.0}],
        "unit_costs": {"W-H913": {"material": 10.0, "labor": 5.0, "overhead": 5.0}},
    }, ctx)
    data = result["data"]
    assert data["rolled_products"] == []                 # 显式单位成本优先，不滚动
    assert data["total_cost"] == 40.0
    assert data["gross_profit"] == 160.0
    assert data["status"] == "passed"


async def test_audit_incomplete_never_looks_profitable(registry, tmp_path):
    """缺单位成本/缺价的行 → status=cost_incomplete，**不得**判成 passed。"""
    ctx = _ctx(tmp_path)
    no_facts = await registry.call("audit_order_cost", {
        "order_lines": [{"product_code": "W-NOPE", "qty": 1, "unit_price": 999.0}],
    }, ctx)
    assert no_facts["data"]["status"] == "cost_incomplete"
    reasons = {item["reason"] for item in no_facts["data"]["missing"]}
    assert {"missing_unit_cost", "missing_product_facts"} <= reasons

    # 有事实但费率缺失 → 滚动出来的成本不全 → 整单照样 cost_incomplete
    degraded = await registry.call("audit_order_cost", {
        "order_lines": [{"product_code": "W-H913", "qty": 1, "unit_price": 999.0}],
        "products": {"W-H913": _cost_payload(hour_rate=None, overhead_rate=None)},
    }, ctx)
    data = degraded["data"]
    assert data["status"] == "cost_incomplete"
    assert data["cost_incomplete"] is True
    assert "W-H913" in data["rolled_products"]

    # 缺对客单价的行也不得被当 0 价算成"亏损/盈利"
    missing_price = await registry.call("audit_order_cost", {
        "order_lines": [{"product_code": "W-H913", "qty": 1}],
        "unit_costs": {"W-H913": {"material": 1.0, "labor": 0.0, "overhead": 0.0}},
    }, ctx)
    assert missing_price["data"]["status"] == "cost_incomplete"
    assert missing_price["data"]["incomplete"][0]["reason"] == "missing_product_code_qty_price"


async def test_audit_flags_loss_and_low_profit(registry, tmp_path):
    ctx = _ctx(tmp_path)
    unit = {"material": 100.0, "labor": 0.0, "overhead": 0.0}
    loss = await registry.call("audit_order_cost", {
        "order_lines": [{"product_code": "P", "qty": 1, "unit_price": 50.0}],
        "unit_costs": {"P": unit}}, ctx)
    assert loss["data"]["status"] == "loss"
    assert loss["data"]["gross_profit"] == -50.0

    low = await registry.call("audit_order_cost", {
        "order_lines": [{"product_code": "P", "qty": 1, "unit_price": 105.0}],
        "unit_costs": {"P": unit}}, ctx)
    assert low["data"]["status"] == "low_profit"         # 毛利率 4.8% < 15%
    assert low["data"]["min_margin_rate"] == 0.15


# ---------------------------------------------------------------------------
# allocate_expenses：费用分摊
# ---------------------------------------------------------------------------

async def test_allocate_expenses_splits_by_basis_with_assumed_flag(registry, tmp_path):
    ctx = _ctx(tmp_path)
    result = await registry.call("allocate_expenses", {
        "expenses": [{"category": "electricity", "amount": 300.0, "period": PERIOD},
                     {"category": "meals", "amount": 100.0, "period": PERIOD,
                      "allocation_basis": "quantity"}],
        "basis_rows": [{"product_code": "P-1", "quantity": 30},
                       {"product_code": "P-2", "quantity": 10}],
    }, ctx)
    data = result["data"]
    assert data["total_expense"] == 400.0
    assert data["total_allocated"] == 400.0
    per_product = {row["product_code"]: row for row in data["by_product"]}
    assert per_product["P-1"]["allocated_total"] == 300.0     # 30/40
    assert per_product["P-2"]["allocated_total"] == 100.0     # 10/40
    assert per_product["P-1"]["allocated_per_unit"] == 10.0
    # 口径痕：未显式给口径 → assumed=true；基准来源 explicit
    assert data["assumptions"]["allocation_basis"] == "quantity"
    assert data["assumptions"]["allocation_basis_assumed"] is True
    assert data["basis_source"] == "explicit"
    assert data["cost_incomplete"] is False
    assert _digest(ctx) == []


async def test_allocate_expenses_period_filter_and_missing_reported(registry, tmp_path):
    ctx = _ctx(tmp_path)
    filtered = await registry.call("allocate_expenses", {
        "period": PERIOD,
        "expenses": [{"category": "electricity", "amount": 300.0, "period": PERIOD},
                     {"category": "electricity", "amount": 999.0, "period": "2026-08"}],
        "basis_rows": [{"product_code": "P-1", "quantity": 1}],
    }, ctx)
    assert filtered["data"]["total_expense"] == 300.0          # 别的期间不参与分摊

    # 空费用/空基准 → 明示缺失，不用 0 冒充
    empty = await registry.call("allocate_expenses",
                                {"expenses": [], "basis_rows": []}, ctx)
    assert empty["data"]["cost_incomplete"] is True
    assert {item["reason"] for item in empty["data"]["missing"]} == {
        "missing_expenses", "missing_basis_rows"}

    # 按人数分摊但基准只有产量 → 该产品进 missing_basis_value（不推算）
    wrong_basis = await registry.call("allocate_expenses", {
        "allocation_basis": "headcount",
        "expenses": [{"category": "social_insurance", "amount": 100.0, "period": PERIOD}],
        "basis_rows": [{"product_code": "P-1", "quantity": 30}],
    }, ctx)
    assert any(item["reason"] == "missing_basis_value"
               for item in wrong_basis["data"]["missing"])


# ---------------------------------------------------------------------------
# 装配（bridge）：来源可溯 + 缺权威输入即 BLOCKED_INPUT
# ---------------------------------------------------------------------------

def _bridge_state(**request_extra):
    from yunpai_orchestrator.state import new_state_v2

    return new_state_v2({"message": "算账", **request_extra})


def test_bridge_assembles_product_facts_for_both_costs(tmp_path, monkeypatch):
    """save 与 get_product_cost 走**同一个** `_assemble_costing_facts`（同口径的保证）。"""
    from yunpai_orchestrator.orchestration_bridge import bridge_payload

    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0-absent.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "m4b-absent.sqlite"))
    state = _bridge_state(product_code="W-H913", bom_lines=BOM_LINES,
                          routing_steps=ROUTING_STEPS, inventory=INVENTORY,
                          hour_rate=50, overhead_rate=20)

    preview = bridge_payload(state, "get_product_cost")
    save = bridge_payload(state, "save_costing_snapshot" if False else "get_product_cost")
    assert preview == save                                  # 两个分支同源
    assert preview["bom_lines"] == BOM_LINES
    assert preview["hour_rate"] == 50
    assert [row["material_code"] for row in preview["inventory"]] == ["MAT-A"]

    # 缺 product_code / 缺 BOM → BLOCKED_INPUT（不拿别的产品顶上、不空转算 0）
    no_product = bridge_payload(_bridge_state(), "get_product_cost")
    assert no_product["code"] == "BLOCKED_INPUT"
    no_bom = bridge_payload(_bridge_state(product_code="W-H913"), "get_product_cost")
    assert no_bom["code"] == "BLOCKED_INPUT"
    assert no_bom["data"]["missing_fields"] == ["已批准 BOM 行"]


def test_bridge_assembles_order_lines_and_product_facts_for_audit(tmp_path, monkeypatch):
    from yunpai_orchestrator.orchestration_bridge import bridge_payload

    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0-absent.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "m4b-absent.sqlite"))
    state = _bridge_state(order_lines=[{"product_code": "W-H913", "qty": 2, "unit_price": 9.9}],
                          bom_lines=BOM_LINES, routing_steps=ROUTING_STEPS,
                          hour_rate=50, overhead_rate=20)
    payload = bridge_payload(state, "audit_order_cost")
    assert payload["order_lines"] == [{"product_code": "W-H913", "qty": 2, "unit_price": 9.9}]
    assert payload["products"]["W-H913"]["bom_lines"] == BOM_LINES

    # M1 订单头兜底（无行项时用订单本身；缺对客单价不编造）
    from yunpai_orchestrator.state import new_state_v2

    header_state = new_state_v2({"message": "审计", "order_lines": []})
    header_state["outputs"] = {"ingest_document": {"data": {
        "document": {"header": {"order_id": "SO-9", "product_code": "W-H913",
                               "quantity": 3}}}}}
    header_payload = bridge_payload(header_state, "audit_order_cost")
    assert header_payload["order_lines"][0]["product_code"] == "W-H913"
    assert header_payload["order_lines"][0]["unit_price"] is None

    empty = bridge_payload(_bridge_state(), "audit_order_cost")
    assert empty["code"] == "BLOCKED_INPUT"


def test_bridge_reads_expense_facts_from_canonical_and_marks_source(tmp_path, monkeypatch):
    """费用事实来自 canonical（信封拆开）；基准来自报工事实；来源写进 basis_source。"""
    from yunpai_orchestrator.orchestration_bridge import bridge_payload
    from yunpai_orchestrator.state import new_state_v2

    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0-absent.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)

    state = new_state_v2({"message": "分摊"})
    state["outputs"] = {
        "data_catalog_ingest_publish": {"data": {}},
    }
    # canonical 读口：直接给桥看的实体（形状 = m0_facts.list_entities 信封）
    monkeypatch.setattr(
        "yunpai_orchestrator.orchestration_bridge._read_m0_entities",
        lambda state, entity_type: {
            "expense": [
                {"canonical_key": "EXP-1", "payload": {"category": "electricity",
                                                       "amount": 300.0, "period": PERIOD},
                 "identity": {"business_key": "EXP-1"}},
                {"canonical_key": "EXP-2", "payload": {"category": "meals",
                                                       "amount": 100.0, "period": PERIOD}},
            ],
            "production_daily_report": [
                {"canonical_key": "R-1", "payload": {"product_code": "P-1", "quantity": 30}},
                {"canonical_key": "R-2", "payload": {"product_code": "P-2", "quantity": 10}},
            ],
        }.get(entity_type, []))

    payload = bridge_payload(state, "allocate_expenses")
    assert payload["basis_source"] == "canonical"
    # 信封已拆开：业务字段在顶层（否则按 category/amount 过滤永远匹配不到）
    assert {row["category"] for row in payload["expenses"]} == {"electricity", "meals"}
    assert payload["expenses"][0]["amount"] == 300.0
    assert payload["basis_rows"] == [{"product_code": "P-1", "quantity": 30},
                                     {"product_code": "P-2", "quantity": 10}]

    # 没有任何费用事实 → basis_source=missing（正常状态，不是装配缺口、不开门）
    monkeypatch.setattr("yunpai_orchestrator.orchestration_bridge._read_m0_entities",
                        lambda state, entity_type: [])
    empty = bridge_payload(new_state_v2({"message": "分摊"}), "allocate_expenses")
    assert empty["basis_source"] == "missing"
    assert empty["expenses"] == []


# ---------------------------------------------------------------------------
# 契约 ↔ 规则一致性：三件读工具无副作用、无门
# ---------------------------------------------------------------------------

def test_cost_read_tools_are_ungated_and_declared_readonly(registry):
    for tool in ("get_product_cost", "audit_order_cost", "allocate_expenses"):
        spec = registry.specs[tool]
        assert spec.module == "m6", tool
        assert spec.side_effect == "none", tool
        assert spec.review_gate == "none", tool
        assert rules.gate_type_for(tool, spec) == "", tool
        assert rules.evaluate(tool, {"success": True, "data": {}}) == [], tool
        assert tool not in rules.RULES, f"{tool} 是只读工具，不该有门的 RULES 条目"
        # 只读工具也不得因失败开补数门（缺数用 cost_incomplete 表达）
        assert rules.evaluate(tool, {"success": False, "code": "INVALID_INPUT"}) == [], tool


def test_read_tool_completes_in_graph_without_gate(tmp_path, monkeypatch):
    """图里跑一遍 `get_product_cost`：不开门（无 interrupt）、不写库、结果可读。

    RULES 层「无门」只是声明；**图内真实路径**才是证据（v2 唯一开门点
    `reviewer_check_node` 会消费 RULES + 合同声明推导的门）。
    """
    import asyncio

    from langgraph.checkpoint.memory import MemorySaver
    from test_graph_smoke import _deps

    from yunpai_orchestrator.graph import build_graph
    from yunpai_orchestrator.state import new_state_v2

    monkeypatch.setenv("YUNPAI_M6_DB", str(tmp_path / "m6.sqlite"))
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0-absent.sqlite"))
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "m4b-absent.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)

    state = new_state_v2({"message": "算一下 W-H913 的单台成本",
                          "tools": ["get_product_cost"], "product_code": "W-H913",
                          "bom_lines": BOM_LINES, "routing_steps": ROUTING_STEPS,
                          "inventory": INVENTORY, "hour_rate": 50, "overhead_rate": 20,
                          "purchase_tracking_rows": TRACKING_ROWS,
                          "purchase_order_items": ORDER_ITEMS})
    graph = build_graph(_deps(), checkpointer=MemorySaver())
    out = asyncio.run(graph.ainvoke(
        state, {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 64}))

    assert not out.get("__interrupt__"), "只读工具不得开人工门"
    assert out["status"] == "completed", f"errors={out.get('errors')}"
    assert out["outputs"]["get_product_cost"]["data"]["unit_cost"] == 102.3
    assert _digest({"m6_db_path": str(tmp_path / "m6.sqlite")}) == []
