"""M6 成本核算内核（``m6_cost``）——纯函数层。

来源：老仓 ``yunpai-39092`` ``tests/test_m6_cost.py``（340 行）中**纯函数**部分逐条移植，
断言体不改（仅 import 由 ``yunpai_langgraph.m6_cost`` 改为 ``yunpai_orchestrator.m6_cost``）。
未移植的是该文件的 7 个 registry 端到端用例（``audit_order_cost`` / ``generate_quotation`` /
``generate_statement`` / ``calculate_piece_pay`` / ``calculate_monthly_pay`` /
``get_product_cost`` 等**工具**级用例）——它们随 M6 工具注册（批次 B1/B6）一并移植，
届时断言体同样不改。

覆盖：材料（含损耗）/ 人工制费（分钟→小时）/ 报价加价 / 单台成本合并 / 订单审计四态
（passed / low_profit / loss / cost_incomplete）/ 计件（生效期+报废扣减）/ 月薪（加班-缺勤+计件）/
对账（期初+入-出）/ 效益分摊。

红线：缺事实一律标 ``cost_incomplete``，不用 0 或默认值冒充（不编造）。
"""

from __future__ import annotations

from yunpai_orchestrator.m6_cost import (
    audit_order_cost,
    compute_asset_benefit,
    compute_material_cost,
    compute_monthly_pay,
    compute_piece_pay,
    compute_process_cost,
    compute_quotation_price,
    compute_statement,
    compute_unit_cost,
)


def test_material_cost_rolls_price_qty_and_loss():
    result = compute_material_cost(
        [
            {"material_code": "M1", "unit_price": 10.0, "qty_per": 2, "loss_rate": 0.05},
            {"material_code": "M2", "unit_price": 5.0, "qty_per": 1, "loss_rate": 0.0},
        ]
    )
    assert result["unit_material_cost"] == 26.0  # 10*2*1.05 + 5*1*1.0
    assert result["cost_incomplete"] is False
    assert result["priced_lines"] == 2


def test_material_cost_marks_incomplete_on_missing_price():
    result = compute_material_cost(
        [
            {"material_code": "M1", "unit_price": 10.0, "qty_per": 1},
            {"material_code": "M2", "qty_per": 3},
        ]
    )
    assert result["cost_incomplete"] is True
    assert result["priced_lines"] == 1
    assert result["unit_material_cost"] == 10.0
    assert result["incomplete_lines"][0]["material_code"] == "M2"


def test_process_cost_converts_minutes_to_hours():
    result = compute_process_cost(
        [{"standard_minutes": 30}, {"standard_minutes": 30}],
        hour_rate=20.0,
        overhead_rate=10.0,
    )
    assert result["total_standard_hours"] == 1.0
    assert result["unit_labor_cost"] == 20.0
    assert result["unit_overhead_cost"] == 10.0
    assert result["cost_incomplete"] is False


def test_process_cost_marks_incomplete_on_missing_minutes():
    result = compute_process_cost([{"standard_minutes": 60}, {"operation": "no-time"}])
    assert result["cost_incomplete"] is True
    assert result["total_standard_hours"] == 1.0


def test_quotation_price_applies_markup():
    result = compute_quotation_price(20.0, 10.0, 5.0, markup_rate=0.2)
    assert result["base_cost"] == 35.0
    assert result["quote_price"] == 42.0


def test_order_audit_passed_when_margin_above_threshold():
    unit_costs = {"P1": {"material": 5.0, "labor": 2.0, "overhead": 1.0}}
    result = audit_order_cost(
        [{"product_code": "P1", "qty": 100, "unit_price": 10.0}],
        unit_costs,
        min_margin_rate=0.15,
    )
    assert result["revenue"] == 1000.0
    assert result["total_cost"] == 800.0
    assert result["gross_profit"] == 200.0
    assert result["gross_margin"] == 0.2
    assert result["status"] == "passed"
    assert len(result["lines"]) == 1


def test_order_audit_low_profit_below_threshold():
    unit_costs = {"P1": {"material": 8.0, "labor": 1.0, "overhead": 0.5}}
    result = audit_order_cost(
        [{"product_code": "P1", "qty": 10, "unit_price": 10.0}],
        unit_costs,
        min_margin_rate=0.15,
    )
    assert result["gross_margin"] == 0.05
    assert result["status"] == "low_profit"


