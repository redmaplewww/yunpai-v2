"""确定性跨模块桥接（任务书 T2）：主链装配不再在 graph._payload_for 用大段
临时字典重建事实，而是由本模块按上游 outputs/canonical 回读装配 payload。

原则：
- M1->M2  从 m1.document.v2 与已发布 canonical order/product 读取；
- M2->M3  只消费已批准 BOM/route，库存来自库存事实快照，不接受隐式默认；
- M3->M4  消费 M3 handoff envelope（revision/checksum），供应商/交期由
          M4 供应/采购事实解析；
- M4->M5  消费 M4 supply snapshot（PO/ETA/收货/QC），不忽略 M4 输出；
- 只调用 ToolRegistry 已注册工具，传播 TaskID/tenant/site/trace/幂等键；
- 缺任何权威输入 -> 结构化 BLOCKED_INPUT
  {success:false, code:"BLOCKED_INPUT", errors:[{code,message,details}],
   data:{missing_fields, source_module, required_tool, recovery, snapshot_kind?},
   trace_id, evidence:[...]}。

本桥接只服务于受控 workflow（m1_m5_document_to_plan / canonical_to_m5）；
旧 m0_m5 已删除，不再有兼容路径。
"""
from __future__ import annotations

"""[收编] 旧 orchestration_bridge（书二 §5.1）：W913 验证过的确定性装配实现。

v2 语义变化：本模块不再只服务 BRIDGED_WORKFLOWS——worker.assembler 对 workflow 与
free 路径统一调用 bridge_payload（这正是修复旧 free 死点的关键）。除注释与本 docstring
外与旧实现逐行一致；行为修改必须走书二 §5.1 的规则表评审。
"""


import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any

from .fact_gateway import read_entities
from .models import RunState, summarize
from .planning_snapshot import SIX_CLASS_BUNDLE, blocked_input, snapshot_counts

BRIDGED_WORKFLOWS = ("m1_m5_document_to_plan", "canonical_to_m5")

#: 缺权威输入时的恢复动作模板（人类 Gate 可见）。
_RECOVERY = "请补充 {fields} 的权威来源（canonical 回读/已批准事实），再重试本步骤"


def _now_task(state: RunState) -> str:
    return str(state.get("task_id") or "task")


def _trace(state: RunState, tool: str) -> str:
    return f"{_now_task(state)}:{tool}"


