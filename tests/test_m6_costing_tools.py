"""B1 第二批：M6 成本账工具（F-008 / 契约见 ``registry-manifests/m6.json``）。

本文件锁住四件事：

1. **D5 定价口径**：有库存（态已知且为原料）→ 库存成本价（BOM 价）；缺料 → M4 采购
   追踪的实际单价；**两缺 → 该行 ``missing`` + 整单 ``cost_incomplete``，绝不编造**；
2. **三段式的 propose 段**：`save_costing_snapshot` 只落 ``status=trial``、不进月末汇总；
3. **三段式的 commit 段发起**：`confirm_costing_snapshot` / `close_month_costing` 自身
   **不改状态**，只做前置校验并按 ``pending_*`` 约定请求人工门（翻正/冻结在 graph 钩子，
   见 ``test_m6_finance_gate.py``）；
4. **D-008「试算快照刻意无门」**：显式 ``review_gate="none"`` 的声明必须保留（check_contracts
   会为此报一条 W2，属已裁定；被改掉就等于决定失效）。
"""

from __future__ import annotations

import pytest

from yunpai_orchestrator.canonical_schema import STOCK_CLASSES as CANONICAL_STOCK_CLASSES
from yunpai_orchestrator.m6_price_source import STOCK_CLASSES, resolve_material_prices
from yunpai_orchestrator.m6_store import M6Store, STATUS_CONFIRMED, STATUS_TRIAL
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.reviewer import rules

PERIOD = "2026-09"

#: 三行 BOM：A 有库存（走库存价）、B 缺料（走采购价）、C 缺料且无价（必须标不全）。
BOM_LINES = [
    {"material_code": "MAT-A", "qty_per": 2, "unit_price": 5.0, "loss_rate": 0.1},
    {"material_code": "MAT-B", "qty_per": 1, "unit_price": 8.0, "loss_rate": 0.0},
    {"material_code": "MAT-C", "qty_per": 4, "unit_price": 3.0, "loss_rate": 0.0},
]
ROUTING_STEPS = [
    {"operation_id": "OP-10", "standard_minutes": 30},
    {"operation_id": "OP-20", "standard_minutes": 90},
]
INVENTORY = [{"material_code": "MAT-A", "available_qty": 100, "stock_class": "raw",
              "warehouse": "原料仓"}]
TRACKING_ROWS = [{
    "id": 7, "purchase_order_no": "PO-1", "purchase_order_item_id": 21,
    "supplier_name": "供应商一", "promised_date": "2026-09-01",
    "unit_price": "8.5", "currency": "CNY",
}]
ORDER_ITEMS = [{"id": 21, "item_code": "MAT-B", "internal_material_no": "IM-B"}]


def _ctx(tmp_path, **extra):
    return {"tenant_id": "default", "task_id": "task-m6",
            "m6_db_path": str(tmp_path / "m6.sqlite"), **extra}


