"""M6 费用归集分摊（``m6_cost.compute_expense_allocation``）+ 口径层（``m6_defaults``）
+ canonical 实体收口（``expense`` / ``delivery_note`` / ``workshop``）。

来源：老仓 ``tests/test_m6_expense.py``（214 行）的**纯函数与 schema**部分逐条移植，
断言体不改（仅 import 改名）。未移植的是该文件的 registry/M0Store 端到端用例
（``allocate_expenses`` 工具、``m0_expenses_import`` / ``m0_delivery_notes_import``）——
它们随 B0b（建 expense/delivery_note 实体与写入面）与 B1（注册 M6 工具）移植。

覆盖财务.xmind 缺口 1（社保/税费/电费/运费/伙食费/杂项 → 成本支出）。
核心红线：缺事实标 ``cost_incomplete``，绝不把默认值或空集合编造成已确认结果。
"""

from __future__ import annotations

import pytest

from yunpai_orchestrator.canonical_schema import validate_canonical
from yunpai_orchestrator.m6_cost import compute_expense_allocation
from yunpai_orchestrator.m6_defaults import (
    DEFAULT_ALLOCATION_BASIS,
    PENDING_FINANCE_CONFIRMATION,
    expense_assumptions,
)


@pytest.fixture
def expenses() -> list[dict]:
    return [
        {"category": "electricity", "amount": 1000.0, "period": "2026-09"},
        {"category": "social_insurance", "amount": 500.0, "period": "2026-09"},
    ]


@pytest.fixture
def basis_rows() -> list[dict]:
    return [
        {"product_code": "P1", "quantity": 300, "headcount": 10},
        {"product_code": "P2", "quantity": 100, "headcount": 10},
    ]


def test_allocation_proportional_to_default_quantity_basis(expenses, basis_rows):
    result = compute_expense_allocation(expenses, basis_rows)
    # 默认口径 quantity：P1:P2 = 300:100 = 3:1，两笔费用均按此分摊。
    assert result["cost_incomplete"] is False
    assert result["total_expense"] == 1500.0
    assert result["total_allocated"] == 1500.0
    by = {row["product_code"]: row for row in result["by_product"]}
    assert by["P1"]["allocated_total"] == 1125.0
    assert by["P2"]["allocated_total"] == 375.0
    assert by["P1"]["by_category"]["electricity"] == 750.0
    assert by["P1"]["allocated_per_unit"] == round(1125.0 / 300, 4)
    assert result["assumptions"]["allocation_basis"] == DEFAULT_ALLOCATION_BASIS
    assert result["assumptions"]["allocation_basis_assumed"] is True


def test_per_expense_basis_override_uses_headcount(expenses, basis_rows):
    # 导图口径：社保按人数固定 → 单条费用自带 allocation_basis=headcount。
    expenses[1]["allocation_basis"] = "headcount"
    result = compute_expense_allocation(expenses, basis_rows)
    assert result["cost_incomplete"] is False
    by = {row["product_code"]: row for row in result["by_product"]}
    # 电费按产量 3:1；社保按人数 10:10 → 各 250。
    assert by["P1"]["by_category"]["electricity"] == 750.0
    assert by["P1"]["by_category"]["social_insurance"] == 250.0
    assert by["P2"]["by_category"]["social_insurance"] == 250.0


def test_missing_amount_is_flagged_not_fabricated(basis_rows):
    result = compute_expense_allocation(
        [
            {"category": "electricity", "amount": 100.0, "period": "2026-09"},
            {"category": "tax", "period": "2026-09"},  # 缺 amount
        ],
        basis_rows,
    )
    assert result["cost_incomplete"] is True
    assert any(item["reason"] == "missing_amount" for item in result["missing"])
    assert result["total_expense"] == 100.0  # 只按可算行求和，不用 0 冒充


def test_unknown_basis_and_missing_basis_value_are_flagged(basis_rows):
    result = compute_expense_allocation(
        [
            {"category": "tax", "amount": 100.0, "period": "2026-09",
             "allocation_basis": "not_a_basis"},
            {"category": "electricity", "amount": 80.0, "period": "2026-09"},
        ],
        basis_rows + [{"product_code": "P3"}],  # P3 缺 quantity 基准值
    )
    reasons = {item["reason"] for item in result["missing"]}
    assert "unknown_allocation_basis" in reasons
    assert "missing_basis_value" in reasons
    assert result["cost_incomplete"] is True
    by = {row["product_code"]: row for row in result["by_product"]}
    assert "P3" in by and by["P3"]["allocated_total"] == 0.0