def _data(result: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    data = result.get("data")
    return data if isinstance(data, dict) else result


def blocked(state: RunState, *, source_module: str, tool: str,
            missing_fields: list[str], required_tool: str = "",
            recovery: str = "") -> dict[str, Any]:
    recovery = recovery or _RECOVERY.format(fields="、".join(sorted(set(missing_fields))) or "对应事实")
    return blocked_input(
        missing_fields=missing_fields,
        source_module=source_module,
        required_tool=required_tool or tool,
        recovery=recovery,
    )


# ---------------------------------------------------------------------------
# 上游读取辅助：outputs 按 tool 名取 result，再取 data/子路径。
# ---------------------------------------------------------------------------

def output_data(state: RunState, tool: str) -> dict[str, Any]:
    return _data(state.get("outputs", {}).get(tool))


def _pick(*values: Any) -> Any:
    for value in values:
        if isinstance(value, dict) and value:
            return value
        if isinstance(value, list) and value:
            return value
    return None


def _normalize_order(order: dict[str, Any]) -> dict[str, Any]:
    """订单号字段归一：order_id 与 order_number 互为回填（M1 有时只给 order_number）。"""
    if not isinstance(order, dict):
        return order
    out = dict(order)
    if not str(out.get("order_id") or "").strip():
        candidate = out.get("order_number") or out.get("order_no")
        if candidate:
            out["order_id"] = str(candidate)
    if not str(out.get("order_number") or "").strip():
        candidate = out.get("order_id")
        if candidate:
            out["order_number"] = str(candidate)
    return out


def read_order(state: RunState) -> dict[str, Any]:
    """M1/m0 输出的订单权威字段：优先 canonical/M1，而不是最初 request。"""
    from_m1 = output_data(state, "ingest_document")
    order = _pick(
        from_m1.get("order"), from_m1.get("extraction", {}).get("order"),
        from_m1.get("document", {}).get("header"),
    )
    supplement = from_m1.get("semantic_supplement")
    supplement_document = supplement.get("document") if isinstance(supplement, dict) else None
    supplement_header = supplement_document.get("header") if isinstance(supplement_document, dict) else None
    request = state.get("request", {})
    # A replay may carry a source-backed structured document under
    # request.document (or the legacy request.order) while M1's external
    # parser only returns the raw document/header.  Merge that explicit,
    # user-supplied fact without allowing empty values to overwrite M1.
    request_document = request.get("document") or request.get("order") or {}
    request_product = request.get("product") or {}
    explicit: dict[str, Any] = {}
    for key in ("order_id", "order_number", "product_code", "product_name", "quantity", "order_qty", "due_date", "order_date", "customer"):
        value = request.get(key)
        if value in (None, "") and isinstance(request_document, dict):
            value = request_document.get(key)
        if value in (None, "") and key in {"product_code", "product_name"} and isinstance(request_product, dict):
            value = request_product.get(key)
        if value not in (None, ""):
            explicit[key] = value
    if isinstance(order, dict):
        if isinstance(supplement_header, dict):
            # Keep external M1 values and use only non-empty, source-backed
            # supplement fields for gaps. This makes line model -> header
            # product_code explicit without overwriting an external fact.
            merged = dict(order)
            for key, value in supplement_header.items():
                if merged.get(key) in (None, "") and value not in (None, ""):
                    merged[key] = value
            merged.update({key: value for key, value in explicit.items() if merged.get(key) in (None, "")})
            return _normalize_order(merged)
        return _normalize_order({**order, **{key: value for key, value in explicit.items() if order.get(key) in (None, "")}})
    if isinstance(supplement_header, dict):
        return _normalize_order({**supplement_header, **{key: value for key, value in explicit.items() if supplement_header.get(key) in (None, "")}})
    return _normalize_order(explicit)


def read_lines(state: RunState) -> list[dict[str, Any]]:
    from_m1 = output_data(state, "ingest_document")
    lines = _pick(
        from_m1.get("lines"),
        from_m1.get("document", {}).get("lines"),
        from_m1.get("extraction", {}).get("lines"),
    )
    if not isinstance(lines, list) or not lines:
        supplement = from_m1.get("semantic_supplement")
        supplement_document = supplement.get("document") if isinstance(supplement, dict) else None
        lines = supplement_document.get("lines") if isinstance(supplement_document, dict) else None
    return [item for item in lines if isinstance(item, dict)] if isinstance(lines, list) else []


def read_approved_bom(state: RunState) -> list[dict[str, Any]]:
    """只消费 reviewer 已批准（engineering Gate 通过）的 M2 BOM 行。"""
    m2 = output_data(state, "run_bom_sop_workflow")
    generation = m2.get("bom_generation")
    if not isinstance(generation, dict):
        return []
    if str(generation.get("approval_status") or "") != "approved":
        # graph._apply_approval 批准 engineering Gate 时写入 approval_status。
        return []
    lines = generation.get("bom_lines")
    return [item for item in lines if isinstance(item, dict)] if isinstance(lines, list) else []


def _bom_lines_from_overview(payload: dict[str, Any], product_code: str) -> list[dict[str, Any]]:
    """Extract only an approved BOM for the requested product from M0 overview."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    indexes = data.get("indexes")
    boms = indexes.get("boms") if isinstance(indexes, dict) else None
    if not isinstance(boms, list):
        return []
    for item in boms:
        entity = item.get("entity") if isinstance(item, dict) else None
        if not isinstance(entity, dict) or (str(entity.get("review_status") or "") not in {"", "approved"} and str(entity.get("status") or "") != "active"):
            continue
        attrs = entity.get("attributes")
        if not isinstance(attrs, dict):
            continue
        entity_product = str(attrs.get("product_code") or entity.get("business_key") or "")
        if entity_product != product_code:
            continue
        lines = attrs.get("lines")
        if not isinstance(lines, list):
            continue
        normalized: list[dict[str, Any]] = []
        for index, line in enumerate(lines, start=1):
            if not isinstance(line, dict) or not str(line.get("material_code") or "").strip():
                continue
            normalized.append({
                "line_id": str(line.get("line_id") or line.get("line_no") or f"{product_code}::BOM-{index}"),
                "material_code": str(line.get("material_code") or ""),
                "material_name": str(line.get("material_name") or line.get("material_code") or ""),
                "quantity_per": line.get("quantity_per", line.get("quantity", 0)),
                "quantity": line.get("quantity", line.get("quantity_per", 0)),
                "uom": str(line.get("uom") or line.get("unit") or "pcs"),
                "loss_rate": line.get("loss_rate", 0),
                "requires_procurement": line.get("requires_procurement", True),
            })
        return normalized
    return []


def _bom_lines_from_entities(state: RunState, product_code: str) -> list[dict[str, Any]]:
    """从 M0 catalog/entities（entity_type=bom）读回该产品的 BOM 行。

    本地 M0 后端（m0_backend）没有 products/{code}/overview 端点，只有
    catalog/entities；这里按 product_code 精确匹配并归一化 lines。
    """
    entities = _read_m0_entities(state, "bom")
    for item in entities:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
        identity = item.get("identity") if isinstance(item.get("identity"), dict) else {}
        code = str(identity.get("business_key") or payload.get("product_code") or "")
        if code != product_code:
            continue
        lines = payload.get("lines")
        if not isinstance(lines, list):
            return []
        normalized: list[dict[str, Any]] = []
        for index, line in enumerate(lines, start=1):
            if not isinstance(line, dict) or not str(line.get("material_code") or "").strip():
                continue
            normalized.append({
                "line_id": str(line.get("line_id") or line.get("line_no") or f"{product_code}::BOM-{index}"),
                "material_code": str(line.get("material_code") or ""),
                "material_name": str(line.get("material_name") or line.get("material_code") or ""),
                "quantity_per": line.get("quantity_per", line.get("quantity", 0)),
                "quantity": line.get("quantity", line.get("quantity_per", 0)),
                "uom": str(line.get("uom") or line.get("unit") or "pcs"),
                "loss_rate": line.get("loss_rate", 0),
                "requires_procurement": line.get("requires_procurement", True),
            })
        return normalized
    return []


def _route_steps_from_entities(state: RunState, product_code: str) -> list[dict[str, Any]]:
    """从 M0 catalog/entities（entity_type=document, role=SOP）读回 SOP 工序。

    匹配策略（确定性，不编造）：先精确产品码匹配；否则若只有一份 SOP
    （或产品码为通用类别如 HDMI），作为家族 SOP 回退。
    """
    entities = _read_m0_entities(state, "document")
    sops: list[dict[str, Any]] = []
    for item in entities:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
        if str(payload.get("role") or "").upper() not in {"SOP", "INSTRUCTION"}:
            continue
        steps = payload.get("route_steps")
        if not isinstance(steps, list) or not steps:
            continue
        codes = payload.get("product_codes")
        codes = [str(c) for c in codes] if isinstance(codes, list) else []
        sops.append({"codes": codes, "steps": steps})
    if not sops:
        return []
    for sop in sops:
        if product_code in sop["codes"]:
            return sop["steps"]
    # 无精确产品码匹配：优先取「无产品码限制」的通用 SOP，否则回退第一份
    # （家族 SOP，如 HDMI/HDTV 系列；多份重复 SOP 也走这里）。
    for sop in sops:
        if not sop["codes"]:
            return sop["steps"]
    return sops[0]["steps"]


def _read_m0_product_overview(state: RunState, product_code: str) -> dict[str, Any]:
    """Best-effort M0 canonical read; callers fail closed when it is unavailable."""
    base_url = str(os.getenv("M0_URL") or "").rstrip("/")
    tenant_id = str(state.get("tenant_id") or "").strip()
    if not base_url or not tenant_id or not product_code:
        return {}
    url = f"{base_url}/api/m0/catalog/products/{product_code}/overview"
    request = Request(url, headers={"X-Tenant-ID": tenant_id, "X-Yunpai-Tenant": tenant_id, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=float(os.getenv("M0_CATALOG_TIMEOUT_S", "3"))) as response:
            body = response.read()
        parsed = json.loads(body.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        return {}


def _m0_local_entities(entity_type: str, tenant_id: str, db_path: str) -> list[dict[str, Any]]:
    """进程内读 M0 canonical（``YUNPAI_M0_DB``）——唯一允许的本地读口。

    父会话 M0 读口裁决（2026-09-09）：一律走 ``m0_backend.M0Store.list_entities``，
    **禁止**直连 sqlite3 读 ``canonical_entities``；形状归一（``payload.attributes``
    并入顶层、顶层优先，兼容 SOP 文档把 route_steps 放在 attributes 下的形状；
    设备/工位/人员/日历在 M0 是平铺形状，归一化对它们是无操作）统一由
    ``fact_gateway.read_entities`` 实现（集成收口 1.1，单点化）。
    """
    return read_entities(entity_type, tenant_id, db_path)


def _read_m0_entities(state: RunState, entity_type: str) -> list[dict[str, Any]]:
    """按 entity_type 读回 M0 canonical 已批准实体（payload_json 列表）。

    M0 canonical 是通用 envelope；设备/工位/人员/模治具/日历分别以
    equipment_master/station_master/worker_master/tooling_master/production_calendar
    落库。读不到或未审批时返回空列表，调用方失败关闭。

    读口优先级（集成口径 = M0 与 M5 并集）：显式配置 ``YUNPAI_M0_DB`` 时读该本地库
    （进程内，不依赖 ``M0_URL``）；未配置时若 ``M0_URL`` 也未配置则读默认
    ``runtime/yunpai-m0.sqlite``（库文件不存在返回空、不创建空库），配置了
    ``M0_URL`` 则走 HTTP。本地分支一律经 ``_m0_local_entities``（``M0Store.list_entities``
    + ``attributes`` 并入顶层）。
    """
    from pathlib import Path

    base_url = str(os.getenv("M0_URL") or "").rstrip("/")
    tenant_id = str(state.get("tenant_id") or "").strip() or "default"
    # 本地 canonical 读口（M0 与 M5 两分片口径并集；两者都经 M0Store.list_entities —— 裁决 R1，
    # 且在读取处把 payload.attributes 并入顶层 —— 裁决 R2）：
    #   * M0（S1 C4）：显式配置 YUNPAI_M0_DB 时进程内读 canonical，不再依赖 M0_URL HTTP 假站位；
    #   * M5：M0_URL 未配置时读本地库（YUNPAI_M0_DB，默认 runtime/yunpai-m0.sqlite），
    #     库文件不存在则返回空、**不创建空库**。
    # 集成口径：显式 YUNPAI_M0_DB 优先（比隐式 HTTP 更确定）；本地分支统一走
    # `_m0_local_entities`（M5 版：attributes 归一化 + M0Store 读口），因此 M0 的
    # `m0_facts.list_entities` 调用点被其覆盖（语义等价且多一层 R2 归一化）。
    local_db = str(os.getenv("YUNPAI_M0_DB") or "")
    if local_db or not base_url:
        local_db = local_db or "runtime/yunpai-m0.sqlite"
        if not Path(local_db).exists():
            return []
        try:
            return _m0_local_entities(entity_type, tenant_id, local_db)
        except Exception:  # noqa: BLE001 —— 桥接读失败按「读不到」处理，调用方失败关闭
            return []
    url = f"{base_url}/api/m0/catalog/entities?entity_type={entity_type}&tenant_id={tenant_id}"
    request = Request(url, headers={"X-Tenant-ID": tenant_id, "X-Yunpai-Tenant": tenant_id, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=float(os.getenv("M0_CATALOG_TIMEOUT_S", "3"))) as response:
            body = response.read()
        parsed = json.loads(body.decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        return []
    data = parsed.get("data") if isinstance(parsed, dict) else parsed
    entities = data.get("entities") if isinstance(data, dict) else None
    if not isinstance(entities, list):
        return []
    out: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        payload = entity.get("payload_json")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if not isinstance(payload, dict):
            continue
        key = entity.get("canonical_key")
        out.append({"canonical_key": key, **payload})
    return out


def _normalize_status(value: Any) -> str:
    """把 M0 主数据的人读状态归一化成 M5 resource_snapshot 的枚举。"""
    text = str(value or "").upper()
    return {
        "AVAILABLE": "ACTIVE", "ACTIVE": "ACTIVE", "IDLE": "ACTIVE",
        "MAINTENANCE": "MAINTENANCE", "DOWN": "INACTIVE", "DISABLED": "INACTIVE",
        "INACTIVE": "INACTIVE",
    }.get(text, "ACTIVE")


def _resource_section(entities: list[dict[str, Any]], key_field: str) -> list[dict[str, Any]]:
    """把 canonical 实体映射成 M5 resource_snapshot 子段（字段名与 M5 一致，仅剥离 canonical_key 并归一化 status）。"""
    items: list[dict[str, Any]] = []
    for entity in entities:
        item = {k: v for k, v in entity.items() if k != "canonical_key"}
        if not item.get(key_field):
            item[key_field] = str(entity.get("canonical_key") or "")
        if "status" in item:
            item["status"] = _normalize_status(item["status"])
        items.append(item)
    return items


def _flatten_entities(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 M0 canonical 实体归一化成「业务字段在顶层」的形状。

    兼容两种落库形状：
    - agent 识别的 m0.ingest.v1 envelope（业务字段嵌套在 ``payload``）；
    - 旧 business_catalog 主数据（业务字段直接平铺在顶层）。
    """
    out: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        payload = entity.get("payload")
        if isinstance(payload, dict):
            item = dict(payload)
        else:
            item = {k: v for k, v in entity.items() if k != "canonical_key"}
        item["canonical_key"] = entity.get("canonical_key") or ""
        out.append(item)
    return out


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso_date(value: Any) -> str:
    """把各种日期（含中文 2026年7月10日 / 空）归一化成 ISO date；无效给远期默认。"""
    import re as _re

    text = str(value or "").strip()
    m = _re.search(r"(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})", text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return "2099-12-31"


def _normalize_bom_qty_lines(bom_lines: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 BOM 行归一化成 M3 合同形状，并按「关键字段」降级。

    - 关键字段：material_code（缺失行直接丢弃）与 qty_per（必须 > 0，M3 外部
      合同硬校验）。qty_per 缺失/非正数（线材按长度、包材按装箱数量未填）的行
      降级为 deferred，不阻塞整单，且逐行保留来源与原因，绝不编造数值。
    - 非关键字段：uom/loss_rate/requires_procurement 缺失时补合理默认。
    """
    lines: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for index, line in enumerate(bom_lines, start=1):
        if not isinstance(line, dict):
            continue
        material_code = str(line.get("material_code") or "").strip()
        if not material_code:
            continue
        raw_qty = line.get("qty_per", line.get("quantity_per", line.get("quantity")))
        qty = _num(raw_qty, None)  # type: ignore[arg-type]
        if qty is None or qty <= 0:
            deferred.append({
                "material_code": material_code,
                "material_name": str(line.get("material_name") or material_code or ""),
                "raw_quantity": raw_qty,
                "uom": str(line.get("uom") or line.get("unit") or "pcs"),
                "reason": "qty_per 缺失或非正数（线材按长度/规格、包材按装箱数量，源表未填数值用量）",
            })
            continue
        lines.append({
            "line_id": str(line.get("line_id") or f"line-{index}"),
            "material_code": material_code,
            "material_name": str(line.get("material_name") or material_code or ""),
            "qty_per": qty,
            "uom": str(line.get("uom") or line.get("unit") or "pcs"),
            "loss_rate": _num(line.get("loss_rate"), 0.0),
            "requires_procurement": line.get("requires_procurement", True),
        })
    return lines, deferred


def read_m5_resource_facts(state: RunState) -> dict[str, Any] | None:
    """从 M0 canonical 读回设备/工位/人员/模治具，组装 M5 resource_snapshot。

    兼容两种实体命名：旧 business_catalog 的 *_master，与 agent 识别的
    equipment/station/worker/tooling（取并集，允许不全）。
    """
    equipment = _flatten_entities(_read_m0_entities(state, "equipment_master") + _read_m0_entities(state, "equipment"))
    stations = _flatten_entities(_read_m0_entities(state, "station_master") + _read_m0_entities(state, "station"))
    workers = _flatten_entities(_read_m0_entities(state, "worker_master") + _read_m0_entities(state, "worker"))
    tooling = _flatten_entities(_read_m0_entities(state, "tooling_master") + _read_m0_entities(state, "tooling"))
    if not (equipment or stations or workers or tooling):
        return None
    return {
        "snapshot_id": f"SNAP-RES-M0-{_now_task(state)}",
        "revision": 1,
        "equipment": _resource_section(equipment, "equipment_code"),
        "stations": _resource_section(stations, "station_code"),
        "persons": _resource_section(workers, "person_code"),
        "tooling": _resource_section(tooling, "tooling_code"),
    }


def read_m5_calendar_facts(state: RunState) -> dict[str, Any] | None:
    """从 M0 canonical 读回生产日历，组装 M5 calendar_snapshot。"""
    calendars = _flatten_entities(_read_m0_entities(state, "production_calendar") + _read_m0_entities(state, "calendar"))
    if not calendars:
        return None
    intervals: list[dict[str, Any]] = []
    unavailability: list[dict[str, Any]] = []
    for entity in calendars:
        item = {k: v for k, v in entity.items() if k != "canonical_key"}
        if isinstance(item.get("working_intervals"), list):
            intervals.extend(item["working_intervals"])
        if isinstance(item.get("unavailability"), list):
            unavailability.extend(item["unavailability"])
    if not intervals:
        return None
    return {
        "snapshot_id": f"SNAP-CAL-M0-{_now_task(state)}",
        "revision": 1,
        "working_intervals": intervals,
        "unavailability": unavailability,
    }


def _is_degraded(request: dict[str, Any]) -> bool:
    """降级运行开关：人工显式补充 degraded/legacy_preview 时，M4/M5 用非权威默认跑通。"""
    return bool(request.get("degraded") or request.get("legacy_preview"))


def _planning_horizon_date(state: RunState) -> Any:
    """排产地平线（显式 ``planning_start`` > 订单交期）→ ``date``；取不到返回 None。

    只做「有没有一个可解析的日期」的判定，不做默认值填充（``_iso_date`` 的
    2099 兜底是给快照字段用的，不能拿来当地平线）。
    """
    import re as _re
    from datetime import date as _date

    request = state.get("request", {})
    order = read_order(state) if isinstance(state.get("request"), dict) else {}
    for value in (request.get("planning_start"), order.get("due_date"),
                  request.get("due_date"), order.get("due_time")):
        text = str(value or "").strip()
        if not text:
            continue
        match = _re.search(r"(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})", text)
        if not match:
            continue
        try:
            return _date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
    return None


def _degraded_calendar_snapshot(state: RunState) -> dict[str, Any]:
    """降级默认日历：**按排产地平线**生成的单班（08:00-17:00）窗口〔K5〕。

    非权威默认，显式标记 ``degraded``/``source``。原实现硬编码「今天起未来 7 天」，
    订单交期早于今天时日历里根本没有可用窗口 → ``NO_FEASIBLE_WINDOW``。确定性规则：

    - ``start = min(今天 08:00, 地平线日 00:00) - 1 天``
    - ``end   = max(今天, 地平线日) + 30 天``
    - 每天一个单班窗口 08:00-17:00（``calendar_ref=CAL-DEGRADED``）；
    - **未给交期/``planning_start``（或日期不可解析）→ 退化为原「今天起未来 7 天」行为**。
    """
    from datetime import datetime, timedelta, timezone as _tz

    tz = _tz(timedelta(hours=8))
    start = datetime.now(tz).replace(hour=8, minute=0, second=0, microsecond=0)
    horizon = _planning_horizon_date(state)
    if horizon is None:
        days = [start + timedelta(days=i) for i in range(7)]
    else:
        horizon_at = datetime(horizon.year, horizon.month, horizon.day, 8, 0, 0, tzinfo=tz)
        first = min(start, horizon_at) - timedelta(days=1)
        last = max(start, horizon_at) + timedelta(days=30)
        days = [first + timedelta(days=i) for i in range((last.date() - first.date()).days + 1)]
    intervals: list[dict[str, Any]] = []
    for day in days:
        intervals.append({
            "calendar_ref": "CAL-DEGRADED",
            "start_at": day.isoformat(),
            "end_at": day.replace(hour=17).isoformat(),
            "shift_code": "DAY",
        })
    return {
        "snapshot_id": f"SNAP-CAL-DEGRADED-{_now_task(state)}",
        "revision": 1,
        "working_intervals": intervals,
        "unavailability": [],
        "degraded": True,
        "source": "degraded-default-single-shift",
    }


def _standard_minutes_of(step: dict[str, Any]) -> float | None:
    """工序标准工时归一化（**口径 = 分钟**）〔K4〕。

    - ``standard_minutes`` / ``processing_minutes``：直接按分钟取值；
    - 只有 ``standard_time_s`` / ``standard_time`` 时按 **秒** 显式 /60
      （M2 HTTP 合同的 ``standard_time`` 是秒，见 ``_normalize_m2_route_steps``）；
    - 都没有 → None（由调用方兜底，**绝不编造工时**）。
    """
    for key in ("standard_minutes", "processing_minutes"):
        value = step.get(key)
        if value not in (None, ""):
            return _num(value, None)  # type: ignore[arg-type]
    for key in ("standard_time_s", "standard_time"):
        value = step.get(key)
        if value not in (None, ""):
            seconds = _num(value, None)  # type: ignore[arg-type]
            return None if seconds is None else seconds / 60
    return None


def _normalize_m5_route_steps(steps: list[dict[str, Any]], product_code: str) -> list[dict[str, Any]]:
    """把 SOP 路线（operation_code/station/standard_minutes）归一化成 M5 路线合同。"""
    normalized: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        step = dict(step)
        step.setdefault("product_id", product_code)
        step.setdefault("product_code", product_code)
        op_id = str(step.get("operation_id") or step.get("operation_code") or f"OP-{index:02d}")
        step.setdefault("operation_id", op_id)
        step.setdefault("op_code", op_id)
        station = str(step.get("station") or step.get("station_code") or "").strip()
        if station and not step.get("required_station_codes"):
            step["required_station_codes"] = [station]
            step["station_code"] = station
        if step.get("standard_minutes") is None:
            # K4：工时口径统一为分钟（只给秒的补数在此显式换算；无工时保持 None）。
            minutes = _standard_minutes_of(step)
            if minutes is not None:
                step["standard_minutes"] = minutes
        normalized.append(step)
    return normalized


def _degrade_resource_snapshot(resource_snapshot: dict[str, Any] | None, route_steps: list[dict[str, Any]]) -> dict[str, Any]:
    """降级资源：设备补默认日历/产能/能力，工位从路线 station 名生成（非权威默认）。

    K6：**已存在**的 stations/persons/tooling 也必须与设备同口径兜底——canonical
    SOP 派生的工位常常没有 ``calendar_ref``/``status``/``parallel_slots``，M5 严格
    校验（``pmc_v2_snapshots._validate_*``）会报 ``MISSING_VALUE ... calendar_ref``；
    且 ``read_m5_resource_facts`` 优先于人工补数，门里补 ``resource_snapshot`` 也无效。
    兜底只填**缺失**字段并打 ``degraded`` 标记，不覆盖任何已有事实。
    """
    resource = dict(resource_snapshot) if isinstance(resource_snapshot, dict) else {}
    equipment: list[dict[str, Any]] = []
    seen_equipment: set[str] = set()
    for item in (resource.get("equipment") or []):
        if not isinstance(item, dict):
            continue
        item = dict(item)
        code = str(item.get("equipment_code") or "").strip()
        if not code or code in seen_equipment:
            continue  # 重复设备码（同文件多次落库）只保留首个
        seen_equipment.add(code)
        if not str(item.get("calendar_ref") or "").strip():
            item["calendar_ref"] = "CAL-DEGRADED"
        if not str(item.get("equipment_type") or "").strip():
            item["equipment_type"] = "machine"
        if not item.get("capability_codes"):
            item["capability_codes"] = ["generic"]
        if not item.get("capacity_per_hour"):
            item["capacity_per_hour"] = "60"
        if not item.get("efficiency_factor"):
            item["efficiency_factor"] = "1"
        if not str(item.get("status") or "").strip():
            item["status"] = "ACTIVE"
        equipment.append(item)
    # K6：已存在的工位/人员/模治具逐条兜底（缺什么补什么，保持 degraded 标记）。
    stations: list[dict[str, Any]] = []
    for entry in (resource.get("stations") or []):
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        code = str(item.get("station_code") or "").strip()
        if not code:
            continue
        if not str(item.get("station_name") or "").strip():
            item["station_name"] = code
        if not str(item.get("work_center_code") or "").strip():
            item["work_center_code"] = code
        if not isinstance(item.get("parallel_slots"), int) or isinstance(item.get("parallel_slots"), bool) \
                or int(item.get("parallel_slots") or 0) < 1:
            item["parallel_slots"] = 1
        if not str(item.get("status") or "").strip():
            item["status"] = "ACTIVE"
        if not str(item.get("calendar_ref") or "").strip():
            item["calendar_ref"] = "CAL-DEGRADED"
        item["degraded"] = True
        stations.append(item)
    if not stations:
        seen: set[str] = set()
        for step in route_steps:
            station = str(step.get("station") or step.get("station_code") or "").strip()
            if not station or station in seen:
                continue
            seen.add(station)
            stations.append({
                "station_code": station,
                "station_name": station,
                "work_center_code": station,
                "parallel_slots": 1,
                "status": "ACTIVE",
                "calendar_ref": "CAL-DEGRADED",
                "degraded": True,
            })
    # K6：人员/模治具同样对**已存在**条目兜底（缺 calendar_ref/status 等字段时）。
    persons: list[dict[str, Any]] = []
    for entry in (resource.get("persons") or []):
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        code = str(item.get("person_code") or "").strip()
        if not code:
            continue
        if not isinstance(item.get("skill_codes"), list):
            item["skill_codes"] = ["generic"]
        if not isinstance(item.get("qualified_operation_codes"), list):
            item["qualified_operation_codes"] = []
        if not isinstance(item.get("max_parallel_tasks"), int) or isinstance(item.get("max_parallel_tasks"), bool) \
                or int(item.get("max_parallel_tasks") or 0) != 1:
            item["max_parallel_tasks"] = 1
        if not str(item.get("status") or "").strip():
            item["status"] = "ACTIVE"
        if not str(item.get("calendar_ref") or "").strip():
            item["calendar_ref"] = "CAL-DEGRADED"
        item["degraded"] = True
        persons.append(item)
    tooling_items: list[dict[str, Any]] = []
    for entry in (resource.get("tooling") or []):
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        code = str(item.get("tooling_code") or "").strip()
        if not code:
            continue
        if not str(item.get("tooling_type") or "").strip():
            item["tooling_type"] = "generic"
        if not isinstance(item.get("capability_codes"), list):
            item["capability_codes"] = ["generic"]
        if not isinstance(item.get("compatible_product_codes"), list):
            item["compatible_product_codes"] = []
        if not isinstance(item.get("quantity_available"), int) or isinstance(item.get("quantity_available"), bool) \
                or int(item.get("quantity_available") or 0) < 1:
            item["quantity_available"] = 1
        if not str(item.get("status") or "").strip():
            item["status"] = "ACTIVE"
        if not str(item.get("calendar_ref") or "").strip():
            item["calendar_ref"] = "CAL-DEGRADED"
        item["degraded"] = True
        tooling_items.append(item)
    resource.update({
        "snapshot_id": str(resource.get("snapshot_id") or "SNAP-RES-DEGRADED"),
        "revision": int(resource.get("revision") or 1),
        "equipment": equipment,
        "stations": stations,
        "persons": persons,
        "tooling": tooling_items,
        "degraded": True,
        "source": "degraded-resource-from-route",
    })
    return resource


def _assemble_m5_bundle(state: RunState) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """组装 M5 六类 snapshot bundle（用求解器 canonical 的 build_bundle）。

    成功返回 (bundle, scenario_id)；缺资源/日历或组装失败返回 (None, blocked)。
    """
    from .pmc_v2_adapter import build_bundle
    from .pmc_v2_snapshots import PmcError, finalize_snapshot
    from .m5_fact_validation import validate_m5_facts

    order = read_order(state)
    request = state.get("request", {})
    request_payload = _assembly_payloads_from_state(state)
    product_code = str(order.get("product_code") or "")
    degraded = _is_degraded(request)

    # 路线来源：request 显式 > M2 已批准 SOP > M0 canonical 文档（agent 识别落库）。
    raw_route = request_payload.get("routing_steps") or []
    if not raw_route:
        raw_route = read_approved_route(state)
    if not raw_route and product_code:
        raw_route = _route_steps_from_entities(state, product_code)
    routing_steps = _normalize_m5_route_steps(raw_route, product_code)

    resource_snapshot = (
        read_m5_resource_facts(state)
        or request_payload.get("resource_snapshot")
        or ({"resources": request_payload.get("resources") or []} if request_payload.get("resources") else None)
    )
    calendar_snapshot = read_m5_calendar_facts(state) or request_payload.get("calendar_snapshot")

    if degraded:
        # 降级保底：日历/资源缺失用非权威默认补齐，显式标记 degraded，不静默冒充事实。
        calendar_snapshot = calendar_snapshot or _degraded_calendar_snapshot(state)
        resource_snapshot = _degrade_resource_snapshot(resource_snapshot, routing_steps)
    else:
        fact_validation = validate_m5_facts(
            resource_snapshot=resource_snapshot,
            calendar_snapshot=calendar_snapshot,
            wip_status=request.get("wip_status"),
            require_wip=str(request.get("scenario_purpose") or "production") == "wip_pmc",
        )
        if fact_validation["status"] != "ready":
            return None, blocked(
                state, source_module="m5", tool="ingest_m5_planning_snapshot",
                missing_fields=fact_validation["missing_fields"],
                required_tool="ingest_m5_planning_snapshot",
                recovery="请补齐人员技能、设备能力、工位、生产日历及必要 WIP 快照后重试",
            )

    lines = read_lines(state) or [{
        "order_line_id": f"{order.get('order_id')}::L1", "product_code": product_code,
        "qty": order.get("quantity", 0), "uom": "PCS",
        "due_date": order.get("due_date"), "priority": "normal",
    }]
    orders: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        orders.append({
            "order_id": str(order.get("order_id") or ""),
            "order_no": str(order.get("order_no") or order.get("order_id") or ""),
            # 行号缺省按序唯一：多行订单若统一兜底 ::L1 会让 m5 快照批内撞 UNIQUE
            "order_line_id": str(line.get("order_line_id") or f"{order.get('order_id')}::L{index}"),
            "product_code": str(line.get("product_code") or product_code or ""),
            "quantity": line.get("qty") or line.get("quantity") or order.get("quantity") or 0,
            "uom": str(line.get("uom") or "PCS"),
            "due_time": _iso_date(line.get("due_date") or order.get("due_date") or ""),
            "priority": str(line.get("priority") or "normal"),
        })
    if isinstance(resource_snapshot, dict) and not resource_snapshot.get("checksum"):
        resource_snapshot = finalize_snapshot(resource_snapshot)
    bundle_payload = {
        "orders": orders,
        "routing_steps": routing_steps,
        "resource_snapshot": resource_snapshot,
        "calendar_windows": (calendar_snapshot or {}).get("working_intervals") or [],
        "supply_entries": request_payload.get("supply_entries") or [],
        "setup_matrix": request_payload.get("setup_matrix") or request_payload.get("changeover_rules") or {},
        "scenario_purpose": str(request.get("scenario_purpose") or "production"),
        "route_approval_ref": request.get("route_approval_ref") or "",
        "route_code": request.get("route_code") or "",
        "route_version": request.get("route_version") or "",
        "legacy_preview": bool(degraded),
    }
    try:
        bundle = build_bundle(bundle_payload)
    except PmcError as exc:
        return None, blocked(
            state, source_module="m5", tool="ingest_m5_planning_snapshot",
            missing_fields=[str(getattr(exc, "message", exc))],
            required_tool="ingest_m5_planning_snapshot",
            recovery="六类 snapshot 组装失败；请补齐已批准 route/资源/日历/供应事实后重试",
        )
    scenario_id = str(request.get("scenario_id") or f"scenario-{order.get('order_id', '')}")
    return bundle, scenario_id


def _route_steps_from_overview(payload: dict[str, Any], product_code: str) -> list[dict[str, Any]]:
    """Read an approved SOP route from the M0 product overview."""
    data = payload.get("data") if isinstance(payload, dict) else None
    indexes = data.get("indexes") if isinstance(data, dict) else None
    sops = indexes.get("sops") if isinstance(indexes, dict) else None
    if not isinstance(sops, list):
        return []
    for item in sops:
        entity = item.get("entity") if isinstance(item, dict) else None
        if not isinstance(entity, dict) or (str(entity.get("review_status") or "") not in {"", "approved"} and str(entity.get("status") or "") != "active"):
            continue
        attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        nested = attrs.get("attributes") if isinstance(attrs.get("attributes"), dict) else {}
        product_codes = attrs.get("product_codes") or nested.get("product_codes") or entity.get("product_codes") or []
        if product_code not in [str(code) for code in product_codes]:
            continue
        raw_steps = attrs.get("route_steps") or attrs.get("operations") or attrs.get("route") or nested.get("route_steps") or nested.get("operations") or nested.get("route") or []
        if not isinstance(raw_steps, list):
            continue
        steps: list[dict[str, Any]] = []
        for index, step in enumerate(raw_steps, start=1):
            if not isinstance(step, dict):
                continue
            steps.append({
                "product_id": product_code,
                "operation_id": str(step.get("operation_id") or step.get("operation_code") or f"OP-{index:02d}"),
                "operation_name": str(step.get("operation_name") or step.get("name") or ""),
                "name": str(step.get("name") or step.get("operation_name") or step.get("operation_code") or f"OP-{index:02d}"),
                "description": str(step.get("description") or (step.get("attributes") or {}).get("instructions") or ""),
                "station": str(step.get("station") or step.get("station_code") or ""),
                "machine_model": str(step.get("machine_model") or ""),
                "sequence": int(step.get("sequence") or step.get("sequence_no") or index),
                "standard_minutes": step.get("standard_minutes") if step.get("standard_minutes") is not None else None,
                "standard_time_s": step.get("standard_time_s"),
                "eligible_resources": step.get("eligible_resources") or [],
                "required_equipment_codes": step.get("required_equipment_codes") or step.get("equipment_codes") or [],
                "station_code": str(step.get("station_code") or ""),
                "attributes": step.get("attributes") if isinstance(step.get("attributes"), dict) else {},
            })
        if steps:
            return steps
    return []
def merge_m2_canonical_bom(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Preserve an M0-approved BOM when the M2 generator cannot consume it directly."""
    if not isinstance(result, dict):
        return result
    lines = payload.get("bom_lines") if isinstance(payload, dict) else None
    if not isinstance(lines, list) or not lines:
        return result
    generation = result.setdefault("bom_generation", {})
    if not isinstance(generation, dict):
        generation = {}
        result["bom_generation"] = generation
    if not generation.get("bom_lines"):
        generation["bom_lines"] = [line for line in lines if isinstance(line, dict)]
    result["canonical_bom_match"] = {
        "status": "matched",
        "source": "m0.get_m0_product_overview",
        "product_code": str((payload.get("product_profile") or {}).get("product_code") or ""),
        "line_count": len(generation.get("bom_lines") or []),
        "review_status": "approved",
    }
    route = payload.get("routing_steps") if isinstance(payload, dict) else None
    from .m2_fact_validation import validate_engineering_facts
    generation_sop = result.get("sop_generation") if isinstance(result.get("sop_generation"), dict) else {}
    result["engineering_fact_validation"] = validate_engineering_facts(
        product_code=(payload.get("product_profile") or {}).get("product_code"),
        bom_lines=generation.get("bom_lines") or [],
        bom_version=generation.get("bom_version"),
        bom_effective_from=generation.get("effective_from"),
        bom_effective_to=generation.get("effective_to"),
        route_steps=route if isinstance(route, list) else generation_sop.get("route_steps") or [],
        sop_version=generation_sop.get("sop_version"),
        sop_effective_from=generation_sop.get("effective_from"),
        sop_effective_to=generation_sop.get("effective_to"),
    )
    if isinstance(route, list) and route:
        generation_sop = result.setdefault("sop_generation", {})
        if isinstance(generation_sop, dict):
            generation_sop["route_steps"] = [step for step in route if isinstance(step, dict)]
            generation_sop["operation_count"] = len(generation_sop["route_steps"])
            generation_sop["status"] = "matched"
        result["canonical_sop_match"] = {
            "status": "matched",
            "source": "m0.get_m0_product_overview",
            "product_code": str((payload.get("product_profile") or {}).get("product_code") or ""),
            "operation_count": len(route),
            "standard_minutes_missing": sum(1 for step in route if isinstance(step, dict) and step.get("standard_minutes") in (None, "")),
        }
    return result


def merge_m3_deferred_bom(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """把 M3 桥接时降级（qty_per 缺失/非正数）的 BOM 行回贴到 M3 结果，避免静默丢失。"""
    if not isinstance(result, dict):
        return result
    deferred = payload.get("bom_deferred_lines") if isinstance(payload, dict) else None
    if isinstance(deferred, list) and deferred:
        result["bom_deferred_lines"] = deferred
        result["bom_deferred_count"] = len(deferred)
        result["data_quality"] = {
            "degraded": True,
            "reason": "部分 BOM 行缺少正数量化用量（线材按长度/规格、包材按装箱数量未填），已从齐套计算中降级排除",
            "deferred_material_codes": [str(item.get("material_code") or "") for item in deferred if isinstance(item, dict)],
        }
    return result


def read_approved_route(state: RunState) -> list[dict[str, Any]]:
    request = state.get("request", {})
    steps = request.get("routing_steps") or []
    if not steps:
        m2 = output_data(state, "run_bom_sop_workflow")
        sop_generation = m2.get("sop_generation") if isinstance(m2, dict) else None
        if isinstance(sop_generation, dict) and str(sop_generation.get("approval_status") or "") == "approved":
            steps = sop_generation.get("route_steps") or []
    return [item for item in steps if isinstance(item, dict)] if isinstance(steps, list) else []


def _normalize_m2_route_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapt the shared route shape to the M2 HTTP contract.

    The workflow request uses the M5-facing ``operation_id`` /
    ``processing_minutes`` names, while M2 requires ``name`` and
    ``standard_time`` (seconds).  Keep the original fields for downstream
    evidence and add the M2 aliases deterministically.
    """
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(steps, start=1):
        if not isinstance(item, dict):
            continue
        value = dict(item)
        name = str(item.get("name") or item.get("operation_name") or item.get("operation_id") or f"OP-{index}")
        if item.get("standard_time") not in (None, ""):
            seconds = float(item["standard_time"])
        elif item.get("standard_time_s") not in (None, ""):
            seconds = float(item["standard_time_s"])
        elif item.get("standard_minutes") not in (None, ""):
            seconds = float(item["standard_minutes"]) * 60
        else:
            seconds = float(item.get("processing_minutes") or 0) * 60
        value.setdefault("name", name)
        value.setdefault("description", str(item.get("description") or item.get("operation_name") or name))
        value.setdefault("station", str(item.get("station") or item.get("station_code") or ""))
        value["standard_time"] = seconds
        value["standard_time_s"] = seconds
        if item.get("standard_minutes") in (None, ""):
            value["standard_minutes"] = seconds / 60
        normalized.append(value)
    return normalized


def read_inventory_facts(state: RunState) -> list[dict[str, Any]]:
    """库存来源：request 显式快照 > M0 canonical（agent 识别落库的 inventory）。

    关键字段 = material_code + available_qty（缺料计算够用）；非关键字段
    warehouse/lot_no/qc_status 缺失时降级默认值，不卡死整链。
    """
    request = state.get("request", {})
    inventory = request.get("inventory_snapshot") or request.get("inventory") or []
    if not isinstance(inventory, list):
        inventory = []
    items = [item for item in inventory if isinstance(item, dict)]
    if not items:
        # 从 M0 canonical 读回 agent 识别落库的 inventory 实体。
        for entity in _flatten_entities(_read_m0_entities(state, "inventory")):
            if not entity.get("material_code"):
                continue
            items.append({
                "material_code": str(entity.get("material_code") or ""),
                "warehouse": str(entity.get("warehouse") or "默认仓"),
                "available_qty": entity.get("available_qty") if entity.get("available_qty") not in (None, "") else 0,
                "lot_no": str(entity.get("lot_no") or ""),
                "qc_status": str(entity.get("qc_status") or "released"),
                "unit": str(entity.get("unit") or ""),
                # stock_class（库存四态）必须带出：**D5「有库存且态=raw 走库存成本价」
                # 依赖它**——投影里漏掉它就等于所有 canonical 库存都"态未知"，
                # 那条分支永远触发不了（即使数据侧补了字段）。缺失留空串，
                # 由 `resolve_material_prices` / 库存视图按"态未知"fail-closed。
                "stock_class": str(entity.get("stock_class") or ""),
            })
    # 非关键字段降级默认值：缺 warehouse/lot/qc/locked_qty/received_at 不影响
    # 「按物料汇总可用库存」，桥接补合理默认。
    from datetime import datetime, timezone

    received_at = datetime.now(timezone.utc).isoformat()
    return [
        {
            **item,
            "warehouse": item.get("warehouse") or "默认仓",
            "lot_no": item.get("lot_no") or "默认批次",
            "qc_status": item.get("qc_status") or "released",
            "locked_qty": item.get("locked_qty", 0),
            "received_at": item.get("received_at") or received_at,
        }
        for item in items
        if isinstance(item, dict)
    ]


def read_supplier_facts(state: RunState) -> dict[str, Any]:
    request = state.get("request", {})
    supplier = request.get("supplier_by_material") or {}
    return supplier if isinstance(supplier, dict) else {}


def read_m4_supply_snapshot(state: RunState) -> dict[str, Any]:
    """M4 供应快照：只消费 M4 输出，不从最初 request 重建。"""
    m4 = output_data(state, "import_m4_purchase_suggestions_json")
    if not m4:
        return {}
    return m4


#: M4 追踪行/采购单行项一次读取的上限（装配层只取事实，不做分页语义）。
_M4_PRICE_ROW_LIMIT = 10_000


def read_m4_tracking_rows(state: RunState) -> list[dict[str, Any]]:
    """M4B 采购追踪**原始行**（含 `arrival_status`/`supplier_name`/`unit_price`）。

    `read_m4_tracking_price_facts` 只用其中的单价；`generate_statement` 的供应商侧
    还要用「是否已入库」判定对账明细，故单出一个原始行读口（同一份事实，不重复读库）。
    """
    request = state.get("request", {})
    rows = request.get("purchase_tracking_rows")
    if isinstance(rows, list) and rows:
        return [row for row in rows if isinstance(row, dict)]
    from pathlib import Path

    m4b_db = str(request.get("m4b_db_path") or os.getenv("YUNPAI_M4B_DB")
                 or "runtime/yunpai-m4b.sqlite")
    if not Path(m4b_db).exists():
        return []
    try:
        from .m4b_store import M4BStore

        tracking, _total = M4BStore(m4b_db).tracking_list(
            str(state.get("tenant_id") or "default"), offset=0, limit=_M4_PRICE_ROW_LIMIT)
    except Exception:  # noqa: BLE001 —— 读不到按「无事实」处理，调用方标 missing
        return []
    return [row for row in tracking if isinstance(row, dict)]


def read_m4_tracking_price_facts(state: RunState) -> dict[str, Any]:
    """R1a 价源事实：从 M4B 采购追踪取**实际采购单价**行（D-007）。

    为什么在装配层读、而不是让 M6 自己读：**D9** —— M6 不自开 M4 store。这里只把
    M4A/M4B 的事实原样取出来交给 M6，解析（哪一行是有效单价、同键取新）由 M6 的纯函数
    `m6_price_source.purchase_prices_from_tracking` 做，语义有单测锁定。

    读口优先级：``request.purchase_tracking_rows``（显式给出/离线联调）→ M4B 库
    （``request.m4b_db_path`` → ``YUNPAI_M4B_DB`` → 默认库，**库不存在即返回空**，不建库）；
    采购单只用于拼 ``purchase_order_item_id → item_code/internal_material_no`` 映射
    （``request.m4_db_path`` → ``YUNPAI_M4_DB``）。取不到就返回空——M6 侧会把缺价的行标
    ``cost_incomplete``（绝不编造价）。
    """
    request = state.get("request", {})
    rows = read_m4_tracking_rows(state)
    if not rows:
        return {}
    items = request.get("purchase_order_items")
    if isinstance(items, list) and items:
        return {"purchase_tracking_rows": rows, "purchase_order_items": items}
    from pathlib import Path

    order_items: list[dict[str, Any]] = []
    m4_db = str(request.get("m4_db_path") or os.getenv("YUNPAI_M4_DB")
                or "runtime/yunpai-m4.sqlite")
    if Path(m4_db).exists():
        try:
            from .m4_store import M4Store

            listed = M4Store(m4_db).list_purchase_orders(
                page=1, page_size=1000, status=None, supplier_name=None,
                tenant_id=str(state.get("tenant_id") or "default"))
            for order in listed.get("items") or []:
                order_items.extend(
                    item for item in (order.get("items") or []) if isinstance(item, dict))
        except Exception:  # noqa: BLE001 —— 映射缺失时 M6 只能按行项 id 键查价（保守但可用）
            order_items = []
    return {"purchase_tracking_rows": rows, "purchase_order_items": order_items}


def delivery_note_bodies(state: RunState,
                         counterparty_code: str = "") -> list[dict[str, Any]]:
    """M0 canonical 送货单（**已拆信封**，业务字段在顶层）。

    拆信封必须做：`_read_m0_entities` 返回的是 ``{canonical_key, **envelope}``，业务体
    嵌套在 ``payload`` 里——直接按 ``note_no``/``counterparty_code`` 过滤永远匹配不到
    （B0b 的两个读工具就是这么恒返回空的）。这里复用桥接既有的 `_flatten_entities`
    （同一处归一化，不另写一份）。
    """
    rows = _flatten_entities(_read_m0_entities(state, "delivery_note"))
    if counterparty_code:
        rows = [row for row in rows
                if str(row.get("counterparty_code") or "") == counterparty_code]
    return rows


def received_purchase_rows(state: RunState, supplier: str = "") -> list[dict[str, Any]]:
    """M4B 追踪里「已入库」的行（供应商侧对账依据：入库 → 应付增加）。

    匹配口径：``supplier_name`` 或 ``supplier_code`` 等于给定供应商键——M4 追踪落的是
    供应商**名**，而往来单位常用**编码**，两者都认；取不到就返回空，由调用方标 missing。
    """
    rows = [row for row in read_m4_tracking_rows(state)
            if str(row.get("arrival_status") or "") == "received"]
    if supplier:
        rows = [row for row in rows
                if supplier in (str(row.get("supplier_name") or ""),
                                str(row.get("supplier_code") or ""))]
    return rows


def _period_of(value: Any) -> str:
    """从日期事实推账期（``YYYY-MM``）；形态不符返回空（不猜账期）。"""
    text = str(value or "")
    return text[:7] if len(text) >= 7 and text[4] == "-" else ""


def _assemble_costing_facts(state: RunState, product_code: str) -> dict[str, Any] | None:
    """**产品级成本事实**（BOM 行 / 工艺工序 / 库存 / 费率 / M4 采购价源）。

    为什么抽成一个函数：`save_costing_snapshot`（落试算快照）与 `get_product_cost`
    （当场算/报价预览）必须吃**同一套事实**——两条装配各写一份，迟早会出现
    「预览 100、落库 105」这种数字漂移（D8 的当场算与账上快照必须可比）。

    product_code 缺失 → 返回 ``None``（调用方失败关闭，不拿别的产品顶上）。
    BOM 行缺 → 返回的 ``bom_lines`` 为空列表（由调用方决定 blocked，两者文案不同）。
    """
    request = state.get("request", {})
    if not product_code:
        return None
    bom_lines = (read_approved_bom(state)
                 or _bom_lines_from_entities(state, product_code)
                 or (request.get("bom_lines") if isinstance(request.get("bom_lines"), list)
                     else []))
    facts: dict[str, Any] = {
        "product_code": product_code,
        "bom_lines": bom_lines,
        "routing_steps": (read_approved_route(state)
                          or _route_steps_from_entities(state, product_code)),
        # 库存事实（D5/D6）：有库存走库存成本价；缺料才用下面的采购价
        "inventory": read_inventory_facts(state),
        "hour_rate": request.get("hour_rate"),
        "overhead_rate": request.get("overhead_rate"),
    }
    # R1a：M4 采购追踪的实际单价（缺价由 M6 标 cost_incomplete，不编造）
    facts.update(read_m4_tracking_price_facts(state))
    return facts


def _order_lines_from_order(state: RunState, order: dict[str, Any]) -> list[dict[str, Any]]:
    """把 M1 订单行归一成审计可用的 ``{product_code, qty, unit_price}``。

    M1 行项的字段名不稳定（``product_code``/``model``/``product_id``、``quantity``/``qty``、
    ``unit_price``/``price``），此处只做键名归一；**取不到的对客单价不编造**——
    缺价的行留给内核标 ``incomplete``（审计结论必须是"算不全"，不能是"很赚钱"）。
    """
    lines: list[dict[str, Any]] = []
    for line in read_lines(state):
        code = str(line.get("product_code") or line.get("model") or line.get("product_id") or "")
        price = line.get("unit_price", line.get("price"))
        lines.append({
            "product_code": code,
            "qty": line.get("quantity", line.get("qty")),
            "unit_price": price,
        })
    if lines:
        return lines
    # 无行项时退回订单头（单产品订单；缺价同样不编造）
    if str(order.get("product_code") or ""):
        return [{"product_code": str(order.get("product_code") or ""),
                 "qty": order.get("order_qty") or order.get("quantity"),
                 "unit_price": order.get("unit_price", order.get("price"))}]
    return []


def _basis_rows_from_reports(state: RunState) -> list[dict[str, Any]]:
    """从 M0 canonical 的报工事实（``production_daily_report``）聚合分摊基准。

    canonical 报工只有 ``quantity``（无工时/人数），故只产出 ``quantity`` 基准；
    费用若按工时/人数分摊，该产品会进 ``missing_basis_value``（**不推算、不编造**）。
    """
    totals: dict[str, float] = {}
    order: list[str] = []
    for row in _flatten_entities(_read_m0_entities(state, "production_daily_report")):
        code = str(row.get("product_code") or "").strip()
        if not code:
            continue
        try:
            qty = float(row.get("quantity") or row.get("total_quantity") or 0)
        except (TypeError, ValueError):
            continue
        if code not in totals:
            order.append(code)
            totals[code] = 0.0
        totals[code] += qty
    return [{"product_code": code, "quantity": round(totals[code], 6)} for code in order]


def _basis_rows_from_order_lines(state: RunState) -> list[dict[str, Any]]:
    """兜底：用订单行的产品数量当分摊基准（产量口径；缺报工事实时的降级来源）。"""
    request = state.get("request", {})
    lines = list(read_lines(state))
    if not lines:
        explicit = request.get("order_lines")
        lines = [line for line in explicit if isinstance(line, dict)] \
            if isinstance(explicit, list) else []
    totals: dict[str, float] = {}
    order: list[str] = []
    for line in lines:
        code = str(line.get("product_code") or line.get("model") or "").strip()
        if not code:
            continue
        try:
            qty = float(line.get("quantity") or line.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if code not in totals:
            order.append(code)
            totals[code] = 0.0
        totals[code] += qty
    return [{"product_code": code, "quantity": round(totals[code], 6)} for code in order]


# ---------------------------------------------------------------------------
# 主链 payload 装配
# ---------------------------------------------------------------------------

def _assembly_payloads_from_state(state: RunState) -> dict[str, Any]:
    """把 orchestrator 已批准的订单/路线/库存/供应事实包装成 planning_snapshot
    可消费结构（含显式 snapshot 字段则原样传递）。"""
    request = state.get("request", {})
    payloads: dict[str, Any] = {}
    for key in ("orders", "routing_steps", "resources", "resource_snapshot",
                "calendar_windows", "calendar", "calendar_snapshot",
                "supply_entries", "material_availability", "wip_status",
                "changeover_rules", "setup_matrix", "order_kitting"):
        if request.get(key) is not None:
            payloads[key] = request[key]
    return payloads


def forward_m1_order_canonical(files: list[dict[str, Any]], m1_output: Any) -> list[dict[str, Any]]:
    """Canonical 模式（配置 ``YUNPAI_M0_DB``）的 M1→M0 前向组装〔K1〕。

    逐行语义等价迁自 `_wt/REALFLOW/src/yunpai_langgraph/orchestration_bridge.py:872-912`
    （W913 唯一跑通过全链的实现）。当 M1 已确定性解析出 ``m1.document.v2``
    （order_id + lines 行项）且附件中没有现成 JSON 时，把订单转成
    ``m0.ingest.v1`` 结构化 records 文件返回；否则原样返回（沙箱模式 /
    raw 附件直达行为不变，raw 文件由 canonical store 隔离为
    ``quarantined:[no_structured_records]`` → run=failed 且不开门）。
    """
    import base64 as _b64

    if not files or not os.getenv("YUNPAI_M0_DB"):
        return files
    m1_doc = (m1_output or {}).get("document") if isinstance(m1_output, dict) else None
    if not isinstance(m1_doc, dict):
        return files
    m1_header = m1_doc.get("header") if isinstance(m1_doc.get("header"), dict) else {}
    m1_lines = m1_doc.get("lines") if isinstance(m1_doc.get("lines"), list) else []
    order_id = str(m1_header.get("order_id") or m1_header.get("order_number") or "")
    has_json = any(str(f.get("filename") or "").lower().endswith(".json")
                   for f in files if isinstance(f, dict))
    if not (order_id and m1_lines and not has_json):
        return files
    total_qty = sum(float(line.get("quantity") or 0) for line in m1_lines if isinstance(line, dict))
    records = [{
        "entity_type": "order",
        "order_id": order_id,
        "filename": "order-canonical.json",
        "identity": {"business_key": order_id},
        "payload": {
            "order_id": order_id,
            "order_number": m1_header.get("order_number") or order_id,
            "product_code": m1_header.get("product_code")
            or (str(m1_lines[0].get("product_code") or "") if m1_lines else ""),
            "quantity": total_qty,
            "due_date": m1_header.get("due_date"),
            "lines": m1_lines,
        },
    }]
    raw = json.dumps({"records": records}, ensure_ascii=False).encode("utf-8")
    return [{"filename": "order-canonical.json", "content_type": "application/json",
             "content_b64": _b64.b64encode(raw).decode()}]


def _statement_basis(state: RunState, request: dict[str, Any],
                     counterparty: str) -> dict[str, Any]:
    """对账依据事实（B3）：客户侧取 canonical 送货单、供应商侧取 M4 已入库追踪行。

    只**取事实**，不做方向映射也不推断金额——「发货 → 应收增加」这类领域语义留在 M6
    （`m6_tools._statement_transactions`），一处实现，免得两边各算一套。
    """
    statement_type = str(request.get("statement_type") or "customer")
    if statement_type == "supplier":
        return {"purchase_receipts": received_purchase_rows(state, counterparty)}
    return {"delivery_notes": delivery_note_bodies(state, counterparty)}


def _quotation_facts(state: RunState) -> dict[str, Any]:
    """报价输入：**行**（显式 > 订单行）+ **逐产品成本事实**（供行内滚动）。

    成本怎么算仍由 M6 侧按 D5 口径做（`m6_tools._line_costs` → `_cost_breakdown`），
    装配层只负责把事实凑齐——不在这里复算一遍，免得两处口径漂移。
    """
    request = state.get("request", {})
    products: dict[str, Any] = {}
    lines = request.get("lines")
    if isinstance(lines, list) and lines:
        lines = [line for line in lines if isinstance(line, dict)]
    else:
        order = read_order(state)
        lines = _order_lines_from_order(state, order)
        lines = [{"product_code": line.get("product_code"), "qty": line.get("qty")}
                 for line in lines]
    for code in dict.fromkeys(str(line.get("product_code") or "") for line in lines):
        if not code:
            continue
        facts = _assemble_costing_facts(state, code)
        if facts is not None:
            products[code] = facts
    payload: dict[str, Any] = {"lines": lines, "products": products}
    for key in ("quote_no", "doc_no", "customer_code", "doc_date", "direction",
                "markup_rate", "source_ref"):
        if request.get(key) not in (None, "", [], {}):
            payload[key] = request[key]
    return payload


def bridge_payload(state: RunState, tool: str) -> dict[str, Any]:
    """为受控 workflow 的某个 tool 装配 payload；缺权威输入返回 BLOCKED_INPUT 结构。

    返回的 dict 若带 ``success=False`` 且 ``code==BLOCKED_INPUT``，graph 侧
    将把它作为步骤结果交给 Reviewer（开数据 Gate），不会调用该 tool。
    """
    request = state.get("request", {})
    if tool == "ingest_document":
        attachments = request.get("attachments") or request.get("documents") or []
        order_attachment = next(
            (item for item in attachments if isinstance(item, dict) and item.get("kind") == "order"),
            None,
        )
        file_value = (
            order_attachment
            if isinstance(order_attachment, dict)
            and isinstance(order_attachment.get("content_b64"), str)
            and order_attachment.get("content_b64")
            else None
        )
        if not file_value and request.get("document"):
            file_value = {
                "filename": "order.json",
                "content_type": "application/json",
                "content_b64": request["document"].get("_encoded", ""),
            }
        if not file_value:
            return blocked(state, source_module="m1", tool=tool,
                           missing_fields=["request.attachments(原始文件)"],
                           required_tool="ingest_document",
                           recovery="请上传原始订单/业务文件后再重试 M1 解析")
        # 注册工具 ingest_document 的本地 handler（workers.m1_parse）会按字节结构
        # 用同一 order_semantics 确定性解析 XLSX，这里只传文件，不在编排层预解析。
        return {"file": file_value}
    if tool == "data_import_run":
        m1 = output_data(state, "ingest_document")
        # M0 以 M1 已复核解析为依据（保留 sha 证据），本地 fixture 模式允许
        # 原始附件直达以兼容 preview；HTTP transport 由真实 M0 处理。
        file_value = None
        attachments = request.get("attachments") or request.get("documents") or []
        order_attachment = next(
            (item for item in attachments if isinstance(item, dict) and item.get("kind") == "order"),
            None,
        )
        file_value = order_attachment if isinstance(order_attachment, dict) else None
        if file_value is None and m1.get("document"):
            source = m1.get("document", {}).get("source", {})
            file_value = {
                "filename": str(source.get("original_filename") or "m1-document.json"),
                "content_type": "application/json",
                "content_b64": request.get("_m1_encoded", ""),
            }
        if file_value is None:
            return blocked(state, source_module="m0", tool=tool,
                           missing_fields=["ingest_document 输出或原始附件"],
                           required_tool="ingest_document")
        payload: dict[str, Any] = {"files": [file_value]}
        # K1：Canonical 模式前向——M1 已解析的行项转成 order-canonical.json
        # （与 REALFLOW graph._payload_for 同语义）。raw 附件直达会被 canonical
        # store 隔离（no_structured_records）→ run=failed 且不开门。
        payload["files"] = forward_m1_order_canonical(payload["files"], m1)
        return payload
    if tool == "data_import_preview":
        # S1 C3：预览需要批次号；从上游 data_import_run 产出回填（我方 graph.py:852-854 同口径）。
        imported = output_data(state, "data_import_run")
        batch_id = str(request.get("batch_id") or imported.get("batch_id") or imported.get("id") or "")
        if not batch_id:
            return blocked(state, source_module="m0", tool=tool,
                           missing_fields=["data_import_run.batch_id"],
                           required_tool="data_import_run")
        return {"batch_id": batch_id}
    if tool == "data_import_resolve":
        # S1 C3：裁决必须显式给 kind/action（禁止伪造裁决），批次号从上游回填。
        imported = output_data(state, "data_import_run")
        batch_id = str(request.get("batch_id") or imported.get("batch_id") or imported.get("id") or "")
        if not batch_id:
            return blocked(state, source_module="m0", tool=tool,
                           missing_fields=["data_import_run.batch_id"],
                           required_tool="data_import_run")
        kind = str(request.get("kind") or "")
        action = str(request.get("action") or "")
        if kind not in {"entity", "mapping"}:
            raise ValueError("data_import_resolve 需要显式 kind(entity|mapping)，禁止伪造裁决")
        if action not in {"approve", "reject"}:
            raise ValueError("data_import_resolve 需要显式 action(approve|reject)，禁止伪造裁决")
        payload: dict[str, Any] = {"batch_id": batch_id, "kind": kind, "action": action}
        for key in ("id", "candidate_id", "note"):
            if request.get(key) not in (None, ""):
                payload[key] = request[key]
        return payload
    if tool == "data_import_commit":
        imported = output_data(state, "data_import_run")
        batch_id = imported.get("batch_id") or imported.get("id")
        if not batch_id:
            return blocked(state, source_module="m0", tool=tool,
                           missing_fields=["data_import_run.batch_id"],
                           required_tool="data_import_run")
        return {"batch_id": str(batch_id), "require_resolved": True}
    if tool == "run_bom_sop_workflow":
        order = read_order(state)
        product_code = str(order.get("product_code") or "")
        if not product_code:
            return blocked(state, source_module="m1", tool=tool,
                           missing_fields=["m1 订单 header.product_code"],
                           required_tool="ingest_document",
                           recovery="请先完成 M1 解析/复核，使订单产品编码成为权威事实")
        approved_bom = read_approved_bom(state)
        bom_lines = approved_bom
        if not bom_lines:
            bom_lines = request.get("bom_lines") or []
        if not bom_lines:
            bom_lines = _bom_lines_from_entities(state, product_code)
        routing_steps = read_approved_route(state)
        if not routing_steps:
            routing_steps = _route_steps_from_entities(state, product_code)
        routing_steps = _normalize_m2_route_steps(routing_steps)
        attachments = [item for item in (request.get("attachments") or [])
                       if isinstance(item, dict) and item.get("kind") == "master_data"]
        lines = []
        for index, line in enumerate(bom_lines if isinstance(bom_lines, list) else [], start=1):
            if not isinstance(line, dict):
                continue
            lines.append({
                "item_no": str(line.get("line_id") or index),
                "material_code": str(line.get("material_code") or ""),
                "name": str(line.get("material_name") or line.get("material_code") or "未命名物料"),
                "specification": str(line.get("specification") or ""),
                "quantity": str(line.get("quantity_per", line.get("quantity", ""))),
                "note": str(line.get("note") or ""),
            })
        return {
            "product_profile": {"product_code": product_code, "product_name": order.get("product_name") or order.get("product_code") or ""},
            "bom_lines": bom_lines,
            "bom_items": lines,
            "routing_steps": routing_steps,
            "requirement_text": str(request.get("message") or request.get("requirement_text") or ""),
            "rule_package_path": str(request.get("rule_package_path") or os.getenv("YUNPAI_RULE_PACKAGE_PATH", "runtime/rule_packages/material_numbering")),
            "document_no": str(request.get("document_no") or product_code or "M2-DRAFT"),
            "history_bom_paths": request.get("history_bom_paths") or [],
            "history_sop_paths": request.get("history_sop_paths") or [],
            "template_confirmation": request.get("template_confirmation") or {"confirmed": True},
            "customer_answers": request.get("customer_answers") or {},
            "machine_hints": request.get("machine_hints") or [],
            "station": str(request.get("station") or ""),
            "enable_bom_model": bool(request.get("enable_bom_model", False)),
            "enable_sop_model": bool(request.get("enable_sop_model", False)),
            "bom_files": request.get("bom_files") or attachments,
            "sop_files": request.get("sop_files") or attachments,
            "use_demo_sources": False,
            "_source": {
                "module": "m0" if not approved_bom and not request.get("bom_lines") else "m1",
                "ref": "get_m0_product_overview" if not approved_bom and not request.get("bom_lines") else "ingest_document",
                "evidence": bool(order),
            },
        }
    if tool == "run_m3_procurement_requirements":
        order = read_order(state)
        product_code = str(order.get("product_code") or "")
        order_id = str(order.get("order_id") or "")
        # BOM 行优先取 M0 canonical（agent 识别落库、数量列已按区块修正），
        # 其次回退到 M2 工程 BOM。M3 只消费可量化的行（qty_per>0），其余降级。
        bom_lines = _bom_lines_from_entities(state, product_code) if product_code else []
        if not bom_lines:
            bom_lines = read_approved_bom(state)
        inventory = read_inventory_facts(state)
        # M1 HTTP responses may expose quantity only on extracted order lines,
        # not on the header object consumed by read_order.  Preserve that
        # source-backed quantity for the M3 contract instead of sending zero.
        line_quantity = sum(
            float(line.get("quantity") or line.get("order_qty") or 0)
            for line in read_lines(state)
            if isinstance(line, dict)
        )
        order_quantity = order.get("order_qty") or order.get("quantity") or line_quantity
        if not bom_lines:
            return blocked(state, source_module="m2", tool=tool,
                           missing_fields=["已批准 BOM 行"],
                           required_tool="run_bom_sop_workflow",
                           recovery="请先完成工程 Gate 批准 BOM，再执行齐套计算")
        if not inventory:
            if bool(request.get("legacy_preview")):
                inventory = [{"material_code": str(line.get("material_code") or ""), "available_qty": 0} for line in bom_lines]
            else:
                return blocked(state, source_module="m3", tool=tool,
                               missing_fields=["inventory_snapshot(含 warehouse/lot/qc)"],
                               required_tool="get_material_readiness_snapshot",
                               recovery="缺少权威库存快照；请提供 M3 库存事实后重试")
        from .m3_m4_fact_validation import validate_inventory_facts
        inventory_validation = validate_inventory_facts(
            inventory,
            [],
            strict=False,
        )
        if inventory_validation["status"] != "ready" and not bool(request.get("legacy_preview")):
            validation_fields = [
                item.get("field", "inventory_fact")
                for item in inventory_validation["missing_fields"] + inventory_validation["validation_issues"]
                if isinstance(item, dict)
            ]
            return blocked(
                state, source_module="m3", tool=tool,
                missing_fields=validation_fields,
                required_tool="get_material_readiness_snapshot",
                recovery="请补齐每个物料的仓库、批次、质检状态、数量和快照时间",
            )
        bom_payload_lines, bom_deferred_lines = _normalize_bom_qty_lines(bom_lines)
        if not bom_payload_lines:
            return blocked(state, source_module="m2", tool=tool,
                           missing_fields=["bom.lines[].qty_per(可量化 BOM 行)"],
                           required_tool="run_bom_sop_workflow",
                           recovery="BOM 行全部缺少正数量化用量；请补全各物料的单件用量后重试")
        payload: dict[str, Any] = {
            "tenant_id": state.get("tenant_id", "default"),
            "order": {
                "project_id": str(request.get("project_id") or order_id or product_code or "PROJECT-DEFAULT"),
                "order_id": order_id or product_code or "ORDER-DEFAULT",
                "bom_id": str(request.get("bom_id") or f"BOM-{product_code}"),
                "product_name": order.get("product_name") or product_code or "",
                "order_qty": order_quantity,
                "due_date": _iso_date(order.get("due_date")),
            },
            "bom": {
                "bom_id": str(request.get("bom_id") or f"BOM-{product_code}"),
                "product_name": order.get("product_name") or product_code or "",
                "lines": bom_payload_lines,
            },
            "inventory_snapshot": inventory,
            "bom_deferred_lines": bom_deferred_lines,
        }
        return payload
    if tool == "import_m4_purchase_suggestions_json":
        m3 = output_data(state, "run_m3_procurement_requirements")
        shortage_lines = m3.get("shortage_lines") or []
        if not isinstance(shortage_lines, list):
            shortage_lines = []
        if not shortage_lines:
            # A no-shortage M3 result is a valid procurement handoff: M4 must
            # persist an empty suggestion batch/supply snapshot so M5 can
            # consume the explicit "ready" state.  Missing M3 output remains
            # blocked below via the normal payload checks.
            if not m3:
                return blocked(state, source_module="m3", tool=tool,
                               missing_fields=["run_m3_procurement_requirements"],
                               required_tool="run_m3_procurement_requirements",
                               recovery="请先完成 M3 齐套计算后再进入 M4")
        supplier_facts = read_supplier_facts(state)
        from .m3_m4_fact_validation import validate_supplier_facts
        supplier_validation = validate_supplier_facts(
            supplier_facts,
            [str(line.get("material_code") or "") for line in shortage_lines if isinstance(line, dict)],
        )
        if supplier_validation["status"] != "ready" and not _is_degraded(request):
            validation_fields = [
                item.get("field", "supplier_fact")
                for item in supplier_validation["missing_fields"] + supplier_validation["validation_issues"]
                if isinstance(item, dict)
            ]
            validation_fields.append("supplier_by_material(权威供应商主数据)")
            return blocked(
                state, source_module="m4", tool=tool,
                missing_fields=validation_fields,
                required_tool="list_m4_suppliers",
                recovery="请补齐每个缺料物料的权威供应商映射后重试",
            )
        suggestions = []
        for line in shortage_lines:
            if not isinstance(line, dict):
                continue
            material_code = str(line.get("material_code") or "")
            suggestions.append({
                "item_code": material_code,
                "item_name": line.get("material_name") or material_code,
                "quantity": line.get("suggest_purchase_qty", line.get("shortage_qty", 0)),
                "unit": line.get("uom", "pcs"),
                "supplier_name": supplier_facts.get(material_code, ""),
                "required_date": str(m3.get("due_date") or ""),
                "project_code": str(m3.get("project_id") or ""),
            })
        if any(not item["supplier_name"] for item in suggestions) and not _is_degraded(request):
            return blocked(state, source_module="m4", tool=tool,
                           missing_fields=["supplier_by_material(权威供应商主数据)"],
                           required_tool="list_m4_suppliers",
                           recovery="缺少权威供应商映射；请提供 M4 供应商主数据后重试")
        # S3 整合（R049）：幂等键随交接载荷内容派生（供应商解析/补料变化 → 新键），
        # 与 M4 受控导入「同 (task,key) 同正文 replay / 异正文 conflict」契约对齐：
        # 主链先建批次后经 procurement gate 补供应商重跑时，载荷升级走新批次草稿
        # （旧草稿保留、未发布），不再触发 IDEMPOTENCY_CONFLICT 死锁。
        import hashlib as _hashlib
        import json as _json

        content_digest = _hashlib.sha256(
            _json.dumps(suggestions, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]
        return {
            "suggestions": suggestions,
            "tenant_id": state.get("tenant_id", "default"),
            "site_id": str(request.get("site_id") or "default"),
            "tracking_task_id": state.get("task_id", ""),
            "idempotency_key": f"{state.get('task_id', 'task')}:m4:{content_digest}",
            "source_module": "m3",
            "procurement_plan_id": str(m3.get("procurement_plan_id") or ""),
            "order_id": str(m3.get("order_id") or ""),
            "source_plan_checksum": str((m3.get("handoff_envelope") or {}).get("source_plan_checksum") or "") if isinstance(m3.get("handoff_envelope"), dict) else "",
            "degraded": bool(_is_degraded(request) and any(not item["supplier_name"] for item in suggestions)),
        }
    if tool == "ingest_m5_planning_snapshot":
        bundle, scenario_id = _assemble_m5_bundle(state)
        if bundle is None:
            return scenario_id  # blocked dict
        request = state.get("request", {})
        return {
            "scenario_id": scenario_id,
            "tenant_id": state.get("tenant_id", "default"),
            "site_id": str(request.get("site_id") or "default"),
            "scenario_purpose": str(request.get("scenario_purpose") or "production"),
            "replace_existing": True,
            "source_systems": ["manual"],
            "observed_at": request.get("observed_at"),
            "source_observed_at": request.get("source_observed_at") or {},
            "pmc_v2_bundle": bundle,
            "idempotency_key": f"{state.get('task_id', 'task')}:m5-snapshot",
        }
    if tool == "solve_scheduling":
        snapshots = output_data(state, "ingest_m5_planning_snapshot")
        # M5 求解必须以已持久化的 snapshot 回读为准；若 snapshot 步骤被跳过
        # （未绑定/失败），不重建事实。
        persisted = snapshots.get("readiness", {}).get("status") if isinstance(snapshots.get("readiness"), dict) else ""
        if persisted != "snapshots_stored":
            return blocked(state, source_module="m5", tool=tool,
                           missing_fields=["ingest_m5_planning_snapshot 持久化回读"],
                           required_tool="ingest_m5_planning_snapshot",
                           recovery="请先完成六类 snapshot ingest 并回读成功，再求解")
        order = read_order(state)
        lines = read_lines(state)
        orders = []
        if lines:
            for index, line in enumerate(lines, start=1):
                orders.append({
                    "order_id": str(order.get("order_id") or ""),
                    "order_line_id": str(line.get("line_id") or f"{order.get('order_id')}::L{index}"),
                    "product_id": str(line.get("product_code") or line.get("model") or order.get("product_code") or ""),
                    "quantity": line.get("quantity", 0),
                    "due_time": str(line.get("due_date") or order.get("due_date") or ""),
                    "priority": request.get("priority", "normal"),
                    "status": "firm",
                })
        else:
            orders = [{"order_id": str(order.get("order_id") or ""), "product_id": order.get("product_code") or "", "quantity": order.get("quantity", 0), "due_time": str(order.get("due_date") or ""), "priority": request.get("priority", "normal"), "status": "firm"}]
        scenario_id = str(request.get("scenario_id") or f"scenario-{order.get('order_id', '')}")
        # 重新组装 pmc_v2_bundle（与 ingest 同源），交给 solve 的 v2 求解器；
        # 同时提供扁平 routing_steps/resources 以满足 solve 工具合同（仅作 schema 占位）。
        bundle, _ = _assemble_m5_bundle(state)
        if bundle is None:
            return blocked(state, source_module="m5", tool=tool,
                           missing_fields=["六类 snapshot 组装失败"], required_tool="ingest_m5_planning_snapshot")
        request_payload = _assembly_payloads_from_state(state)
        product_code = str(order.get("product_code") or "")
        routing_steps = []
        for step in request_payload.get("routing_steps") or []:
            if not isinstance(step, dict):
                continue
            op_id = str(step.get("operation_id") or step.get("op_code") or "")
            codes = [str(c) for c in (step.get("required_equipment_codes") or step.get("required_station_codes") or step.get("required_person_codes") or []) if c]
            # K4：工时口径 = 分钟（秒/字符串在 _standard_minutes_of 处显式换算），
            # 合同 processing_minutes 的 minimum=1 → 亚分钟工时兜底为 1 分钟
            # （0 会被 solve 合同校验拒绝）。
            minutes = _standard_minutes_of(step)
            processing_minutes = max(1, int(minutes if minutes is not None else 1))
            eligible_resources = step.get("eligible_resources")
            if not eligible_resources:
                eligible_resources = [{"resource_id": code, "processing_minutes": processing_minutes}
                                      for code in codes]
            if not eligible_resources:
                # K4（对齐 REALFLOW orchestration_bridge.py:1268-1273）：SOP 工序只带
                # 工位名（如「排卡」）时，用工位名作占位资源，满足 solve 工具合同的
                # minItems=1；实际求解以 pmc_v2_bundle 的路线为准。
                station_name = str(step.get("station") or step.get("station_code") or "").strip()
                if station_name:
                    eligible_resources = [{"resource_id": station_name,
                                           "processing_minutes": processing_minutes}]
            routing_steps.append({
                "product_id": str(step.get("product_id") or step.get("product_code") or product_code),
                "operation_id": op_id,
                "operation_name": str(step.get("operation_name") or step.get("name") or op_id),
                "sequence": step.get("sequence") or 1,
                "eligible_resources": eligible_resources,
            })
        # 求解器合同的扁平 resources 优先用 M0 回读事实；本地/fixture 无 M0 时
        # 回退到请求里显式的 resource_snapshot（与 _assemble_m5_bundle 同源）。
        resource_facts = read_m5_resource_facts(state) or request_payload.get("resource_snapshot") or {}
        resources = []
        for section, key in (("equipment", "equipment_code"), ("stations", "station_code"),
                             ("persons", "person_code"), ("tooling", "tooling_code")):
            for item in (resource_facts or {}).get(section, []):
                code = item.get(key)
                if code:
                    resources.append({"resource_id": str(code), "name": str(code), "status": "available"})
        return {
            "idempotency_key": f"{state.get('task_id', 'task')}:m5",
            "scenario_id": scenario_id,
            "scenario_purpose": str(request.get("scenario_purpose") or "production"),
            "planning_start": request.get("planning_start") or str(order.get("due_date") or ""),
            "orders": orders,
            "routing_steps": routing_steps,
            "resources": resources,
            "pmc_v2_bundle": bundle,
            "source_systems": ["manual"],
            "source_observed_at": request.get("source_observed_at") or {},
            "tracking_task_id": state.get("task_id", ""),
        }
    if tool == "get_m5_schedule":
        solve = output_data(state, "solve_scheduling")
        plan_version = solve.get("schedule", {}).get("plan_version") if isinstance(solve.get("schedule"), dict) else None
        if not plan_version:
            return blocked(state, source_module="m5", tool=tool,
                           missing_fields=["solve_scheduling 输出的 plan_version"],
                           required_tool="solve_scheduling")
        payload = {"plan_version": str(plan_version)}
        # 预览/本地模式：M5 repository 未持久化计划，读回直接从 RunState 的 solve
        # 输出提供 schedule + lifecycle，避免 PLAN_NOT_FOUND；不冒充已发布 canonical。
        if bool(request.get("legacy_preview")):
            schedule = solve.get("schedule") if isinstance(solve.get("schedule"), dict) else {}
            payload["preview"] = True
            payload["schedule"] = schedule
            payload["scenario_id"] = str(schedule.get("scenario_id") or solve.get("scenario_id") or "")
            payload["lifecycle_status"] = str(solve.get("lifecycle_status") or "released")
        return payload
    # ── M6 财务（F-008）：成本计算的输入全部来自**只读**事实（D9）────────────────
    if tool == "save_costing_snapshot":
        order = read_order(state)
        product_code = str(order.get("product_code") or request.get("product_code") or "")
        facts = _assemble_costing_facts(state, product_code)
        if facts is None:
            return blocked(state, source_module="m1", tool=tool,
                           missing_fields=["订单 header.product_code（或 request.product_code）"],
                           required_tool="ingest_document",
                           recovery="请先完成 M1 解析/复核或显式给出产品编码，成本对象不能缺")
        bom_lines = facts["bom_lines"]
        if not bom_lines:
            return blocked(state, source_module="m2", tool=tool,
                           missing_fields=["已批准 BOM 行"],
                           required_tool="run_bom_sop_workflow",
                           recovery="请先完成工程 Gate 批准 BOM（或显式给出 bom_lines）再算成本")
        period = str(request.get("period") or _period_of(order.get("due_date")) or "")
        if not period:
            return blocked(state, source_module="m6", tool=tool,
                           missing_fields=["request.period（账期 YYYY-MM）"],
                           required_tool="save_costing_snapshot",
                           recovery="成本快照必须落在明确账期上；请给出 period 或订单交期")
        payload = {
            **facts,
            "period": period,
            "order_id": str(order.get("order_id") or request.get("order_id") or ""),
            "batch_no": str(request.get("batch_no") or ""),
            "quantity": order.get("order_qty") or order.get("quantity"),
        }
        # R1a：M4 采购追踪的实际单价（缺价由 M6 标 cost_incomplete，不编造）
        payload.update(read_m4_tracking_price_facts(state))
        return payload
    if tool in ("confirm_costing_snapshot", "get_costing_snapshot"):
        # 快照号优先取上游 save_costing_snapshot 的产出（那条链的落地物）。
        saved = output_data(state, "save_costing_snapshot")
        snapshot_id = str(request.get("snapshot_id") or saved.get("snapshot_id") or "")
        if not snapshot_id:
            return blocked(state, source_module="m6", tool=tool,
                           missing_fields=["save_costing_snapshot.snapshot_id"],
                           required_tool="save_costing_snapshot",
                           recovery="请先算出并落一版试算快照，再确认/回读它")
        return {"snapshot_id": snapshot_id}
    if tool == "close_month_costing":
        saved = output_data(state, "save_costing_snapshot")
        period = str(request.get("period") or saved.get("period") or "")
        if not period:
            return blocked(state, source_module="m6", tool=tool,
                           missing_fields=["request.period（账期 YYYY-MM）"],
                           required_tool="save_costing_snapshot",
                           recovery="月结必须指定账期；请给出 period 或先落一版该期间快照")
        return {"period": period}
    if tool == "get_product_cost":
        order = read_order(state)
        product_code = str(request.get("product_code") or order.get("product_code") or "")
        facts = _assemble_costing_facts(state, product_code)
        if facts is None:
            return blocked(state, source_module="m1", tool=tool,
                           missing_fields=["product_code（订单 header 或显式参数）"],
                           required_tool="ingest_document",
                           recovery="成本对象不能缺；请给出产品编码或先完成 M1 解析")
        if not facts["bom_lines"]:
            return blocked(state, source_module="m2", tool=tool,
                           missing_fields=["已批准 BOM 行"],
                           required_tool="run_bom_sop_workflow",
                           recovery="请先完成工程 Gate 批准 BOM（或显式给出 bom_lines）再算成本")
        return facts
    if tool == "audit_order_cost":
        order = read_order(state)
        order_lines = request.get("order_lines")
        if not isinstance(order_lines, list) or not order_lines:
            order_lines = _order_lines_from_order(state, order)
        if not order_lines:
            return blocked(state, source_module="m1", tool=tool,
                           missing_fields=["订单行（product_code/qty/unit_price）"],
                           required_tool="ingest_document",
                           recovery="审计需要订单行事实（产品/数量/对客单价）；请先完成 M1 解析或显式给出 order_lines")
        products: dict[str, Any] = {}
        for code in dict.fromkeys(str(line.get("product_code") or "") for line in order_lines
                                  if isinstance(line, dict)):
            if not code:
                continue
            facts = _assemble_costing_facts(state, code)
            if facts is not None and facts["bom_lines"]:
                products[code] = facts
        payload: dict[str, Any] = {
            "order_id": str(order.get("order_id") or request.get("order_id") or ""),
            "order_lines": order_lines,
            "products": products,
        }
        for key in ("unit_costs", "min_margin_rate"):
            if request.get(key) not in (None, ""):
                payload[key] = request[key]
        return payload
    if tool == "allocate_expenses":
        expenses = request.get("expenses")
        basis_source = "explicit"
        if not isinstance(expenses, list) or not expenses:
            # 费用事实的唯一主 = M0 canonical（D4／老仓 D-020 = v2 D-009）；装配层读、并在此拆信封
            expenses = _flatten_entities(_read_m0_entities(state, "expense"))
            basis_source = "canonical" if expenses else "missing"
        basis_rows = request.get("basis_rows")
        if not isinstance(basis_rows, list) or not basis_rows:
            basis_rows = _basis_rows_from_reports(state) or _basis_rows_from_order_lines(state)
        payload = {
            "expenses": expenses,
            "basis_rows": basis_rows if isinstance(basis_rows, list) else [],
            "basis_source": basis_source,
        }
        for key in ("period", "allocation_basis"):
            if request.get(key) not in (None, ""):
                payload[key] = request[key]
        return payload
    # ── M6 单据台账（B2）：报价单 / 对账单 ──────────────────────────────────
    if tool in ("generate_quotation", "save_quotation"):
        payload = _quotation_facts(state)
        if not payload["lines"]:
            return blocked(state, source_module="m1", tool=tool,
                           missing_fields=["报价行（request.lines 或订单行）"],
                           required_tool="ingest_document",
                           recovery="报价需要产品与数量：请给出 lines，或先完成 M1 解析得到订单行")
        return payload
    if tool == "save_statement":
        counterparty = str(request.get("counterparty_code") or request.get("customer_code")
                           or request.get("supplier_code") or "")
        if not counterparty:
            return blocked(state, source_module="m6", tool=tool,
                           missing_fields=["counterparty_code（对账对象）"],
                           required_tool="save_statement",
                           recovery="对账单必须指定往来单位；请给出 counterparty_code")
        basis = _statement_basis(state, request, counterparty)
        has_basis = any(isinstance(value, list) and value for value in basis.values())
        has_detail = (isinstance(request.get("transactions"), list) and request["transactions"]) \
            or (isinstance(request.get("lines"), list) and request["lines"]) or has_basis
        if not has_detail:
            return blocked(state, source_module="m6", tool=tool,
                           missing_fields=["对账明细（transactions/lines 或可用的送货单/入库依据）"],
                           required_tool="save_statement",
                           recovery="对账单必须有对账依据：请给出 transactions，"
                                    "或先在 canonical 落该往来单位的送货单（客户侧）/"
                                    "M4 入库记录（供应商侧）")
        payload = {"counterparty_code": counterparty, **basis}
        for key in ("doc_no", "statement_type", "direction", "doc_date", "opening_balance",
                    "transactions", "lines", "amount", "source_ref"):
            if request.get(key) not in (None, "", [], {}):
                payload[key] = request[key]
        return payload
    # ── M6 凭据（B3）：送货单读取 + 对账明细生成 ────────────────────────────
    if tool == "get_delivery_note":
        note_no = str(request.get("note_no") or "")
        if not note_no:
            return blocked(state, source_module="m6", tool=tool,
                           missing_fields=["note_no（送货单号）"],
                           required_tool="get_delivery_note",
                           recovery="按单号读回送货单；请给出 note_no")
        return {"note_no": note_no,
                "delivery_notes": delivery_note_bodies(state,
                                                       str(request.get("counterparty_code") or ""))}
    if tool == "generate_statement":
        payload = dict(_statement_basis(state, request,
                                        str(request.get("counterparty_code")
                                            or request.get("supplier_code") or "")))
        for key in ("statement_type", "counterparty_code", "supplier_code", "period",
                    "date_from", "date_to", "opening_balance"):
            if request.get(key) not in (None, ""):
                payload[key] = request[key]
        return payload
    # ── M6 库存财务视图（B5）：库存四态 + 在途（M4B 追踪） ──────────────────
    if tool == "get_inventory_finance_view":
        payload = {
            "inventory": read_inventory_facts(state),
            "purchase_tracking_rows": read_m4_tracking_rows(state),
        }
        # unit_costs（材料编码→单价）：v2 的 inventory 无价格字段，金额只能外部带入；
        # 不给即由内核标 cost_incomplete（不编造成本）。
        for key in ("unit_costs", "valuation_price_source"):
            if request.get(key) not in (None, ""):
                payload[key] = request[key]
        return payload
    # ── M6 订单列表（B6）：canonical `order`（已拆信封） ─────────────────────
    if tool == "list_orders":
        payload = {"orders": _flatten_entities(_read_m0_entities(state, "order"))}
        for key in ("product_code", "customer_name", "period", "limit"):
            if request.get(key) not in (None, ""):
                payload[key] = request[key]
        return payload
    # ── M6 工资两件（B6）：**v2 没有 canonical 工资事实面**，只能显式给 ──────────
    #
    # 老仓的四类工资事实分别取自 canonical `usage_log`（报工）/ `piece_rate`（计件单价）/
    # `salary_standard`（月薪）/ `attendance_summary`（考勤），且 `_m6_report_events_from_usage_logs`
    # 明确禁止把 `quantity` 当报工数量（那是**资产使用数量**）。**v2 这四张面一张都没有**
    # （`canonical_schema` 与 `ENTITY_TYPES` 实测 0 命中）——所以这里不做任何"拿别的实体顶替"
    # 的推断，四类事实一律只从调用方取；不给即由内核标 `missing`（fail-closed，不编造）。
    # 要让它们有 canonical 来源，需按 B0b 建 `expense`/`delivery_note` 的先例另立实体 + facade
    # （见交接"遗留"）。
    if tool == "calculate_piece_pay":
        # 只放**非空列表**键：契约里 `report_events`/`piece_rates` 声明为 array，
        # 放 `None` 会被 `registry.call` 的 input_schema 校验直接拒掉（装配产出必须
        # 能通过契约校验，否则工具永远调不起来）。缺键时内核按"没有事实"处理 → missing。
        payload: dict[str, Any] = {}
        for key in ("report_events", "piece_rates"):
            if isinstance(request.get(key), list) and request[key]:
                payload[key] = request[key]
        return payload
    if tool == "calculate_monthly_pay":
        return {key: request.get(key)
                for key in ("salary_standards", "attendance", "piece_pay",
                            "overtime_multiplier", "work_days", "hours_per_day")
                if request.get(key) not in (None, "")}
    return {}