def _save_payload(**overrides):
    payload = {
        "period": PERIOD, "order_id": "SO-1", "product_code": "W-H913", "batch_no": "B1",
        "quantity": 10, "bom_lines": BOM_LINES, "routing_steps": ROUTING_STEPS,
        "inventory": INVENTORY, "hour_rate": 50, "overhead_rate": 20,
        "purchase_tracking_rows": TRACKING_ROWS, "purchase_order_items": ORDER_ITEMS,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def registry():
    return build_default_registry()


# ---------------------------------------------------------------------------
# D5 定价口径（纯函数层）
# ---------------------------------------------------------------------------

def test_stock_classes_mirror_matches_canonical():
    """纯函数层不 import v2 模块，故库存四态是本地镜像——必须与 canonical 单一事实源一致。"""
    assert set(STOCK_CLASSES) == set(CANONICAL_STOCK_CLASSES)


def test_resolve_prefers_stock_price_when_stock_known():
    resolved = resolve_material_prices(BOM_LINES[:1], INVENTORY, {})
    line = resolved["lines"][0]
    assert line["source_kind"] == "stock"
    assert line["unit_price"] == 5.0        # 库存口径 = BOM 价
    assert line["stock_class_known"] is True
    assert resolved["missing"] == [] and resolved["cost_incomplete"] is False
    assert resolved["basis"] == "stock"


def test_resolve_falls_back_to_purchase_price_without_stock():
    facts = {"prices": {"MAT-B": {"unit_price": 8.5, "source_ref": "tracking:7@PO-1"}}}
    resolved = resolve_material_prices([BOM_LINES[1]], [], facts)
    line = resolved["lines"][0]
    assert line["source_kind"] == "purchase"
    assert line["unit_price"] == 8.5
    assert line["source_ref"] == "tracking:7@PO-1"      # 证据链带来源
    assert resolved["basis"] == "purchase"


def test_resolve_does_not_guess_stock_when_state_missing_or_non_raw():
    """库存态缺失/未知或不是原料 → **不猜有库存**，回落实际采购价（fail-closed 不编造）。"""
    facts = {"prices": {"MAT-A": {"unit_price": 9.9}}}
    for inventory in ([{"material_code": "MAT-A", "available_qty": 100}],               # 态缺失
                      [{"material_code": "MAT-A", "available_qty": 100,
                        "stock_class": "finished"}]):                                   # 非原料在库
        resolved = resolve_material_prices([BOM_LINES[0]], inventory, facts)
        assert resolved["lines"][0]["source_kind"] == "purchase"
        assert resolved["lines"][0]["unit_price"] == 9.9


def test_resolve_reports_missing_instead_of_inventing_price():
    resolved = resolve_material_prices(BOM_LINES, INVENTORY, {})
    missing = {row["material_code"]: row for row in resolved["missing"]}
    assert set(missing) == {"MAT-B", "MAT-C"}          # 缺料且无价的都不编造
    assert all(row["reason"] == "missing_purchase_price" for row in missing.values())
    assert resolved["cost_incomplete"] is True
    assert len(resolved["lines"]) == 1                 # 只有 MAT-A 定出了价


# ---------------------------------------------------------------------------
# propose 段：save_costing_snapshot
# ---------------------------------------------------------------------------

async def test_save_writes_trial_snapshot_with_priced_lines(registry, tmp_path):
    result = await registry.call("save_costing_snapshot", _save_payload(), _ctx(tmp_path))
    assert result["success"] is True, result.get("errors")
    data = result["data"]
    assert data["status"] == STATUS_TRIAL              # propose 段：只写草稿
    assert data["basis"] == "mixed"                    # A 走库存、B 走采购
    assert data["cost_incomplete"] is True             # C 无价 → 成本不全
    assert {row["material_code"] for row in data["missing"]} == {"MAT-C"}

    # 材料 = 5*2*1.1 + 8.5*1 = 19.5；人工 = 2h*50 = 100；制费 = 2h*20 = 40
    assert data["unit_cost"] == 159.5
    assert data["total_cost"] == 1595.0                # ×10 台

    store = M6Store(_ctx(tmp_path)["m6_db_path"])
    snapshot = store.get_snapshot(data["snapshot_id"])
    assert snapshot["status"] == STATUS_TRIAL
    assert snapshot["cost_incomplete"] is True
    kinds = {row["element"]: row for row in snapshot["lines"]}
    assert kinds["material"]["source_kind"] in {"stock", "purchase"}
    assert kinds["labor"]["source_kind"] == "route"    # 工时的真实来源是工艺路线
    assert kinds["labor"]["quantity"] == 2.0
    assert kinds["overhead"]["amount"] == 40.0
    # 证据链：每行都有 source_ref，快照头有口径痕迹
    assert all(row["source_ref"] for row in snapshot["lines"])
    assert snapshot["evidence"]["assumptions"]["valuation_price_source_assumed"] is True
    assert snapshot["evidence"]["assumptions"]["hour_rate_assumed"] is False

    # 试算不进月末汇总（护栏：只认 confirmed）
    summary = store.month_summary(PERIOD)
    assert summary["snapshot_count"] == 0 and summary["total_cost"] == 0.0
    assert summary["trial_count"] == 1 and summary["trial_excluded"] is True


async def test_save_complete_cost_when_all_prices_available(registry, tmp_path):
    payload = _save_payload(purchase_tracking_rows=[
        *TRACKING_ROWS,
        {"id": 8, "purchase_order_no": "PO-2", "purchase_order_item_id": 22,
         "promised_date": "2026-09-02", "unit_price": "3.2", "currency": "CNY"},
    ], purchase_order_items=[*ORDER_ITEMS,
                             {"id": 22, "item_code": "MAT-C", "internal_material_no": "IM-C"}])
    result = await registry.call("save_costing_snapshot", payload, _ctx(tmp_path))
    data = result["data"]
    assert data["cost_incomplete"] is False
    assert data["missing"] == []
    # 5*2*1.1 + 8.5*1 + 3.2*4 = 32.3；+100 +40 = 172.3
    assert data["unit_cost"] == 172.3
    assert data["basis"] == "mixed"


async def test_save_derives_snapshot_id_and_reports_duplicate(registry, tmp_path):
    first = await registry.call("save_costing_snapshot", _save_payload(), _ctx(tmp_path))
    second = await registry.call("save_costing_snapshot", _save_payload(), _ctx(tmp_path))
    assert first["data"]["snapshot_id"] != second["data"]["snapshot_id"]   # 历史不覆盖
    assert first["data"]["duplicate_of"] is None
    assert second["data"]["duplicate_of"]["other_count"] == 1
    assert second["data"]["duplicate_of"]["latest"]["snapshot_id"] == first["data"]["snapshot_id"]

    # 显式指定已存在的快照号 → 显式拒绝，不做隐式覆盖
    clash = await registry.call("save_costing_snapshot",
                                _save_payload(snapshot_id=first["data"]["snapshot_id"]),
                                _ctx(tmp_path))
    assert clash["success"] is False and clash["code"] == "SNAPSHOT_EXISTS"


async def test_save_missing_required_inputs_is_reported_not_fabricated(registry, tmp_path):
    """缺 BOM/工艺/费率时不编造：成本不全 + 明示缺什么。"""
    result = await registry.call("save_costing_snapshot",
                                 {"period": PERIOD, "quantity": 1}, _ctx(tmp_path))
    data = result["data"]
    assert data["cost_incomplete"] is True
    assert data["unit_cost"] == 0.0
    assert {item["reason"] for item in data["missing_inputs"]} == {"missing_bom_lines",
                                                                  "missing_routing_steps"}
    snapshot = M6Store(_ctx(tmp_path)["m6_db_path"]).get_snapshot(data["snapshot_id"])
    assert snapshot["evidence"]["missing_inputs"] == data["missing_inputs"]


async def test_save_rejected_after_month_close(registry, tmp_path):
    ctx = _ctx(tmp_path)
    store = M6Store(ctx["m6_db_path"])
    store.close_month(period=PERIOD, totals={"total_cost": 0.0}, snapshot_count=0,
                      actor="finance-officer")
    result = await registry.call("save_costing_snapshot", _save_payload(), ctx)
    assert result["success"] is False and result["code"] == "MONTH_CLOSED"
    assert store.get_snapshot("M6-2026-09-SO-1-B1-v1") is None      # 确实没落库


# ---------------------------------------------------------------------------
# commit 段的发起：确认 / 月结（工具自身不得翻状态）
# ---------------------------------------------------------------------------

async def test_confirm_requests_gate_without_flipping(registry, tmp_path):
    ctx = _ctx(tmp_path)
    saved = (await registry.call("save_costing_snapshot", _save_payload(), ctx))["data"]
    result = await registry.call("confirm_costing_snapshot",
                                 {"snapshot_id": saved["snapshot_id"]}, ctx)
    assert result["success"] is True
    assert result["data"]["pending_confirmation"] is True
    # 关键：工具**没有**翻状态（trial→confirmed 属 commit 段，由门批准后的钩子执行）
    assert result["data"]["status"] == STATUS_TRIAL
    store = M6Store(ctx["m6_db_path"])
    assert store.get_snapshot(saved["snapshot_id"])["status"] == STATUS_TRIAL
    assert store.month_summary(PERIOD)["snapshot_count"] == 0


async def test_confirm_is_idempotent_and_explicit_about_missing(registry, tmp_path):
    ctx = _ctx(tmp_path)
    saved = (await registry.call("save_costing_snapshot", _save_payload(), ctx))["data"]
    store = M6Store(ctx["m6_db_path"])
    store.confirm_snapshot(saved["snapshot_id"], actor="finance-officer")

    again = await registry.call("confirm_costing_snapshot",
                                {"snapshot_id": saved["snapshot_id"]}, ctx)
    assert again["success"] is True
    assert again["data"]["pending_confirmation"] is False    # 已确认 → 不再开第二次门
    assert again["data"]["changed"] is False

    missing = await registry.call("confirm_costing_snapshot", {"snapshot_id": "S-NOPE"}, ctx)
    assert missing["success"] is False and missing["code"] == "NOT_FOUND"


async def test_confirm_rejected_when_month_closed(registry, tmp_path):
    ctx = _ctx(tmp_path)
    saved = (await registry.call("save_costing_snapshot", _save_payload(), ctx))["data"]
    store = M6Store(ctx["m6_db_path"])
    store.close_month(period=PERIOD, totals={}, snapshot_count=0, actor="finance-officer")
    result = await registry.call("confirm_costing_snapshot",
                                 {"snapshot_id": saved["snapshot_id"]}, ctx)
    assert result["success"] is False and result["code"] == "MONTH_CLOSED"
    assert store.get_snapshot(saved["snapshot_id"])["status"] == STATUS_TRIAL   # 未被翻正


async def test_close_month_reports_reviewed_totals_without_freezing(registry, tmp_path):
    ctx = _ctx(tmp_path)
    saved = (await registry.call("save_costing_snapshot", _save_payload(), ctx))["data"]
    store = M6Store(ctx["m6_db_path"])

    # 库里只有 trial（未确认）时：待冻结合计必须是 0，且 trial 以 trial_count 明示
    pending = await registry.call("close_month_costing", {"period": PERIOD}, ctx)
    assert pending["success"] is True and pending["data"]["pending_close"] is True
    assert pending["data"]["snapshot_count"] == 0
    assert pending["data"]["trial_count"] == 1
    assert store.get_month_close(PERIOD) is None             # 工具没有冻结月账

    store.confirm_snapshot(saved["snapshot_id"], actor="finance-officer")
    pending2 = await registry.call("close_month_costing", {"period": PERIOD}, ctx)
    assert pending2["data"]["total_cost"] == 1595.0          # 被审阅的就是被冻结的那组数
    assert pending2["data"]["snapshot_count"] == 1

    store.close_month(period=PERIOD, totals={"total_cost": 1595.0}, snapshot_count=1,
                      actor="finance-officer")
    again = await registry.call("close_month_costing", {"period": PERIOD}, ctx)
    assert again["success"] is False and again["code"] == "MONTH_ALREADY_CLOSED"


# ---------------------------------------------------------------------------
# 读工具
# ---------------------------------------------------------------------------

async def test_read_tools_expose_trial_and_confirmed_separately(registry, tmp_path):
    ctx = _ctx(tmp_path)
    first = (await registry.call("save_costing_snapshot", _save_payload(), ctx))["data"]
    second = (await registry.call("save_costing_snapshot", _save_payload(batch_no="B2"),
                                  ctx))["data"]
    store = M6Store(ctx["m6_db_path"])
    store.confirm_snapshot(second["snapshot_id"], actor="finance-officer")

    listed = await registry.call("list_costing_snapshots", {"period": PERIOD}, ctx)
    assert listed["data"]["count"] == 2
    trial_only = await registry.call("list_costing_snapshots",
                                     {"period": PERIOD, "status": "trial"}, ctx)
    assert [row["snapshot_id"] for row in trial_only["data"]["snapshots"]] == \
        [first["snapshot_id"]]

    detail = await registry.call("get_costing_snapshot",
                                 {"snapshot_id": second["snapshot_id"]}, ctx)
    assert detail["data"]["snapshot"]["status"] == STATUS_CONFIRMED
    assert detail["data"]["snapshot"]["lines"]
    assert (await registry.call("get_costing_snapshot", {"snapshot_id": "S-NOPE"},
                                ctx))["code"] == "NOT_FOUND"

    month = await registry.call("list_month_costing", {"period": PERIOD}, ctx)
    assert month["data"]["months"][0]["snapshot_count"] == 1     # 只认 confirmed
    assert month["data"]["months"][0]["total_cost"] == second["total_cost"]
    assert month["data"]["months"][0]["trial_count"] == 1

    all_months = await registry.call("list_month_costing", {}, ctx)
    assert [row["period"] for row in all_months["data"]["months"]] == [PERIOD]


# ---------------------------------------------------------------------------
# R1a 装配接线（orchestration_bridge）——M6 自己不开 M4 store（D9）
# ---------------------------------------------------------------------------

def _bridge_state(**request_extra):
    from yunpai_orchestrator.state import new_state_v2

    return new_state_v2({"message": "算成本", **request_extra})


def test_bridge_reads_actual_purchase_prices_from_m4b(tmp_path, monkeypatch):
    """价源事实由装配层从 M4B 采购追踪读（M6 不自开 store），并可直接喂 D5 定价。"""
    from yunpai_orchestrator.m4b_store import M4BStore
    from yunpai_orchestrator.orchestration_bridge import read_m4_tracking_price_facts

    m4b = M4BStore(str(tmp_path / "m4b.sqlite"))
    m4b.tracking_upsert(
        tenant_id="default", purchase_order_id="PO-1", purchase_order_no="PO-1",
        purchase_order_item_id="21", supplier_name="供应商一", promised_date="2026-09-01",
        unit_price="8.5", currency="CNY", exception_type=None, exception_description=None)
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "m4b.sqlite"))
    monkeypatch.setenv("YUNPAI_M4_DB", str(tmp_path / "m4-absent.sqlite"))

    facts = read_m4_tracking_price_facts(_bridge_state())
    assert [row["unit_price"] for row in facts["purchase_tracking_rows"]] == ["8.5"]

    # 走一遍 M6 的解析：无映射时按行项 id 键回落（BOM 料号即该 id）
    from yunpai_orchestrator.m6_price_source import purchase_prices_from_tracking

    parsed = purchase_prices_from_tracking(facts["purchase_tracking_rows"],
                                           {"21": {"item_code": "MAT-B"}})
    resolved = resolve_material_prices(
        [{"material_code": "MAT-B", "qty_per": 1, "unit_price": 8.0}], [], parsed)
    assert resolved["lines"][0]["unit_price"] == 8.5
    assert resolved["lines"][0]["source_ref"].startswith("tracking:")


