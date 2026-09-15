"""M6 成本与财务：确定性核算引擎。

实现 11-白板业务蓝图拆解与开发需求 §4A 中 M6 的计算内核，全部为纯函数、确定性、
不 agent 化（遵循 02-架构设计文档 §6 Agent 自由化原则）：事实值照抄、公式固定、
缺数据显式标 ``cost_incomplete`` 而非编造。输入为从 M0/M5 读回的字典事实，输出为
可回读的成本/审计结构。金额单位与输入一致（默认元），损耗率为小数（0.05=5%）。

规则溯源：
- ``compute_material_cost``      → R-COST-1（DEV-03）
- ``compute_process_cost``       → R-AUDIT-0 人工/制费（DEV-16）
- ``audit_order_cost``           → R-AUDIT-1/2（DEV-16）
- ``compute_quotation_price``    → R-QT-1（DEV-13）

来源：老仓 ``yunpai-39092`` ``src/yunpai_langgraph/m6_cost.py``（601 行，原样搬运，
代码逻辑逐行不变）。本模块为纯函数、不碰库、**不 import 任何 v2 模块**（仅依赖
``.m6_defaults`` 与 ``typing``），以保持"跟基线无关"（计划 §13）。
"""

from __future__ import annotations

from typing import Any

from .m6_defaults import (  # 口径单一事实源：本文件不再重复默认值字面量
    DEFAULT_ALLOCATION_BASIS,
    DEFAULT_HOURS_PER_DAY,
    DEFAULT_MIN_MARGIN_RATE,
    DEFAULT_OVERTIME_MULTIPLIER,
    DEFAULT_VALUATION_PRICE_SOURCE,
    DEFAULT_WORK_DAYS,
    PENDING_FINANCE_CONFIRMATION,
)


def _num_optional(value: Any) -> float | None:
    """解析数值；缺失或不可解析返回 None（调用方据此判定成本不完整）。"""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> float:
    """解析数值，缺省为 0.0（用于费率类参数）。"""
    got = _num_optional(value)
    return 0.0 if got is None else got


def compute_material_cost(bom_lines: Any) -> dict[str, Any]:
    """R-COST-1：单台直接材料成本 = Σ(单价 × 单台用量 × (1 + 损耗率))。

    任一行缺单价/用量 → 计入 incomplete_lines 并置 cost_incomplete=True，
    不阻塞其余行求和（不编造数值）。
    """
    rows = bom_lines if isinstance(bom_lines, list) else []
    total = 0.0
    priced = 0
    incomplete: list[dict[str, Any]] = []
    for index, line in enumerate(rows, start=1):
        if not isinstance(line, dict):
            incomplete.append({"line": index, "reason": "invalid_row"})
            continue
        code = str(line.get("material_code") or line.get("item_code") or "").strip() or f"line{index}"
        price = _num_optional(line.get("unit_price"))
        qty = _num_optional(line.get("qty_per") or line.get("quantity"))
        loss = _num(line.get("loss_rate"))
        if price is None or qty is None:
            incomplete.append({"line": index, "material_code": code, "reason": "missing_price_or_qty"})
            continue
        total += price * qty * (1.0 + loss)
        priced += 1
    return {
        "unit_material_cost": round(total, 4),
        "priced_lines": priced,
        "total_lines": len(rows),
        "cost_incomplete": bool(incomplete),
        "incomplete_lines": incomplete,
    }


def compute_process_cost(
    routing_steps: Any,
    hour_rate: Any = None,
    overhead_rate: Any = None,
) -> dict[str, Any]:
    """R-AUDIT-0：单台人工/制费 = Σ(工序标准工时) × 费率。

    standard_minutes 以分钟计；缺标准工时的工序计入 incomplete_steps。
    """
    rows = routing_steps if isinstance(routing_steps, list) else []
    hours = 0.0
    incomplete: list[dict[str, Any]] = []
    for index, step in enumerate(rows, start=1):
        if not isinstance(step, dict):
            incomplete.append({"step": index, "reason": "invalid_row"})
            continue
        minutes = _num_optional(step.get("standard_minutes") or step.get("standard_time_minutes"))
        if minutes is None:
            incomplete.append({"step": index, "reason": "missing_standard_minutes"})
            continue
        hours += minutes / 60.0
    hour_value = _num_optional(hour_rate)
    overhead_value = _num_optional(overhead_rate)
    if rows and hour_value is None:
        incomplete.append({"reason": "missing_hour_rate"})
    if rows and overhead_value is None:
        incomplete.append({"reason": "missing_overhead_rate"})
    labor = hours * (hour_value if hour_value is not None else 0.0)
    overhead = hours * (overhead_value if overhead_value is not None else 0.0)
    return {
        "total_standard_hours": round(hours, 4),
        "unit_labor_cost": round(labor, 4),
        "unit_overhead_cost": round(overhead, 4),
        "cost_incomplete": bool(incomplete),
        "incomplete_steps": incomplete,
    }


