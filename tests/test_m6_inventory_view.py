"""B5 `get_inventory_finance_view`：财务口径库存视图（四态分账 + 在途）的验收测试。

## 本文件锁什么

- **态不猜**：`stock_class` 缺失或未知 → 单列 `unknown`，**绝不归到任何一态**（与 D5 定价
  口径同源：分不清态就明说）；
- **金额不编**：v2 的 `inventory` 实体**没有价格字段** → 缺 `unit_costs` 即 `cost_incomplete`
  + `missing_unit_cost`，金额保持 0 且**不冒充算完**；
- **在途不猜**：追踪行 `arrival_status` 为空 → 单列 `unknown_status`，不假设它在路上；
- **真读口投影**：`stock_class` 必须穿过 `read_inventory_facts` 的投影到达视图。这是本批修的
  **真问题**：该函数原先只投影 6 个字段、**把 `stock_class` 丢了** → 所有 canonical 库存都是
  「态未知」，D5「有库存且态=raw 走库存成本价」**即使数据侧补了字段也永远触发不了**。
"""

from __future__ import annotations

import pytest

from yunpai_orchestrator.orchestration_bridge import bridge_payload
from yunpai_orchestrator.registry import build_default_registry

CTX = {"task_id": "TASK-M6-INV-VIEW", "tenant_id": "default", "actor": "tester"}


@pytest.fixture
def inventory_entities(monkeypatch):
    """把 canonical 读口换成**真实产出形状**（`{canonical_key, payload}`）。

    为什么不用 `CatalogService.publish_records` 种数据：**`inventory` 不在 `m0.ingest.v1` 的
    `ENTITY_TYPES` 白名单里**（它有 `CANONICAL_SCHEMA` 定义但不能走 ingest/facade 发布），
    canonical 里的 `inventory` 记录只能由 M0 的**导入批次路径**写入——那是另一条线的口径，
    不在 M6 能决定的范围内（已记入交接"遗留"）。这里直接喂读口产出形状，测的正是 M6 自己
    那段投影逻辑（回归点就在这里）。
    """
    canned = [
        {"canonical_key": "INV-RAW-01",
         "payload": {"material_code": "M-RAW", "available_qty": 100, "stock_class": "raw"}},
        {"canonical_key": "INV-FIN-01",
         "payload": {"material_code": "M-FIN", "available_qty": 10, "stock_class": "finished"}},
        {"canonical_key": "INV-NO-CLASS",
         "payload": {"material_code": "M-NO-CLASS", "available_qty": 5}},
    ]
    monkeypatch.setattr("yunpai_orchestrator.orchestration_bridge._read_m0_entities",
                        lambda state, entity_type: canned if entity_type == "inventory" else [])
    return canned


# ---------------------------------------------------------------------------
# 纯算数：四态分账 / 态不猜 / 金额不编 / 在途不猜
# ---------------------------------------------------------------------------

async def test_view_splits_by_four_stock_classes():
    registry = build_default_registry()
    result = await registry.call("get_inventory_finance_view", {
        "inventory": [
            {"material_code": "M-RAW", "available_qty": 100, "stock_class": "raw"},
            {"material_code": "M-FIN", "available_qty": 10, "stock_class": "finished"},
            {"material_code": "M-SEMI", "available_qty": 5, "stock_class": "semi"},
            {"material_code": "M-WIP", "available_qty": 3, "stock_class": "wip"},
        ],
        "unit_costs": {"M-RAW": 2.0, "M-FIN": 50.0, "M-SEMI": 8.0, "M-WIP": 4.0},
    }, CTX)
    assert result["success"] is True, result.get("errors")
    by_class = result["data"]["by_class"]
    assert by_class["raw"]["total_qty"] == 100
    assert by_class["raw"]["total_amount"] == 200.0
    assert by_class["finished"]["total_amount"] == 500.0
    assert by_class["semi"]["line_count"] == 1
    assert by_class["wip"]["total_qty"] == 3
    assert by_class["unknown"]["line_count"] == 0
    # by_class.finished 即计划所称「成品账」，不另设重复字段
    assert by_class["finished"]["label"] == "成品在库"
    assert result["data"]["cost_incomplete"] is False