def test_empty_inputs_are_missing_not_zero():
    result = compute_expense_allocation([], [])
    assert result["cost_incomplete"] is True
    reasons = {item["reason"] for item in result["missing"]}
    assert reasons == {"missing_expenses", "missing_basis_rows"}
    assert result["total_allocated"] == 0.0
    assert PENDING_FINANCE_CONFIRMATION  # 待确认口径清单非空且随输出可回读


# ── 口径层（m6_defaults，决策 D-006）─────────────────────────────────────
def test_assumptions_marks_default_as_assumed_and_explicit_as_given():
    """默认口径必须带 assumed=True 审计痕；显式传入则 assumed=False（不得混同）。"""
    defaulted = expense_assumptions()
    assert defaulted["allocation_basis_assumed"] is True
    assert defaulted["valuation_price_source_assumed"] is True
    assert defaulted["basis_source"] == "explicit"
    assert defaulted["pending_finance_confirmation"] == list(PENDING_FINANCE_CONFIRMATION)

    explicit = expense_assumptions(
        allocation_basis="headcount", valuation_price_source="latest_purchase_price",
    )
    assert explicit["allocation_basis"] == "headcount"
    assert explicit["allocation_basis_assumed"] is False
    assert explicit["valuation_price_source_assumed"] is False


# ── canonical 实体收口（B0a：单一事实源分裂修复）──────────────────────────
def test_expense_is_canonical_with_facade_required_fields():
    """expense 收口进 CANONICAL_SCHEMA（老仓只在 facade 层，属源分裂）。

    必填字段取老仓 facade 口径（category/amount/period），识别链才不会拒收。
    """
    ok = validate_canonical("expense", [
        {"category": "electricity", "amount": 100.0, "period": "2026-09"},
    ])
    assert ok["errors"] == []
    assert ok["clean_records"][0]["amount"] == 100.0

    missing_amount = validate_canonical("expense", [{"category": "tax", "period": "2026-09"}])
    assert any("amount" in e["message"] for e in missing_amount["errors"])

    bad_amount = validate_canonical("expense", [
        {"category": "tax", "amount": "一百", "period": "2026-09"},
    ])
    assert any("amount" in e["message"] and "数字" in e["message"] for e in bad_amount["errors"])


def test_delivery_note_is_canonical_and_accepts_signoff_fields():
    """delivery_note 收口进 CANONICAL_SCHEMA；身份键 note_no；签收面字段放行。"""
    result = validate_canonical("delivery_note", [{
        "note_no": "DN-2026-09-001", "counterparty_code": "CUST-01",
        "note_date": "2026-09-07", "direction": "out",
        "signed_by": "张三", "signed_at": "2026-09-07 15:00",
        "warehouse_confirmed_by": "李四", "qc_status": "passed",
        "ref_order_id": "SO-001",
    }])
    assert result["errors"] == []
    assert result["clean_records"][0]["note_no"] == "DN-2026-09-001"

    missing_key = validate_canonical("delivery_note", [
        {"counterparty_code": "CUST-01", "note_date": "2026-09-07"},
    ])
    assert any("note_no" in e["message"] for e in missing_key["errors"])


def test_tooling_and_equipment_accept_workshop_field():
    tooling = validate_canonical("tooling", [{
        "tooling_code": "TL-1", "tooling_name": "注塑模", "workshop": "一车间",
    }])
    equipment = validate_canonical("equipment", [{
        "equipment_code": "EQ-1", "equipment_name": "注塑机", "workshop": "二车间",
    }])
    assert tooling["clean_records"] and not tooling["errors"]
    assert equipment["clean_records"] and not equipment["errors"]
    # 越界字段仍须拒绝——workshop 放行不破坏字段白名单机制。
    rejected = validate_canonical("tooling", [{
        "tooling_code": "TL-2", "tooling_name": "x", "not_a_field": 1,
    }])
    assert rejected["errors"]