def compute_quotation_price(
    unit_material: Any,
    unit_labor: Any,
    unit_overhead: Any,
    *,
    markup_rate: Any = 0.0,
) -> dict[str, Any]:
    """R-QT-1：报价单价 = (材料 + 人工 + 制费) × (1 + 加价率)。"""
    base = _num(unit_material) + _num(unit_labor) + _num(unit_overhead)
    return {
        "base_cost": round(base, 4),
        "markup_rate": _num(markup_rate),
        "quote_price": round(base * (1.0 + _num(markup_rate)), 4),
    }


def compute_unit_cost(
    bom_lines: Any,
    routing_steps: Any,
    hour_rate: Any = None,
    overhead_rate: Any = None,
) -> dict[str, Any]:
    """单产品单台成本：材料（BOM 行）+ 人工/制费（工艺×费率）。

    供订单成本审计从 BOM/route 自动滚动单位成本（DEV-03），缺价/缺工时标 cost_incomplete。
    """
    material = compute_material_cost(bom_lines)
    process = compute_process_cost(routing_steps, hour_rate, overhead_rate)
    missing_inputs: list[dict[str, Any]] = []
    if not isinstance(bom_lines, list) or not bom_lines:
        missing_inputs.append({"reason": "missing_bom_lines"})
    if not isinstance(routing_steps, list) or not routing_steps:
        missing_inputs.append({"reason": "missing_routing_steps"})
    return {
        "material": material["unit_material_cost"],
        "labor": process["unit_labor_cost"],
        "overhead": process["unit_overhead_cost"],
        "unit_cost": round(
            material["unit_material_cost"] + process["unit_labor_cost"] + process["unit_overhead_cost"], 4,
        ),
        "cost_incomplete": bool(missing_inputs) or material["cost_incomplete"] or process["cost_incomplete"],
        "incomplete": missing_inputs + material["incomplete_lines"] + process["incomplete_steps"],
    }


def _audit_status(
    gross_profit: float,
    gross_margin: float | None,
    min_margin_rate: float,
    has_missing: bool,
) -> str:
    if has_missing or gross_margin is None:
        return "cost_incomplete"
    if gross_profit < 0:
        return "loss"
    if gross_margin < min_margin_rate:
        return "low_profit"
    return "passed"


def audit_order_cost(
    order_lines: Any,
    unit_costs: Any,
    *,
    min_margin_rate: Any = DEFAULT_MIN_MARGIN_RATE,
) -> dict[str, Any]:
    """R-AUDIT-1/2：订单成本审计与利润核算。

    order_lines: [{product_code, qty, unit_price}]
    unit_costs:   {product_code: {"material": 单台材料, "labor": 单台人工, "overhead": 单台制费}}
    返回订单级 收入/材料/人工/制费/毛利/毛利率/status + 逐行明细 + 缺口清单。
    status ∈ {passed, low_profit, loss, cost_incomplete}。
    """
    rows = order_lines if isinstance(order_lines, list) else []
    costs = unit_costs if isinstance(unit_costs, dict) else {}
    min_margin = _num(min_margin_rate)

    revenue = material = labor = overhead = 0.0
    lines: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []

    for index, line in enumerate(rows, start=1):
        if not isinstance(line, dict):
            incomplete.append({"line": index, "reason": "invalid_row"})
            continue
        code = str(line.get("product_code") or "").strip()
        qty = _num_optional(line.get("qty") or line.get("quantity"))
        price = _num_optional(line.get("unit_price"))
        if not code or qty is None or price is None:
            incomplete.append({"line": index, "reason": "missing_product_code_qty_price"})
            continue
        uc = costs.get(code)
        if not isinstance(uc, dict):
            incomplete.append({"line": index, "product_code": code, "reason": "missing_unit_cost"})
            continue

        line_rev = price * qty
        line_mat = _num(uc.get("material")) * qty
        line_lab = _num(uc.get("labor")) * qty
        line_oh = _num(uc.get("overhead")) * qty
        line_gp = line_rev - (line_mat + line_lab + line_oh)
        line_margin = (line_gp / line_rev) if line_rev > 0 else None

        revenue += line_rev
        material += line_mat
        labor += line_lab
        overhead += line_oh

        lines.append({
            "product_code": code,
            "qty": qty,
            "unit_price": round(price, 4),
            "revenue": round(line_rev, 4),
            "material": round(line_mat, 4),
            "labor": round(line_lab, 4),
            "overhead": round(line_oh, 4),
            "gross_profit": round(line_gp, 4),
            "gross_margin": round(line_margin, 4) if line_margin is not None else None,
        })

    total_cost = material + labor + overhead
    gross_profit = revenue - total_cost
    gross_margin = (gross_profit / revenue) if revenue > 0 else None

    return {
        "revenue": round(revenue, 4),
        "material": round(material, 4),
        "labor": round(labor, 4),
        "overhead": round(overhead, 4),
        "total_cost": round(total_cost, 4),
        "gross_profit": round(gross_profit, 4),
        "gross_margin": round(gross_margin, 4) if gross_margin is not None else None,
        "min_margin_rate": min_margin,
        "status": _audit_status(gross_profit, gross_margin, min_margin, bool(incomplete)),
        "lines": lines,
        "incomplete": incomplete,
    }