def test_bridge_assembles_costing_payload_from_canonical_facts(tmp_path, monkeypatch):
    from yunpai_orchestrator.orchestration_bridge import bridge_payload

    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0-absent.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "m4b-absent.sqlite"))

    state = _bridge_state(
        period=PERIOD, product_code="W-H913", order_id="SO-1", batch_no="B1", quantity=10,
        bom_lines=BOM_LINES, routing_steps=ROUTING_STEPS, inventory=INVENTORY,
        hour_rate=50, overhead_rate=20)
    payload = bridge_payload(state, "save_costing_snapshot")
    assert payload["period"] == PERIOD
    assert payload["product_code"] == "W-H913"
    assert payload["bom_lines"] == BOM_LINES
    assert payload["routing_steps"] == ROUTING_STEPS
    # 库存事实经 read_inventory_facts 归一（补 warehouse/lot/qc 等非关键默认值）
    assert [(row["material_code"], row["available_qty"], row["stock_class"])
            for row in payload["inventory"]] == [("MAT-A", 100, "raw")]
    assert payload["hour_rate"] == 50

    # 缺权威输入必须 BLOCKED_INPUT（不是空载荷交给工具瞎算）
    no_bom = bridge_payload(_bridge_state(period=PERIOD, product_code="W-H913"),
                            "save_costing_snapshot")
    assert no_bom["code"] == "BLOCKED_INPUT"
    assert no_bom["data"]["missing_fields"] == ["已批准 BOM 行"]
    no_product = bridge_payload(_bridge_state(period=PERIOD), "save_costing_snapshot")
    assert no_product["code"] == "BLOCKED_INPUT"
    no_period = bridge_payload(
        _bridge_state(product_code="W-H913", bom_lines=BOM_LINES), "save_costing_snapshot")
    assert no_period["code"] == "BLOCKED_INPUT"       # 账期不能不猜


