"""P-016 M6 fixture pack: fixture provenance, deterministic values and missing facts."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from yunpai_orchestrator.m6_cost import (
    compute_asset_benefit, compute_expense_allocation, compute_inventory_finance_view,
    compute_monthly_pay, compute_piece_pay, compute_statement,
)
from yunpai_orchestrator.m6_store import M6Store
from yunpai_orchestrator.m6_tools import m6_save_costing_snapshot


PACK = json.loads((Path(__file__).parent / "fixtures" / "m6" / "fixture_pack.json").read_text(encoding="utf-8"))


def _fixture(name: str) -> dict:
    return PACK["fixtures"][name]


def test_fixture_pack_has_provenance_on_every_record():
    for name, fixture in PACK["fixtures"].items():
        assert fixture["source_ref"] == f"fixture:{name}"
        assert fixture["evidence"]["fixture_id"] == name
        for key in ("bom_lines", "routing_steps", "inventory", "report_events", "piece_rates", "expenses", "basis_rows", "asset_usage", "transactions"):
            for row in fixture["input"].get(key, []):
                assert str(row.get("source_ref", "")).startswith(f"fixture:{name}:")


def test_fixture_cost_is_deterministic(tmp_path):
    fixture = _fixture("FX-COST-001")
    payload = {**fixture["input"], "evidence": {**fixture["evidence"], "source_ref": fixture["source_ref"]}}
    result = asyncio.run(m6_save_costing_snapshot(payload, {"tenant_id": "fixture-tenant", "task_id": "fixture-cost", "m6_db_path": str(tmp_path / "m6.sqlite")}))
    data = result["data"]
    assert result["success"] is True
    assert data["unit_cost"] == fixture["expected"]["unit_cost"]
    assert data["total_cost"] == fixture["expected"]["total_cost"]
    assert data["cost_incomplete"] is False
    snapshot = M6Store(str(tmp_path / "m6.sqlite")).get_snapshot(
        data["snapshot_id"], tenant_id="fixture-tenant")
    assert snapshot["evidence"]["caller_evidence"]["fixture_id"] == "FX-COST-001"


def test_fixture_missing_quantity_keeps_unit_cost_and_null_total(tmp_path):
    fixture = _fixture("FX-COST-002")
    result = asyncio.run(m6_save_costing_snapshot({**fixture["input"], "evidence": fixture["evidence"]}, {"tenant_id": "fixture-tenant", "task_id": "fixture-cost-missing", "m6_db_path": str(tmp_path / "m6.sqlite")}))
    data = result["data"]
    assert data["unit_cost"] == 81.0
    assert data["total_cost"] is None
    assert data["cost_incomplete"] is True
    assert {item["reason"] for item in data["missing_inputs"]} >= {"missing_quantity"}


def test_fixture_piece_pay_distinguishes_partial_missing_rate():
    fixture = _fixture("FX-PAY-001")
    result = compute_piece_pay(**fixture["input"])
    assert {row["worker_id"]: row["piece_pay"] for row in result["totals"]} == {"W001": 237.5, "W002": 200.0}
    assert result["facts_present"] is True
    assert result["cost_incomplete"] is True
    assert result["missing"][-1]["reason"] == "missing_piece_rate"


def test_fixture_empty_payroll_is_explicitly_absent():
    piece = compute_piece_pay([], [])
    monthly = compute_monthly_pay({}, {}, {})
    for result in (piece, monthly):
        assert result["facts_present"] is False
        assert result["cost_incomplete"] is False
        assert result["missing"] == [{"reason": "missing_salary_facts"}]


def test_fixture_monthly_pay_values():
    result = compute_monthly_pay(**_fixture("FX-PAY-002")["input"])
    rows = {row["worker_id"]: row for row in result["rows"]}
    assert round(rows["W001"]["gross_pay"], 4) == 6754.7414
    assert round(rows["W002"]["gross_pay"], 4) == 4770.1149
    assert result["facts_present"] is True and result["cost_incomplete"] is False


def test_fixture_expense_asset_statement_and_inventory_values():
    expense = compute_expense_allocation(**_fixture("FX-EXP-001")["input"])
    assert {r["product_code"]: r["allocated_total"] for r in expense["by_product"]} == {"P1": 1800.0, "P2": 1200.0}
    asset = compute_asset_benefit(**_fixture("FX-ASSET-001")["input"])
    assert {r["product_code"]: r["allocated_cost"] for r in asset["allocations"]} == {"P1": 600.0, "P2": 400.0}
    statement = compute_statement(**_fixture("FX-STMT-001")["input"])
    assert statement["closing_balance"] == 1300.0
    inventory = compute_inventory_finance_view(**_fixture("FX-INV-001")["input"])
    assert inventory["by_class"]["raw"]["total_amount"] == 500.0
    assert inventory["by_class"]["wip"]["total_amount"] == 400.0
    assert inventory["by_class"]["unknown"]["total_qty"] == 10.0
    assert inventory["cost_incomplete"] is True