def _pick_rate(candidates: list[dict[str, Any]], report_date: str) -> dict[str, Any] | None:
    """按报工日挑选生效中的计件单价（effective_from <= 报工日 <= effective_to），取最新生效版本。"""
    active = [
        r for r in candidates
        if str(r.get("effective_from") or "") <= report_date
        and (not r.get("effective_to") or str(r.get("effective_to")) >= report_date)
    ]
    if not active:
        return None
    return max(active, key=lambda r: str(r.get("effective_from") or ""))


def compute_piece_pay(report_events: Any, piece_rates: Any) -> dict[str, Any]:
    """R-PAY-1：计件工资 = Σ(合格数量 × 单价)；合格数量 = quantity_report − scrap。

    单价按 (station_code, product_code) 匹配，取报工日生效版本；无单价报工计入 missing。
    """
    events = report_events if isinstance(report_events, list) else []
    rates = piece_rates if isinstance(piece_rates, list) else []

    # An empty fact set means that this period has no available payroll
    # source.  It is not the same as an in-place set with an unmatched row:
    # the former is explicitly absent, while the latter is incomplete.
    if not events or not rates:
        return {
            "totals": [], "details": [], "cost_incomplete": False,
            "facts_present": False,
            "missing": [{"reason": "missing_salary_facts"}],
        }

    rate_map: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rate in rates:
        if not isinstance(rate, dict):
            continue
        station = str(rate.get("station_code") or "").strip()
        product = str(rate.get("product_code") or "").strip()
        unit = _num_optional(rate.get("unit_rate"))
        if not station or not product or unit is None:
            continue
        rate_map.setdefault((station, product), []).append({
            "unit_rate": unit,
            "effective_from": str(rate.get("effective_from") or ""),
            "effective_to": str(rate.get("effective_to") or "") or None,
        })

    by_worker: dict[str, float] = {}
    details: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            missing.append({"line": index, "reason": "invalid_row"})
            continue
        worker = str(event.get("worker_id") or "").strip()
        station = str(event.get("station_code") or "").strip()
        product = str(event.get("product_code") or "").strip()
        qty = _num_optional(event.get("quantity_report"))
        scrap = _num(event.get("scrap"))
        report_date = str(event.get("report_date") or event.get("observed_at") or "")
        if not worker or qty is None:
            missing.append({"line": index, "reason": "missing_worker_or_quantity"})
            continue
        rate = _pick_rate(rate_map.get((station, product), []), report_date)
        if rate is None:
            missing.append({
                "line": index, "worker_id": worker,
                "station_code": station, "product_code": product, "reason": "missing_piece_rate",
            })
            continue
        good_qty = max(0.0, qty - scrap)
        amount = good_qty * rate["unit_rate"]
        by_worker[worker] = by_worker.get(worker, 0.0) + amount
        details.append({
            "worker_id": worker, "station_code": station, "product_code": product,
            "good_qty": round(good_qty, 4), "unit_rate": rate["unit_rate"], "amount": round(amount, 4),
        })
    totals = [{"worker_id": worker, "piece_pay": round(total, 4)} for worker, total in sorted(by_worker.items())]
    return {"totals": totals, "details": details, "cost_incomplete": bool(missing),
            "facts_present": True, "missing": missing}


