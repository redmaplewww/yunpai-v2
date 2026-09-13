"""canonical_schema 单一事实源 + 确定性校验器。"""

from __future__ import annotations

from yunpai_orchestrator.canonical_schema import (
    CANONICAL_SCHEMA,
    allowed_fields,
    known_entity_types,
    required_fields,
    validate_canonical,
)


def test_schema_covers_core_entities():
    for entity_type in ("order", "product", "bom", "material", "equipment",
                        "station", "worker", "inventory", "supplier", "tooling",
                        "route", "operation", "calendar", "document"):
        assert entity_type in CANONICAL_SCHEMA
        assert required_fields(entity_type)
        assert allowed_fields(entity_type)


def test_validate_accepts_clean_material_records():
    result = validate_canonical("material", [
        {"material_code": "YA.001", "material_name": "铜箔", "unit": "m"},
        {"material_code": "YA.002", "material_name": "线材"},
    ])
    assert result["errors"] == []
    assert len(result["clean_records"]) == 2
    assert result["clean_records"][0]["material_code"] == "YA.001"


def test_validate_rejects_unknown_entity_type():
    result = validate_canonical("bogus", [{"a": 1}])
    assert result["errors"][0]["code"] == "UNKNOWN_ENTITY_TYPE"


def test_validate_rejects_missing_required_field():
    result = validate_canonical("material", [{"material_name": "铜箔"}])
    assert any("material_code" in e["message"] for e in result["errors"])


def test_validate_rejects_unknown_field():
    result = validate_canonical("material", [{"material_code": "M-1", "material_name": "x", "随意字段": 1}])
    assert any("随意字段" in e["message"] for e in result["errors"])


def test_validate_rejects_non_numeric_quantity():
    result = validate_canonical("inventory", [
        {"material_code": "M-1", "available_qty": "一百", "warehouse": "WH-1"},
    ])
    assert any("available_qty" in e["message"] and "数字" in e["message"] for e in result["errors"])


def test_validate_accepts_numeric_quantity_and_strips_evidence():
    result = validate_canonical("inventory", [
        {"material_code": "M-1", "available_qty": 100, "warehouse": "WH-1",
         "_source": {"sheet": "库存", "row": 3, "col": 4, "raw": 100}},
    ])
    assert result["errors"] == []
    assert "_source" not in result["clean_records"][0]
    assert result["evidence"][0]["raw"] == 100


def test_known_entity_types_is_non_empty():
    assert len(known_entity_types()) >= 14


def test_inventory_accepts_stock_class_four_states():
    """库存四态（DEV-06，F-008/D5 依赖）：stock_class 放行且取值受枚举约束。

    该字段为**可选**——既有记录不带它仍合法（向后兼容）；带非法值必须拒（不静默通过）。
    """
    from yunpai_orchestrator.canonical_schema import STOCK_CLASSES

    assert STOCK_CLASSES == frozenset({"raw", "finished", "semi", "wip"})

    for stock_class in sorted(STOCK_CLASSES):
        result = validate_canonical("inventory", [
            {"material_code": "M-1", "available_qty": 10, "stock_class": stock_class},
        ])
        assert result["errors"] == [], stock_class
        assert result["clean_records"][0]["stock_class"] == stock_class

    # 不带 stock_class 仍合法（向后兼容：既有记录不需回填）。
    without = validate_canonical("inventory", [{"material_code": "M-1", "available_qty": 10}])
    assert without["errors"] == []

    # 非法态拒绝（不静默通过）。
    invalid = validate_canonical("inventory", [
        {"material_code": "M-1", "available_qty": 10, "stock_class": "unknown_state"},
    ])
    assert any("stock_class" in e["message"] and "枚举" in e["message"] for e in invalid["errors"])
