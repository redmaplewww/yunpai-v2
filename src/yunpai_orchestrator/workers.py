from __future__ import annotations

import base64
import json
import os
from io import BytesIO
from hashlib import sha256
from math import ceil
from typing import Any


def _trace(ctx: dict[str, Any], suffix: str) -> str:
    return f"{ctx.get('task_id', 'local')}:{suffix}"


def _evidence(module: str, ref: str, detail: str) -> dict[str, Any]:
    return {"module": module, "source_ref": ref, "evidence_ref": f"{module}:{ref}", "detail": detail}


def _decode_file(file_value: Any) -> tuple[str, bytes]:
    if isinstance(file_value, str):
        return "document.bin", base64.b64decode(file_value)
    if not isinstance(file_value, dict):
        raise ValueError("file must be a base64 string or file object")
    return str(file_value.get("filename") or "document.bin"), base64.b64decode(str(file_value.get("content_b64") or ""))


def _json_content(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _number(value: Any, default: float = 0.0) -> float:
    """Normalize nullable spreadsheet/provider numbers without masking bad types."""
    if value is None or value == "":
        return default
    return float(value)


def _m0_store(ctx: dict[str, Any]):
    import os

    # M0 分片 C1：配置 YUNPAI_M0_DB 时走进程内 canonical 写面（m0_import_store），
    # 否则维持 M0SandboxStore 沙箱语义（不隐式读 runtime 大库）。
    db_path = os.getenv("YUNPAI_M0_DB")
    if db_path:
        from .m0_import_store import CanonicalImportStore

        return CanonicalImportStore(db_path)
    from .m0_sandbox import M0SandboxStore

    sandbox_db = ctx.get("m0_sandbox_db") or os.getenv("YUNPAI_M0_SANDBOX_DB") or "runtime/yunpai-m0-sandbox.sqlite"
    return M0SandboxStore(sandbox_db)


def _extract_uploaded_bom(files: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse uploaded BOM workbooks into M2 lines with source evidence.

    M2 receives bytes only after a Gate retry, so this deterministic parser is
    deliberately independent of the LLM and preserves workbook coordinates.
    """
    if not isinstance(files, list):
        return [], []
    try:
        from openpyxl import load_workbook
    except ImportError:
        return [], [{"code": "PARSER_UNAVAILABLE", "message": "缺少 openpyxl，无法解析 BOM XLSX"}]
    aliases = {
        "material_code": ("物料编码", "料号", "物料编号", "材料编码", "编码"),
        "material_name": ("材料名称", "原材料名称", "物料名称", "品名", "名称"),
        "specification": ("规格", "规格型号", "型号"),
        "quantity": ("用量", "数量", "用量/装箱数量", "单机用量"),
        "unit": ("单位",),
        "supplier": ("供应商",),
    }
    lines: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for file_value in files:
        filename, raw = _decode_file(file_value)
        if not filename.lower().endswith(".xlsx"):
            issues.append({"code": "UNSUPPORTED_BOM_FILE", "filename": filename, "message": "BOM 上传当前需要 XLSX"})
            continue
        try:
            workbook = load_workbook(BytesIO(raw), read_only=True, data_only=True)
            for sheet in workbook.worksheets:
                rows = list(sheet.iter_rows(values_only=True))
                header = None
                mapping: dict[str, int] = {}
                for row_index, row in enumerate(rows[:30], start=1):
                    candidate = {
                        key: index
                        for key, alias_list in aliases.items()
                        for index, value in enumerate(row)
                        if any(alias in str(value or "").replace(" ", "") for alias in alias_list)
                    }
                    if "material_code" in candidate and ("material_name" in candidate or "quantity" in candidate):
                        header, mapping = row_index, candidate
                        break
                if header is None:
                    continue
                for row_index, row in enumerate(rows[header:], start=header + 1):
                    code_index = mapping.get("material_code")
                    code = row[code_index] if code_index is not None and code_index < len(row) else None
                    if code in (None, ""):
                        continue
                    line = {"material_code": str(code).strip(), "source_file": filename, "source_sheet": sheet.title, "source_row": row_index}
                    for key, index in mapping.items():
                        if index < len(row) and row[index] not in (None, ""):
                            line[key] = row[index]
                    if "quantity" not in line:
                        issues.append({"code": "MISSING_BOM_QUANTITY", "filename": filename, "sheet": sheet.title, "row": row_index, "message": "BOM 行缺少用量"})
                    lines.append(line)
            workbook.close()
        except Exception as exc:
            issues.append({"code": "BOM_PARSE_FAILED", "filename": filename, "message": str(exc)})
    return lines, issues


async def m0_import(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from .file_sniff import sniff_documents

    files = payload.get("files") or []
    items = [item for item in files if isinstance(item, dict)]
    store = _m0_store(ctx)
    if getattr(store, "canonical", False):
        # C1 canonical 模式：全部文件交给 store 统一处理——结构化 JSON → canonical 候选；
        # 其余 → m0_quarantines 落库并随批次返回（不伪造候选）。
        registered = store.register_batch(
            task_id=str(ctx.get("task_id") or "local"),
            tenant_id=str(ctx.get("tenant_id") or "default"),
            files=items,
            batch_id=payload.get("batch_id"),
        )
        batch_id = registered["batch_id"]
        preview_documents = store.preview(batch_id).get("documents", [])
        quarantined = registered.get("quarantined") or []
        if not preview_documents:
            return {"id": batch_id, "batch_id": batch_id, "status": "failed",
                    "candidates": [], "quarantined": quarantined,
                    "canonical": True, "environment": "local_canonical",
                    "readback": {"available": False,
                                 "detail": "没有可登记的结构化 records；原始业务文件解析由识别/解析链提供，不伪造候选"},
                    "evidence": []}
        return {
            "id": batch_id, "batch_id": batch_id,
            "status": "awaiting_review",
            "candidates": [
                {
                    "candidate_id": doc["candidate_id"], "filename": doc["filename"], "sha256": doc["sha256"],
                    "status": doc["review_status"], "document_kind": doc["document_kind"],
                    "records": doc.get("payload_json") if isinstance(doc.get("payload_json"), list) else [],
                    "evidence": [_evidence("m0", doc["filename"], "canonical 候选登记哈希")],
                }
                for doc in preview_documents
            ],
            "quarantined": quarantined,
            "provider": "local_canonical",
            "canonical": True,
            "transport": "local",
            "environment": "local_canonical",
            "readback": {"available": False,
                         "detail": "候选待人工裁决并 commit 后提供 canonical 回读"},
            "evidence": [_evidence("m0", "import", f"{len(preview_documents)} canonical candidates registered")],
        }

    # ---- 沙箱默认路径（保持原行为） ----
    sniffed = sniff_documents(items)
    accepted = [item for item in sniffed if item.get("status") == "accepted"]
    skipped = [item for item in sniffed if item.get("status") != "accepted"]
    encoded_by_name = {str(item.get("filename")): item for item in items}
    registered = store.register_batch(
        task_id=str(ctx.get("task_id") or "local"),
        tenant_id=str(ctx.get("tenant_id") or "default"),
        files=[{**encoded_by_name.get(item.get("filename"), {}), "filename": item.get("filename")} for item in accepted],
        batch_id=payload.get("batch_id"),
    )
    batch_id = registered["batch_id"]
    quarantined = [{"filename": item.get("filename"), "reason": item.get("reason") or "unsupported_or_invalid"} for item in skipped]
    preview_documents = store.preview(batch_id).get("documents", [])
    if not preview_documents and not quarantined:
        return {"id": batch_id, "batch_id": batch_id, "status": "failed", "candidates": [], "quarantined": [], "environment": "sandbox", "canonical": False, "readback": {"available": False, "detail": "没有可登记文件"}, "evidence": []}
    return {
        "id": batch_id, "batch_id": batch_id,
        "status": "awaiting_review",
        "candidates": [
            {
                "candidate_id": doc["candidate_id"], "filename": doc["filename"], "sha256": doc["sha256"],
                "status": doc["review_status"], "document_kind": doc["document_kind"],
                "records": doc.get("payload_json") if isinstance(doc.get("payload_json"), list) else [],
                "evidence": [_evidence("m0", doc["filename"], "sandbox 候选登记哈希")],
            }
            for doc in preview_documents
        ],
        "quarantined": quarantined,
        # 本地 sandbox 语义：候选不是 M0 canonical；生产发布需 HTTP transport + 真实 M0 回读。
        "provider": "local_fixture",
        "canonical": False,
        "transport": "local",
        "environment": "sandbox",
        "readback": {"available": False, "detail": "本地 sandbox 只登记候选，未发布 canonical；需要 YUNPAI_TOOL_TRANSPORT=http 与真实 M0 base URL/审核授权"},
        "evidence": [_evidence("m0", "import", f"{len(preview_documents)} candidates registered in sandbox (non-canonical)")],
    }


async def m0_status(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    result = _m0_store(ctx).status(str(payload.get("batch_id") or ""))
    if result is None:
        raise ValueError(f"batch not found: {payload.get('batch_id')}")
    return result


async def m0_preview(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    return _m0_store(ctx).preview(str(payload.get("batch_id") or ""))


async def m0_resolve(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    batch_id = str(payload.get("batch_id") or "")
    kind = str(payload.get("kind") or "")
    action = str(payload.get("action") or "")
    raw_id = payload.get("id")
    candidate_id = str(payload.get("candidate_id") or "")
    if kind not in {"entity", "mapping", "candidate"}:
        raise ValueError("resolve kind 必须为 entity|mapping|candidate")
    if action not in {"approve", "reject"}:
        raise ValueError("resolve action 必须为 approve|reject")
    store = _m0_store(ctx)
    try:
        if candidate_id:
            return store.resolve(batch_id=batch_id, candidate_id=candidate_id, action=action, actor=str(ctx.get("actor") or "operator"))
        resolve_id = int(raw_id) if str(raw_id).strip().isdigit() else None
        if resolve_id is None:
            raise ValueError("resolve 需要显式 id(候选序号) 或 candidate_id")
        return store.resolve(batch_id=batch_id, resolve_id=resolve_id, action=action, actor=str(ctx.get("actor") or "operator"))
    except ValueError as exc:
        if "already decided" in str(exc):
            raise
        raise ValueError(str(exc)) from exc


async def m0_commit(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    batch_id = str(payload.get("batch_id") or "")
    store = _m0_store(ctx)
    status_result = store.status(batch_id)
    if status_result is None:
        raise ValueError(f"batch not found: {batch_id}")
    require_resolved = str(payload.get("require_resolved") or "").lower() in {"true", "1", "yes"}
    pending = store.pending_count(batch_id)
    if require_resolved and pending > 0:
        return {
            "status": "blocked", "code": "BLOCKED_INPUT",
            "errors": [{"code": "PENDING_REVIEW", "message": f"batch {batch_id} 仍有 {pending} 个候选未裁决，禁止 commit", "details": []}],
            "batch_id": batch_id,
            # 本地 sandbox 语义：绝不表述为已发布 canonical。
            "provider": "local_fixture", "canonical": False, "transport": "local", "environment": "sandbox",
            "readback": {"available": False, "detail": "未完成人工裁决，未发布 canonical"},
            "evidence": [_evidence("m0", batch_id, "commit 被拒：存在未裁决候选")],
        }
    if getattr(store, "canonical", False):
        # C1 canonical 模式：commit = 发布已 approve 候选到 canonical（只发已批、可回读）。
        # canonical 语义要求发布前零未裁决（未裁决候选不允许与已发布批次悬空并存）。
        if pending > 0:
            return {
                "status": "blocked", "code": "BLOCKED_INPUT",
                "errors": [{"code": "PENDING_REVIEW", "message": f"batch {batch_id} 仍有 {pending} 个候选未裁决，禁止 commit", "details": []}],
                "batch_id": batch_id,
                "canonical": True, "environment": "local_canonical",
                "readback": {"available": False, "detail": "未完成人工裁决，未发布 canonical"},
                "evidence": [_evidence("m0", batch_id, "commit 被拒：存在未裁决候选")],
            }
        publish = store.publish(
            batch_id,
            actor=str(ctx.get("actor") or payload.get("approved_by") or "operator"),
            human_override=bool(payload.get("human_override")),
        )
        if publish.get("status") == "no_approved_candidates":
            return {
                "status": "blocked", "code": "BLOCKED_INPUT",
                "errors": [{"code": "NO_APPROVED_CANDIDATES", "message": f"batch {batch_id} 无已批准候选，禁止发布（存在未裁决/全部驳回）", "details": []}],
                "batch_id": batch_id, "canonical": True, "environment": "local_canonical",
                "readback": {"available": False, "detail": "无已批准候选"},
                "evidence": [_evidence("m0", batch_id, "commit 被拒：无已批准候选")],
            }
        if publish.get("status") == "already_published":
            return {
                "status": "already_published", "duplicate": True, "batch_id": batch_id,
                "canonical": True, "environment": "local_canonical",
                "readback": {"available": True,
                             "approved_candidates": publish.get("approved_candidates", 0),
                             "ledger_count": publish.get("ledger_count", 0),
                             "outbox_count": publish.get("outbox_count", 0),
                             "detail": "重复 commit：批次已发布，返回既有回读"},
                "evidence": [_evidence("m0", batch_id, "duplicate commit")],
            }
        return {
            "status": "published",
            "batch_id": batch_id,
            "provider": "local_canonical",
            "canonical": True,
            "transport": "local",
            "environment": "local_canonical",
            "revision": str(publish.get("approved_candidates", 0)),
            "ledger_id": batch_id,
            "master_counts": {},
            "pending_review_before_commit": 0,
            "readback": {
                "available": True,
                "approved_candidates": publish.get("approved_candidates", 0),
                "ledger_count": publish.get("ledger_count", 0),
                "outbox_count": publish.get("outbox_count", 0),
                "detail": "canonical 发布完成（进程内 local_canonical store）",
            },
            "evidence": [_evidence("m0", batch_id, "published canonical entities with ledger/outbox readback")],
        }
    return {
        # 任务书 §1.4：data_import_commit=committed 只有在 canonical entity/version、
        # ledger、outbox 可回读时才成立。本地 sandbox 无真实 M0 表，故只记录意图，
        # 状态显式标记 fixture_recorded，不得表述为已发布 canonical。
        "status": "fixture_recorded",
        "batch_id": batch_id,
        "provider": "local_fixture",
        "canonical": False,
        "transport": "local",
        "environment": "sandbox",
        "revision": "",
        "ledger_id": "",
        "master_counts": {},
        "pending_review_before_commit": pending if require_resolved else 0,
        "readback": {
            "available": False,
            "detail": "local transport 无 M0 canonical 表与回读接口；真实发布需部署方提供 M0 URL、PostgreSQL schema/权限、审核授权和写入回读接口",
        },
        "evidence": [_evidence("m0", batch_id or "fixture", "本地 sandbox 记录发布意图；未发布 canonical、无 ledger/outbox 回读，需人工 Gate 后才可对接真实 M0")],
    }


async def m0_history(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """批次历史（canonical 模式）。"""
    store = _m0_store(ctx)
    if not getattr(store, "canonical", False):
        raise ValueError("data_import_history 需要 YUNPAI_M0_DB canonical 库；sandbox 无历史语义")
    return store.history(tenant_id=str(ctx.get("tenant_id") or "default"),
                         limit=int(payload.get("limit") or 100))


async def m0_quarantine_list(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """隔离区文件列表（canonical 模式）。"""
    store = _m0_store(ctx)
    if not getattr(store, "canonical", False):
        raise ValueError("data_import_quarantine 需要 YUNPAI_M0_DB canonical 库；sandbox 无隔离语义")
    # R1-REQ-2：store 已支持按批次过滤，契约已回补 batch_id。
    return store.list_quarantine(tenant_id=str(ctx.get("tenant_id") or "default"),
                                 limit=int(payload.get("limit") or 100),
                                 batch_id=str(payload.get("batch_id") or "") or None)


async def m0_rollback(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """按 ledger 回滚已发布批次（canonical 模式；证据保留）。"""
    store = _m0_store(ctx)
    if not getattr(store, "canonical", False):
        raise ValueError("data_import_rollback 需要 YUNPAI_M0_DB canonical 库；sandbox 无回滚语义")
    batch_id = str(payload.get("batch_id") or "")
    if not batch_id:
        raise ValueError("data_import_rollback 需要 batch_id")
    return store.rollback(batch_id, actor=str(ctx.get("actor") or "operator"))


def _require_canonical_store(store: Any, tool: str) -> None:
    if not getattr(store, "canonical", False):
        raise ValueError(f"{tool} 需要 YUNPAI_M0_DB canonical 库；sandbox 无 canonical 读语义")


def _catalog_svc(ctx: dict[str, Any]):
    """catalog 语义层（canonical-only）。"""
    import os

    from .m0_catalog_ingest import CatalogService

    db_path = os.getenv("YUNPAI_M0_DB")
    if not db_path:
        raise ValueError("data_catalog_* / m0_*_import 需要 YUNPAI_M0_DB canonical 库；未配置时不做 catalog 语义（不伪造成功）")
    return CatalogService(db_path)


async def m0_read_entities(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from .m0_facts import list_entities as _m0_list_entities

    store = _m0_store(ctx)
    _require_canonical_store(store, "list_m0_entities")
    entity_type = str(payload.get("entity_type") or "")
    if not entity_type:
        raise ValueError("list_m0_entities 需要 entity_type")
    tenant = str(payload.get("tenant_id") or ctx.get("tenant_id") or "default")
    rows = _m0_list_entities(entity_type, tenant_id=tenant)
    return {"success": True, "data": {"entity_type": entity_type, "tenant_id": tenant,
                                      "count": len(rows), "entities": rows}}


async def m0_read_inventory(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from .m0_facts import inventory_rows

    store = _m0_store(ctx)
    _require_canonical_store(store, "list_m0_inventory")
    tenant = str(ctx.get("tenant_id") or "default")
    rows = inventory_rows(tenant_id=tenant,
                          material_code=str(payload.get("material_code") or "") or None,
                          limit=int(payload.get("limit") or 0) or None)
    return {"success": True, "data": {"tenant_id": tenant, "count": len(rows), "inventory": rows}}


async def m0_read_documents(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from .m0_facts import document_rows

    store = _m0_store(ctx)
    _require_canonical_store(store, "list_m0_documents")
    tenant = str(ctx.get("tenant_id") or "default")
    rows = document_rows(tenant_id=tenant,
                         doc_type=str(payload.get("doc_type") or "") or None,
                         product_code=str(payload.get("product") or payload.get("material") or "") or None,
                         limit=int(payload.get("limit") or 0) or None)
    return {"success": True, "data": {"tenant_id": tenant, "count": len(rows), "documents": rows}}


def _canonical_bodies(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 canonical 实体行归一成「业务字段在顶层」（含 ``business_key``）。

    为什么需要：``m0_facts.list_entities`` 的形状是
    ``{canonical_key, **envelope}``——业务体嵌套在 ``payload`` 里，**直接按业务字段过滤
    永远匹配不到**（B0b 的两个读工具因此恒返回空，由一个 canonical 导入→回读往返测试
    抓出）。信封→业务体的拆解单点收在 ``fact_gateway``（裁决 R2），此处只调用它。
    """
    from .fact_gateway import split_envelope

    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        body, identity = split_envelope(row)
        out.append({"canonical_key": row.get("canonical_key") or "",
                    "business_key": str(identity.get("business_key") or ""),
                    **body})
    return out


async def m0_read_expenses(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """list_expenses（F-008）：M0 canonical 费用支出事实（只读）。

    M6 的 `allocate_expenses` 消费 category/amount/period/allocation_basis；
    可按 category / period 精确过滤（省略即全量，受 limit 截断）。
    """
    from .m0_facts import list_entities

    store = _m0_store(ctx)
    _require_canonical_store(store, "list_expenses")
    tenant = str(ctx.get("tenant_id") or "default")
    rows = _canonical_bodies(list_entities("expense", tenant_id=tenant))
    category = str(payload.get("category") or "")
    period = str(payload.get("period") or "")
    if category:
        rows = [row for row in rows if str(row.get("category") or "") == category]
    if period:
        rows = [row for row in rows if str(row.get("period") or "") == period]
    limit = int(payload.get("limit") or 0)
    if limit > 0:
        rows = rows[:limit]
    return {"success": True, "data": {"tenant_id": tenant, "count": len(rows), "expenses": rows}}


async def m0_read_delivery_notes(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """list_delivery_notes（F-008）：送货单事实（对账依据，只读）。

    canonical `delivery_note` 为唯一主（D4／老仓 D-020 = v2 D-009），M6 不建 create；可按
    counterparty_code / ref_order_id 精确过滤。
    """
    from .m0_facts import list_entities

    store = _m0_store(ctx)
    _require_canonical_store(store, "list_delivery_notes")
    tenant = str(ctx.get("tenant_id") or "default")
    rows = _canonical_bodies(list_entities("delivery_note", tenant_id=tenant))
    counterparty = str(payload.get("counterparty_code") or "")
    ref_order = str(payload.get("ref_order_id") or "")
    if counterparty:
        rows = [row for row in rows if str(row.get("counterparty_code") or "") == counterparty]
    if ref_order:
        rows = [row for row in rows if str(row.get("ref_order_id") or "") == ref_order]
    limit = int(payload.get("limit") or 0)
    if limit > 0:
        rows = rows[:limit]
    return {"success": True, "data": {"tenant_id": tenant, "count": len(rows), "delivery_notes": rows}}


async def m0_product_overview(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from .m0_facts import product_overview as _overview

    store = _m0_store(ctx)
    _require_canonical_store(store, "get_m0_product_overview")
    product_code = str(payload.get("product_code") or "")
    if not product_code:
        raise ValueError("get_m0_product_overview 需要 product_code")
    tenant = str(ctx.get("tenant_id") or "default")
    return {"success": True, "data": _overview(product_code, tenant_id=tenant)}


async def m0_product_graph(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from .m0_facts import product_graph as _graph

    store = _m0_store(ctx)
    _require_canonical_store(store, "get_m0_product_graph")
    product_code = str(payload.get("product_code") or "")
    if not product_code:
        raise ValueError("get_m0_product_graph 需要 product_code")
    tenant = str(ctx.get("tenant_id") or "default")
    depth = int(payload.get("depth") or 2)
    return {"success": True, "data": _graph(product_code, tenant_id=tenant, depth=depth)}


async def catalog_ingest_validate(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """data_catalog_ingest_validate：m0.ingest.v1 dry-run（canonical-only，无写）。"""
    records = payload.get("records") or []
    if not isinstance(records, list) or not records:
        raise ValueError("data_catalog_ingest_validate 需要 records 数组（≥1）")
    if len(records) > 5000:
        raise ValueError("records 超过上限 5000")
    svc = _catalog_svc(ctx)
    report = svc.validate_records(
        records, tenant_id=str(ctx.get("tenant_id") or "default"),
        task_id=str(ctx.get("task_id") or ""),
        actor=str(ctx.get("actor") or "operator"))
    return {"success": True, "data": report, "errors": [],
            "trace_id": _trace(ctx, "data_catalog_ingest_validate"),
            "evidence": [_evidence("m0", "catalog", f"dry-run records={report['summary']['records']} errors={report['summary']['errors']}")]}


async def catalog_ingest_publish(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """data_catalog_ingest_publish：approved 记录原子发布（canonical-only）。"""
    records = payload.get("records") or []
    if not isinstance(records, list) or not records:
        raise ValueError("data_catalog_ingest_publish 需要 records 数组（≥1）")
    if len(records) > 5000:
        raise ValueError("records 超过上限 5000")
    svc = _catalog_svc(ctx)
    try:
        data = svc.publish_records(
            records, tenant_id=str(ctx.get("tenant_id") or "default"),
            task_id=str(ctx.get("task_id") or ""),
            actor=str(ctx.get("actor") or "operator"))
    except ValueError as exc:
        from .m0_catalog_ingest import CatalogValidationError

        if isinstance(exc, CatalogValidationError):
            return {"success": False, "code": exc.code,
                    "errors": [{"code": exc.code, "message": str(exc), "details": []}],
                    "data": exc.report, "trace_id": _trace(ctx, "data_catalog_ingest_publish"),
                    "evidence": [_evidence("m0", "catalog", f"publish rejected: {exc.code}")]}
        raise
    return {"success": True, "data": data, "errors": [],
            "trace_id": _trace(ctx, "data_catalog_ingest_publish"),
            "evidence": [_evidence("m0", "catalog",
                                   f"published={data.get('published')} duplicates={data.get('duplicates')}")]}


async def catalog_document_candidate_validate(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """data_catalog_document_candidate_validate：候选 dry-run（canonical-only，无写）。"""
    from .m0_catalog_ingest import adapt_document_candidates, document_relation_issues, merge_relation_issues

    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("data_catalog_document_candidate_validate 需要 candidates 数组（≥1）")
    if len(candidates) > 5000:
        raise ValueError("candidates 超过上限 5000")
    tenant = str(ctx.get("tenant_id") or "default")
    svc = _catalog_svc(ctx)
    adaptation = adapt_document_candidates(candidates, tenant_id=tenant,
                                           actor=str(ctx.get("actor") or "operator"))
    validation = None
    if adaptation["valid"]:
        report = svc.validate_records(
            adaptation["records"], tenant_id=tenant,
            task_id=str(ctx.get("task_id") or ""),
            actor=str(ctx.get("actor") or "operator"))
        rel_issues = document_relation_issues(adaptation["records"], tenant_id=tenant,
                                              lookup=svc._entity_lookup)
        validation = merge_relation_issues(report, rel_issues)
    data = {"adaptation": adaptation, "validation": validation,
            "publishable": bool(validation and validation["publishable"])}
    return {"success": True, "data": data, "errors": [],
            "trace_id": _trace(ctx, "data_catalog_document_candidate_validate"),
            "evidence": [_evidence("m0", "catalog",
                                   f"doc-candidates dry-run adapted={len(adaptation['records'])} errors={adaptation['summary']['errors']}")]}


async def catalog_document_candidate_publish(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """data_catalog_document_candidate_publish：approved 候选 → document 实体+产品文档边。"""
    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("data_catalog_document_candidate_publish 需要 candidates 数组（≥1）")
    if len(candidates) > 5000:
        raise ValueError("candidates 超过上限 5000")
    svc = _catalog_svc(ctx)
    try:
        data = svc.publish_documents(
            candidates, tenant_id=str(ctx.get("tenant_id") or "default"),
            task_id=str(ctx.get("task_id") or ""),
            actor=str(ctx.get("actor") or "operator"))
    except ValueError as exc:
        from .m0_catalog_ingest import CatalogValidationError

        if isinstance(exc, CatalogValidationError):
            return {"success": False, "code": exc.code,
                    "errors": [{"code": exc.code, "message": str(exc), "details": []}],
                    "data": exc.report, "trace_id": _trace(ctx, "data_catalog_document_candidate_publish"),
                    "evidence": [_evidence("m0", "catalog", f"doc-candidates publish rejected: {exc.code}")]}
        raise
    return {"success": True, "data": data, "errors": [],
            "trace_id": _trace(ctx, "data_catalog_document_candidate_publish"),
            "evidence": [_evidence("m0", "catalog",
                                   f"doc published={data['publication'].get('published')} duplicates={data['publication'].get('duplicates')}")]}


def _file_inputs(payload: dict[str, Any]) -> tuple[str, bytes, str, str, str, str]:
    """file 端点公共参数抽取：file{filename,content_b64} + 模板/来源/审核。"""
    import base64 as _b64

    file = payload.get("file") or {}
    if not isinstance(file, dict) or not file.get("filename") or not file.get("content_b64"):
        raise ValueError("file{filename, content_b64} 必填")
    try:
        raw = _b64.b64decode(str(file.get("content_b64")), validate=True)
    except ValueError as exc:
        raise ValueError(f"file.content_b64 非法 base64: {exc}") from exc
    template_version = str(payload.get("template_version") or "")
    source_system = str(payload.get("source_system") or "")
    source_external_id = str(payload.get("source_external_id") or "")
    review_status = str(payload.get("review_status") or "candidate")
    return (str(file.get("filename")), raw, template_version, source_system,
            source_external_id, review_status)


async def catalog_file_validate(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """data_catalog_file_validate：版本化 CSV/Excel 模板 dry-run（canonical-only，无写）。"""
    from .m0_catalog_ingest import merge_relation_issues, product_dependency_issues
    from .m0_catalog_templates import adapt_tabular_file

    filename, raw, template_version, source_system, source_external_id, review_status = _file_inputs(payload)
    tenant = str(ctx.get("tenant_id") or "default")
    actor = str(ctx.get("actor") or "operator")
    svc = _catalog_svc(ctx)
    adaptation = adapt_tabular_file(
        filename=filename, data=raw, template_version=template_version,
        tenant_id=tenant, source_system=source_system, source_external_id=source_external_id,
        review_status=review_status, reviewed_by=actor if review_status in ("approved", "rejected") else "")
    validation = None
    if adaptation["valid"]:
        report = svc.validate_records(adaptation["records"], tenant_id=tenant,
                                      task_id=str(ctx.get("task_id") or ""), actor=actor)
        rel = product_dependency_issues(adaptation["records"], tenant_id=tenant,
                                        lookup=svc._entity_lookup)
        validation = merge_relation_issues(report, rel)
    data = {"adaptation": adaptation, "validation": validation,
            "publishable": bool(validation and validation["publishable"])}
    return {"success": True, "data": data, "errors": [],
            "trace_id": _trace(ctx, "data_catalog_file_validate"),
            "evidence": [_evidence("m0", "catalog",
                                   f"file dry-run adapted={adaptation['summary']['records']} errors={adaptation['summary']['errors']}")]}


async def catalog_file_publish(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """data_catalog_file_publish：approved 模板文件 → 发布（计数在 data.publication）。"""
    from .m0_catalog_ingest import merge_relation_issues, product_dependency_issues
    from .m0_catalog_templates import adapt_tabular_file

    filename, raw, template_version, source_system, source_external_id, review_status = _file_inputs(payload)
    if review_status != "approved":
        return {"success": False, "code": "REVIEW_REQUIRED",
                "errors": [{"code": "REVIEW_REQUIRED",
                            "message": "data_catalog_file_publish 需要 review_status=approved", "details": []}],
                "data": {}, "trace_id": _trace(ctx, "data_catalog_file_publish"),
                "evidence": [_evidence("m0", "catalog", "file publish rejected: review_status != approved")]}
    tenant = str(ctx.get("tenant_id") or "default")
    actor = str(ctx.get("actor") or "operator")
    svc = _catalog_svc(ctx)
    adaptation = adapt_tabular_file(
        filename=filename, data=raw, template_version=template_version,
        tenant_id=tenant, source_system=source_system, source_external_id=source_external_id,
        review_status="approved", reviewed_by=actor)
    if not adaptation["valid"]:
        return {"success": False, "code": "VALIDATION_FAILED",
                "errors": [{"code": "VALIDATION_FAILED",
                            "message": "模板适配失败（见 data.adaptation.issues）", "details": []}],
                "data": {"adaptation": adaptation, "validation": None, "publishable": False},
                "trace_id": _trace(ctx, "data_catalog_file_publish"),
                "evidence": [_evidence("m0", "catalog", "file publish rejected: adaptation invalid")]}
    report = svc.validate_records(adaptation["records"], tenant_id=tenant,
                                  task_id=str(ctx.get("task_id") or ""), actor=actor)
    rel = product_dependency_issues(adaptation["records"], tenant_id=tenant,
                                    lookup=svc._entity_lookup)
    report = merge_relation_issues(report, rel)
    if not report["valid"] or not report["publishable"]:
        flat = [i for r in report["records"] for i in r["issues"]]
        if any(i["code"] == "IDEMPOTENCY_CONFLICT" for i in flat):
            code = "IDEMPOTENCY_CONFLICT"
        elif any(i["code"] == "UNRESOLVED_RELATION_ENDPOINT" for i in flat):
            code = "UNRESOLVED_RELATION_ENDPOINT"
        elif not report["valid"]:
            code = "VALIDATION_FAILED"
        else:
            code = "REVIEW_REQUIRED"
        return {"success": False, "code": code,
                "errors": [{"code": code, "message": code, "details": []}],
                "data": {"adaptation": adaptation, "validation": report, "publishable": False},
                "trace_id": _trace(ctx, "data_catalog_file_publish"),
                "evidence": [_evidence("m0", "catalog", f"file publish rejected: {code}")]}
    try:
        publication = svc.publish_records(adaptation["records"], tenant_id=tenant,
                                          task_id=str(ctx.get("task_id") or ""), actor=actor)
    except ValueError as exc:
        from .m0_catalog_ingest import CatalogValidationError

        if isinstance(exc, CatalogValidationError):
            return {"success": False, "code": exc.code,
                    "errors": [{"code": exc.code, "message": str(exc), "details": []}],
                    "data": exc.report, "trace_id": _trace(ctx, "data_catalog_file_publish"),
                    "evidence": [_evidence("m0", "catalog", f"file publish rejected: {exc.code}")]}
        raise
    svc.persist_derived_relations(adaptation["records"], tenant_id=tenant)
    publication = {**publication, "catalog_counts": svc._catalog_counts(tenant)}
    return {"success": True,
            "data": {"adaptation": adaptation, "validation": report, "publishable": True,
                     "publication": publication},
            "errors": [], "trace_id": _trace(ctx, "data_catalog_file_publish"),
            "evidence": [_evidence("m0", "catalog",
                                   f"file published={publication.get('published')} duplicates={publication.get('duplicates')}")]}


async def _facade_publish(expected_type: str, payload: dict[str, Any],
                          ctx: dict[str, Any], tool: str) -> dict[str, Any]:
    """v1 typed facade 公共壳：单类型闸门 + approved 发布 + 派生关系/回填/抢占。"""
    from .m0_catalog_ingest import (
        CatalogValidationError,
        merge_relation_issues,
        product_dependency_issues,
    )

    records = payload.get("records") or []
    if not isinstance(records, list) or not records:
        raise ValueError(f"{tool} 需要 records 数组（≥1）")
    if len(records) > 5000:
        raise ValueError("records 超过上限 5000")
    mixed = [str(r.get("entity_type") or "") for r in records if isinstance(r, dict)]
    if any(t != expected_type for t in mixed):
        return {"success": False, "code": "ENTITY_TYPE_MISMATCH",
                "errors": [{"code": "ENTITY_TYPE_MISMATCH",
                            "message": f"混批拒绝：{tool} 只接受 entity_type={expected_type}",
                            "details": []}],
                "data": {"expected": expected_type},
                "trace_id": _trace(ctx, tool),
                "evidence": [_evidence("m0", "catalog", f"{tool} rejected: entity_type mismatch")]}
    tenant = str(ctx.get("tenant_id") or "default")
    actor = str(ctx.get("actor") or "operator")
    svc = _catalog_svc(ctx)
    # 产品引用 fail-closed 合并（order/bom/process_route 非可选）
    report = svc.validate_records(records, tenant_id=tenant,
                                  task_id=str(ctx.get("task_id") or ""), actor=actor)
    rel = product_dependency_issues(records, tenant_id=tenant, lookup=svc._entity_lookup)
    report = merge_relation_issues(report, rel)
    if not report["valid"]:
        flat = [i for r in report["records"] for i in r["issues"]]
        if any(i["code"] == "IDEMPOTENCY_CONFLICT" for i in flat):
            code = "IDEMPOTENCY_CONFLICT"
        elif any(i["code"] == "UNRESOLVED_RELATION_ENDPOINT" for i in flat):
            code = "UNRESOLVED_RELATION_ENDPOINT"
        else:
            code = "VALIDATION_FAILED"
        return {"success": False, "code": code,
                "errors": [{"code": code, "message": code, "details": []}],
                "data": {"validation": report},
                "trace_id": _trace(ctx, tool),
                "evidence": [_evidence("m0", "catalog", f"{tool} rejected: {code}")]}
    if not report["publishable"]:
        return {"success": False, "code": "REVIEW_REQUIRED",
                "errors": [{"code": "REVIEW_REQUIRED",
                            "message": "facade 只发布 review_status=approved 记录", "details": []}],
                "data": {"validation": report},
                "trace_id": _trace(ctx, tool),
                "evidence": [_evidence("m0", "catalog", f"{tool} rejected: review required")]}
    try:
        data = svc.publish_records(records, tenant_id=tenant,
                                   task_id=str(ctx.get("task_id") or ""), actor=actor)
    except CatalogValidationError as exc:
        return {"success": False, "code": exc.code,
                "errors": [{"code": exc.code, "message": str(exc), "details": []}],
                "data": exc.report, "trace_id": _trace(ctx, tool),
                "evidence": [_evidence("m0", "catalog", f"{tool} rejected: {exc.code}")]}
    svc.persist_derived_relations(records, tenant_id=tenant)
    svc.backfill_declared_relations(records, tenant_id=tenant)
    if expected_type == "bom":
        svc.supersede_active_boms(records, tenant_id=tenant)
    data = {**data, "catalog_counts": svc._catalog_counts(tenant)}
    return {"success": True, "data": data, "errors": [],
            "trace_id": _trace(ctx, tool),
            "evidence": [_evidence("m0", "catalog",
                                   f"{tool} published={data.get('published')} duplicates={data.get('duplicates')}")]}


#: v1 typed facade：工具名 → canonical entity_type（单类型闸门）。
FACADE_KINDS: dict[str, str] = {
    "m0_products_import": "product",
    "m0_orders_import": "order",
    "m0_boms_import": "bom",
    "m0_materials_import": "material",
    "m0_suppliers_import": "supplier",
    "m0_equipment_import": "equipment",
    "m0_routes_import": "process_route",
    "m0_operations_import": "operation",
    "m0_tooling_import": "tooling",
    # F-008（M6 财务）：费用支出 + 送货单写入面（老仓 `m0_expenses_import` /
    # `m0_delivery_notes_import`，本处收口到 v2 的统一 facade）。
    "m0_expenses_import": "expense",
    "m0_delivery_notes_import": "delivery_note",
}


def _facade_handler(tool: str, expected_type: str):
    async def handler(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        return await _facade_publish(expected_type, payload, ctx, tool)
    handler.__name__ = tool
    return handler


FACADE_HANDLERS: dict[str, Any] = {
    tool: _facade_handler(tool, etype) for tool, etype in FACADE_KINDS.items()
}


async def m1_parse(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """ingest_document（S2 M1-1 真实化）：确定性解析 → m1.document.v2 任务持久化。

    迁自 `_wt/INT/src/yunpai_langgraph/workers.py:764-785`（rows-S2 第 1 行
    「改造后搬」）。V2 原实现是 ``provider="local_fixture"/fixture=True`` 的
    preview 版（无任务持久化，docstring 自述 never production），按 rows-S2
    备注「替换 V2 现有 fixture 版」换成 ``m1_domain.process_upload_file``：

    - 本地 M1 领域库（``m1_domain.M1Store``，``YUNPAI_M1_DB``）任务状态机
      created→parsing→…→needs_review|done|failed；
    - JSON/含订单证据的 XLSX/XLSM 直通；自由格式（PDF/图片/工程图/其它）显式
      失败（C 型缺模型不伪造），失败码保持 ``LOCAL_FIXTURE_UNSUPPORTED_FORMAT``；
    - ``provider``：真实解析="local"；``_fixture_document`` 注入路径（测试便利）
      ="local_fixture"/``fixture=True``；
    - 契约声明的 3 个入参（``doc_type_hint``/``document_subtype_hint``/
      ``semantic_enrichment``）真正生效（rows-S2 盘点第 6 类，R2 已在
      ``m1_domain`` 实现）。
    """
    from .m1_domain import process_upload_file

    filename, raw = _decode_file(payload["file"])
    result = process_upload_file(
        filename=filename, raw=raw,
        tenant_id=str(ctx.get("tenant_id") or "default"),
        tracking_task_id=str(ctx.get("task_id") or "task"),
        fixture=payload.get("_fixture_document"),
        doc_type_hint=payload.get("doc_type_hint"),
        document_subtype_hint=payload.get("document_subtype_hint"),
        semantic_enrichment=bool(payload.get("semantic_enrichment", True)))
    return {**result, "trace_id": _trace(ctx, "ingest_document")}


async def m1_archive_ingest(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """ingest_m1_archive（M1-1）：安全解包 → 每个叶子文件建子任务并解析。

    迁自 `_wt/INT/.../workers.py:788-846`（rows-S2 第 2 行）。依赖
    ``archive_extract``（V2 已有且与 INT 逐字节相同）＋ ``m1_domain``。
    契约的 3 个可选参数由子文件继承（R2-2）。
    """
    import tempfile
    from pathlib import Path

    from .archive_extract import extract_archive_safe, unpack_archive
    from .m1_domain import M1Store, process_upload_file

    filename, raw = _decode_file(payload["file"])
    store = M1Store()
    tenant = str(ctx.get("tenant_id") or "default")
    tracking = str(ctx.get("task_id") or "task")
    unpacked = unpack_archive(raw, filename=filename)
    parent = store.create_task(tenant_id=tenant, tracking_task_id=tracking,
                               kind="archive", filename=filename,
                               sha256_digest=_sha256_digest(raw))
    if unpacked.get("error"):
        store.update(tenant, parent["task_id"], status="failed", stage="failed",
                     error=unpacked["error"])
        return {"task_id": parent["task_id"], "status": "failed",
                "code": "ARCHIVE_UNSUPPORTED", "message": unpacked["error"],
                "child_count": 0, "child_ids": [], "provider": "local",
                "fixture": False, "environment": "local_m1",
                "trace_id": _trace(ctx, "ingest_m1_archive")}
    child_ids: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "members"
        extract_archive_safe(raw, dest)
        for member in unpacked.get("members") or []:
            if member.get("status") != "accepted" or member.get("is_dir"):
                continue
            rel = str(member.get("relative_path") or "")
            member_path = dest / rel.replace("\\", "/")
            if not member_path.is_file():
                continue
            child_raw = member_path.read_bytes()
            # 子文件继承与 m1_parse 相同的 3 个契约参数
            result = process_upload_file(
                filename=rel, raw=child_raw, tenant_id=tenant,
                tracking_task_id=tracking, parent_id=parent["task_id"], kind="child",
                doc_type_hint=payload.get("doc_type_hint"),
                document_subtype_hint=payload.get("document_subtype_hint"),
                semantic_enrichment=bool(payload.get("semantic_enrichment", True)))
            child_ids.append(result["task_id"])
    summary = store.batch_summary(tenant, parent["task_id"])
    if summary["failed_count"] and summary["done_count"] == 0 and summary["review_count"] == 0:
        parent_status = "failed"
    else:
        parent_status = "done"
    store.update(tenant, parent["task_id"], status=parent_status, stage="complete",
                 confidence=float(len(child_ids)) and 1.0)
    return {
        "task_id": parent["task_id"], "status": parent_status,
        "child_count": summary["child_count"], "child_ids": child_ids,
        "done_count": summary["done_count"], "failed_count": summary["failed_count"],
        "review_count": summary["review_count"], "pending_count": summary["pending_count"],
        "provider": "local", "fixture": False, "environment": "local_m1",
        "trace_id": _trace(ctx, "ingest_m1_archive"),
    }


async def m1_task_get(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """get_m1_task：任务摘要 + 终态文档（轮询语义）。

    迁自 `_wt/INT/.../workers.py:849-868`（rows-S2 第 3 行）。manifest 的
    ``output_schema`` 要求 ``task_id``/``status`` 在**顶层**（required），旧的
    ``{"success":…, "data":{…}}`` 信封会让 ``registry.call`` 的输出校验失败
    （rows-S2「需先修」），故返回域层 ``task_readback`` 的契约平铺形状。
    """
    from .m1_domain import M1Store, task_readback

    store = M1Store()
    tenant = str(ctx.get("tenant_id") or "default")
    task_id = str(payload.get("task_id") or "")
    if not task_id:
        raise ValueError("get_m1_task 需要 task_id")
    task = store.get_task(tenant, task_id)
    if task is None:
        raise ValueError(f"task not found: {task_id}")
    doc_row = store.get_document(tenant, task_id)
    return {**task_readback(task, document=doc_row["document"] if doc_row else None),
            "trace_id": _trace(ctx, "get_m1_task")}


async def m1_batch_get(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """get_m1_batch：父任务 + 子任务聚合计数。

    迁自 `_wt/INT/.../workers.py:871-887`（rows-S2 第 4 行）。契约
    ``additionalProperties: false``（7 键），因此不能带 ``trace_id``/``evidence``
    之外的键；返回域层 ``batch_readback``。
    """
    from .m1_domain import M1Store, batch_readback

    store = M1Store()
    tenant = str(ctx.get("tenant_id") or "default")
    parent_id = str(payload.get("parent_id") or "")
    if not parent_id:
        raise ValueError("get_m1_batch 需要 parent_id")
    parent = store.get_task(tenant, parent_id)
    if parent is None:
        raise ValueError(f"batch not found: {parent_id}")
    return batch_readback(parent, store.batch_summary(tenant, parent_id))


async def m1_document_get(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """get_m1_document：按任务取完整 m1.document.v2（含 source 契约字段）。

    迁自 `_wt/INT/.../workers.py:890-908`（rows-S2 第 5 行）。返回域层
    ``document_readback``（内部保证 ``source`` 存在，文档未就绪时抛
    ``document not ready``）。
    """
    from .m1_domain import M1Store, document_readback

    store = M1Store()
    tenant = str(ctx.get("tenant_id") or "default")
    task_id = str(payload.get("task_id") or "")
    if not task_id:
        raise ValueError("get_m1_document 需要 task_id")
    task = store.get_task(tenant, task_id)
    if task is None:
        raise ValueError(f"task not found: {task_id}")
    doc_row = store.get_document(tenant, task_id)
    return {**document_readback(task, doc_row["document"] if doc_row else None),
            "trace_id": _trace(ctx, "get_m1_document")}


def _sha256_digest(raw: bytes) -> str:
    from hashlib import sha256 as _sha

    return _sha(raw).hexdigest()


async def m1_tasks_list(payload: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """list_m1_tasks：任务列表（不含子任务；按状态过滤/分页）。

    迁自 `_wt/INT/.../workers.py:917-936`（rows-S2 第 8 行）。
    """
    from .m1_domain import M1Store

    store = M1Store()
    tenant = str(ctx.get("tenant_id") or "default")
    status = str(payload.get("status") or "") or None
    limit = int(payload.get("limit") or 0) or 200
    offset = int(payload.get("offset") or 0)
    rows = store.list_tasks(tenant, status=status, limit=10000)
    summaries = []
    for t in rows:
        if t["kind"] == "child":
            continue
        summaries.append({
            "task_id": t["task_id"], "filename": t["filename"], "status": t["status"],
            "needs_review": t["status"] == "needs_review",
            "overall_confidence": t["confidence"] or None,
        })
    return summaries[offset:offset + limit]


async def m1_review_queue(payload: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """list_m1_review_queue：当前待人工审核任务（HITL 入口）。

    迁自 `_wt/INT/.../workers.py:939-954`（rows-S2 第 9 行）。
    """
    from .m1_domain import M1Store

    store = M1Store()
    tenant = str(ctx.get("tenant_id") or "default")
    limit = int(payload.get("limit") or 0) or 200
    offset = int(payload.get("offset") or 0)
    rows = store.list_tasks(tenant, status="needs_review", limit=10000)
    out = []
    for t in rows:
        if t["kind"] == "child":
            continue
        out.append({"task_id": t["task_id"], "filename": t["filename"],
                    "overall_confidence": t["confidence"] or None})
    return out[offset:offset + limit]


async def m1_submit_review(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """submit_m1_review：人工审核闭合 HITL——approve → done（应用修正），驳回 → failed。

    迁自 `_wt/INT/.../workers.py:957-979`（rows-S2 第 10 行）。两处改造：

    - **ctx 键映射**（rows-S2 ③不一致）：INT 只读 ``ctx["actor"]``，V2 的
      ``worker/executor.py`` 现在同时提供 ``actor``/``actor_user``
      （INFRA-DECISIONS §1.2 键名映射别名），这里两个键都读，保证直接调用
      handler（不带别名）时也不会退化成 ``"anonymous"``；
    - 契约声明的 ``line_corrections``/``issue_resolutions`` 真正生效（R2-6，
      ``m1_domain.apply_review``，引用不存在的 line_id/code 时 fail-closed）。
    """
    from .m1_domain import M1Store, apply_review

    store = M1Store()
    tenant = str(ctx.get("tenant_id") or "default")
    task_id = str(payload.get("task_id") or "")
    if not task_id:
        raise ValueError("submit_m1_review 需要 task_id")
    reviewer = str(payload.get("reviewer") or ctx.get("actor_user")
                   or ctx.get("actor") or "anonymous")
    return apply_review(
        store, tenant_id=tenant, task_id=task_id,
        approve=bool(payload.get("approve", True)),
        reviewer=reviewer,
        comment=str(payload.get("comment") or ""),
        corrections=payload.get("corrections"),
        header_corrections=payload.get("header_corrections"),
        line_corrections=payload.get("line_corrections"),
        issue_resolutions=payload.get("issue_resolutions"))


def _m1_doc_rows(store: Any, tenant: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """有文档的任务行（kind=file/child，status done|needs_review）→ (task, document)。"""
    rows = []
    for t in store.list_tasks(tenant, limit=10000):
        if t["kind"] == "child" or t["status"] not in ("done", "needs_review"):
            continue
        doc = store.get_document(tenant, t["task_id"])
        if doc is not None and doc.get("document"):
            rows.append((t, doc["document"]))
    return rows


def _path_values(document: dict[str, Any], field_path: str) -> list[Any]:
    """支持子集：$.k / $.header.k / $.lines[*].k / $.source.k；其余显式不支持。"""
    path = str(field_path or "").strip()
    if not path.startswith("$"):
        raise ValueError(f"field_path 必须以 $ 开头: {field_path}")
    tokens = [p for p in path[1:].split(".") if p]
    if not tokens:
        return [document]
    head = tokens[0]
    if head in ("lines", "lines[*]") and len(tokens) >= 2:
        lines = document.get("lines") if isinstance(document.get("lines"), list) else []
        if tokens[1] == "*":
            return lines
        field = tokens[1]
        return [ln.get(field) for ln in lines if isinstance(ln, dict)]
    if head in ("header", "source") and len(tokens) == 2:
        container = document.get(head)
        if isinstance(container, dict):
            return [container.get(tokens[1])]
        return []
    if len(tokens) == 1:
        return [document.get(head)]
    raise ValueError(f"field_path 暂不支持（本地子集 $/$.header/$.lines[*]/$.source）: {field_path}")


async def m1_orders_search(payload: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """search_m1_orders：按订单号/型号/名称/属性/日期检索订单明细行（契约数组）。

    迁自 `_wt/INT/.../workers.py:1019-1088`（rows-S2 第 6 行）。契约
    ``additionalProperties: false`` 且 5 个字段声明为 ``{"type": "string"}``，
    本地文档缺字段时 INT 会把 ``None`` 写进去（输出校验直接
    ``ValidationError``），故这里对纯字符串字段做 ``str(... or "")`` 归一
    （缺 = 空串，不伪造值）。
    """
    from .m1_domain import M1Store

    store = M1Store()
    tenant = str(ctx.get("tenant_id") or "default")
    limit = int(payload.get("limit") or 100)
    offset = int(payload.get("offset") or 0)
    order_number = str(payload.get("order_number") or "").lower()
    model_q = str(payload.get("model") or "").lower()
    name_q = str(payload.get("name") or "").lower()
    attr_filters = {k: str(payload.get(k) or "").lower() for k in
                    ("interface", "length", "color", "connector", "conductor", "od")}
    attr_filters = {k: v for k, v in attr_filters.items() if v}
    category_q = str(payload.get("category") or "").lower()
    date_from = str(payload.get("date_from") or "")
    date_to = str(payload.get("date_to") or "")

    out: list[dict[str, Any]] = []
    for task, document in _m1_doc_rows(store, tenant):
        header = document.get("header") or {}
        order_no = str(header.get("order_id") or "")
        due = str(header.get("due_date") or header.get("order_date") or "")
        if order_number and order_number not in order_no.lower():
            continue
        if date_from and due < date_from:
            continue
        if date_to and due > date_to:
            continue
        for line in document.get("lines") or []:
            if not isinstance(line, dict):
                continue
            model = str(line.get("model") or line.get("product_code") or "")
            blob = " ".join(str(v) for v in line.values()).lower()
            if model_q and model_q not in model.lower() and model_q not in blob:
                continue
            if name_q and name_q not in blob:
                continue
            if category_q and category_q not in blob:
                continue
            if any(k not in blob for k in attr_filters.values()):
                continue
            quantity = line.get("quantity")
            if not isinstance(quantity, (int, float)):
                try:
                    quantity = float(str(quantity).replace(",", ""))
                except (TypeError, ValueError):
                    quantity = None
            item = {
                "task_id": task["task_id"],
                "line_id": str(line.get("line_id") or f"{order_no}::L0"),
                "line_no": line.get("line_no"),
                "order_number": order_no,
                "document_date": due,
                "model": model,
                "product_code": str(line.get("product_code") or ""),
                "name_raw": str(line.get("name_raw") or line.get("product_name") or ""),
                "name_normalized": str(line.get("name") or line.get("name_normalized") or ""),
                "full_product_name": str(line.get("full_product_name") or ""),
                "product_category": str(line.get("product_category") or ""),
                "quantity": quantity,
                "unit": str(line.get("uom") or line.get("unit") or ""),
                "line": line,
            }
            out.append(item)
            if len(out) >= offset + limit:
                break
        if len(out) >= offset + limit:
            break
    return out[offset:offset + limit]


async def m1_documents_search(payload: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """search_m1_documents：全文/类型/字段路径过滤检索（契约数组）。

    迁自 `_wt/INT/.../workers.py:1091-1137`（rows-S2 第 7 行）。``field_path``
    仅支持本地子集（见 ``_path_values``），manifest 描述写"任意 JSON 字段路径"
    属契约漂移，此处保持显式 ``ValueError`` 而非静默返回空。
    """
    from .m1_domain import M1Store

    store = M1Store()
    tenant = str(ctx.get("tenant_id") or "default")
    limit = int(payload.get("limit") or 50)
    offset = int(payload.get("offset") or 0)
    q = str(payload.get("q") or "").lower()
    doc_type = str(payload.get("document_type") or "")
    doc_subtype = str(payload.get("document_subtype") or "")
    field_path = str(payload.get("field_path") or "")
    field_value = str(payload.get("field_value") or "").lower()

    out: list[dict[str, Any]] = []
    for task, document in _m1_doc_rows(store, tenant):
        if doc_type and str(document.get("document_type") or "") != doc_type:
            continue
        if doc_subtype and str(document.get("document_subtype") or "") != doc_subtype:
            continue
        header = document.get("header") or {}
        if q:
            blob = (json.dumps(document, ensure_ascii=False) + " " + task["filename"]).lower()
            if q not in blob:
                continue
        if field_path:
            values = _path_values(document, field_path)
            if not any(field_value in str(v).lower() for v in values if v is not None):
                continue
        elif field_value:
            raise ValueError("field_value 必须与 field_path 配合使用")
        title = document.get("title") or None
        order_number = (header or {}).get("order_id") or None
        normalized_date = (document.get("normalized_date")
                           or (header or {}).get("due_date")
                           or (header or {}).get("order_date") or None)
        out.append({
            "task_id": task["task_id"], "schema_version": "m1.document.v2",
            "document_type": str(document.get("document_type") or "order"),
            "document_subtype": str(document.get("document_subtype") or ""),
            "title": title, "order_number": order_number,
            "normalized_date": normalized_date,
            "filename": task["filename"], "sha256": task["sha256"],
        })
        if len(out) >= offset + limit:
            break
    return out[offset:offset + limit]


def _export_segment(value: Any, fallback: str = "default") -> str:
    """导出路径段的安全化（租户/任务 ID 只保留字母数字与 ``-_.``）。"""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(value or ""))
    return cleaned.strip("._") or fallback


async def m1_export_order(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """export_m1_order：生成订单标准 Excel（订单头/产品明细/校验问题/原始证据 4 sheet）。

    迁自 `_wt/INT/.../workers.py:1140-1195`（rows-S2 第 11 行）。两处改造：

    - **list-into-cell 崩溃修复**（rows-S2「行为缺陷需先修」）：``paths`` 是
      ``list[str]``，直接写单元格会 ``ValueError: Cannot convert [...] to
      Excel``（只要文档有校验问题就必现）→ 改为 ``", ".join``；
    - **导出目录租户隔离**：INT 硬编码 ``runtime/m1-exports``，不同租户的
      ``{task_id}.xlsx`` 同目录。改为 ``<YUNPAI_M1_EXPORT_DIR 或
      runtime/m1-exports>/<tenant>/<task_id>.xlsx``，路径段做安全化。
    """
    from io import BytesIO as _BytesIO
    from pathlib import Path as _Path

    from .m1_domain import M1Store

    store = M1Store()
    tenant = str(ctx.get("tenant_id") or "default")
    task_id = str(payload.get("task_id") or "")
    if not task_id:
        raise ValueError("export_m1_order 需要 task_id")
    task = store.get_task(tenant, task_id)
    if task is None:
        raise ValueError(f"task not found: {task_id}")
    doc_row = store.get_document(tenant, task_id)
    if doc_row is None or doc_row.get("document") is None:
        raise ValueError(f"document not ready for task: {task_id}")
    document = doc_row["document"]
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise ValueError("导出需要 openpyxl") from exc
    header = document.get("header") or {}
    wb = Workbook()
    ws_head = wb.active
    ws_head.title = "订单头"
    for k, v in header.items():
        ws_head.append([k, v])
    ws_lines = wb.create_sheet("产品明细")
    lines = document.get("lines") or []
    if lines:
        ws_lines.append(list(lines[0].keys()))
        for line in lines:
            ws_lines.append([line.get(k) for k in lines[0].keys()])
    ws_issues = wb.create_sheet("校验问题")
    for issue in document.get("validation_issues") or []:
        if isinstance(issue, dict):
            paths = issue.get("paths")
            if isinstance(paths, (list, tuple)):
                # openpyxl 不能写 list 到单元格（INT 原实现会 ValueError）
                paths = ", ".join(str(item) for item in paths)
            ws_issues.append([issue.get("code"), issue.get("message"), paths])
    ws_evidence = wb.create_sheet("原始证据")
    ws_evidence.append(["source", json.dumps(document.get("source"), ensure_ascii=False)])
    for fe in document.get("field_evidence") or []:
        if isinstance(fe, dict):
            ws_evidence.append([fe.get("key"),
                                json.dumps(fe.get("locator"), ensure_ascii=False),
                                fe.get("excerpt")])
    buf = _BytesIO()
    wb.save(buf)
    export_dir = _Path(os.getenv("YUNPAI_M1_EXPORT_DIR") or "runtime/m1-exports") \
        / _export_segment(tenant)
    export_dir.mkdir(parents=True, exist_ok=True)
    target = (export_dir / f"{_export_segment(task_id, 'task')}.xlsx").resolve()
    target.write_bytes(buf.getvalue())
    filename = f"{str(header.get('order_id') or task_id)}.xlsx"
    return {"task_id": task_id, "filename": filename,
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "download_url": target.as_uri(), "generated": True}


async def m1_generate_report(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """generate_m1_report：Markdown 综合报告（单任务或批次父任务）。

    迁自 `_wt/INT/.../workers.py:1198-1213`（rows-S2 第 12 行）。契约声明的
    ``force`` 真正生效（R2-7，``m1_domain.build_report``：首次 generated /
    内容未变 cached / force=True 强制重生成）。
    """
    from .m1_domain import M1Store, build_report

    store = M1Store()
    tenant = str(ctx.get("tenant_id") or "default")
    task_id = str(payload.get("task_id") or "")
    if not task_id:
        raise ValueError("generate_m1_report 需要 task_id")
    return build_report(store, tenant_id=tenant, task_id=task_id,
                        note=str(payload.get("note") or ""),
                        force=bool(payload.get("force", False)))


async def m2_bom(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from .m2_fact_validation import validate_engineering_facts

    profile = payload["product_profile"]
    lines = payload.get("bom_lines") or []
    parse_issues: list[dict[str, Any]] = []
    parsed_from_files = False
    if not lines and payload.get("bom_files"):
        lines, parse_issues = _extract_uploaded_bom(payload.get("bom_files"))
        parsed_from_files = True
    routing_steps = payload.get("routing_steps") or []
    if not profile.get("product_code") or not lines or parse_issues:
        _m2_record_run(ctx, run_id=f"m2-{str(ctx.get('task_id') or 'task')[-10:]}",
                       status="human_input_required",
                       product_code=str(profile.get("product_code") or ""),
                       product_name=str(profile.get("product_name") or ""),
                       summary={"code": "BLOCKED_INPUT"})
        return {
            "status": "human_input_required", "run_id": f"m2-{ctx['task_id'][-10:]}",
            "workflow_sequence": ["validate_input"], "bom_generation": {"bom_lines": []},
            "sop_generation": {}, "open_customer_questions": [
                {"field": "product_code_or_bom", "question": "请补充产品编码和已确认 BOM 行"},
                *([{"field": "bom_file_parse", "question": issue["message"]} for issue in parse_issues[:5]]),
            ], "artifacts": {}, "code": "BLOCKED_INPUT",
        }
    duplicate_codes = sorted({code for code in (str(line.get("material_code") or "") for line in lines) if code and sum(1 for item in lines if str(item.get("material_code") or "") == code) > 1})
    # R3-1.1（P1-9）：本地草稿占位实现**不执行**历史 BOM 检索，因此不得返回
    # {"status": "matched", "score": 1.0} 这类伪造成功标记（「不伪造事实」红线）。
    # 真实相似度检索由 search_m2_bom_history 提供；契约描述已注明本字段为占位判定。
    matching = {
        "status": "not_run",
        "reason": "本地草稿占位实现未执行历史 BOM/SOP 检索；真实相似度检索由 search_m2_bom_history 提供",
        "matched_by": [],
        "ambiguous_candidates": 0,
        "unmatched_fields": [],
    }
    fact_validation = validate_engineering_facts(
        product_code=profile.get("product_code"),
        bom_lines=lines,
        bom_version=payload.get("bom_version") or "draft-1",
        bom_effective_from=payload.get("bom_effective_from"),
        bom_effective_to=payload.get("bom_effective_to"),
        route_steps=routing_steps,
        sop_version=payload.get("sop_version"),
        sop_effective_from=payload.get("sop_effective_from"),
        sop_effective_to=payload.get("sop_effective_to"),
    )
    _m2_record_run(ctx, run_id=f"m2-{str(ctx.get('task_id') or 'task')[-10:]}",
                   status="draft_created",
                   product_code=str(profile.get("product_code") or ""),
                   product_name=str(profile.get("product_name") or ""),
                   summary={"bom_lines": len(lines), "routing_steps": len(routing_steps),
                            "missing_fields": [item["field"] for item in fact_validation.get("missing_fields", [])]})
    # R3-1.3：workflow_sequence 必须与真实执行一致。本地草稿占位实现只做
    # 「入参校验 →（可选）上传件解析 → 工程事实校验 → 草稿装配」，不做历史检索、
    # BOM/SOP 生成与受控发布——那些只在 HTTP POST /api/run 的完整流水线里发生。
    workflow_sequence = ["validate_input"]
    if parsed_from_files:
        workflow_sequence.append("parse_sources")
    workflow_sequence.append("engineering_fact_validation")
    return {
        "success": True,
        "status": "draft_created", "run_id": f"m2-{ctx['task_id'][-10:]}",
        "workflow_sequence": workflow_sequence,
        "bom_generation": {"product_code": profile["product_code"], "bom_version": "draft-1", "bom_lines": lines, "assumptions": [], "duplicate_material_codes": duplicate_codes, "evidence": [_evidence("m2", "bom_lines", "受控 BOM 输入")]},
        "sop_generation": {"status": "draft", "operation_count": len(routing_steps or lines), "source_files": payload.get("sop_files") or []},
        "engineering_fact_validation": fact_validation,
        "matching": matching,
        "open_customer_questions": [
            {"field": item["field"], "question": f"请补充工程事实：{item['field']}"}
            for item in fact_validation["missing_fields"]
        ], "artifacts": {},
        "evidence": [_evidence("m2", "workflow", "BOM/SOP draft")],
    }


def _m2_record_run(ctx: dict[str, Any], *, run_id: str, status: str,
                   product_code: str = "", product_name: str = "",
                   order_id: str = "", summary: dict[str, Any] | None = None) -> None:
    """env-gated run 落库（YUNPAI_M2_DB 或 ctx.m2_db 设置才写；best-effort 不影响主链）。"""
    import os

    if not (os.getenv("YUNPAI_M2_DB") or ctx.get("m2_db")):
        return
    try:
        from .m2_local import M2RunStore

        M2RunStore(ctx.get("m2_db") or None).save_run(
            tenant_id=str(ctx.get("tenant_id") or "default"), run_id=run_id,
            tool="run_bom_sop_workflow", status=status, order_id=order_id,
            product_code=product_code, product_name=product_name, summary=summary or {})
    except Exception:  # noqa: BLE001 - 可选记录层失败不阻断主链（R034 注记）
        pass


async def m3_mrp(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from .m3_local import m2_package_to_order_bom, open_po_quantities

    order, bom = payload.get("order") or {}, payload.get("bom") or {}
    # R4-REQ-1：契约 anyOf[1] 允许 m2_package 作为 order+bom 的替代，但本地 handler
    # 原先只读 order/bom，该分支实测直接 BLOCKED_INPUT（契约分支未实现）。转换是
    # 确定性的（不推测填充），缺字段由 m2_package_to_order_bom 抛 ValueError。
    # 空对象视为「未提供」——此时仍走下面的 MISSING_BOM 缺参路径，避免把「没给 BOM」
    # 误报成「m2_package 不完整」。
    if (not bom.get("lines") and isinstance(payload.get("m2_package"), dict)
            and payload["m2_package"]):
        try:
            converted = m2_package_to_order_bom(payload["m2_package"])
        except ValueError as exc:
            fallback = payload.get("order") or {}
            return {
                "success": False, "code": "BLOCKED_INPUT",
                "errors": [{"code": "M2_PACKAGE_INCOMPLETE", "message": str(exc), "details": []}],
                "data": {
                    "procurement_plan_id": f"blocked-{ctx['task_id'][-10:]}",
                    "project_id": str(fallback.get("project_id") or fallback.get("order_id") or ""),
                    "order_id": str(fallback.get("order_id") or ""), "bom_id": str(fallback.get("bom_id") or ""),
                    "product_name": str(fallback.get("product_name") or ""),
                    "order_qty": _number(fallback.get("order_qty")), "due_date": str(fallback.get("due_date") or ""),
                    "status": "requires_material_review", "availability_status": "no_procurement_materials",
                    "lines": [], "shortage_lines": [], "warnings": ["m2_package 不完整，停止需求计算"],
                    "material_matching": [], "quality_issues": [],
                    "supply_source": {"owner": "m3", "provider": "local_fixture", "upstream_supply_ignored": False},
                },
                "evidence": [_evidence("m3", "m2_package", f"m2_package 转换失败：{exc}")],
                "trace_id": _trace(ctx, "m3"),
            }
        order, bom = converted["order"], converted["bom"]
    if not bom.get("lines"):
        order_id = str(order.get("order_id") or "")
        bom_id = str(order.get("bom_id") or bom.get("bom_id") or "")
        return {
            "success": False, "code": "BLOCKED_INPUT", "errors": [{"code": "MISSING_BOM", "message": "缺少可计算的 BOM 行", "details": []}],
            "data": {
                "procurement_plan_id": f"blocked-{ctx['task_id'][-10:]}", "project_id": str(order.get("project_id") or order_id),
                "order_id": order_id, "bom_id": bom_id, "product_name": str(order.get("product_name") or bom.get("product_name") or ""),
                "order_qty": _number(order.get("order_qty")), "due_date": str(order.get("due_date") or ""),
                "status": "requires_material_review", "availability_status": "no_procurement_materials", "lines": [], "shortage_lines": [],
                "warnings": ["缺少 BOM 行"], "material_matching": [], "quality_issues": [],
                "supply_source": {"owner": "m3", "provider": "local_fixture", "upstream_supply_ignored": False},
            },
            "evidence": [_evidence("m3", "bom", "未提供 BOM 行，停止需求计算")], "trace_id": _trace(ctx, "m3"),
        }
    inventory = {str(x.get("material_code")): _number(x.get("available_qty")) for x in payload.get("inventory_snapshot", [])}
    # R4-REQ-2：open_purchase_orders（在途量）此前被完全忽略（open_po_qty 恒 0.0）。
    # 现按物料汇总并参与缺口计算；语义边界明确，不猜交期/供应商。
    open_po = open_po_quantities(payload)
    output_lines = []
    for index, line in enumerate(bom.get("lines", []), start=1):
        code = str(line.get("material_code") or "")
        gross = _number(order.get("order_qty")) * _number(line.get("qty_per")) * (1 + _number(line.get("loss_rate")))
        available = inventory.get(code, 0.0)
        open_po_qty = open_po.get(code, 0.0)
        shortage = max(0.0, gross - available - open_po_qty)
        output_lines.append({
            "line_id": str(line.get("line_id") or f"line-{index}"), "material_code": code,
            "material_name": str(line.get("material_name") or code), "uom": str(line.get("uom") or "pcs"),
            "gross_required_qty": gross, "book_qty": available, "available_qty": available,
            "open_po_qty": open_po_qty, "shortage_qty": shortage, "suggest_purchase_qty": shortage,
            "readiness": "shortage" if shortage else "ready",
            "recommendation": "purchase" if shortage else "use_inventory",
        })
    shortages = [line for line in output_lines if line["shortage_qty"] > 0]
    data = {
        "procurement_plan_id": str(order.get("procurement_plan_id") or f"plan-{ctx['task_id'][-10:]}"),
        "project_id": str(order.get("project_id") or ""), "order_id": str(order.get("order_id") or ""),
        "bom_id": str(order.get("bom_id") or bom.get("bom_id") or ""),
        "product_name": str(order.get("product_name") or bom.get("product_name") or ""),
        "order_qty": _number(order.get("order_qty")), "due_date": str(order.get("due_date") or ""),
        "status": "ready_for_m4", "availability_status": "partial_shortage" if shortages else "ready",
        "lines": output_lines, "shortage_lines": shortages, "warnings": [],
        "material_matching": [], "quality_issues": [],
        "supply_source": {"owner": "m3", "provider": "local_fixture", "upstream_supply_ignored": False},
    }
    return {"success": True, "data": data, "errors": [], "trace_id": _trace(ctx, "m3"), "evidence": [_evidence("m3", "inventory_snapshot", "固定库存快照计算")]}


async def m5_schedule(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    # Explicit WIP/v2 facts are handled by the frozen constrained scheduler.
    # The legacy branch remains available only for explicitly preview/sandbox
    # marked requests; production requests without v2 facts fail closed.
    purpose = str(payload.get("scenario_purpose") or "production")
    v2_marked = bool(
        payload.get("pmc_v2") or payload.get("pmc_v2_bundle") or payload.get("calendar_windows")
        or any(isinstance(step, dict) and (step.get("standard_minutes") is not None or step.get("std_minutes") is not None)
               for step in payload.get("routing_steps", []))
    )
    legacy_preview = bool(payload.get("legacy_preview")) or str(purpose).lower() in {"preview", "sandbox"}
    if not v2_marked and payload.get("orders") and not payload.get("routing_steps"):
        return {
            "success": False, "code": "BLOCKED_INPUT", "errors": [{"code": "MISSING_SOP", "message": "缺少可执行的 SOP/工艺路线", "details": []}],
            "data": {
                "idempotency_key": str(payload.get("idempotency_key") or ""),
                "schedule": {"scenario_purpose": purpose, "operations": [], "metrics": {"makespan_minutes": 0, "operation_count": 0}},
                "scenario_purpose": purpose, "lifecycle_status": "draft", "input_hash": "",
                "parent_plan_version": payload.get("expected_head_plan_version"), "tracking_task_id": ctx.get("task_id"),
            },
            "evidence": [_evidence("m5", "routing_steps", "未提供 SOP/工艺路线，停止排程")], "trace_id": _trace(ctx, "m5"),
        }
    if v2_marked:
        from .pmc_v2_adapter import PmcError, run_pmc_v2
        try:
            result = run_pmc_v2(payload)
            # The M5 manifest requires lifecycle identity and the root TaskID
            # even for v2 candidates; keep these fields at the adapter edge so
            # output-schema validation cannot silently drop traceability.
            data = result.setdefault("data", {})
            data.setdefault("parent_plan_version", payload.get("expected_head_plan_version"))
            data.setdefault("tracking_task_id", ctx.get("task_id"))
            result["trace_id"] = result.get("trace_id") or _trace(ctx, "m5-pmc-v2")
            persisted = _persist_v2_draft(result, payload, ctx)
            if persisted is not None:
                return persisted
            return result
        except PmcError as exc:
            return {"success": False, "code": exc.code, "errors": [{"code": exc.code, "message": exc.message, "details": []}], "data": {"idempotency_key": payload.get("idempotency_key", ""), "schedule": {"scenario_purpose": purpose, "operations": [], "metrics": {"operation_count": 0, "makespan_minutes": 0}, "algorithm_version": "pmc-v2-frozen-20260902"}, "scenario_purpose": purpose, "lifecycle_status": "draft", "input_hash": "", "algorithm_version": "pmc-v2-frozen-20260902", "parent_plan_version": payload.get("expected_head_plan_version"), "tracking_task_id": ctx.get("task_id")}, "trace_id": _trace(ctx, "m5-pmc-v2-blocked"), "evidence": [_evidence("m5", "pmc_v2", exc.message)]}
    if not legacy_preview:
        # Production requests must go through PMC v2.  Without an explicit
        # preview/sandbox marker the legacy branch must not run.
        return {
            "success": False, "code": "BLOCKED_INPUT",
            "errors": [{"code": "LEGACY_PRODUCTION_BLOCKED", "message": "production 请求必须携带 v2 事实并进入 PMC v2；legacy 分支只能显式标记为 preview/sandbox", "details": []}],
            "data": {
                "idempotency_key": str(payload.get("idempotency_key") or ""),
                "schedule": {"scenario_purpose": "production", "operations": [], "metrics": {"makespan_minutes": 0, "operation_count": 0}},
                "scenario_purpose": "production", "lifecycle_status": "draft", "input_hash": "",
                "parent_plan_version": payload.get("expected_head_plan_version"), "tracking_task_id": ctx.get("task_id"),
            },
            "evidence": [_evidence("m5", "legacy", "production 请求不得进入 legacy 分支")], "trace_id": _trace(ctx, "m5"),
        }
    resources = {str(item["resource_id"]): item for item in payload["resources"]}
    operations, cursor = [], 0
    for order in payload["orders"]:
        product_id = str(order["product_id"])
        routes = sorted((r for r in payload["routing_steps"] if str(r["product_id"]) == product_id), key=lambda item: item["sequence"])
        for route in routes:
            eligible = route.get("eligible_resources") or []
            selected = next((x for x in eligible if x.get("resource_id") in resources and resources[x["resource_id"]].get("status", "available") == "available"), None)
            if not selected:
                data = {"idempotency_key": payload["idempotency_key"], "schedule": {"scenario_purpose": payload.get("scenario_purpose", "production")}, "scenario_purpose": payload.get("scenario_purpose", "production"), "input_hash": "", "parent_plan_version": payload.get("expected_head_plan_version"), "tracking_task_id": ctx.get("task_id")}
                return {"success": False, "data": data, "errors": [{"code": "BLOCKED_INPUT", "message": "没有可用资源", "details": [route["operation_id"]]}], "trace_id": _trace(ctx, "m5")}
            duration = float(selected.get("processing_minutes") or selected.get("cycle_minutes") or 1) * float(order["quantity"])
            start, cursor = cursor, cursor + max(1, ceil(duration))
            operations.append({"order_id": order["order_id"], "operation_id": route["operation_id"], "resource_id": selected["resource_id"], "start_minute": start, "end_minute": cursor})
    purpose = payload.get("scenario_purpose", "production")
    digest = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    schedule = {"scenario_purpose": purpose, "plan_version": f"draft-{digest[:10]}", "operations": operations, "metrics": {"operation_count": len(operations), "makespan_minutes": cursor}, "validation_report": {"status": "pass", "errors": []}}
    data = {"idempotency_key": payload["idempotency_key"], "schedule": schedule, "scenario_purpose": purpose, "lifecycle_status": "draft", "input_hash": digest, "parent_plan_version": payload.get("expected_head_plan_version"), "tracking_task_id": ctx.get("task_id"), "replayed": False}
    return {"success": True, "data": data, "errors": [], "trace_id": _trace(ctx, "m5"), "evidence": [_evidence("m5", "planning_snapshot", "订单/路线/资源快照")]}


def _persist_v2_draft(result: dict[str, Any], payload: dict[str, Any],
                      ctx: dict[str, Any]) -> dict[str, Any] | None:
    """Task 2: when an M5 repository is configured, persist the six snapshot
    kinds and a draft plan with idempotent replay / same-key conflict rules.

    Enabled only when ``ctx["m5_db_path"]`` or the ``YUNPAI_M5_DB`` env var is
    set, so the default stateless registry path is unchanged.  Returns the
    final handler response when the repository path is enabled (including a
    replayed or conflicted response), otherwise ``None``.
    """
    db_path = (ctx or {}).get("m5_db_path") or os.getenv("YUNPAI_M5_DB")
    if not db_path:
        return None
    from .m5_repository import M5Repository, M5RepositoryError
    scenario_id = str(payload.get("scenario_id") or "")
    idem = str(payload.get("idempotency_key") or "")
    data = result.get("data") or {}
    digest = str(data.get("input_hash") or "")
    bundle = data.get("input_package")
    if not scenario_id or not bundle or not digest:
        return None
    repo = M5Repository(db_path)
    purpose = str(data.get("scenario_purpose") or "production")
    version = f"plan-{scenario_id}-{digest[:10]}"
    try:
        existing = repo.find_by_idempotency(scenario_id, idem) if idem else None
        if existing is not None:
            stored = repo.get_plan(existing["plan_version"])
            if stored is not None and existing["input_hash"] == digest:
                schedule = stored.get("schedule") or {}
                replayed_data = {
                    "idempotency_key": idem,
                    "schedule": schedule,
                    "scenario_purpose": stored.get("scenario_purpose", purpose),
                    "lifecycle_status": stored.get("lifecycle_status", "draft"),
                    "input_hash": stored.get("input_hash", digest),
                    "plan_version": stored["plan_version"],
                    "parent_plan_version": stored.get("parent_plan_version"),
                    "tracking_task_id": ctx.get("task_id"),
                    "replayed": True,
                }
                return {"success": True, "data": replayed_data, "errors": [],
                        "trace_id": _trace(ctx, "m5-pmc-v2-replay"),
                        "evidence": [_evidence("m5", "pmc_v2", "idempotent replay of stored draft")]}
            # same idempotency key, different input -> conflict (Task 2)
            return {
                "success": False, "code": "BLOCKED_INPUT",
                "errors": [{"code": "IDEMPOTENCY_CONFLICT",
                            "message": f"同幂等键 {idem} 已用于不同输入（scenario {scenario_id}）",
                            "details": [{"stored_hash": existing["input_hash"], "new_hash": digest}]}],
                "data": {
                    "idempotency_key": idem,
                    "schedule": {"scenario_purpose": purpose, "operations": [], "metrics": {"operation_count": 0, "makespan_minutes": 0}},
                    "scenario_purpose": purpose, "lifecycle_status": "draft",
                    "input_hash": digest, "parent_plan_version": payload.get("expected_head_plan_version"),
                    "tracking_task_id": ctx.get("task_id"), "replayed": False,
                },
                "trace_id": _trace(ctx, "m5-pmc-v2-conflict"),
                "evidence": [_evidence("m5", "pmc_v2", "idempotency conflict")],
            }
        repo.store_snapshots(scenario_id, bundle, tenant_id=str(ctx.get("tenant_id") or "default"),
                             task_id=str(ctx.get("task_id") or ""))
        repo.save_plan(
            plan_version=version, scenario_id=scenario_id,
            tenant_id=str(ctx.get("tenant_id") or "default"), task_id=str(ctx.get("task_id") or ""),
            lifecycle_status="draft", parent_plan_version=payload.get("expected_head_plan_version"),
            input_hash=digest, solver_hash=f"solver-{data.get('algorithm_version', 'pmc-v2')}",
            algorithm_version=data.get("algorithm_version", "pmc-v2-frozen-20260902"),
            scenario_purpose=purpose, validation_report=data.get("validator") or {},
            bundle=bundle, schedule=data.get("schedule") or {},
            idempotency_key=idem or None,
        )
        data["plan_version"] = version
        schedule = data.setdefault("schedule", {})
        schedule["plan_version"] = version
        return None
    except M5RepositoryError as exc:
        return {
            "success": False, "code": "BLOCKED_INPUT",
            "errors": [{"code": exc.code, "message": exc.message, "details": []}],
            "data": {
                "idempotency_key": idem,
                "schedule": {"scenario_purpose": purpose, "operations": [], "metrics": {"operation_count": 0, "makespan_minutes": 0}},
                "scenario_purpose": purpose, "lifecycle_status": "draft",
                "input_hash": digest, "parent_plan_version": payload.get("expected_head_plan_version"),
                "tracking_task_id": ctx.get("task_id"), "replayed": False,
            },
            "trace_id": _trace(ctx, "m5-pmc-v2-persist-blocked"),
            "evidence": [_evidence("m5", "pmc_v2", exc.message)],
        }


def _m5(name: str):
    """Lazily import the local M5 PMC handler for a manifest tool name."""
    from .m5_tools import M5_HANDLERS
    return M5_HANDLERS[name]


def _m6(name: str):
    """Lazily import the local M6 finance handler for a manifest tool name（契约见 m6.json）。"""
    from .m6_tools import M6_HANDLERS
    return M6_HANDLERS[name]


def _m2(name: str):
    """Lazily import the local M2 BOM/SOP handler for a manifest tool name."""
    from .m2_local import LOCAL_HANDLERS
    return LOCAL_HANDLERS[name]


def _m3(name: str):
    """Lazily import the local M3 handler for a manifest tool name.

    M3 本地实现只有一个正式工具（``get_material_readiness_snapshot``）；
    ``run_m3_procurement_requirements`` 的 handler 在本文件（``m3_mrp``）。
    其余 13 件 LEGACY 与 receive 类按 rows-S4 判「保留HTTP / 不搬」，不得登记
    （登记会遮蔽 HTTP 绑定；receive 属 ORCHESTRATION_INTERNAL，见 §3.3）。
    """
    from .m3_local import LOCAL_HANDLERS
    return LOCAL_HANDLERS[name]


def _m4(name: str):
    """Lazily import the local M4 handler for a manifest tool name.

    M4 的本地实现分三个文件（采购 / 供应商 / 跟踪），统一在本工厂里合并查找；
    名字写错 → ``KeyError`` 在导入期就炸，而不是运行时静默 UNBOUND。
    """
    from .m4_purchase_local import LOCAL_HANDLERS as PURCHASE
    from .m4_supplier_local import LOCAL_HANDLERS as SUPPLIER
    from .m4_tracking_local import LOCAL_HANDLERS as TRACKING
    handler = {**PURCHASE, **SUPPLIER, **TRACKING}.get(name)
    if handler is None:
        raise KeyError(f"M4 local handler not found: {name}")
    return handler


async def sample_file(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """确定性采样：把文件压成 LLM 可看的表头 + 前 N 行样本，不伪造内容。"""
    from .recognized_store import sample_file as _sample

    encoded = str(payload.get("content_b64") or "")
    filename = str(payload.get("filename") or "document.bin")
    if not encoded:
        return {"success": False, "code": "MISSING_FILE",
                "errors": [{"code": "MISSING_FILE", "message": "缺少 content_b64", "details": []}],
                "data": {}, "trace_id": _trace(ctx, "sample_file")}
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        return {"success": False, "code": "INVALID_BASE64",
                "errors": [{"code": "INVALID_BASE64", "message": str(exc), "details": []}],
                "data": {}, "trace_id": _trace(ctx, "sample_file")}
    sample = _sample(raw, filename, max_rows=int(payload.get("max_rows") or 10))
    return {"success": True, "data": sample, "errors": [],
            "trace_id": _trace(ctx, "sample_file"),
            "evidence": [_evidence("catalog", "sample_file", f"sampled {filename} ({sample['sniff']['detected_format']})")]}


async def ingest_recognized(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """确定性落库：schema 校验 + PII 脱敏 + 自描述表存储 + sha256 幂等。"""
    from .recognized_store import RecognizedTableStore

    kind = str(payload.get("kind") or "")
    filename = str(payload.get("filename") or "")
    sha256 = str(payload.get("sha256") or "")
    columns = payload.get("columns") or []
    rows = payload.get("rows") or []
    confidence = float(payload.get("confidence") or 0.0)
    redact = bool(payload.get("redact", True))
    if not (kind and filename and sha256):
        return {"success": False, "code": "MISSING_REQUIRED",
                "errors": [{"code": "MISSING_REQUIRED", "message": "kind/filename/sha256 必填", "details": []}],
                "data": {}, "trace_id": _trace(ctx, "ingest_recognized")}
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        return {"success": False, "code": "INVALID_COLUMNS",
                "errors": [{"code": "INVALID_COLUMNS", "message": "columns 必须为字符串数组", "details": []}],
                "data": {}, "trace_id": _trace(ctx, "ingest_recognized")}
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return {"success": False, "code": "INVALID_ROWS",
                "errors": [{"code": "INVALID_ROWS", "message": "rows 必须为对象数组", "details": []}],
                "data": {}, "trace_id": _trace(ctx, "ingest_recognized")}
    store = RecognizedTableStore(ctx.get("recognized_db"))
    try:
        result = store.ingest(kind=kind, filename=filename, sha256=sha256,
                              columns=list(columns), rows=rows, confidence=confidence,
                              redact=redact, tenant_id=str(ctx.get("tenant_id") or "default"))
    except ValueError as exc:
        return {"success": False, "code": "INVALID_KIND",
                "errors": [{"code": "INVALID_KIND", "message": str(exc), "details": []}],
                "data": {}, "trace_id": _trace(ctx, "ingest_recognized")}
    return {"success": True, "data": result, "errors": [],
            "trace_id": _trace(ctx, "ingest_recognized"),
            "evidence": [_evidence("catalog", "ingest_recognized", f"{kind} rows={result['inserted_rows']} dup={result['duplicate']}")]}


async def query_recognized_table(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """agent 读回：对已落库的自描述表做过滤/聚合。"""
    from .recognized_store import RecognizedTableStore

    store = RecognizedTableStore(ctx.get("recognized_db"))
    rows = store.query(
        kind=payload.get("kind"),
        filters=payload.get("filters"),
        aggregate=payload.get("aggregate"),
        limit=int(payload.get("limit") or 200),
        tenant_id=str(ctx.get("tenant_id") or "default"),
    )
    return {"success": True, "data": {"rows": rows, "count": len(rows)}, "errors": [],
            "trace_id": _trace(ctx, "query_recognized_table"),
            "evidence": [_evidence("catalog", "query_recognized_table", f"returned {len(rows)} rows")]}


async def ingest_canonical(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """确定性落库：agent 映射后的 canonical 记录，schema 校验 + PII + sha256 幂等。

    生产 transport（配置了 M0_URL）时，校验通过后经 M0 canonical 发布
    （validate + publish + 回读核验）；本地/单元测试无 M0_URL 时只写 sandbox SQLite。
    """
    import os

    from .canonical_ingest import CanonicalLandingStore, to_m0_records

    entity_type = str(payload.get("entity_type") or "")
    filename = str(payload.get("filename") or "")
    sha256 = str(payload.get("sha256") or "")
    records = payload.get("records")
    confidence = float(payload.get("confidence") or 0.0)
    if not entity_type or not isinstance(records, list):
        return {"success": False, "code": "MISSING_REQUIRED",
                "errors": [{"code": "MISSING_REQUIRED", "message": "entity_type/records 必填", "details": []}],
                "data": {}, "trace_id": _trace(ctx, "ingest_canonical")}
    store = CanonicalLandingStore(ctx.get("canonical_db"))
    result = store.ingest(entity_type=entity_type, records=records, filename=filename,
                          sha256=sha256, confidence=confidence,
                          tenant_id=str(ctx.get("tenant_id") or "default"))
    m0_publication: dict[str, Any] | None = None
    if result.get("success") and os.getenv("M0_URL"):
        try:
            from .m0_catalog import publish_records

            m0_records = to_m0_records(
                entity_type, result.get("data", {}).get("clean_records") or [],
                filename=filename, sha256=sha256,
                tenant_id=str(ctx.get("tenant_id") or "default"),
                actor=str(ctx.get("actor") or "operator"),
            )
            m0_publication = publish_records(
                m0_records,
                tenant_id=str(ctx.get("tenant_id") or "default"),
                task_id=str(ctx.get("task_id") or "task"),
                actor=str(ctx.get("actor") or "operator"),
            )
        except Exception as exc:  # noqa: BLE001 - M0 不可达不阻断本地落库，如实上报
            m0_publication = {"status": "failed", "published": 0, "error": str(exc)}
    return {**result,
            "m0_publication": m0_publication,
            "trace_id": _trace(ctx, "ingest_canonical"),
            "evidence": [_evidence("catalog", "ingest_canonical", f"{entity_type} rows={result['data'].get('inserted_rows')} dup={result['data'].get('duplicate')}")]}


from .m1_knowledge import (  # noqa: E402 - S2 M1-3 知识链（canonical 事实源，随迁）
    knowledge_entity_get as _k_entity_get,
    knowledge_graph_get as _k_graph_get,
    knowledge_list_entities as _k_list_entities,
    knowledge_search as _k_search,
    knowledge_stats as _k_stats,
)

HANDLERS = {
    "data_import_run": m0_import,
    "data_import_status": m0_status,
    "data_import_preview": m0_preview,
    "data_import_resolve": m0_resolve,
    "data_import_commit": m0_commit,
    # ── M0 数据基础其余 23 个工具（S1 迁移；契约见 registry-manifests/m0.json）──
    # canonical 批次面（需 YUNPAI_M0_DB；未配置时 fail-closed，不伪造成功）
    "data_import_rollback": m0_rollback,
    "data_import_history": m0_history,
    "data_import_quarantine": m0_quarantine_list,
    # catalog 语义层（m0.ingest.v1 校验 / 发布 / 文档候选 / 模板文件）
    "data_catalog_ingest_validate": catalog_ingest_validate,
    "data_catalog_ingest_publish": catalog_ingest_publish,
    "data_catalog_document_candidate_validate": catalog_document_candidate_validate,
    "data_catalog_document_candidate_publish": catalog_document_candidate_publish,
    "data_catalog_file_validate": catalog_file_validate,
    "data_catalog_file_publish": catalog_file_publish,
    # typed facade（单类型闸门 + 派生关系/回填/BOM 抢占）
    "m0_products_import": FACADE_HANDLERS["m0_products_import"],
    "m0_orders_import": FACADE_HANDLERS["m0_orders_import"],
    "m0_boms_import": FACADE_HANDLERS["m0_boms_import"],
    "m0_materials_import": FACADE_HANDLERS["m0_materials_import"],
    "m0_suppliers_import": FACADE_HANDLERS["m0_suppliers_import"],
    "m0_equipment_import": FACADE_HANDLERS["m0_equipment_import"],
    "m0_routes_import": FACADE_HANDLERS["m0_routes_import"],
    "m0_operations_import": FACADE_HANDLERS["m0_operations_import"],
    "m0_tooling_import": FACADE_HANDLERS["m0_tooling_import"],
    # F-008（M6 财务）写入面：费用支出 / 送货单（canonical 唯一主，M6 侧只读）
    "m0_expenses_import": FACADE_HANDLERS["m0_expenses_import"],
    "m0_delivery_notes_import": FACADE_HANDLERS["m0_delivery_notes_import"],
    # canonical 读面（进程内 m0_facts，需 YUNPAI_M0_DB）
    "get_m0_product_overview": m0_product_overview,
    "get_m0_product_graph": m0_product_graph,
    "list_m0_documents": m0_read_documents,
    "list_m0_inventory": m0_read_inventory,
    "list_m0_entities": m0_read_entities,
    "list_expenses": m0_read_expenses,
    "list_delivery_notes": m0_read_delivery_notes,
    # ── M1 文档解析（本地实现；契约见 registry-manifests/m1.json）──────────
    # 迁自 _wt/INT/src/yunpai_langgraph/workers.py（rows-S2 全 17 条「改造后搬」）。
    # ingest_document 覆盖 V2 原 fixture/preview 版（provider=local + M1Store 持久化）。
    "ingest_document": m1_parse,
    "ingest_m1_archive": m1_archive_ingest,
    "get_m1_task": m1_task_get,
    "get_m1_batch": m1_batch_get,
    "get_m1_document": m1_document_get,
    "list_m1_tasks": m1_tasks_list,
    "list_m1_review_queue": m1_review_queue,
    "submit_m1_review": m1_submit_review,
    "search_m1_orders": m1_orders_search,
    "search_m1_documents": m1_documents_search,
    "export_m1_order": m1_export_order,
    "generate_m1_report": m1_generate_report,
    # S2 M1-3：知识链 5 个（真实本地，canonical 事实源经 M0Store.list_entities）
    "search_m1_knowledge": _k_search,
    "list_m1_knowledge_entities": _k_list_entities,
    "get_m1_knowledge_entity": _k_entity_get,
    "get_m1_knowledge_graph": _k_graph_get,
    "get_m1_knowledge_stats": _k_stats,
    "run_bom_sop_workflow": m2_bom,
    # ── M2 BOM/SOP 本地实现（契约见 registry-manifests/m2.json；rows-S3.md）──
    # run_bom_sop_workflow 保留 V2 既有 m2_bom（已同步 P1-9 修复）；其余 6 个由
    # m2_local.LOCAL_HANDLERS 惰性提供。
    "search_m2_bom_history": _m2("search_m2_bom_history"),
    "generate_m2_bom_controlled": _m2("generate_m2_bom_controlled"),
    "onboard_m2_bom_template": _m2("onboard_m2_bom_template"),
    "generate_m2_sop": _m2("generate_m2_sop"),
    "list_m2_runs": _m2("list_m2_runs"),
    "get_m2_run": _m2("get_m2_run"),
    "run_m3_procurement_requirements": m3_mrp,
    # M3 正式齐套快照（本地实现；契约见 registry-manifests/m3.json）
    "get_material_readiness_snapshot": _m3("get_material_readiness_snapshot"),
    # ── M4 采购 / 供应商 / 跟踪（本地实现；契约见 registry-manifests/m4.json）──
    # 逐工具结论见 _migration/rows-S5.md（直接搬 22 / 改造后搬 2 / 废弃 1 / 不搬 1）。
    # 未登记：import_m4_purchase_suggestions（CSV，废弃不搬）与
    # receive_m4_schedule_impact_proposal（ORCHESTRATION_INTERNAL，不得进 HANDLERS）。
    "import_m4_purchase_suggestions_json": _m4("import_m4_purchase_suggestions_json"),
    "list_m4_purchase_suggestions": _m4("list_m4_purchase_suggestions"),
    "generate_m4_purchase_orders": _m4("generate_m4_purchase_orders"),
    "list_m4_purchase_orders": _m4("list_m4_purchase_orders"),
    "get_m4_purchase_order": _m4("get_m4_purchase_order"),
    "submit_m4_purchase_order_review": _m4("submit_m4_purchase_order_review"),
    "approve_m4_purchase_order": _m4("approve_m4_purchase_order"),
    "request_changes_m4_purchase_order": _m4("request_changes_m4_purchase_order"),
    "generate_m4_purchase_inquiry_message": _m4("generate_m4_purchase_inquiry_message"),
    # local_only + remote_invocation=forbidden（P0-5）：registry 永不为其安装 HTTP 适配器。
    "send_m4_purchase_order": _m4("send_m4_purchase_order"),
    "list_m4_suppliers": _m4("list_m4_suppliers"),
    "create_m4_supplier": _m4("create_m4_supplier"),
    "update_m4_supplier": _m4("update_m4_supplier"),
    "create_m4_supplier_reply": _m4("create_m4_supplier_reply"),
    "parse_m4_supplier_reply": _m4("parse_m4_supplier_reply"),
    "confirm_m4_supplier_reply": _m4("confirm_m4_supplier_reply"),
    "confirm_m4_supplier_fact": _m4("confirm_m4_supplier_fact"),
    "list_m4_tracking": _m4("list_m4_tracking"),
    "scan_m4_purchase_alerts": _m4("scan_m4_purchase_alerts"),
    "list_m4_purchase_alerts": _m4("list_m4_purchase_alerts"),
    "generate_m4_urge_message": _m4("generate_m4_urge_message"),
    "query_m4_material_supply_snapshot": _m4("query_m4_material_supply_snapshot"),
    "list_m4_material_supply_events": _m4("list_m4_material_supply_events"),
    "get_m4_material_supply_snapshot": _m4("get_m4_material_supply_snapshot"),
    "solve_scheduling": m5_schedule,
    # M5 PMC v2 tool handlers (Taskbook Tasks 3-5); the two excluded tools
    # (report_workload, bind_worker_to_order) intentionally stay unbound.
    "get_m5_schedule": _m5("get_m5_schedule"),
    "list_m5_schedules": _m5("list_m5_schedules"),
    "get_m5_pmc_progress": _m5("get_m5_pmc_progress"),
    "get_m5_material_readiness": _m5("get_m5_material_readiness"),
    "get_m5_integration_contracts": _m5("get_m5_integration_contracts"),
    "ingest_m5_planning_snapshot": _m5("ingest_m5_planning_snapshot"),
    "replan_m5_schedule": _m5("replan_m5_schedule"),
    "dispatch_m5_schedule": _m5("dispatch_m5_schedule"),
    "get_m5_execution_summary": _m5("get_m5_execution_summary"),
    "search_m5_knowledge": _m5("search_m5_knowledge"),
    "record_m5_knowledge": _m5("record_m5_knowledge"),
    "prepare_m5_department_message": _m5("prepare_m5_department_message"),
    "get_m5_department_message": _m5("get_m5_department_message"),
    "get_m5_department_message_delivery": _m5("get_m5_department_message_delivery"),
    "advise_m5_schedule": _m5("advise_m5_schedule"),
    "run_m5_intelligent_schedule": _m5("run_m5_intelligent_schedule"),
    "generate_m5_material_procurement_plan": _m5("generate_m5_material_procurement_plan"),
    # ── M6 财务：成本账（F-008；契约见 registry-manifests/m6.json）──────────
    # 三段式（书二 §6.2.1 / D-005）：save_* 属 propose 段（只落 trial 草稿，无门）；
    # confirm_* / close_month_* 是 commit 段的**发起**（开 finance 门，翻正/冻结在
    # graph.py 的 `_apply_m6_*` 于 approve 后执行）。三个读工具一律经 m6_store 只读口。
    "save_costing_snapshot": _m6("save_costing_snapshot"),
    "confirm_costing_snapshot": _m6("confirm_costing_snapshot"),
    "close_month_costing": _m6("close_month_costing"),
    "list_costing_snapshots": _m6("list_costing_snapshots"),
    "get_costing_snapshot": _m6("get_costing_snapshot"),
    "list_month_costing": _m6("list_month_costing"),
    # 内核读工具（纯算数：算产品成本 / 订单成本审计 / 费用分摊）——无副作用、无门
    "get_product_cost": _m6("get_product_cost"),
    "audit_order_cost": _m6("audit_order_cost"),
    "allocate_expenses": _m6("allocate_expenses"),
    # 单据台账（B2）：报价单 / 对账单——save_* 属 propose 段（落 trial 草稿 + 开 finance 门，
    # 生效由 `_apply_m6_document_commit` 在 approve 后翻 confirmed）；generate_* 纯算数不落库。
    "generate_quotation": _m6("generate_quotation"),
    "save_quotation": _m6("save_quotation"),
    "list_quotations": _m6("list_quotations"),
    "get_quotation": _m6("get_quotation"),
    "save_statement": _m6("save_statement"),
    "list_statements": _m6("list_statements"),
    # 凭据（B3）：get_delivery_note 读 canonical 送货单（唯一主，M6 不建 create）；
    # generate_statement 依据送货单/入库事实生成对账明细（纯算数、不落库、无门）。
    "get_delivery_note": _m6("get_delivery_note"),
    "generate_statement": _m6("generate_statement"),
    # 资产台账（B4）：upsert_asset_ledger 属 propose 段（追加 trial 修订 + 开 finance 门，
    # 生效由 `_apply_m6_asset_commit` 在 approve 后翻 confirmed）；其余两件纯读/纯算数。
    "get_asset_ledger": _m6("get_asset_ledger"),
    "upsert_asset_ledger": _m6("upsert_asset_ledger"),
    "compute_asset_benefit": _m6("compute_asset_benefit"),
    # 库存财务视图（B5）：四态分账 + 在途，纯算数只读、无门、不落库。
    "get_inventory_finance_view": _m6("get_inventory_finance_view"),
    # 订单列表 + 工资两件（B6）：canonical 读 + 纯算数，三件都不落库、无门。
    # 工资是敏感数据，但 M6 本层不判授权（敏感读的保护属身份层权限，见 rules.py M6 块）。
    "list_orders": _m6("list_orders"),
    "calculate_piece_pay": _m6("calculate_piece_pay"),
    "calculate_monthly_pay": _m6("calculate_monthly_pay"),
    # M0 自描述表识别（agent-driven file recognition，确定性安全网）
    "sample_file": sample_file,
    "ingest_recognized": ingest_recognized,
    "query_recognized_table": query_recognized_table,
    "ingest_canonical": ingest_canonical,
}