def test_bridge_chains_snapshot_id_and_period_from_upstream(tmp_path, monkeypatch):
    """确认/月结/回读的快照号与账期从上游 save 的产出回流（不靠人再抄一遍）。"""
    from yunpai_orchestrator.orchestration_bridge import bridge_payload

    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0-absent.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)
    state = _bridge_state()
    state["outputs"] = {"save_costing_snapshot": {"data": {"snapshot_id": "M6-X-v1",
                                                          "period": PERIOD}}}
    assert bridge_payload(state, "confirm_costing_snapshot") == {"snapshot_id": "M6-X-v1"}
    assert bridge_payload(state, "get_costing_snapshot") == {"snapshot_id": "M6-X-v1"}
    assert bridge_payload(state, "close_month_costing") == {"period": PERIOD}

    empty = bridge_payload(_bridge_state(), "confirm_costing_snapshot")
    assert empty["code"] == "BLOCKED_INPUT"           # 上游没产出 → 明示缺什么


# ---------------------------------------------------------------------------
# 契约 ↔ 规则一致性（B-002 教训：声明与生效门型必须一一对应）
# ---------------------------------------------------------------------------

def test_m6_contracts_and_rules_agree(registry):
    expected = {
        # 工具: (side_effect, review_gate, 归一化后的门)
        "save_costing_snapshot": ("local_write", "none", ""),        # propose 段（D-008）
        "confirm_costing_snapshot": ("local_write", "finance", "finance"),
        "close_month_costing": ("local_write", "finance", "finance"),
        "list_costing_snapshots": ("none", "none", ""),
        "get_costing_snapshot": ("none", "none", ""),
        "list_month_costing": ("none", "none", ""),
    }
    for tool, (side_effect, review_gate, gate) in expected.items():
        spec = registry.specs[tool]
        assert spec.module == "m6", tool
        assert spec.side_effect == side_effect, tool
        assert spec.review_gate == review_gate, tool
        assert rules.gate_type_for(tool, spec) == gate, tool


