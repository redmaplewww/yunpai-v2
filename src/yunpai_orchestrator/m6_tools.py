"""M6 财务成本账本地 handler（F-008 / D-005 三段式 / D-006 口径层 / D-007 R1a）。

六个工具（契约见 ``registry-manifests/m6.json``）分两类：

- **写**：`save_costing_snapshot`（propose 段：只落 ``status=trial`` 草稿）、
  `confirm_costing_snapshot` / `close_month_costing`（commit 段的**发起**：只做前置
  校验并请求人工门，**翻正/冻结由 `graph.py` 的 `_apply_m6_*` 在 approve 后执行**）。
- **读**：`list_costing_snapshots` / `get_costing_snapshot` / `list_month_costing`，
  一律经 `m6_store` 只读口，月末汇总只认 ``confirmed``。

## 红线（与 `m6_cost`/`m6_defaults` 同源）

- **事实值永不默认**：单价取不到就不给数，该行进 ``missing`` 且整单标
  ``cost_incomplete``（``m6_price_source.resolve_material_prices``）；
- **口径值带痕**：估价价源/费率未显式给定时按口径层默认并在 ``assumptions`` 标
  ``assumed=true``，同时附 ``PENDING_FINANCE_CONFIRMATION``；
- **本层不做授权判断**：门由 `reviewer/rules.py` 的 ``finance`` 门 + `graph.py` 钩子
  决定（本模块只保证 propose 段不产生生效写）。

信封口径与 M5 本地工具一致：``{success, data, errors, trace_id, evidence}``；
失败用 ``code`` 显式给出（``NOT_FOUND`` / ``MONTH_CLOSED`` / ``MONTH_ALREADY_CLOSED`` /
``SNAPSHOT_EXISTS``），不伪造成功。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .m6_cost import (
    audit_order_cost,
    compute_asset_benefit,
    compute_expense_allocation,
    compute_inventory_finance_view,
    compute_material_cost,
    compute_monthly_pay,
    compute_piece_pay,
    compute_process_cost,
    compute_quotation_price,
    compute_statement,
)
from .m6_defaults import (
    DEFAULT_ALLOCATION_BASIS,
    DEFAULT_HOURS_PER_DAY,
    DEFAULT_MIN_MARGIN_RATE,
    DEFAULT_OVERTIME_MULTIPLIER,
    DEFAULT_VALUATION_PRICE_SOURCE,
    DEFAULT_WORK_DAYS,
    PENDING_FINANCE_CONFIRMATION,
)
from .m6_price_source import (
    price_for,
    purchase_prices_from_tracking,
    resolve_material_prices,
)
from .m6_store import M6Store, STATUS_CONFIRMED, STATUS_TRIAL, store_path


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _store(ctx: dict[str, Any] | None) -> M6Store:
    """M6 库路径：``ctx.m6_db_path`` → ``YUNPAI_M6_DB`` → ``runtime/yunpai-m6.sqlite``。"""
    return M6Store(store_path(ctx))


def _tenant(ctx: dict[str, Any] | None) -> str:
    return str((ctx or {}).get("tenant_id") or "default")


def _trace(ctx: dict[str, Any] | None, suffix: str) -> str:
    return f"{(ctx or {}).get('task_id') or 'task'}:{suffix}"


def _evidence(source_ref: str, detail: str) -> dict[str, Any]:
    return {"module": "m6", "source_ref": source_ref,
            "evidence_ref": f"m6:{source_ref}", "detail": detail}


def _ok(data: dict[str, Any], ctx: dict[str, Any] | None, suffix: str,
        evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    # Keep the v2 ``data/errors`` envelope while exposing the frozen Tool
    # contract names required by the current FactoryBrain file standard.
    return {"success": True, "result": data, "data": data, "error": None,
            "business_status": "completed", "errors": [],
            "trace_id": _trace(ctx, suffix), "evidence": evidence or []}


def _fail(code: str, message: str, ctx: dict[str, Any] | None, suffix: str,
          data: dict[str, Any] | None = None) -> dict[str, Any]:
    """硬失败信封（``success=False``）——不得当成功吞掉（reviewer 侧规则同步 fail）。"""
    error = {"code": code, "message": message, "details": []}
    return {"success": False, "code": code, "result": data or {},
            "errors": [error], "error": error, "business_status": "failed",
            "data": data or {}, "trace_id": _trace(ctx, suffix), "evidence": []}


def _num(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _num_optional(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_price_facts(value: Any) -> dict[str, Any] | None:
    """归一价源入参：``purchase_prices_from_tracking`` 的输出 / 扁平 ``{料号: 价}`` 都收。"""
    if not isinstance(value, dict) or not value:
        return None
    if isinstance(value.get("prices"), dict):
        return {"prices": dict(value["prices"]),
                "missing": list(value.get("missing") or []),
                "cost_incomplete": bool(value.get("cost_incomplete"))}
    prices: dict[str, dict[str, Any]] = {}
    for key, item in value.items():
        if isinstance(item, dict):
            price = _num_optional(item.get("unit_price"))
            if price is None:
                continue
            prices[str(key)] = {**item, "unit_price": price}
        else:
            price = _num_optional(item)
            if price is None:
                continue
            prices[str(key)] = {"unit_price": price, "source_ref": f"price:{key}"}
    return {"prices": prices, "missing": [], "cost_incomplete": False} if prices else None


def _price_facts(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """构造价源：显式 price facts > M4 追踪行解析 > 空（缺料行即 missing）。"""
    explicit = _normalize_price_facts(payload.get("purchase_price_facts"))
    if explicit is not None:
        return explicit, "explicit"
    rows = payload.get("purchase_tracking_rows")
    if isinstance(rows, list) and rows:
        lookup: dict[str, dict[str, Any]] = {}
        for item in payload.get("purchase_order_items") or []:
            if isinstance(item, dict) and item.get("id") not in (None, ""):
                lookup[str(item["id"])] = item
        return purchase_prices_from_tracking(rows, lookup), "m4_tracking"
    return {}, "missing"


def _assumptions(valuation_price_source: Any, hour_rate: Any,
                 overhead_rate: Any) -> dict[str, Any]:
    """口径痕迹（R0 三层分离）：口径值未显式给出 → ``assumed=true``，不冒充已确认事实。"""
    return {
        "valuation_price_source": str(valuation_price_source or DEFAULT_VALUATION_PRICE_SOURCE),
        "valuation_price_source_assumed": not bool(valuation_price_source),
        "hour_rate_assumed": hour_rate in (None, ""),
        "overhead_rate_assumed": overhead_rate in (None, ""),
        "pending_finance_confirmation": list(PENDING_FINANCE_CONFIRMATION),
    }


# ---------------------------------------------------------------------------
# propose 段：算成本 + 落试算快照
# ---------------------------------------------------------------------------

def _cost_lines(resolved: dict[str, Any], process: dict[str, Any], product_code: str,
                hour_rate: Any, overhead_rate: Any) -> list[dict[str, Any]]:
    """成本明细：材料（逐 BOM 行，带价格来源）＋ 人工 ＋ 制费。"""
    lines: list[dict[str, Any]] = []
    for row in resolved.get("lines") or []:
        qty = _num(row.get("qty_per"))
        price = _num(row.get("unit_price"))
        loss = _num(row.get("loss_rate"))
        lines.append({
            "element": "material",
            "material_code": str(row.get("material_code") or ""),
            "quantity": qty,
            "unit_price": price,
            "amount": round(price * qty * (1.0 + loss), 4),
            "source_kind": str(row.get("source_kind") or ""),
            "source_ref": str(row.get("source_ref") or ""),
            "evidence": row.get("evidence") or {},
        })
    hours = _num(process.get("total_standard_hours"))
    for element, amount, rate in (("labor", process.get("unit_labor_cost"), hour_rate),
                                  ("overhead", process.get("unit_overhead_cost"), overhead_rate)):
        if not hours and not _num(amount):
            continue          # 无工时也无金额：不落空行（缺工时已在 cost_incomplete 标注）
        lines.append({
            "element": element,
            "asset_code": str(product_code or ""),
            "quantity": hours,
            "unit_price": _num_optional(rate),
            "amount": _num(amount),
            "source_kind": "route",
            "source_ref": f"canonical:route/{product_code or ''}",
            "evidence": {"price_basis": "standard_minutes",
                         "rate_assumed": rate in (None, "")},
        })
    return lines


def _next_snapshot_id(store: M6Store, period: str, order_id: str, batch_no: str,
                      tenant_id: str) -> str:
    """确定性派生快照号：同业务键第 N 版（历史快照不覆盖，查重靠 duplicate_of 提示）。"""
    rows = store.list_snapshots(period=period, order_id=order_id, tenant_id=tenant_id)
    same = [row for row in rows if str(row.get("batch_no") or "") == batch_no]
    return f"M6-{period}-{order_id or 'NA'}-{batch_no or 'NA'}-v{len(same) + 1}"


def _cost_breakdown(payload: dict[str, Any]) -> dict[str, Any]:
    """**共用的成本预算**（D5 定价 → 材料/人工/制费 → 缺口 → 口径痕）。

    为什么抽出来：`save_costing_snapshot`（propose 落快照）与 `get_product_cost`
    （当场算/报价预览）必须是**同一套口径**——否则"D8 当场算 vs 账上正式成本"
    会因为两条实现漂移而给出不同数字。两条路径都调本函数。
    """
    bom_lines = payload.get("bom_lines") if isinstance(payload.get("bom_lines"), list) else []
    routing_steps = (payload.get("routing_steps")
                     if isinstance(payload.get("routing_steps"), list) else [])
    inventory = payload.get("inventory") if isinstance(payload.get("inventory"), list) else []
    hour_rate = payload.get("hour_rate")
    overhead_rate = payload.get("overhead_rate")
    valuation_price_source = str(payload.get("valuation_price_source") or "")

    facts, facts_source = _price_facts(payload)
    resolved = resolve_material_prices(
        bom_lines, inventory, facts,
        valuation_price_source=valuation_price_source or DEFAULT_VALUATION_PRICE_SOURCE)
    material = compute_material_cost(resolved["lines"])
    process = compute_process_cost(routing_steps, hour_rate, overhead_rate)

    missing_inputs: list[dict[str, Any]] = []
    if not bom_lines:
        missing_inputs.append({"reason": "missing_bom_lines"})
    if not routing_steps:
        missing_inputs.append({"reason": "missing_routing_steps"})
    unit_cost = round(
        _num(material["unit_material_cost"]) + _num(process["unit_labor_cost"])
        + _num(process["unit_overhead_cost"]), 4)
    return {
        "bom_lines": bom_lines,
        "routing_steps": routing_steps,
        "hour_rate": hour_rate,
        "overhead_rate": overhead_rate,
        "resolved": resolved,
        "material": material,
        "process": process,
        "unit_cost": unit_cost,
        "missing_inputs": missing_inputs,
        "cost_incomplete": bool(
            missing_inputs or material["cost_incomplete"] or process["cost_incomplete"]
            or resolved["cost_incomplete"]),
        "assumptions": _assumptions(valuation_price_source, hour_rate, overhead_rate),
        "price_source": facts_source,
    }


async def m6_save_costing_snapshot(payload: dict[str, Any],
                                   ctx: dict[str, Any]) -> dict[str, Any]:
    """`save_costing_snapshot`（**propose 段**）：按 D5 定价 → 算成本 → 落 ``trial`` 快照。

    只写 ``status=trial``（不进月末汇总、不构成正式成本），因此**不触门**；
    「正式成本」由 `confirm_costing_snapshot` + `_apply_m6_costing_confirm` 落。
    """
    period = str(payload.get("period") or "").strip()
    if not period:
        return _fail("INVALID_INPUT", "缺少 period（账期 YYYY-MM）", ctx, "m6-save")
    tenant_id = _tenant(ctx)
    order_id = str(payload.get("order_id") or "")
    product_code = str(payload.get("product_code") or "")
    batch_no = str(payload.get("batch_no") or "")

    breakdown = _cost_breakdown(payload)
    resolved = breakdown["resolved"]
    material = breakdown["material"]
    process = breakdown["process"]
    missing_inputs = breakdown["missing_inputs"]
    cost_incomplete = breakdown["cost_incomplete"]
    unit_cost = breakdown["unit_cost"]
    assumptions = breakdown["assumptions"]

    quantity = _num_optional(payload.get("quantity"))
    if quantity is None:
        missing_inputs = [*missing_inputs, {"reason": "missing_quantity"}]
    cost_incomplete = bool(cost_incomplete or quantity is None)
    # A missing production quantity is different from a zero quantity.  Keep
    # the unit-cost preview, but never persist a fabricated zero total.
    total_cost = round(unit_cost * quantity, 4) if quantity is not None else None

    resolved_evidence = {
        "price_source": breakdown["price_source"],
        "bom_line_count": len(breakdown["bom_lines"]),
        "routing_step_count": len(breakdown["routing_steps"]),
        "available_stock_lines": sum(1 for row in resolved["lines"]
                                     if row.get("source_kind") == "stock"),
        "purchase_price_lines": sum(1 for row in resolved["lines"]
                                    if row.get("source_kind") == "purchase"),
        "missing_lines": resolved["missing"],
        "missing_inputs": missing_inputs,
        "assumptions": assumptions,
        **({"caller_evidence": payload["evidence"]}
           if isinstance(payload.get("evidence"), dict) else {}),
    }

    store = _store(ctx)
    snapshot_id = str(payload.get("snapshot_id") or "") or _next_snapshot_id(
        store, period, order_id, batch_no, tenant_id)
    if store.get_snapshot(snapshot_id, tenant_id) is not None:
        return _fail("SNAPSHOT_EXISTS",
                     f"快照 {snapshot_id} 已存在（历史快照不覆盖；如需新版请不要指定 snapshot_id）",
                     ctx, "m6-save", data={"snapshot_id": snapshot_id})

    saved = store.save_snapshot(
        snapshot_id=snapshot_id, period=period, order_id=order_id,
        product_code=product_code, batch_no=batch_no, quantity=quantity,
        unit_cost=unit_cost, total_cost=total_cost, basis=str(resolved["basis"]),
        cost_incomplete=cost_incomplete,
        lines=_cost_lines(resolved, process, product_code,
                          breakdown["hour_rate"], breakdown["overhead_rate"]),
        evidence=resolved_evidence, task_id=str((ctx or {}).get("task_id") or ""),
        tenant_id=tenant_id)
    if not saved.get("success"):
        code = str(saved.get("code") or "SAVE_FAILED")
        message = ("该期间月账已冻结：不得再产出新成本（月结后重开属财务决策）"
                   if code == "MONTH_CLOSED" else f"试算快照未落库（{code}）")
        return _fail(code, message, ctx, "m6-save", data={"period": period})

    data = {**saved, "period": period, "order_id": order_id, "product_code": product_code,
            "batch_no": batch_no, "unit_cost": unit_cost, "total_cost": total_cost,
            "cost_incomplete": cost_incomplete, "basis": str(resolved["basis"]),
            "missing": resolved["missing"], "missing_inputs": missing_inputs,
            "assumptions": assumptions,
            "material_cost": material, "process_cost": process}
    return _ok(data, ctx, "m6-save", evidence=[
        _evidence(f"snapshot:{snapshot_id}",
                  f"试算快照（trial）：单价口径={resolved['basis']}，"
                  f"明细 {saved.get('line_count')} 行，cost_incomplete={cost_incomplete}")])


# ---------------------------------------------------------------------------
# commit 段的发起（翻正/冻结在 graph._apply_m6_*）
# ---------------------------------------------------------------------------

async def m6_confirm_costing_snapshot(payload: dict[str, Any],
                                      ctx: dict[str, Any]) -> dict[str, Any]:
    """`confirm_costing_snapshot`：前置校验 + 请求财务确认，**不翻状态**。

    三段式：本工具属 commit 段的**发起**（propose 段已由 save 完成），实际
    ``trial→confirmed`` 由 ``graph._apply_m6_costing_confirm`` 在 approve 后执行；
    ``reject`` 路径不落任何生效行。
    """
    snapshot_id = str(payload.get("snapshot_id") or "").strip()
    if not snapshot_id:
        return _fail("INVALID_INPUT", "缺少 snapshot_id", ctx, "m6-confirm")
    tenant_id = _tenant(ctx)
    store = _store(ctx)
    snapshot = store.get_snapshot(snapshot_id, tenant_id)
    if snapshot is None:
        return _fail("NOT_FOUND", f"快照 {snapshot_id} 不存在", ctx, "m6-confirm",
                     data={"snapshot_id": snapshot_id})
    if str(snapshot.get("status")) == STATUS_CONFIRMED:
        # 幂等：已确认的快照不再开门（重复确认不该产生第二次人工确认）。
        return _ok({"snapshot_id": snapshot_id, "status": STATUS_CONFIRMED,
                    "changed": False, "pending_confirmation": False,
                    "period": snapshot.get("period")}, ctx, "m6-confirm")
    if store.get_month_close(str(snapshot.get("period")), tenant_id) is not None:
        return _fail("MONTH_CLOSED",
                     f"{snapshot.get('period')} 月账已冻结：该期间不得再确认成本",
                     ctx, "m6-confirm",
                     data={"snapshot_id": snapshot_id, "period": snapshot.get("period")})
    return _ok({
        "snapshot_id": snapshot_id, "status": STATUS_TRIAL, "pending_confirmation": True,
        "period": snapshot.get("period"), "order_id": snapshot.get("order_id"),
        "product_code": snapshot.get("product_code"), "batch_no": snapshot.get("batch_no"),
        "quantity": snapshot.get("quantity"), "unit_cost": snapshot.get("unit_cost"),
        "total_cost": snapshot.get("total_cost"), "basis": snapshot.get("basis"),
        "cost_incomplete": snapshot.get("cost_incomplete"),
        "note": str(payload.get("note") or ""),
    }, ctx, "m6-confirm", evidence=[
        _evidence(f"snapshot:{snapshot_id}",
                  "请求财务确认：trial→confirmed 须 finance 门批准后由 commit 钩子落库")])


async def m6_close_month_costing(payload: dict[str, Any],
                                 ctx: dict[str, Any]) -> dict[str, Any]:
    """`close_month_costing`：回报待冻结月账（只含 confirmed），**不写冻结行**。

    批准后由 ``graph._apply_m6_close_month`` 用**被审阅的这组合计**落冻结行
    （审阅什么就冻结什么），此后该期间不得再产成本/再确认。
    """
    period = str(payload.get("period") or "").strip()
    if not period:
        return _fail("INVALID_INPUT", "缺少 period（账期 YYYY-MM）", ctx, "m6-close")
    tenant_id = _tenant(ctx)
    store = _store(ctx)
    frozen = store.get_month_close(period, tenant_id)
    if frozen is not None:
        return _fail("MONTH_ALREADY_CLOSED", f"{period} 月账已结账，不得重复结账",
                     ctx, "m6-close",
                     data={"period": period, "closed_at": frozen.get("closed_at")})
    summary = store.month_summary(period, tenant_id)
    return _ok({
        "period": period, "pending_close": True, "month_summary": summary,
        "snapshot_count": int(summary.get("snapshot_count") or 0),
        "total_cost": _num(summary.get("total_cost")),
        "trial_count": int(summary.get("trial_count") or 0),
        "trial_excluded": True,
        "note": str(payload.get("note") or ""),
    }, ctx, "m6-close", evidence=[
        _evidence(f"month:{period}",
                  f"请求月结：confirmed {summary.get('snapshot_count')} 版 / "
                  f"合计 {summary.get('total_cost')}（trial {summary.get('trial_count')} 版不计入）")])


# ---------------------------------------------------------------------------
# 读（一律经 m6_store 只读口）
# ---------------------------------------------------------------------------

def _limit(payload: dict[str, Any], default: int, *, low: int = 1, high: int = 200) -> int:
    value = payload.get("limit")
    try:
        got = int(value)
    except (TypeError, ValueError):
        return default
    return max(min(got, high), low)


async def m6_list_costing_snapshots(payload: dict[str, Any],
                                    ctx: dict[str, Any]) -> dict[str, Any]:
    """`list_costing_snapshots`：快照列表（trial 与 confirmed 都列出并明示状态）。"""
    store = _store(ctx)
    rows = store.list_snapshots(
        period=str(payload.get("period") or "") or None,
        status=str(payload.get("status") or "") or None,
        order_id=str(payload.get("order_id") or "") or None,
        tenant_id=_tenant(ctx))
    limit = _limit(payload, 50)
    return _ok({"snapshots": rows[:limit], "count": len(rows), "limit": limit},
               ctx, "m6-list-snapshots")


async def m6_get_costing_snapshot(payload: dict[str, Any],
                                  ctx: dict[str, Any]) -> dict[str, Any]:
    """`get_costing_snapshot`：按快照号读回明细（含 source_kind/source_ref/evidence）。"""
    snapshot_id = str(payload.get("snapshot_id") or "").strip()
    if not snapshot_id:
        return _fail("INVALID_INPUT", "缺少 snapshot_id", ctx, "m6-get-snapshot")
    snapshot = _store(ctx).get_snapshot(snapshot_id, _tenant(ctx))
    if snapshot is None:
        return _fail("NOT_FOUND", f"快照 {snapshot_id} 不存在", ctx, "m6-get-snapshot",
                     data={"snapshot_id": snapshot_id})
    return _ok({"snapshot": snapshot}, ctx, "m6-get-snapshot")


async def m6_list_month_costing(payload: dict[str, Any],
                                ctx: dict[str, Any]) -> dict[str, Any]:
    """`list_month_costing`：月度汇总（**只加 confirmed**，trial 以 trial_count 明示）。"""
    store = _store(ctx)
    tenant_id = _tenant(ctx)
    period = str(payload.get("period") or "")
    periods = [period] if period else store.list_periods(tenant_id)
    months = [store.month_summary(item, tenant_id) for item in periods]
    limit = _limit(payload, 24, high=120)
    return _ok({"months": months[:limit], "count": len(months), "limit": limit,
                "period": period}, ctx, "m6-list-month")


# ---------------------------------------------------------------------------
# 内核读工具（纯算数，不写库）：产品成本 / 订单成本审计 / 费用分摊
# ---------------------------------------------------------------------------

async def m6_get_product_cost(payload: dict[str, Any],
                              ctx: dict[str, Any]) -> dict[str, Any]:
    """`get_product_cost`：当场算一个产品的单台成本（**试算/报价预览**，不落库）。

    D8 的「当场算」与 `save_costing_snapshot` 的试算快照**共用 `_cost_breakdown`**
    ——同一套 D5 口径，避免两个数字漂移。缺价/缺工时照样标 `cost_incomplete`，
    不给 0 冒充（报价时若见 `cost_incomplete=True`，这笔报价的依据是不全的）。
    """
    product_code = str(payload.get("product_code") or "").strip()
    if not product_code:
        return _fail("INVALID_INPUT", "缺少 product_code", ctx, "m6-product-cost")
    breakdown = _cost_breakdown(payload)
    resolved = breakdown["resolved"]
    data = {
        "product_code": product_code,
        "unit_cost": breakdown["unit_cost"],
        "material": breakdown["material"],
        "process": breakdown["process"],
        "unit_material_cost": breakdown["material"]["unit_material_cost"],
        "unit_labor_cost": breakdown["process"]["unit_labor_cost"],
        "unit_overhead_cost": breakdown["process"]["unit_overhead_cost"],
        "basis": str(resolved["basis"]),
        "cost_incomplete": breakdown["cost_incomplete"],
        "missing": resolved["missing"],
        "missing_inputs": breakdown["missing_inputs"],
        "assumptions": breakdown["assumptions"],
        "price_source": breakdown["price_source"],
        "lines": resolved["lines"],          # 行级价格来源（哪行走库存、哪行走采购）
    }
    return _ok(data, ctx, "m6-product-cost", evidence=[
        _evidence(f"product:{product_code}",
                  f"当场算单台成本（不落库）：单价口径={resolved['basis']}，"
                  f"cost_incomplete={breakdown['cost_incomplete']}")])


async def m6_audit_order_cost(payload: dict[str, Any],
                              ctx: dict[str, Any]) -> dict[str, Any]:
    """`audit_order_cost`：订单成本审计与毛利核算（确定性、无写操作）。

    单位成本来源两级（与老仓 `m6_service` 同口径）：
    1. `unit_costs` 显式给出（如来自已确认的账上成本）优先；
    2. 未覆盖的产品据 `products[code]` 的 BOM/工艺/费率**当场滚动**（走同一套
       `_cost_breakdown`）。

    缺数量/单价/单位成本的行进 `incomplete`，`status` 取 `cost_incomplete`
    （不编造、也不把「算不全」当「盈利」）。
    """
    order_id = str(payload.get("order_id") or "")
    order_lines = payload.get("order_lines") if isinstance(payload.get("order_lines"), list) else []
    unit_costs = payload.get("unit_costs") if isinstance(payload.get("unit_costs"), dict) else {}
    products = payload.get("products") if isinstance(payload.get("products"), dict) else {}
    min_margin_rate = (payload.get("min_margin_rate")
                       if payload.get("min_margin_rate") not in (None, "")
                       else DEFAULT_MIN_MARGIN_RATE)

    rolled: dict[str, Any] = {}
    roll_missing: list[dict[str, Any]] = []
    for code in dict.fromkeys(
            str(line.get("product_code") or "") for line in order_lines
            if isinstance(line, dict)):
        if not code or code in unit_costs:
            continue
        spec = products.get(code) if isinstance(products.get(code), dict) else {}
        if not spec and payload.get("bom_lines"):
            # 单产品订单的便捷路径：顶层事实即该产品的成本事实
            spec = {"bom_lines": payload.get("bom_lines"),
                    "routing_steps": payload.get("routing_steps"),
                    "hour_rate": payload.get("hour_rate"),
                    "overhead_rate": payload.get("overhead_rate")}
        if not spec:
            roll_missing.append({"product_code": code, "reason": "missing_product_facts"})
            continue
        breakdown = _cost_breakdown(spec)
        unit_costs = {**unit_costs, code: {
            "material": breakdown["material"]["unit_material_cost"],
            "labor": breakdown["process"]["unit_labor_cost"],
            "overhead": breakdown["process"]["unit_overhead_cost"],
        }}
        rolled[code] = {"unit_cost": breakdown["unit_cost"],
                        "cost_incomplete": breakdown["cost_incomplete"],
                        "assumptions": breakdown["assumptions"]}

    result = audit_order_cost(order_lines, unit_costs, min_margin_rate=min_margin_rate)
    # 滚动出来的产品若成本不全，整单状态必须显式降级（不得当"算完了"）
    if any(item.get("cost_incomplete") for item in rolled.values()):
        result["status"] = "cost_incomplete"
    incomplete = list(result.get("incomplete") or [])
    for code, item in rolled.items():
        if item.get("cost_incomplete"):
            incomplete.append({"reason": "rolled_unit_cost_incomplete", "product_code": code})
    data = {**result, "order_id": order_id,
            "unit_costs": {code: {"material": _num(cost.get("material")),
                                  "labor": _num(cost.get("labor")),
                                  "overhead": _num(cost.get("overhead"))}
                           for code, cost in unit_costs.items() if isinstance(cost, dict)},
            "rolled_products": sorted(rolled), "cost_incomplete": bool(incomplete),
            "min_margin_rate": _num(min_margin_rate),
            "missing": (result.get("incomplete") or []) + roll_missing,
            "assumptions": _assumptions(None, None, None)}
    return _ok(data, ctx, "m6-audit-order", evidence=[
        _evidence(f"order:{order_id or 'NA'}",
                  f"订单成本审计：status={result['status']}，"
                  f"滚动产品 {sorted(rolled) or '（无，全部用 unit_costs）'}，"
                  f"缺口 {len(incomplete)} 项")])


async def m6_allocate_expenses(payload: dict[str, Any],
                               ctx: dict[str, Any]) -> dict[str, Any]:
    """`allocate_expenses`：按口径把费用分摊到产品（确定性、无写操作）。

    事实来源：`expenses`（默认由装配层从 M0 canonical 的 `expense` 实体读入，
    信封已在装配侧拆开）与 `basis_rows`（产量/工时/人数/订单数基准）。
    **不落库**——分摊结果只作分析；要进账用 `save_costing_snapshot`。

    口径痕：分摊基准未显式指定时取口径层默认且在 `assumptions` 标
    `assumed=True`；装配层另把 `basis_source`（explicit/canonical/missing）
    写回，避免"基准数据到底哪来的"说不清（内核默认值只说 explicit）。
    """
    expenses = payload.get("expenses") if isinstance(payload.get("expenses"), list) else []
    basis_rows = payload.get("basis_rows") if isinstance(payload.get("basis_rows"), list) else []
    period = str(payload.get("period") or "")
    if period:
        expenses = [row for row in expenses
                    if isinstance(row, dict) and str(row.get("period") or "") == period]
    allocation_basis = payload.get("allocation_basis")
    result = compute_expense_allocation(expenses, basis_rows,
                                       allocation_basis=allocation_basis)
    basis_source = str(payload.get("basis_source") or "explicit")
    assumptions = {**(result.get("assumptions") or {}), "basis_source": basis_source}
    data = {**result, "period": period, "basis_source": basis_source,
            "assumptions": assumptions}
    return _ok(data, ctx, "m6-allocate-expenses", evidence=[
        _evidence(f"expense:{period or 'all'}",
                  f"费用分摊：合计 {result['total_expense']} / 已分摊 "
                  f"{result['total_allocated']}，基准来源={basis_source}，"
                  f"cost_incomplete={result['cost_incomplete']}")])


# ---------------------------------------------------------------------------
# 单据台账（B2）：报价单 / 对账单
# ---------------------------------------------------------------------------
#
# 三段式照 D-005：`save_*` 属 **propose 段**（只写 ``status=trial`` 草稿 + 请财务门），
# 生效（``trial → confirmed``）由 `graph._apply_m6_document_commit` 在 approve 后执行。
# `generate_*` 是纯算数（不落库）——与 `get_product_cost` / `save_costing_snapshot`
# 同一关系：先看数、再决定落草稿。

M6_DOC_TYPES = {
    "quotation": "报价单",
    "statement": "对账单",
}


def _doc_date(payload: dict[str, Any]) -> str:
    return str(payload.get("doc_date") or "") or _now_iso()[:10]


def _next_doc_no(store: M6Store, doc_type: str, doc_date: str, tenant_id: str) -> str:
    """确定性派生单号：``<前缀>-<日期>-<流水>``（历史单据不覆盖，查重靠显式拒绝）。"""
    prefix = {"quotation": "QT", "statement": "ST"}.get(doc_type, "DOC")
    head = f"{prefix}-{(doc_date or _now_iso()[:10]).replace('-', '')}"
    existing = [row for row in store.list_documents(doc_type=doc_type, tenant_id=tenant_id)
                if str(row.get("doc_no") or "").startswith(head)]
    return f"{head}-{len(existing) + 1:03d}"


def _line_costs(line: dict[str, Any],
                products: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """解析一行的三项单位成本：**行内显式 > `products[code]` 的成本事实滚动**。

    返回 ``(成本三项, 缺口)``——两者必有一为 None。**缺成本的行不给报价**：
    把缺失成本当 0 算出来的"报价"就是编造（老仓 `_num(None)=0.0` 正是这么算的），
    本函数宁可让该行进 ``missing``。
    """
    code = str(line.get("product_code") or "").strip()
    explicit = {key: line.get(key) for key in ("unit_material", "unit_labor", "unit_overhead")}
    if all(value not in (None, "") for value in explicit.values()):
        return {key: _num(value) for key, value in explicit.items()}, None
    spec = products.get(code) if isinstance(products, dict) else None
    if not isinstance(spec, dict):
        return None, {"reason": "missing_unit_cost", "product_code": code}
    breakdown = _cost_breakdown({**spec, "product_code": code})
    if breakdown["cost_incomplete"]:
        return None, {"reason": "unit_cost_incomplete", "product_code": code,
                      "missing": breakdown["missing_inputs"] + breakdown["resolved"]["missing"]}
    return {
        "unit_material": breakdown["material"]["unit_material_cost"],
        "unit_labor": breakdown["process"]["unit_labor_cost"],
        "unit_overhead": breakdown["process"]["unit_overhead_cost"],
    }, None


def _quotation_lines(payload: dict[str, Any]) -> dict[str, Any]:
    """**共用的报价预算**：逐行 (材料+人工+制费)×(1+加价率) → 行小计与总计。

    `generate_quotation`（报价预览）与 `save_quotation`（落草稿）共用，避免
    "预览一个价、落库另一个价"。成本项缺（行内三项未给且 `products[code]` 也滚不出来）
    → 该行进 ``missing`` **不计入合计**（见 `_line_costs`）；加价率没给时按 0 算但标
    ``markup_assumed=True``——**不能让 0 加价冒充谈好的报价**。
    """
    raw_lines = payload.get("lines") if isinstance(payload.get("lines"), list) else []
    products = payload.get("products") if isinstance(payload.get("products"), dict) else {}
    default_markup = payload.get("markup_rate")
    out_lines: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    total = 0.0
    markup_assumed = False

    for index, line in enumerate(raw_lines, start=1):
        if not isinstance(line, dict):
            missing.append({"line": index, "reason": "invalid_row"})
            continue
        product_code = str(line.get("product_code") or "").strip()
        qty = _num_optional(line.get("qty", line.get("quantity")))
        if not product_code or qty is None:
            missing.append({"line": index, "reason": "missing_product_code_or_qty"})
            continue
        costs, gap = _line_costs(line, products)
        if gap is not None:
            missing.append({"line": index, **gap})
            continue
        markup = line.get("markup_rate") if line.get("markup_rate") not in (None, "") \
            else default_markup
        if markup in (None, ""):
            markup_assumed = True
            markup = 0.0
        unit = compute_quotation_price(costs["unit_material"], costs["unit_labor"],
                                      costs["unit_overhead"], markup_rate=markup)
        line_total = round(unit["quote_price"] * qty, 4)
        total += line_total
        out_lines.append({
            "product_code": product_code, "qty": qty,
            **costs,
            "base_cost": unit["base_cost"], "markup_rate": unit["markup_rate"],
            "quote_price": unit["quote_price"], "line_total": line_total,
        })
    return {
        "lines": out_lines,
        "total": round(total, 4),
        "missing": missing,
        "markup_assumed": markup_assumed,
        "assumptions": {
            "markup_rate_assumed": markup_assumed,
            "pending_finance_confirmation": list(PENDING_FINANCE_CONFIRMATION),
        },
    }


def _statement_totals(payload: dict[str, Any]) -> dict[str, Any]:
    """**共用的对账预算**：期初 + Σin − Σout（`compute_statement`，纯确定性）。

    对账明细优先取 `transactions`（direction in/out）；只给 `lines`/`amount` 时原样采信
    调用方给的事实（不做二次推算）。
    """
    detail = _statement_transactions(payload)
    if detail["transactions"]:
        computed = compute_statement(payload.get("opening_balance"), detail["transactions"])
        return {**computed, "source": detail["source"],
                "lines": computed["lines"],
                "amount": computed["closing_balance"],
                "missing": detail["missing"]}
    lines = payload.get("lines") if isinstance(payload.get("lines"), list) else []
    return {"opening_balance": _num(payload.get("opening_balance")), "inflow": None,
            "outflow": None,
            "closing_balance": (_num_optional(payload.get("amount"))
                                if payload.get("amount") not in (None, "") else None),
            "lines": lines, "source": "explicit_lines" if lines else "missing",
            "amount": _num_optional(payload.get("amount")),
            "missing": detail["missing"]}


async def m6_generate_quotation(payload: dict[str, Any],
                                ctx: dict[str, Any]) -> dict[str, Any]:
    """`generate_quotation`：算一版报价（**不落库**）——报价预览。

    纯确定性（R-QT-1）：行报价 = (材料+人工+制费)×(1+加价率)。要看数就用本工具，
    要落成可追溯的客户台账请用 `save_quotation`（它走 finance 门）。
    """
    quote = _quotation_lines(payload)
    if not quote["lines"]:
        return _fail("INVALID_INPUT", "报价行全不可用（缺 product_code/qty）", ctx,
                     "m6-quotation", data={"missing": quote["missing"]})
    data = {"quote_no": str(payload.get("quote_no") or ""),
            "customer_code": str(payload.get("customer_code") or ""),
            "doc_date": _doc_date(payload),
            "lines": quote["lines"], "total": quote["total"],
            "missing": quote["missing"], "assumptions": quote["assumptions"]}
    return _ok(data, ctx, "m6-quotation", evidence=[
        _evidence(f"quotation:{data['quote_no'] or 'preview'}",
                  f"报价预览（不落库）：{len(quote['lines'])} 行，合计 {quote['total']}，"
                  f"加价率按假设={quote['markup_assumed']}")])


def _propose_document(payload: dict[str, Any], ctx: dict[str, Any], *, doc_type: str,
                      direction: str, lines: Any, amount: Any,
                      extra_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """**propose 段共用落库**：写 ``status=trial`` 单据 + 回报待财务确认。

    单号已存在时**显式拒绝**（``DOC_NO_EXISTS``）——同号单据不静默覆盖，
    由人决定新建/覆盖/查看（计划 §6 的 B 问题）。
    """
    tenant_id = _tenant(ctx)
    store = _store(ctx)
    doc_date = _doc_date(payload)
    counterparty = str(payload.get("counterparty_code") or payload.get("customer_code") or "")
    doc_no = str(payload.get("doc_no") or "")
    if doc_no and store.find_document(doc_type=doc_type, doc_no=doc_no,
                                      tenant_id=tenant_id) is not None:
        return _fail("DOC_NO_EXISTS",
                     f"{M6_DOC_TYPES.get(doc_type, doc_type)} {doc_no} 已存在"
                     "（同号单据不覆盖；如确需新版请换单号）",
                     ctx, f"m6-{doc_type}", data={"doc_no": doc_no})
    doc_no = doc_no or _next_doc_no(store, doc_type, doc_date, tenant_id)
    doc_id = str(payload.get("doc_id") or "") or f"{doc_type.upper()}-{doc_no}"
    if store.get_document(doc_id, tenant_id) is not None:
        return _fail("DOC_ID_EXISTS", f"单据 {doc_id} 已存在", ctx, f"m6-{doc_type}",
                     data={"doc_id": doc_id})
    evidence = {
        "doc_type": doc_type, "direction": direction,
        "line_count": len(lines) if isinstance(lines, list) else 0,
        "assumptions": payload.get("assumptions") or {},
        **(extra_evidence or {}),
    }
    saved = store.save_document(
        doc_id=doc_id, doc_no=doc_no, doc_type=doc_type, counterparty_code=counterparty,
        doc_date=doc_date, direction=direction, amount=amount, lines=lines,
        source_ref=str(payload.get("source_ref") or ""), evidence=evidence,
        task_id=str((ctx or {}).get("task_id") or ""), tenant_id=tenant_id)
    if not saved.get("success"):
        return _fail(str(saved.get("code") or "SAVE_FAILED"),
                     f"{M6_DOC_TYPES.get(doc_type, doc_type)}草稿未落库", ctx,
                     f"m6-{doc_type}", data={"doc_no": doc_no})
    data = {
        **saved, "doc_no": doc_no, "doc_date": doc_date, "counterparty_code": counterparty,
        "direction": direction, "amount": amount, "lines": lines,
        # 约定字段：finance 门批准后由 `graph._apply_m6_document_commit` 据此翻 confirmed
        "pending_document_commit": True,
    }
    return _ok(data, ctx, f"m6-{doc_type}", evidence=[
        _evidence(f"document:{doc_id}",
                  f"{M6_DOC_TYPES.get(doc_type, doc_type)}草稿（trial）：单号 {doc_no}，"
                  f"金额 {amount}，待 finance 门批准后生效")])


async def m6_save_quotation(payload: dict[str, Any],
                            ctx: dict[str, Any]) -> dict[str, Any]:
    """`save_quotation`（**propose 段**）：算报价 → 落 ``trial`` 草稿 → 请财务门。

    报价单是对客户的**生效凭据**，故：工具自身只落草稿（可追溯、不构成对外承诺），
    approve 后由 `_apply_m6_document_commit` 翻 ``confirmed``；reject 路径草稿保留但**不生效**。
    """
    quote = _quotation_lines(payload)
    if not quote["lines"]:
        return _fail("INVALID_INPUT", "报价行全不可用（缺 product_code/qty）", ctx,
                     "m6-quotation", data={"missing": quote["missing"]})
    data = _propose_document(
        {**payload, "assumptions": quote["assumptions"]}, ctx, doc_type="quotation",
        direction=str(payload.get("direction") or "out"), lines=quote["lines"],
        amount=quote["total"],
        extra_evidence={"total": quote["total"], "missing": quote["missing"]})
    if data.get("success"):
        data["data"]["total"] = quote["total"]
        data["data"]["lines"] = quote["lines"]
        data["data"]["missing"] = quote["missing"]
        data["data"]["assumptions"] = quote["assumptions"]
    return data


async def m6_list_quotations(payload: dict[str, Any],
                             ctx: dict[str, Any]) -> dict[str, Any]:
    """`list_quotations`：报价单台账（trial 草稿与 confirmed 生效都列出并明示状态）。"""
    rows = _store(ctx).list_documents(
        doc_type="quotation", status=str(payload.get("status") or "") or None,
        counterparty_code=str(payload.get("customer_code")
                              or payload.get("counterparty_code") or "") or None,
        tenant_id=_tenant(ctx))
    limit = _limit(payload, 50)
    return _ok({"quotations": rows[:limit], "count": len(rows), "limit": limit},
               ctx, "m6-list-quotations")


async def m6_get_quotation(payload: dict[str, Any],
                           ctx: dict[str, Any]) -> dict[str, Any]:
    """`get_quotation`：按单号或主键读回一张报价单（含明细行）。"""
    store = _store(ctx)
    tenant_id = _tenant(ctx)
    doc_id = str(payload.get("doc_id") or "")
    doc_no = str(payload.get("doc_no") or payload.get("quote_no") or "")
    doc = store.get_document(doc_id, tenant_id) if doc_id else None
    if doc is None and doc_no:
        doc = store.find_document(doc_type="quotation", doc_no=doc_no, tenant_id=tenant_id)
        doc = store.get_document(str(doc["doc_id"]), tenant_id) if doc else None
    if not doc_id and not doc_no:
        return _fail("INVALID_INPUT", "需要 doc_no（报价单号）或 doc_id（单据主键）之一",
                     ctx, "m6-get-quotation")
    if doc is None:
        return _fail("NOT_FOUND", f"报价单 {doc_id or doc_no} 不存在", ctx,
                     "m6-get-quotation", data={"doc_id": doc_id, "doc_no": doc_no})
    return _ok({"quotation": doc}, ctx, "m6-get-quotation")


def _statement_transactions(payload: dict[str, Any]) -> dict[str, Any]:
    """对账明细（B3）：**显式 transactions > 依据事实生成**（客户=送货单／供应商=入库）。

    领域映射只在这一处实现（装配层只给事实）：
    - 客户对账：`delivery_note` 的**外发货**（direction=out）→ 明细 ``in``（应收增加），
      ref = 送货单号；单据没给 amount 的**不进明细**（计 missing，不编造金额）。
    - 供应商对账：M4 **已入库**追踪行 → 明细 ``in``（应付增加），ref = 采购单号；
      金额**不取**追踪行的 `unit_price`——那是单价不是入库金额，拿来当对账金额是错的，
      故依据行须自带 ``amount``，否则计 missing。
    """
    explicit = payload.get("transactions")
    if isinstance(explicit, list) and explicit:
        transactions: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for index, tx in enumerate(explicit, start=1):
            if not isinstance(tx, dict):
                missing.append({"line": index, "reason": "invalid_row"})
                continue
            direction = str(tx.get("direction") or "").strip().lower()
            amount = _num_optional(tx.get("amount"))
            if direction not in {"in", "out", "debit", "credit", "increase", "decrease"}:
                missing.append({"line": index, "reason": "missing_or_invalid_direction"})
                continue
            if amount is None:
                missing.append({"line": index, "reason": "missing_transaction_amount",
                                "ref": tx.get("ref")})
                continue
            transactions.append({**tx, "direction": direction, "amount": amount})
        return {"transactions": transactions, "source": "explicit", "missing": missing}
    statement_type = str(payload.get("statement_type") or "customer")
    transactions: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    if statement_type == "supplier":
        rows = payload.get("purchase_receipts")
        rows = rows if isinstance(rows, list) else []
        if not rows:
            missing.append({"reason": "missing_purchase_receipts"})
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            ref = str(row.get("purchase_order_no") or row.get("purchase_order_id") or "")
            amount = _num_optional(row.get("amount"))
            if amount is None:
                missing.append({"line": index, "reason": "missing_receipt_amount", "ref": ref})
                continue
            transactions.append({"direction": "in", "amount": amount, "ref": ref})
        source = "purchase_receipts" if transactions else "missing"
    else:
        notes = payload.get("delivery_notes")
        notes = notes if isinstance(notes, list) else []
        if not notes:
            missing.append({"reason": "missing_delivery_notes"})
        for index, note in enumerate(notes, start=1):
            if not isinstance(note, dict):
                continue
            if str(note.get("direction") or "out") != "out":
                continue                    # 只认外发货（入库单不是应收依据）
            ref = str(note.get("note_no") or note.get("canonical_key") or "")
            amount = _num_optional(note.get("amount"))
            if amount is None:
                missing.append({"line": index, "reason": "missing_delivery_amount", "ref": ref})
                continue
            transactions.append({"direction": "in", "amount": amount, "ref": ref})
        source = "delivery_notes" if transactions else "missing"
    return {"transactions": transactions, "source": source, "missing": missing}


async def m6_generate_statement(payload: dict[str, Any],
                                ctx: dict[str, Any]) -> dict[str, Any]:
    """`generate_statement`（B3 升级）：**依据送货单/采购入库生成**对账明细并算期末。

    纯算数、**不落库**（落库走 `save_statement`，那条链才有 finance 门）。依据取不到时
    以 `basis_source=missing` + `missing` 明示——**绝不出具一份金额为零的空对账单**。
    """
    counterparty = str(payload.get("counterparty_code") or payload.get("supplier_code") or "")
    if not counterparty:
        return _fail("INVALID_INPUT", "需要 counterparty_code（对账对象）", ctx,
                     "m6-generate-statement")
    statement_type = str(payload.get("statement_type") or "customer")
    detail = _statement_transactions(payload)
    # Explicit transactions are caller-provided accounting facts: any
    # malformed row makes the preview unsafe. Generated basis rows may still
    # contain non-usable source rows; those are reported in ``missing`` while
    # valid rows are retained (the established B3 behavior).
    if (detail["source"] == "explicit" and detail["missing"]) or not detail["transactions"]:
        return _fail("INVALID_INPUT", "对账依据不足，无法生成对账单（不编造明细）", ctx,
                     "m6-generate-statement",
                     data={"statement_type": statement_type, "counterparty_code": counterparty,
                           "basis_source": detail["source"], "missing": detail["missing"]})
    computed = compute_statement(payload.get("opening_balance"), detail["transactions"])
    data = {
        "statement_type": statement_type, "counterparty_code": counterparty,
        "date_from": str(payload.get("date_from") or ""),
        "date_to": str(payload.get("date_to") or ""),
        "basis_source": detail["source"],
        **computed,
        "transactions": detail["transactions"], "missing": detail["missing"],
    }
    return _ok(data, ctx, "m6-generate-statement", evidence=[
        _evidence(f"statement:{counterparty}",
                  f"对账单预览（不落库）：依据={detail['source']}，明细 "
                  f"{len(detail['transactions'])} 条，期末 {computed['closing_balance']}")])


async def m6_get_delivery_note(payload: dict[str, Any],
                               ctx: dict[str, Any]) -> dict[str, Any]:
    """`get_delivery_note`：按单号读回 canonical 送货单（**M6 只读，不建 create**）。

    读的是 M0 canonical（唯一主，D4／老仓 D-020 = v2 D-009）；签收面字段（`signed_by`/
    `warehouse_confirmed_by`/`qc_status`）原样照抄回读、不加工。事实由装配层读取
    （`delivery_note_bodies`，**已拆信封**），本层只做匹配与回读。
    """
    note_no = str(payload.get("note_no") or "").strip()
    if not note_no:
        return _fail("INVALID_INPUT", "需要 note_no（送货单号）", ctx, "m6-get-delivery-note")
    rows = payload.get("delivery_notes")
    rows = rows if isinstance(rows, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if note_no in (str(row.get("note_no") or ""), str(row.get("canonical_key") or "")):
            return _ok({"delivery_note": row}, ctx, "m6-get-delivery-note", evidence=[
                _evidence(f"delivery_note:{note_no}", "canonical 送货单回读（只读）")])
    return _fail("NOT_FOUND", f"送货单 {note_no} 不存在", ctx, "m6-get-delivery-note",
                 data={"note_no": note_no})


async def m6_save_statement(payload: dict[str, Any],
                            ctx: dict[str, Any]) -> dict[str, Any]:
    """`save_statement`（**propose 段**）：期末 = 期初 + Σin − Σout → 落 ``trial`` 草稿。

    `statement_type`（customer/supplier）决定方向：我方对客户 = 出（``out``）、
    对供应商 = 入（``in``）；也可显式给 ``direction`` 覆盖。
    明细来源见 `_statement_transactions`（B3 起可依据送货单/入库事实自动生成）。
    """
    totals = _statement_totals(payload)
    if totals["source"] == "missing" or (
            totals.get("missing") and totals["source"] == "explicit"):
        return _fail("INVALID_INPUT",
                     "对账明细缺失或金额无效：请给 transactions（含 direction/amount）或显式 lines",
                     ctx, "m6-statement", data={"missing": totals.get("missing") or []})
    statement_type = str(payload.get("statement_type") or "")
    direction = str(payload.get("direction") or
                    ("in" if statement_type == "supplier" else "out"))
    data = _propose_document(
        payload, ctx, doc_type="statement", direction=direction,
        lines=totals["lines"], amount=totals["amount"],
        extra_evidence={"statement_type": statement_type,
                        "opening_balance": totals["opening_balance"],
                        "inflow": totals["inflow"], "outflow": totals["outflow"],
                        "closing_balance": totals["closing_balance"],
                        "totals_source": totals["source"],
                        "missing": totals.get("missing") or []})
    if data.get("success"):
        data["data"].update({"statement_type": statement_type,
                             "opening_balance": totals["opening_balance"],
                             "inflow": totals["inflow"], "outflow": totals["outflow"],
                             "closing_balance": totals["closing_balance"],
                             "totals_source": totals["source"],
                             "missing": totals.get("missing") or []})
    return data


async def m6_list_statements(payload: dict[str, Any],
                             ctx: dict[str, Any]) -> dict[str, Any]:
    """`list_statements`：对账单台账（双向：customer 出 / supplier 入）。"""
    rows = _store(ctx).list_documents(
        doc_type="statement", status=str(payload.get("status") or "") or None,
        counterparty_code=str(payload.get("counterparty_code") or "") or None,
        tenant_id=_tenant(ctx))
    limit = _limit(payload, 50)
    return _ok({"statements": rows[:limit], "count": len(rows), "limit": limit},
               ctx, "m6-list-statements")


# ---------------------------------------------------------------------------
# 资产台账（B4）：模具/机器台账 + 效益分摊
# ---------------------------------------------------------------------------
#
# 修订模型（见 `m6_store` 的 m6_assets）：`asset_code` 下多 revision 共存，
# **生效行 = revision 最大且已 confirmed**；propose 只追加 trial 修订——所以
# 「先批准后落库」对台账同样成立：未批准的改动**不会改写账上已确认的原值**。

async def m6_upsert_asset_ledger(payload: dict[str, Any],
                                 ctx: dict[str, Any]) -> dict[str, Any]:
    """`upsert_asset_ledger`（**propose 段**）：追加一条资产修订草稿 + 请财务确认。

    入参即「这本台账该长什么样」（原值/购入日/寿命/残值）。已有**待确认修订**时拒绝
    再叠一版（``ASSET_PENDING_EXISTS``）——先让人把上一版批了或撤了，避免堆草稿。
    """
    asset_code = str(payload.get("asset_code") or "").strip()
    if not asset_code:
        return _fail("INVALID_INPUT", "需要 asset_code（资产编码）", ctx, "m6-asset")
    tenant_id = _tenant(ctx)
    store = _store(ctx)
    pending = store.pending_asset(asset_code, tenant_id)
    if pending is not None and not payload.get("replace_pending"):
        return _fail("ASSET_PENDING_EXISTS",
                     f"资产 {asset_code} 已有待确认修订 {pending['asset_id']}"
                     "（先由财务审批或撤回上一版，避免堆草稿）",
                     ctx, "m6-asset",
                     data={"asset_code": asset_code, "pending_asset_id": pending["asset_id"]})
    revision = int(payload.get("revision") or 0) or store.next_asset_revision(asset_code,
                                                                             tenant_id)
    asset_id = str(payload.get("asset_id") or "") or f"ASSET-{asset_code}-v{revision}"
    saved = store.save_asset(
        asset_id=asset_id, asset_code=asset_code, revision=revision,
        asset_name=str(payload.get("asset_name") or ""),
        category=str(payload.get("category") or ""),
        acquisition_cost=payload.get("acquisition_cost"),
        acquired_at=str(payload.get("acquired_at") or ""),
        useful_life_months=payload.get("useful_life_months"),
        salvage_value=payload.get("salvage_value"),
        source_ref=str(payload.get("source_ref") or ""),
        evidence={"revision": revision, "replaces": (pending or {}).get("asset_id"),
                  **(payload.get("evidence") if isinstance(payload.get("evidence"), dict)
                     else {})},
        tenant_id=tenant_id)
    if not saved.get("success"):
        return _fail(str(saved.get("code") or "SAVE_FAILED"),
                     f"资产台账草稿未落库（{saved.get('code')}）", ctx, "m6-asset",
                     data={"asset_code": asset_code, "asset_id": asset_id})
    effective = store.effective_asset(asset_code, tenant_id)
    data = {
        **saved, "asset_name": str(payload.get("asset_name") or ""),
        "acquisition_cost": _num_optional(payload.get("acquisition_cost")),
        # 约定字段：finance 门批准后由 `graph._apply_m6_asset_commit` 据此翻 confirmed
        "pending_asset_commit": True,
        "effective_revision": int(effective["revision"]) if effective else None,
        "effective_acquisition_cost": (_num_optional(effective.get("acquisition_cost"))
                                       if effective else None),
    }
    return _ok(data, ctx, "m6-asset", evidence=[
        _evidence(f"asset:{asset_code}@v{revision}",
                  f"资产台账修订草稿（trial）：{asset_code} v{revision}，"
                  f"原值 {data['acquisition_cost']}，待 finance 门批准后生效")])


async def m6_get_asset_ledger(payload: dict[str, Any],
                              ctx: dict[str, Any]) -> dict[str, Any]:
    """`get_asset_ledger`：读资产台账——**生效值与待确认草稿分开列**（不混淆）。

    这是三段式台账的读口：`effective` 才是账上事实，`pending` 只是等人批的草稿；
    成本/效益分摊只认 effective。
    """
    store = _store(ctx)
    tenant_id = _tenant(ctx)
    asset_code = str(payload.get("asset_code") or "").strip()
    if asset_code:
        revisions = store.asset_revisions(asset_code, tenant_id)
        if not revisions:
            return _fail("NOT_FOUND", f"资产 {asset_code} 不在台账中", ctx, "m6-get-asset",
                         data={"asset_code": asset_code})
        return _ok({"asset_code": asset_code,
                    "effective": store.effective_asset(asset_code, tenant_id),
                    "pending": store.pending_asset(asset_code, tenant_id),
                    "revisions": revisions}, ctx, "m6-get-asset")
    assets = [{"asset_code": code, "effective": store.effective_asset(code, tenant_id),
               "pending": store.pending_asset(code, tenant_id)}
              for code in store.list_asset_codes(tenant_id)]
    return _ok({"assets": assets, "count": len(assets)}, ctx, "m6-get-asset")


async def m6_compute_asset_benefit(payload: dict[str, Any],
                                   ctx: dict[str, Any]) -> dict[str, Any]:
    """`compute_asset_benefit`：把资产成本按口径分摊到产品（纯算数，不落库）。

    资产成本两级来源：显式 `asset_cost` 优先；未给则**取台账生效行的原值**
    （`acquisition_cost`，只看 effective、不看草稿），以 `asset_cost_source` 明示来源。
    台账里没有、也没显式成本的资产 → 进 `missing_asset_cost`（不编造成本）。
    """
    usage = payload.get("asset_usage") if isinstance(payload.get("asset_usage"), list) else []
    if not usage:
        return _fail("INVALID_INPUT", "需要 asset_usage（资产使用记录）", ctx, "m6-asset-benefit")
    ledger_costs: dict[str, float] = {}
    for row in _store(ctx).list_effective_assets(_tenant(ctx)):
        cost = _num_optional(row.get("acquisition_cost"))
        if cost is not None:
            ledger_costs[str(row.get("asset_code") or "")] = cost
    explicit = payload.get("asset_cost") if isinstance(payload.get("asset_cost"), dict) else {}
    asset_cost = {**ledger_costs, **{str(key): _num(value) for key, value in explicit.items()}}
    allocation_basis = payload.get("allocation_basis")
    basis = str(allocation_basis or DEFAULT_ALLOCATION_BASIS)
    result = compute_asset_benefit(usage, asset_cost, allocation_basis=basis)
    source = "explicit" if explicit else ("ledger" if ledger_costs else "missing")
    assumptions = {**_assumptions(None, None, None),
                   "allocation_basis": basis,
                   "allocation_basis_assumed": allocation_basis in (None, ""),
                   "asset_cost_source": source}
    data = {**result, "asset_cost": asset_cost, "asset_cost_source": source,
            "assumptions": assumptions, "ledger_asset_count": len(ledger_costs)}
    return _ok(data, ctx, "m6-asset-benefit", evidence=[
        _evidence("asset:benefit",
                  f"资产效益分摊：{len(result['allocations'])} 条分摊，"
                  f"成本来源={source}，cost_incomplete={result['cost_incomplete']}")])


# ---------------------------------------------------------------------------
# B5 库存财务视图（读，纯算数）
# ---------------------------------------------------------------------------

async def m6_get_inventory_finance_view(payload: dict[str, Any],
                                        ctx: dict[str, Any]) -> dict[str, Any]:
    """`get_inventory_finance_view`：财务口径库存视图（四态分账 + 在途），不落库。

    事实来源（**装配层给，勿手填**）：`inventory`（canonical `inventory` 已拆信封，含
    `stock_class`）与 `purchase_tracking_rows`（M4B 追踪行，用于判在途）。

    金额口径：`unit_costs`（材料编码 → 单价）**只能由装配层/调用方带入**——v2 的
    `inventory` 实体**没有价格字段**，取不到单价即进 `missing` + `cost_incomplete`
    （**不编造成本**）。态缺失/未知单列 `unknown`（不猜"有库存"）；追踪行 `arrival_status`
    为空单列 `unknown_status`（不猜在途）。

    **无门、也不开补数门**：库存事实还没落 canonical 属于业务进度，不是装配缺口；
    缺金额一律以 `cost_incomplete` + `missing` 表达。
    """
    raw_inventory = payload.get("inventory")
    inventory = ([row for row in raw_inventory if isinstance(row, dict)]
                 if isinstance(raw_inventory, list) else [])
    result = compute_inventory_finance_view(
        inventory,
        purchase_tracking_rows=payload.get("purchase_tracking_rows"),
        unit_costs=payload.get("unit_costs"),
        valuation_price_source=payload.get("valuation_price_source"),
    )
    counts = " ".join(f"{name}:{bucket['line_count']}"
                      for name, bucket in result["by_class"].items())
    data = {**result, "inventory_source": "explicit" if inventory else "missing"}
    return _ok(data, ctx, "m6-inventory-finance-view", evidence=[
        _evidence("inventory:finance-view",
                  f"库存财务视图：{len(inventory)} 行（{counts}），"
                  f"在途 {result['in_transit']['line_count']} 行，"
                  f"cost_incomplete={result['cost_incomplete']}")])


# ---------------------------------------------------------------------------
# B6 订单列表 + 工资两件（读，纯算数）
# ---------------------------------------------------------------------------
#
# 三件都**不落库、不开门**：与 `get_product_cost` / `allocate_expenses` 同类（纯算数）。
# 工资是敏感数据，但 M6 本层**不判授权**（与其余 M6 工具一致）——敏感读的保护属身份层
# 权限（现有 `worker.view` 同模式），不在门层；详见 rules.py 的 M6 注释块。


async def m6_list_orders(payload: dict[str, Any],
                         ctx: dict[str, Any]) -> dict[str, Any]:
    """`list_orders`：列 M0 canonical 订单（只读，不落库、无门）。

    事实来源：`orders`（装配层从 canonical `order` 读入并**拆信封**）。字段照 canonical
    `order`（`order_id`/`product_code`/`product_name`/`quantity`/`due_date`/
    `customer_name`/`unit_price`/`total_amount`）+ `line_count`（订单行数，canonical
    订单行不是独立实体时恒为 0，不假装有行）。

    过滤面：`product_code` / `customer_name` 精确匹配、`period` 按 `due_date` 前缀匹配
    （**canonical `order` 没有 `order_date` 字段**，不拿别的字段冒充下单日期）；
    `limit` 截断（默认 50）。
    """
    raw_orders = payload.get("orders")
    orders = ([row for row in raw_orders if isinstance(row, dict)]
              if isinstance(raw_orders, list) else [])
    product_code = str(payload.get("product_code") or "")
    customer = str(payload.get("customer_name") or "")
    period = str(payload.get("period") or "")
    rows: list[dict[str, Any]] = []
    for order in orders:
        if product_code and str(order.get("product_code") or "") != product_code:
            continue
        if customer and str(order.get("customer_name") or "") != customer:
            continue
        if period and not str(order.get("due_date") or "").startswith(period):
            continue
        lines = order.get("lines") if isinstance(order.get("lines"), list) else []
        rows.append({
            "order_id": str(order.get("order_id") or ""),
            "product_code": str(order.get("product_code") or ""),
            "product_name": str(order.get("product_name") or ""),
            "quantity": order.get("quantity"),
            "due_date": str(order.get("due_date") or ""),
            "customer_name": str(order.get("customer_name") or ""),
            "unit_price": order.get("unit_price"),
            "total_amount": order.get("total_amount"),
            "line_count": len(lines),
        })
    total = len(rows)
    rows = rows[:_limit(payload, 50)]
    return _ok({"tenant_id": _tenant(ctx), "count": total, "returned": len(rows),
                "orders": rows,
                "source": "canonical" if orders else "missing",
                "missing": [] if orders else [{"reason": "missing_orders"}]},
               ctx, "m6-list-orders", evidence=[
                   _evidence("order:list",
                             f"订单列表：canonical 读到 {len(orders)} 条，过滤后 {total} 条、"
                             f"返回 {len(rows)} 条")])


async def m6_calculate_piece_pay(payload: dict[str, Any],
                                 ctx: dict[str, Any]) -> dict[str, Any]:
    """`calculate_piece_pay`：计件工资 = Σ(合格数量 × 单价)，纯算数不落库。

    ⚠️ **v2 没有 canonical 工资事实面**：老仓的报工取自 canonical `usage_log`、单价取自
    `piece_rate`，而 v2 的 `canonical_schema`/`ENTITY_TYPES` **一张都没有**（实测 0 命中）
    → `report_events` 与 `piece_rates` **必须显式给出**。

    `report_events` 形状：[{worker_id, station_code, product_code, quantity_report,
    scrap?, report_date}]。缺单价的报工进 `missing`（`missing_piece_rate`）——**不按 0 计、
    也不编造单价**（编错单价 = 多发/少发工资）；**更不拿别的实体顶替**：老仓明确禁止把
    资产使用数量 `quantity` 当报工数量，此处同样不从 `production_daily_report` 推断。
    """
    raw_events = payload.get("report_events")
    raw_rates = payload.get("piece_rates")
    result = compute_piece_pay(raw_events, raw_rates)
    rate_count = len(raw_rates) if isinstance(raw_rates, list) else 0
    data = {**result,
            "report_event_count": len(raw_events) if isinstance(raw_events, list) else 0,
            "piece_rate_count": rate_count,
            "piece_rate_source": "explicit" if rate_count else "missing"}
    return _ok(data, ctx, "m6-calculate-piece-pay", evidence=[
        _evidence("pay:piece",
                  f"计件工资：{data['report_event_count']} 条报工 / {rate_count} 条单价，"
                  f"算出 {len(result['totals'])} 人，missing {len(result['missing'])} 条，"
                  f"cost_incomplete={result['cost_incomplete']}")])


async def m6_calculate_monthly_pay(payload: dict[str, Any],
                                   ctx: dict[str, Any]) -> dict[str, Any]:
    """`calculate_monthly_pay`：月薪 = 月薪标准 + 加班费 − 缺勤扣款 + 计件工资（纯算数）。

    事实值：`salary_standards`（worker_id → 月薪）/ `attendance`（worker_id →
    `overtime_hours`/`absence_hours`）/ `piece_pay`（worker_id → 计件合计，通常取
    `calculate_piece_pay` 的 `totals`）。
    口径值：**加班倍数/计薪天数/每日工时未显式给时取 `m6_defaults`** 并在 `assumptions`
    标 `assumed=true`（这三项在 `PENDING_FINANCE_CONFIRMATION` 里待工厂财务确认）。

    仅把**显式给出**的口径传给内核——传 `None` 会让内核把默认值算成 0（时薪 0 →
    加班费/缺勤扣款全 0），那是静默算错，不是"用默认值"。
    """
    explicit = {key: payload.get(key) for key in ("overtime_multiplier", "work_days",
                                                 "hours_per_day")}
    given = {key: value not in (None, "") for key, value in explicit.items()}
    result = compute_monthly_pay(
        payload.get("salary_standards"),
        payload.get("attendance"),
        payload.get("piece_pay"),
        **{key: value for key, value in explicit.items() if given[key]},
    )
    defaults = {"overtime_multiplier": DEFAULT_OVERTIME_MULTIPLIER,
                "work_days": DEFAULT_WORK_DAYS,
                "hours_per_day": DEFAULT_HOURS_PER_DAY}
    data = {**result,
            "worker_count": len(result["rows"]),
            **{key: (explicit[key] if given[key] else defaults[key]) for key in given},
            "assumptions": {
                "overtime_multiplier_assumed": not given["overtime_multiplier"],
                "work_days_assumed": not given["work_days"],
                "hours_per_day_assumed": not given["hours_per_day"],
                "pending_finance_confirmation": list(PENDING_FINANCE_CONFIRMATION),
            }}
    return _ok(data, ctx, "m6-calculate-monthly-pay", evidence=[
        _evidence("pay:monthly",
                  f"月薪工资：{data['worker_count']} 人，"
                  f"口径 assumed="
                  f"{not given['overtime_multiplier']}/{not given['work_days']}/"
                  f"{not given['hours_per_day']}，"
                  f"cost_incomplete={result['cost_incomplete']}")])


M6_HANDLERS: dict[str, Any] = {
    "save_costing_snapshot": m6_save_costing_snapshot,
    "confirm_costing_snapshot": m6_confirm_costing_snapshot,
    "close_month_costing": m6_close_month_costing,
    "list_costing_snapshots": m6_list_costing_snapshots,
    "get_costing_snapshot": m6_get_costing_snapshot,
    "list_month_costing": m6_list_month_costing,
    # 内核读工具（纯算数，不写库，因此无门）
    "get_product_cost": m6_get_product_cost,
    "audit_order_cost": m6_audit_order_cost,
    "allocate_expenses": m6_allocate_expenses,
    # 单据台账（B2）：报价单 / 对账单——写走 propose（trial 草稿）+ finance 门
    "generate_quotation": m6_generate_quotation,
    "save_quotation": m6_save_quotation,
    "list_quotations": m6_list_quotations,
    "get_quotation": m6_get_quotation,
    "save_statement": m6_save_statement,
    "list_statements": m6_list_statements,
    # 凭据（B3）：送货单只读回读 + 对账明细依据生成（纯算数，不落库）
    "get_delivery_note": m6_get_delivery_note,
    "generate_statement": m6_generate_statement,
    # 资产台账（B4）：upsert 走 propose（trial 修订）+ finance 门；读与分摊纯算数
    "get_asset_ledger": m6_get_asset_ledger,
    "upsert_asset_ledger": m6_upsert_asset_ledger,
    "compute_asset_benefit": m6_compute_asset_benefit,
    # 库存财务视图（B5）：纯算数读，不落库、无门
    "get_inventory_finance_view": m6_get_inventory_finance_view,
    # 订单列表 + 工资两件（B6）：纯算数读，不落库、无门
    "list_orders": m6_list_orders,
    "calculate_piece_pay": m6_calculate_piece_pay,
    "calculate_monthly_pay": m6_calculate_monthly_pay,
}