def compute_monthly_pay(
    salary_standards: Any,
    attendance: Any,
    piece_pay: Any,
    *,
    overtime_multiplier: Any = DEFAULT_OVERTIME_MULTIPLIER,
    work_days: Any = DEFAULT_WORK_DAYS,
    hours_per_day: Any = DEFAULT_HOURS_PER_DAY,
) -> dict[str, Any]:
    """R-PAY-2：月薪工资 = 月薪标准 + 加班费 − 缺勤扣款 + 计件工资（混合制）。

    时薪 = 月薪标准 ÷ work_days ÷ hours_per_day；加班费 = 时薪 × overtime_multiplier × 加班工时；
    缺勤扣款 = 时薪 × 缺勤工时。默认口径（1.5 倍 / 21.75 天 / 8 小时）需工厂财务最终确认。
    """
    salaries = salary_standards if isinstance(salary_standards, dict) else {}
    att = attendance if isinstance(attendance, dict) else {}
    piece = piece_pay if isinstance(piece_pay, dict) else {}

    if not salaries:
        return {
            "rows": [], "cost_incomplete": False, "facts_present": False,
            "incomplete": [], "missing": [{"reason": "missing_salary_facts"}],
        }
    mult = _num(overtime_multiplier)
    days = _num(work_days)
    hours = _num(hours_per_day)

    rows: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    for worker, salary_value in salaries.items():
        salary = _num_optional(salary_value)
        if salary is None:
            incomplete.append({"worker_id": worker, "reason": "missing_salary"})
            continue
        entry = att.get(worker) if isinstance(att.get(worker), dict) else {}
        ot = _num(entry.get("overtime_hours"))
        absence = _num(entry.get("absence_hours"))
        hourly = salary / days / hours if days > 0 and hours > 0 else 0.0
        overtime_pay = hourly * mult * ot
        absence_deduct = hourly * absence
        piece_amount = _num(piece.get(worker))
        gross = salary + overtime_pay - absence_deduct + piece_amount
        rows.append({
            "worker_id": worker,
            "monthly_salary": round(salary, 4),
            "overtime_pay": round(overtime_pay, 4),
            "absence_deduct": round(absence_deduct, 4),
            "piece_pay": round(piece_amount, 4),
            "gross_pay": round(gross, 4),
        })
    return {"rows": rows, "cost_incomplete": bool(incomplete), "facts_present": True,
            "incomplete": incomplete, "missing": incomplete}


def compute_statement(opening_balance: Any, transactions: Any) -> dict[str, Any]:
    """R-STMT-1/2：期末 = 期初 + Σinflow − Σoutflow。

    transaction.direction ∈ {"in", "out"}：in 增加余额（客户=发货确认/供应商=入库），
    out 减少余额（客户=回款/供应商=付款）。方向语义由上层领域映射，本函数只做确定性累加。
    """
    opening = _num(opening_balance)
    inflow = 0.0
    outflow = 0.0
    lines: list[dict[str, Any]] = []
    for index, tx in enumerate(transactions if isinstance(transactions, list) else [], start=1):
        if not isinstance(tx, dict):
            continue
        direction = str(tx.get("direction") or "").strip().lower()
        amount = _num(tx.get("amount"))
        if direction in {"in", "debit", "increase"}:
            inflow += amount
        elif direction in {"out", "credit", "decrease"}:
            outflow += amount
        else:
            continue
        lines.append({"line": index, "direction": direction, "amount": round(amount, 4), "ref": tx.get("ref")})
    closing = opening + inflow - outflow
    return {
        "opening_balance": round(opening, 4),
        "inflow": round(inflow, 4),
        "outflow": round(outflow, 4),
        "closing_balance": round(closing, 4),
        "lines": lines,
    }


_BASIS_KEYS = {"quantity": "quantity", "labor_hours": "labor_hours", "order_count": "order_count"}


