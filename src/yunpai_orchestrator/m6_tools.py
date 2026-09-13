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
    compute_expense_allocation,
    compute_material_cost,
    compute_process_cost,
)
from .m6_defaults import (
    DEFAULT_MIN_MARGIN_RATE,
    DEFAULT_VALUATION_PRICE_SOURCE,
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
    return {"success": True, "data": data, "errors": [],
            "trace_id": _trace(ctx, suffix), "evidence": evidence or []}


def _fail(code: str, message: str, ctx: dict[str, Any] | None, suffix: str,
          data: dict[str, Any] | None = None) -> dict[str, Any]:
    """硬失败信封（``success=False``）——不得当成功吞掉（reviewer 侧规则同步 fail）。"""
    return {"success": False, "code": code,
            "errors": [{"code": code, "message": message, "details": []}],
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
    total_cost = round(unit_cost * (quantity or 0.0), 4)

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
}