def test_trial_snapshot_is_intentionally_ungated(registry):
    """**D-008**：试算快照刻意无门——`check_contracts` 会为此报一条 W2，属已裁定。

    v2 契约模型按 `side_effect` 推导默认门（`registry._contract_defaults`），
    「写库但不要门」只能靠**显式** `review_gate="none"` 表达。凭什么可以无门：
    试算只写 `status=trial`、不进月末汇总、不构成生效事实（计划 §6 A 问题），
    而"每笔成本都人工确认"与试算的快节奏直接冲突。

    若本断言失败，说明有人给它加了门或改回了推导默认门——两种改法都让 D-008 失效，
    请先回看账本 `PROJECT_OVERVIEW.md` 的 D-008 再动。
    """
    spec = registry.specs["save_costing_snapshot"]
    assert spec.side_effect == "local_write"          # 如实声明：它确实写库
    assert spec.review_gate == "none"                 # 显式关闭，不是推导得到的
    assert rules.gate_type_for("save_costing_snapshot", spec) == ""
    assert rules.evaluate("save_costing_snapshot",
                          {"success": True, "data": {"status": "trial"}}) == []
    # 但失败必须能被审出来（不得当成功吞掉）
    assert [f["action"] for f in rules.evaluate("save_costing_snapshot",
                                                {"success": False, "code": "MONTH_CLOSED"})] == ["fail"]


def test_finance_gate_rules_never_auto_approve():
    """红线：finance 门不得被 LLM 置信度绕过（同 `AUTO_APPROVE_ALLOWED` 口径）。"""
    assert rules.AUTO_APPROVE_ALLOWED is False
    for tool in ("confirm_costing_snapshot", "close_month_costing"):
        findings = rules.evaluate(tool, {"success": True, "data": {
            "pending_confirmation": True, "pending_close": True}})
        assert [f["gate"] for f in findings] == ["finance"], tool
        assert rules.default_gate_for_authorized(tool, None, []) is False
        assert rules.default_gate_for_authorized(tool, None, [tool]) is True