def compute_asset_benefit(
    asset_usage: Any,
    asset_cost: Any,
    *,
    allocation_basis: str = DEFAULT_ALLOCATION_BASIS,
) -> dict[str, Any]:
    """DEV-15 效益分摊：按分配口径把模具/机器成本分摊到各产品。

    asset_usage: [{asset_code, product_code, quantity, labor_hours, order_id}]
    asset_cost:  {asset_code: 总成本}
    allocation_basis ∈ quantity / labor_hours / order_count；口径默认 quantity，
    最终口径待工厂财务确认（DQ-8）。确定性分摊，不编造。
    """
    basis_key = _BASIS_KEYS.get(allocation_basis, DEFAULT_ALLOCATION_BASIS)
    usages = asset_usage if isinstance(asset_usage, list) else []
    costs = asset_cost if isinstance(asset_cost, dict) else {}

    # 按资产分组
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for usage in usages:
        if not isinstance(usage, dict):
            continue
        asset = str(usage.get("asset_code") or "").strip()
        if asset:
            by_asset.setdefault(asset, []).append(usage)

    allocations: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for asset, items in by_asset.items():
        cost = _num_optional(costs.get(asset))
        if cost is None:
            missing.append({"asset_code": asset, "reason": "missing_asset_cost"})
            continue
        if basis_key == "order_count":
            total_basis = float(len(items))
        else:
            total_basis = sum(_num(item.get(basis_key)) for item in items)
        if total_basis <= 0:
            missing.append({"asset_code": asset, "reason": "missing_basis_value",
                            "basis": basis_key})
            continue
        for item in items:
            basis = 1.0 if basis_key == "order_count" else _num(item.get(basis_key))
            allocated = cost * basis / total_basis
            allocations.append({
                "asset_code": asset,
                "product_code": str(item.get("product_code") or ""),
                "order_id": str(item.get("order_id") or ""),
                "basis": basis,
                "allocated_cost": round(allocated, 4),
            })
    return {
        "allocation_basis": allocation_basis,
        "allocations": allocations,
        "cost_incomplete": bool(missing),
        "missing": missing,
    }