async def test_unknown_class_is_isolated_and_never_guessed_into_a_state():
    """态缺失 → unknown；态是未知值 → unknown **且**记 missing。都不许落到 raw。"""
    registry = build_default_registry()
    result = await registry.call("get_inventory_finance_view", {
        "inventory": [
            {"material_code": "M-NO-CLASS", "available_qty": 7},                          # 缺态
            {"material_code": "M-BAD-CLASS", "available_qty": 2, "stock_class": "在制品"},  # 未知值
        ],
        "unit_costs": {"M-NO-CLASS": 1.0, "M-BAD-CLASS": 1.0},
    }, CTX)
    by_class = result["data"]["by_class"]
    assert by_class["unknown"]["line_count"] == 2
    assert by_class["unknown"]["total_qty"] == 9
    # 关键：两行都**没有**被归到任何一态
    for name in ("raw", "finished", "semi", "wip"):
        assert by_class[name]["line_count"] == 0, f"{name} 不该收到态不明/缺失的行"
    # 未知值另记 missing（缺态不记：那是合法状态，只是分不清）
    reasons = {(row["reason"], row["material_code"]) for row in result["data"]["missing"]}
    assert ("unknown_stock_class", "M-BAD-CLASS") in reasons
    assert not any(code == "M-NO-CLASS" and reason == "unknown_stock_class"
                   for reason, code in reasons)


async def test_amount_is_incomplete_when_unit_cost_missing():
    """缺单价不是「0 元」——标 missing + cost_incomplete，绝不当算完。"""
    registry = build_default_registry()
    result = await registry.call("get_inventory_finance_view", {
        "inventory": [{"material_code": "M-A", "available_qty": 10, "stock_class": "raw"}],
    }, CTX)
    data = result["data"]
    assert data["cost_incomplete"] is True
    assert data["by_class"]["raw"]["amount_incomplete"] is True
    assert data["by_class"]["raw"]["total_amount"] == 0.0
    assert data["missing"] == [{"line": 1, "material_code": "M-A",
                                "reason": "missing_unit_cost", "stock_class": "raw"}]
    # 估价价源未显式给 → 取口径层默认并标 assumed（口径痕，不冒充已确认事实）
    assert data["valuation_price_source"] == "bom_price"
    assert data["assumptions"]["valuation_price_source_assumed"] is True
    assert data["assumptions"]["pending_finance_confirmation"]


async def test_amount_computed_when_unit_cost_given_and_assumed_cleared():
    registry = build_default_registry()
    result = await registry.call("get_inventory_finance_view", {
        "inventory": [{"material_code": "M-A", "available_qty": 10, "stock_class": "raw"}],
        "unit_costs": {"M-A": 3.5},
        "valuation_price_source": "latest_purchase_price",
    }, CTX)
    data = result["data"]
    assert data["cost_incomplete"] is False
    assert data["by_class"]["raw"]["total_amount"] == 35.0
    assert data["valuation_price_source"] == "latest_purchase_price"
    assert data["assumptions"]["valuation_price_source_assumed"] is False


async def test_in_transit_excludes_received_and_isolates_unknown_status():
    registry = build_default_registry()
    result = await registry.call("get_inventory_finance_view", {
        "inventory": [],
        "purchase_tracking_rows": [
            {"material_code": "M-A", "quantity": 10, "arrival_status": "received"},
            {"material_code": "M-B", "quantity": 20, "arrival_status": "pending"},
            {"material_code": "M-C", "quantity": 30},                    # 状态为空
        ],
    }, CTX)
    data = result["data"]
    assert data["in_transit"]["line_count"] == 1
    assert data["in_transit"]["lines"][0]["material_code"] == "M-B"
    # 已入库不算在途
    assert all(row["material_code"] != "M-A" for row in data["in_transit"]["lines"])
    # 状态为空 → unknown_status，**不假设它在路上**
    assert data["unknown_status"]["line_count"] == 1
    assert data["unknown_status"]["lines"][0]["material_code"] == "M-C"


# ---------------------------------------------------------------------------
# 读口投影回归：stock_class 必须活到视图
# ---------------------------------------------------------------------------

async def test_stock_class_survives_the_canonical_read_projection(inventory_entities):
    """**回归锁**：`read_inventory_facts` 的投影必须带出 `stock_class`。

    修之前该投影只有 6 个字段（material_code/warehouse/available_qty/lot_no/qc_status/unit）
    → canonical 库存**全部**被当成「态未知」，D5「有库存且态=raw 走库存成本价」永远不触发。
    """
    registry = build_default_registry()
    state = {"tenant_id": "default", "request": {}}
    payload = bridge_payload(state, "get_inventory_finance_view")
    assert {row["material_code"]: row["stock_class"] for row in payload["inventory"]} == {
        "M-RAW": "raw", "M-FIN": "finished", "M-NO-CLASS": "",
    }

    result = await registry.call("get_inventory_finance_view",
                                 {**payload, "unit_costs": {"M-RAW": 2.0, "M-FIN": 50.0}}, CTX)
    by_class = result["data"]["by_class"]
    assert by_class["raw"]["line_count"] == 1
    assert by_class["raw"]["total_amount"] == 200.0
    assert by_class["finished"]["total_amount"] == 500.0
    # 缺态的行走 unknown——**不是**被猜成 raw
    assert by_class["unknown"]["line_count"] == 1
    assert result["data"]["inventory_source"] == "explicit"