def test_order_audit_loss_on_negative_profit():
    unit_costs = {"P1": {"material": 12.0, "labor": 0.0, "overhead": 0.0}}
    result = audit_order_cost(
        [{"product_code": "P1", "qty": 10, "unit_price": 10.0}],
        unit_costs,
        min_margin_rate=0.15,
    )
    assert result["gross_profit"] < 0
    assert result["status"] == "loss"


def test_order_audit_cost_incomplete_on_missing_unit_cost():
    result = audit_order_cost(
        [{"product_code": "P1", "qty": 10, "unit_price": 10.0}],
        {},
        min_margin_rate=0.15,
    )
    assert result["status"] == "cost_incomplete"
    assert result["incomplete"][0]["product_code"] == "P1"


def test_asset_benefit_allocates_by_quantity():
    result = compute_asset_benefit(
        [{"asset_code": "TL-1", "product_code": "P1", "quantity": 700},
         {"asset_code": "TL-1", "product_code": "P2", "quantity": 300}],
        {"TL-1": 1000.0},
        allocation_basis="quantity",
    )
    assert result["cost_incomplete"] is False
    assert len(result["allocations"]) == 2
    by_product = {a["product_code"]: a["allocated_cost"] for a in result["allocations"]}
    assert by_product["P1"] == 700.0
    assert by_product["P2"] == 300.0


def test_compute_unit_cost_rolls_material_and_process():
    result = compute_unit_cost(
        [{"material_code": "M1", "unit_price": 5.0, "qty_per": 2, "loss_rate": 0.05}],
        [{"standard_minutes": 30}],
        hour_rate=20.0, overhead_rate=10.0,
    )
    assert result["material"] == 10.5
    assert result["labor"] == 10.0
    assert result["overhead"] == 5.0
    assert result["unit_cost"] == 25.5
    assert result["cost_incomplete"] is False


def test_piece_pay_applies_rate_by_report_date_and_deducts_scrap():
    result = compute_piece_pay(
        [
            {"worker_id": "W1", "station_code": "S1", "product_code": "P1",
             "quantity_report": 100, "scrap": 10, "report_date": "2026-09-07"},
        ],
        [{"station_code": "S1", "product_code": "P1", "unit_rate": 2.0,
          "effective_from": "2026-09-01", "effective_to": None}],
    )
    assert result["cost_incomplete"] is False
    assert result["totals"] == [{"worker_id": "W1", "piece_pay": 180.0}]


def test_piece_pay_marks_missing_fact_set():
    result = compute_piece_pay(
        [{"worker_id": "W1", "station_code": "S1", "product_code": "P1",
          "quantity_report": 10, "report_date": "2026-09-07"}],
        [],
    )
    assert result["cost_incomplete"] is False
    assert result["facts_present"] is False
    assert result["missing"][0]["reason"] == "missing_salary_facts"


def test_monthly_pay_composes_salary_overtime_absence_piece():
    result = compute_monthly_pay(
        {"W1": 8700.0},
        {"W1": {"overtime_hours": 20, "absence_hours": 8}},
        {"W1": 180.0},
    )
    row = result["rows"][0]
    hourly = 8700.0 / 21.75 / 8.0
    assert row["monthly_salary"] == 8700.0
    assert row["overtime_pay"] == round(hourly * 1.5 * 20, 4)
    assert row["absence_deduct"] == round(hourly * 8, 4)
    assert row["piece_pay"] == 180.0
    assert row["gross_pay"] == round(8700.0 + hourly * 1.5 * 20 - hourly * 8 + 180.0, 4)


def test_statement_balances_opening_plus_inflow_minus_outflow():
    result = compute_statement(
        1000.0,
        [
            {"direction": "in", "amount": 500.0, "ref": "PO-1"},
            {"direction": "out", "amount": 200.0, "ref": "PAY-1"},
        ],
    )
    assert result["closing_balance"] == 1300.0
    assert result["inflow"] == 500.0
    assert result["outflow"] == 200.0


def test_asset_benefit_marks_zero_basis_as_incomplete():
    """资产分摊基准缺失时不得静默跳过。"""
    result = compute_asset_benefit(
        [{"asset_code": "MOLD-1", "product_code": "P1"}],
        {"MOLD-1": 1000.0},
    )
    assert result["allocations"] == []
    assert result["cost_incomplete"] is True
    assert result["missing"] == [{
        "asset_code": "MOLD-1", "reason": "missing_basis_value", "basis": "quantity",
    }]