def compute_expense_allocation(
    expenses: Any,
    basis_rows: Any,
    *,
    allocation_basis: Any = None,
) -> dict[str, Any]:
    """F-029 费用归集分摊：Σ(费用 × 产品基准 ÷ 基准合计)，确定性计算。

    expenses:  [{category, amount, period, allocation_basis?}]——单条费用可自带
               口径覆盖全局默认；缺 amount/未知口径计入 missing，不用 0 冒充。
    basis_rows: [{product_code, quantity, labor_hours, headcount, order_count}]
    allocation_basis: 全局默认口径；None 时取 m6_defaults.DEFAULT_ALLOCATION_BASIS
               并在 assumptions 标 assumed=True（口径待财务确认，见 m6_defaults）。

    expenses 或 basis_rows 整体缺失/为空时标 missing_expenses/missing_basis_rows
    （空集合不当作零分摊）；某产品缺所用口径的基准值时该产品计入 missing，
    其余产品照常分摊（口径与 compute_material_cost 一致：不阻塞、不编造）。
    """
    from .m6_defaults import ALLOCATION_BASIS_KEYS, DEFAULT_ALLOCATION_BASIS, expense_assumptions

    rows = expenses if isinstance(expenses, list) else []
    basis = basis_rows if isinstance(basis_rows, list) else []
    global_basis = str(allocation_basis or DEFAULT_ALLOCATION_BASIS)
    assumptions = expense_assumptions(
        allocation_basis=None if allocation_basis in (None, "") else global_basis,
    )

    missing: list[dict[str, Any]] = []
    if not rows:
        missing.append({"reason": "missing_expenses"})
    if not basis:
        missing.append({"reason": "missing_basis_rows"})

    valid_expenses: list[dict[str, Any]] = []
    for index, expense in enumerate(rows, start=1):
        if not isinstance(expense, dict):
            missing.append({"line": index, "reason": "invalid_row"})
            continue
        amount = _num_optional(expense.get("amount"))
        if amount is None:
            missing.append({"line": index, "category": expense.get("category"), "reason": "missing_amount"})
            continue
        category = str(expense.get("category") or "misc").strip() or "misc"
        basis_name = str(expense.get("allocation_basis") or global_basis)
        if basis_name not in ALLOCATION_BASIS_KEYS:
            missing.append({
                "line": index, "category": category,
                "reason": "unknown_allocation_basis", "allocation_basis": basis_name,
            })
            continue
        valid_expenses.append({
            "line": index, "category": category, "amount": amount,
            "period": str(expense.get("period") or ""), "allocation_basis": basis_name,
        })

    # 分摊基准行：按产品去重（后行覆盖），记录全部口径字段供多口径费用共用。
    product_basis: dict[str, dict[str, Any]] = {}
    product_order: list[str] = []
    for row in basis:
        if not isinstance(row, dict):
            continue
        code = str(row.get("product_code") or "").strip()
        if not code:
            continue
        if code not in product_basis:
            product_order.append(code)
            product_basis[code] = {}
        for basis_name in ALLOCATION_BASIS_KEYS:
            value = row.get(basis_name)
            if value not in (None, ""):
                product_basis[code][basis_name] = _num_optional(value)

    # 每个被用到的口径先求基准合计；缺值产品计入 missing（不影响其余产品分摊）。
    used_bases = {expense["allocation_basis"] for expense in valid_expenses}
    basis_totals: dict[str, float] = {}
    for basis_name in used_bases:
        total = 0.0
        for code in product_order:
            value = product_basis[code].get(basis_name)
            if value is None:
                missing.append({
                    "product_code": code, "reason": "missing_basis_value",
                    "allocation_basis": basis_name,
                })
                continue
            total += value
        basis_totals[basis_name] = total

    allocated_by_product: dict[str, dict[str, Any]] = {
        code: {"product_code": code, "allocated_total": 0.0, "by_category": {}}
        for code in product_order
    }
    for expense in valid_expenses:
        basis_name = expense["allocation_basis"]
        total_basis = basis_totals.get(basis_name, 0.0)
        if total_basis <= 0:
            continue
        field = ALLOCATION_BASIS_KEYS[basis_name]
        for code in product_order:
            value = product_basis[code].get(basis_name)
            if value is None:
                continue
            allocated = expense["amount"] * value / total_basis
            entry = allocated_by_product[code]
            entry["allocated_total"] += allocated
            entry["by_category"][expense["category"]] = (
                entry["by_category"].get(expense["category"], 0.0) + allocated
            )

    by_product: list[dict[str, Any]] = []
    for code in product_order:
        entry = allocated_by_product[code]
        quantity = product_basis[code].get("quantity")
        by_product.append({
            "product_code": code,
            "allocated_total": round(entry["allocated_total"], 4),
            "by_category": {
                category: round(amount, 4)
                for category, amount in sorted(entry["by_category"].items())
            },
            "basis_values": product_basis[code],
            # 单台分摊仅在产量口径有值时给出（缺产量不推算）。
            "allocated_per_unit": round(entry["allocated_total"] / quantity, 4) if quantity else None,
        })

    return {
        "total_expense": round(sum(expense["amount"] for expense in valid_expenses), 4),
        "total_allocated": round(sum(item["allocated_total"] for item in by_product), 4),
        "expense_lines": [
            {
                "line": expense["line"], "category": expense["category"],
                "period": expense["period"], "amount": round(expense["amount"], 4),
                "allocation_basis": expense["allocation_basis"],
            }
            for expense in valid_expenses
        ],
        "by_product": by_product,
        "assumptions": assumptions,
        "cost_incomplete": bool(missing),
        "missing": missing,
    }


# ---------------------------------------------------------------------------
# R-INV-FIN：财务口径库存视图
# ---------------------------------------------------------------------------

#: 库存四态（口径名 → 中文标签）。**与 `canonical_schema.STOCK_CLASSES` 同源**，但本模块
#: 按红线不 import 任何 v2 模块，故此处保留副本；任何一侧增删态都必须手工同步。
STOCK_CLASS_LABELS: dict[str, str] = {
    "raw": "原料在库",
    "finished": "成品在库",
    "semi": "半成品在库",
    "wip": "在制",
}

#: 态缺失/未知的归集名（**单列一栏，不归到任何一态**）。
UNKNOWN_CLASS = "unknown"

#: M4 追踪里表示「已入库」的状态值（与 `orchestration_bridge.received_purchase_rows` 同口径）。
RECEIVED_ARRIVAL_STATUS = "received"


def _empty_bucket(name: str, label: str) -> dict[str, Any]:
    return {"stock_class": name, "label": label, "line_count": 0,
            "total_qty": 0.0, "total_amount": 0.0, "amount_incomplete": False}


