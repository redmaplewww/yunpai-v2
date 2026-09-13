"""M6 缺料价源解析（R1a / 账本 D-007）——从 M4 采购追踪取「实际采购单价」。

## 为什么是采购追踪，而不是采购单

原计划（照老仓 `yunpai-39092` 口径写）把缺料价源指向「M4 采购单」。**v2 实测不成立**：
`m4_store.purchase_order_item_to_json` 的行项只有
``item_code / internal_material_no / item_name / quantity / unit / status / remark``
——**采购单根本不含价格**。

v2 里真正的单价在 **M4B 采购追踪行**上：`m4b_store` 表含 ``unit_price``/``currency``，
由 `parse_m4_supplier_reply`（`m4_supplier_local`）从供应商回复文本里解析单价后落库，
读口为 **`list_m4_tracking`**（`m4_tracking_local._tracking_item` 暴露 `unit_price`）。

## 本模块的定位

只做**确定性解析**（纯函数、不碰库、不 import 任何 v2 store / registry），遵守：

- **D9**：M6 不自己开 M4Store；数据由装配层（`orchestration_bridge`）以事实传入；
- **红线（不编造）**：`unit_price` 不可解析的行**不进入价源**，计入 `missing`，
  由计算层（`m6_cost`）标 `cost_incomplete` —— 绝不用默认价或 0 冒充实际采购价；
- **D5**：本价源**只供「缺料行」**使用；有库存的行走库存口径（`bom_price`），
  由计算层按齐套快照判定，本模块不做该判定。

来源：老仓无此模块（老仓 `_pick_rate` 是按报工日挑计件单价，语义不同）；本模块为
v2 新增，规则见《M6-三项决策说明-20260913.md》说明三 §3.4。
"""

from __future__ import annotations

from typing import Any


def _num_optional(value: Any) -> float | None:
    """解析数值；缺失或不可解析返回 None（调用方据此判定缺价，不编造）。"""
    if value in (None, ""):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price


def _item_keys(item_id: Any, item_lookup: dict[str, dict[str, Any]]) -> list[str]:
    """追踪行 → 可用的物料键（优先内部料号，其次采购料号）。

    为什么给多个键：M6 侧 BOM 行的物料键来源不止一种（canonical `material_code`
    可能等于内部料号或采购料号），全部登记让消费方按自己手上的键命中，避免因键名
    不同被当成「没有价」——这与 G2 的 `files`/`documents` 别名同一条思路。

    **只有**在 `item_lookup` 里拿不到任何真实料号时，才回落到行项 id 作为键
    （否则数字 id 会与恰好同名的物料码误命中）。
    """
    ref = str(item_id or "")
    item = item_lookup.get(ref) if ref else None
    keys: list[str] = []
    if isinstance(item, dict):
        for key in ("internal_material_no", "item_code"):
            value = str(item.get(key) or "").strip()
            if value and value not in keys:
                keys.append(value)
    if not keys and ref:
        keys.append(ref)
    return keys


def _is_later(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    """同键多行取新：先比 `promised_date`（字典序即日期序），同日比 `tracking_id`。"""
    cand_date = str(candidate.get("promised_date") or "")
    curr_date = str(current.get("promised_date") or "")
    if cand_date != curr_date:
        return cand_date > curr_date
    return int(candidate.get("tracking_id") or 0) > int(current.get("tracking_id") or 0)


def purchase_prices_from_tracking(
    tracking_rows: Any,
    item_lookup: Any = None,
) -> dict[str, Any]:
    """从 `list_m4_tracking` 行构造 ``{物料键: 价格事实}``。

    ``tracking_rows``: `list_m4_tracking` 返回的 items（含 ``purchase_order_no`` /
        ``purchase_order_item_id`` / ``supplier_name`` / ``promised_date`` /
        ``unit_price`` / ``currency``）。
    ``item_lookup``:   ``{purchase_order_item_id: {item_code, internal_material_no}}``
        —— 来自 M4 采购单行项；缺该映射时只能回落到行项 id 键（仍可用，但消费方
        需用同一 id 查找）。

    返回 ``{"prices": {...}, "missing": [...], "cost_incomplete": bool}``：

    - ``prices[key]`` = ``{"unit_price": float, "currency": str, "supplier_name": str,
      "promised_date": str, "source_ref": "tracking:<id>@<采购单号>", "item_id": str}``
      —— 每个价都带 ``source_ref``，供 M6 把来源证据写进 `m6_cost_lines.source_ref`；
    - ``missing``：无行 / 单价缺失或不可解析 的行（``reason`` 区分
      ``missing_tracking_rows`` / ``unparsable_unit_price``）；
    - ``cost_incomplete``：有 missing 即为 True（缺料价算不全，交由计算层标注）。
    """
    rows = tracking_rows if isinstance(tracking_rows, list) else []
    lookup = item_lookup if isinstance(item_lookup, dict) else {}

    prices: dict[str, dict[str, Any]] = {}
    missing: list[dict[str, Any]] = []
    if not rows:
        missing.append({"reason": "missing_tracking_rows"})

    for row in rows:
        if not isinstance(row, dict):
            missing.append({"reason": "invalid_row"})
            continue
        item_id = row.get("purchase_order_item_id")
        price = _num_optional(row.get("unit_price"))
        if price is None:
            missing.append({
                "reason": "unparsable_unit_price",
                "purchase_order_no": str(row.get("purchase_order_no") or ""),
                "purchase_order_item_id": item_id,
            })
            continue
        fact = {
            "unit_price": price,
            "currency": str(row.get("currency") or ""),
            "supplier_name": str(row.get("supplier_name") or ""),
            "promised_date": str(row.get("promised_date") or ""),
            "source_ref": f"tracking:{row.get('id')}@{row.get('purchase_order_no') or ''}",
            "tracking_id": row.get("id"),
            "item_id": str(item_id or ""),
        }
        for key in _item_keys(item_id, lookup):
            current = prices.get(key)
            if current is None or _is_later(fact, current):
                prices[key] = fact

    return {
        "prices": prices,
        "missing": missing,
        "cost_incomplete": bool(missing),
    }


def price_for(price_facts: Any, material_code: Any) -> dict[str, Any] | None:
    """按物料键取价（缺失返回 None —— 调用方据此标 ``cost_incomplete``，不回落默认价）。"""
    prices = (price_facts or {}).get("prices") if isinstance(price_facts, dict) else None
    if not isinstance(prices, dict):
        return None
    return prices.get(str(material_code or ""))
