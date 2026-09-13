"""R1a（D-007）：M6 缺料价源解析——从 M4 采购追踪取实际采购单价。

红线：单价不可解析的行**不进入价源**，计入 missing 并令 ``cost_incomplete=True``；
绝不用默认价或 0 冒充实际采购价（不编造）。

数据面依据：v2 的单价不在采购单（`purchase_order_item_to_json` 无价格字段），
而在 M4B 采购追踪行（`list_m4_tracking` → `unit_price`）。
"""

from __future__ import annotations

from yunpai_orchestrator.m6_price_source import (
    price_for,
    purchase_prices_from_tracking,
)

ITEM_LOOKUP = {
    "11": {"item_code": "PUR-001", "internal_material_no": "M-001"},
    "12": {"item_code": "PUR-002", "internal_material_no": "M-002"},
}

TRACKING = [
    {"id": 1, "purchase_order_no": "PO-1", "purchase_order_item_id": "11",
     "supplier_name": "甲供应商", "promised_date": "2026-09-01",
     "unit_price": "12.50", "currency": "CNY"},
]


def test_builds_price_by_internal_material_no_with_evidence():
    result = purchase_prices_from_tracking(TRACKING, ITEM_LOOKUP)
    assert result["cost_incomplete"] is False
    # 内部料号 + 采购料号两个键都登记（消费方拿哪个键都能命中）
    assert set(result["prices"]) == {"M-001", "PUR-001"}
    fact = result["prices"]["M-001"]
    assert fact["unit_price"] == 12.5
    assert fact["currency"] == "CNY"
    assert fact["supplier_name"] == "甲供应商"
    # 来源证据：供 m6_cost_lines.source_ref 落库
    assert fact["source_ref"] == "tracking:1@PO-1"


def test_unparsable_price_is_missing_not_fabricated():
    result = purchase_prices_from_tracking(
        [
            {"id": 1, "purchase_order_no": "PO-1", "purchase_order_item_id": "11",
             "unit_price": "12.50"},
            {"id": 2, "purchase_order_no": "PO-2", "purchase_order_item_id": "12",
             "unit_price": "待定"},          # 不可解析
            {"id": 3, "purchase_order_no": "PO-3", "purchase_order_item_id": "12"},  # 缺字段
        ],
        ITEM_LOOKUP,
    )
    assert result["cost_incomplete"] is True
    assert {m["reason"] for m in result["missing"]} == {"unparsable_unit_price"}
    # 有价的那条仍在，无价的两条不入价源（M-002 不得有价）
    assert result["prices"]["M-001"]["unit_price"] == 12.5
    assert "M-002" not in result["prices"]


def test_latest_promised_date_wins_and_same_day_falls_back_to_id():
    result = purchase_prices_from_tracking(
        [
            {"id": 1, "purchase_order_no": "PO-1", "purchase_order_item_id": "11",
             "promised_date": "2026-09-01", "unit_price": "10.00"},
            {"id": 5, "purchase_order_no": "PO-5", "purchase_order_item_id": "11",
             "promised_date": "2026-09-20", "unit_price": "11.00"},
            {"id": 9, "purchase_order_no": "PO-9", "purchase_order_item_id": "11",
             "promised_date": "2026-09-20", "unit_price": "12.00"},
        ],
        ITEM_LOOKUP,
    )
    # 2026-09-20 最新；同日取 id 最大（9）
    assert result["prices"]["M-001"]["unit_price"] == 12.0
    assert result["prices"]["M-001"]["source_ref"] == "tracking:9@PO-9"


def test_empty_tracking_is_missing_not_zero():
    result = purchase_prices_from_tracking([], ITEM_LOOKUP)
    assert result["cost_incomplete"] is True
    assert result["prices"] == {}
    assert result["missing"] == [{"reason": "missing_tracking_rows"}]


def test_falls_back_to_item_id_when_lookup_absent():
    """无采购单行项映射时回落到行项 id 键（仍可用，只是消费方需用同一 id 查）。"""
    result = purchase_prices_from_tracking(TRACKING)
    assert set(result["prices"]) == {"11"}
    assert result["prices"]["11"]["unit_price"] == 12.5


def test_price_for_miss_returns_none():
    facts = purchase_prices_from_tracking(TRACKING, ITEM_LOOKUP)
    assert price_for(facts, "M-001")["unit_price"] == 12.5
    assert price_for(facts, "M-999") is None
    assert price_for(None, "M-001") is None