def compute_inventory_finance_view(
    inventory: Any,
    *,
    purchase_tracking_rows: Any = None,
    unit_costs: Any = None,
    valuation_price_source: Any = None,
) -> dict[str, Any]:
    """R-INV-FIN：财务口径库存视图（四态分账 + 在途）。

    三条口径（与 D5 / D-006 同源，全部 fail-closed）：

    1. **态缺失/未知 → 单列 ``unknown``**，绝不归到任何一态（不猜"有库存"；与
       `resolve_material_prices` 的 D5 定价口径同源）——态明确但不在四态内的值另计
       ``unknown_stock_class``。
    2. **金额只在能取到单价时给**：单价来自 ``unit_costs``（材料编码 → 单价，**事实值**，
       由装配层从 BOM 价/资产台账取，取不到就别给）；任一行缺单价 → 该行进 ``missing``
       且整项 ``cost_incomplete``（**不编造成本**）。这是 v2 现状的必然结果：
       `inventory` 实体**没有价格字段**，所以金额永远来自外部带入的成本。
    3. **在途只认状态明确的行**：``arrival_status`` 非空且非 ``received`` → 在途；
       **状态为空 → 归 ``unknown_status``**（不猜它在路上）。

    ``by_class.finished`` 即计划所称**成品账**（不另设重复字段）。
    """
    rows = [row for row in (inventory if isinstance(inventory, list) else [])
            if isinstance(row, dict)]
    costs = unit_costs if isinstance(unit_costs, dict) else {}

    by_class: dict[str, dict[str, Any]] = {
        name: _empty_bucket(name, label) for name, label in STOCK_CLASS_LABELS.items()
    }
    by_class[UNKNOWN_CLASS] = _empty_bucket(UNKNOWN_CLASS, "态未标明（分不清态）")

    missing: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        code = str(row.get("material_code") or "").strip()
        raw_class = str(row.get("stock_class") or "").strip()
        if raw_class in STOCK_CLASS_LABELS:
            bucket = by_class[raw_class]
        else:
            bucket = by_class[UNKNOWN_CLASS]
            if raw_class:
                missing.append({"line": index, "material_code": code,
                                "reason": "unknown_stock_class", "stock_class": raw_class})
        qty = _num(row.get("available_qty"))
        bucket["line_count"] += 1
        bucket["total_qty"] = round(bucket["total_qty"] + qty, 4)
        unit_cost = _num_optional(costs.get(code)) if code else None
        if unit_cost is None:
            # 缺单价不是「0 元」，是「算不出金额」——标缺失，不把 0 混进合计。
            bucket["amount_incomplete"] = True
            missing.append({"line": index, "material_code": code,
                            "reason": "missing_unit_cost",
                            "stock_class": bucket["stock_class"]})
            continue
        bucket["total_amount"] = round(bucket["total_amount"] + qty * unit_cost, 4)

    tracking = [row for row in (purchase_tracking_rows
                                if isinstance(purchase_tracking_rows, list) else [])
                if isinstance(row, dict)]
    in_transit: list[dict[str, Any]] = []
    unknown_status: list[dict[str, Any]] = []
    for index, row in enumerate(tracking, start=1):
        status = str(row.get("arrival_status") or "").strip()
        entry = {
            "line": index,
            "material_code": str(row.get("material_code")
                                 or row.get("internal_material_no") or ""),
            "supplier": str(row.get("supplier_name") or row.get("supplier_code") or ""),
            "quantity": _num_optional(row.get("quantity") or row.get("arrival_qty")),
            "arrival_status": status,
        }
        if not status:
            unknown_status.append(entry)
        elif status != RECEIVED_ARRIVAL_STATUS:
            in_transit.append(entry)

    return {
        "by_class": {name: by_class[name]
                     for name in (*STOCK_CLASS_LABELS, UNKNOWN_CLASS)},
        "in_transit": {"line_count": len(in_transit), "lines": in_transit},
        "unknown_status": {"line_count": len(unknown_status), "lines": unknown_status},
        "valuation_price_source": str(valuation_price_source
                                      or DEFAULT_VALUATION_PRICE_SOURCE),
        "valuation_price_source_assumed": not bool(valuation_price_source),
        "assumptions": {
            "valuation_price_source_assumed": not bool(valuation_price_source),
            "pending_finance_confirmation": list(PENDING_FINANCE_CONFIRMATION),
        },
        "cost_incomplete": bool(missing),
        "missing": missing,
    }
