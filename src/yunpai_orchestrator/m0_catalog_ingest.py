"""M0 catalog 语义层（Batch D，D-1 核心）。
> 迁入自 `_wt/INT/src/yunpai_langgraph/m0_catalog_ingest.py`（LF 归一 sha256=74c629aa2fead17c727707ffed9605c7dc08053c457682dad7cc0e735afa470a，72719B / 1221 行）。
> M0 分片 C1 随迁（`_migration/rows-S1.md`）；本文件改动：无（逐行迁入）。

复刻旧 catalog 服务的 m0.ingest.v1 校验/发布语义，落本地 canonical 8 表
（M0Store DDL）+ 两个 sqlite 扩展 side-table：

- m0_catalog_ingestions：幂等台账，UNIQUE(tenant_id, task_id, idempotency_key)；
  同 (租户,task,key) 同 record_hash → duplicate；异 hash → conflict。
- m0_catalog_relations：实体关系（活边=status=active）；本增量先支持显式
  relations（仅 product 允许），派生边由 D-3/D-4 接入。

本地等价口径（与旧实现的差距逐条登记于 R 记录/设计文档）：
- 本层发布用**自有单事务写入器**：canonical_entities.canonical_key =
  identity.business_key（稳定身份实体）；版本化实体（bom/document/process_route）
  同 business_key 的不同 version_id 记录 = 同一实体的版本时间线追加
  （payload 信封内含 version_id，与 v3 信封形状一致）。与 data_import 通用管线
  （uuid 回退键）并存互不干扰。
- 内容相等复用：同 business_key 同内容（跨 task/key）不追加版本，计 published
  并写台账（对应旧 repository content 相同复用 version 行）。
- catalog_counts = 本地等价计数 {ingestions, entities, entity_versions, relations,
  evidence→source_documents}（旧为 5 表行数，evidence 无独立本地表）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from .m0_sandbox import utc_now

# m0.ingest.v1 判别联合的 entity_type（schema 白名单）
ENTITY_TYPES: tuple[str, ...] = (
    "product", "product_family", "order", "bom", "document", "material",
    "supplier", "equipment", "process_route", "operation", "tooling",
    # F-008（M6 财务）：费用支出事实 + 送货单（对账依据）。老仓只走 facade 层
    # （`m0_facades.TYPED_ENTITY_REQUIRED`），此处收口进唯一白名单。
    "expense", "delivery_note",
)
# 版本化实体：version_id 必填；其余稳定身份禁带 version_id
VERSIONED_ENTITY_TYPES: frozenset[str] = frozenset({"bom", "document", "process_route"})
# review_status 枚举
REVIEW_STATUSES: tuple[str, ...] = ("candidate", "approved", "rejected")
# role 枚举（document）
DOCUMENT_ROLES: tuple[str, ...] = ("approval_specification", "sop", "engineering_drawing")
# document role → 产品文档证据关系类型
DOC_ROLE_RELATIONS: dict[str, str] = {
    "approval_specification": "has_approval_specification",
    "sop": "has_sop",
    "engineering_drawing": "has_engineering_drawing",
}
DOC_CANDIDATE_SCHEMA = "m0.document-candidate.v1"
# product 显式 relations 允许的 relation_type → target entity_type
PRODUCT_RELATION_TYPES: dict[str, str] = {
    "member_of_family": "product_family",
    "related_product": "product",
}

SIDE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS m0_catalog_ingestions (
  ingestion_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL, task_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
  record_hash TEXT NOT NULL, status TEXT NOT NULL,           -- published|duplicate
  entity_type TEXT NOT NULL DEFAULT '', entity_key TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 0, batch_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(tenant_id, task_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS m0_catalog_relations (
  relation_id TEXT PRIMARY KEY,
  owner_entity_key TEXT NOT NULL, owner_entity_type TEXT NOT NULL, owner_version INTEGER NOT NULL DEFAULT 0,
  relation_type TEXT NOT NULL,
  source_type TEXT NOT NULL, source_key TEXT NOT NULL,
  target_type TEXT NOT NULL, target_key TEXT NOT NULL,
  properties_json TEXT NOT NULL DEFAULT '{}',
  derived_from TEXT NOT NULL DEFAULT 'explicit',
  status TEXT NOT NULL DEFAULT 'active',                      -- active|closed
  created_at TEXT NOT NULL, closed_at TEXT NOT NULL DEFAULT '',
  UNIQUE(owner_entity_key, owner_entity_type, owner_version, relation_type, source_key, target_key)
);
CREATE INDEX IF NOT EXISTS idx_cat_relations_live ON m0_catalog_relations(status, relation_type);
"""


def canonical_json(value: Any) -> str:
    """record_hash 用的 canonical 序列化（sort_keys/compact/ensure_ascii=False）。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _business_content(record: dict[str, Any]) -> dict[str, Any]:
    """实体业务内容（内容相等复用判定用）：信封去掉 transport/溯源字段。

    对应旧 repository 的 entity_content_hash（payload/label/status/attributes/review
    等业务子集）：idempotency_key/source/evidence/relations 不参与内容相等判定。
    """
    return {
        "entity_type": str(record.get("entity_type") or ""),
        "identity": record.get("identity"),
        "payload": record.get("payload"),
        "review_status": str(record.get("review_status") or ""),
        "reviewed_by": str(record.get("reviewed_by") or ""),
    }


def _issue(severity: str, code: str, message: str, field: str = "") -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message, "field": field}


class CatalogValidationError(ValueError):
    """校验/幂等语义失败；携带完整 report（进程内错误通道的载体）。"""

    def __init__(self, code: str, report: dict[str, Any], message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.report = report


def adapt_document_candidates(candidates: list[dict[str, Any]], *, tenant_id: str,
                              actor: str | None = None) -> dict[str, Any]:
    """m0.document-candidate.v1 候选 → m0.ingest.v1 document 记录（D-3）。

    缺 document_no/revision/role/title/product_codes/evidence 即拒绝该候选；
    绝不按标题/文件名/名称猜产品。approved 候选须有审核人（候选值或 actor）。
    返回 adaptation 对象（m0.ingest.adaptation.v1 形状）。
    """
    issues: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    errors = 0
    for index, cand in enumerate(candidates or []):
        cand_issues: list[tuple[str, str, str]] = []  # (code, message, field)
        if not isinstance(cand, dict):
            errors += 1
            issues.append({"index": index, "severity": "error", "code": "CANONICAL_CONTRACT_ERROR",
                           "message": "候选必须是 JSON 对象", "field": "candidates"})
            continue
        if str(cand.get("schema_version") or "") != DOC_CANDIDATE_SCHEMA:
            cand_issues.append(("CANONICAL_CONTRACT_ERROR", f"schema_version 必须为 {DOC_CANDIDATE_SCHEMA}", "schema_version"))
        doc_no = str(cand.get("document_no") or "")
        revision = str(cand.get("revision") or "")
        title = str(cand.get("title") or "")
        role = str(cand.get("role") or "")
        product_codes = cand.get("product_codes")
        evidence = cand.get("evidence")
        review_status = str(cand.get("review_status") or "")
        source = cand.get("source")
        if not (doc_no and revision and title):
            cand_issues.append(("MISSING_STABLE_DOCUMENT_FIELD",
                                "document_no/revision/title 缺一即拒绝（不猜测身份）", "document_no/revision/title"))
        if role not in DOCUMENT_ROLES:
            cand_issues.append(("INVALID_DOCUMENT_ROLE", f"role 必须为 {DOCUMENT_ROLES}", "role"))
        if not isinstance(product_codes, list) or not product_codes or not all(isinstance(p, str) and p for p in product_codes):
            cand_issues.append(("MISSING_PRODUCT_CODE", "product_codes 至少 1 个；绝不按标题/文件名猜产品", "product_codes"))
        if not isinstance(evidence, list) or not evidence:
            cand_issues.append(("MISSING_EVIDENCE", "evidence 至少 1 条", "evidence"))
        if review_status not in REVIEW_STATUSES:
            cand_issues.append(("INVALID_REVIEW_STATUS", f"review_status 必须为 {REVIEW_STATUSES}", "review_status"))
        elif review_status == "approved":
            reviewer = str(cand.get("reviewed_by") or "") or (actor or "")
            if not reviewer:
                cand_issues.append(("REVIEWER_REQUIRED", "approved 候选须有审核人（reviewed_by 或请求主体）", "reviewed_by"))
        if not isinstance(source, dict) or not str(source.get("system") or "") or not str(source.get("external_id") or ""):
            cand_issues.append(("CANONICAL_CONTRACT_ERROR", "source.system/external_id 必填", "source"))
        if cand_issues:
            errors += 1
            for code, message, field in cand_issues:
                issues.append({"index": index, "severity": "error", "code": code,
                               "message": message, "field": field})
            continue
        # 组装 m0.ingest.v1 document 信封
        src_system = str(source.get("system") or "")
        src_external = str(source.get("external_id") or "")
        idem_key = str(cand.get("idempotency_key") or "").strip()
        if not idem_key:
            idem_key = f"{DOC_CANDIDATE_SCHEMA}:{src_system}:{src_external}:document:{doc_no}:{revision}"
        attributes = dict(cand.get("attributes") or {})
        for key in ("parser_metadata", "parser_confidence", "confidence"):
            if cand.get(key) is not None:
                attributes[key] = cand[key]
        payload: dict[str, Any] = {
            "document_no": doc_no, "revision": revision, "role": role, "title": title,
            "product_codes": [str(p) for p in product_codes],
            "status": str(cand.get("status") or "active"),
        }
        if cand.get("content_uri"):
            payload["content_uri"] = str(cand["content_uri"])
        if attributes:
            payload["attributes"] = attributes
        envelope: dict[str, Any] = {
            "schema_version": "m0.ingest.v1",
            "tenant_id": str(cand.get("tenant_id") or tenant_id),
            "idempotency_key": idem_key,
            "source": {k: source[k] for k in ("system", "external_id", "sha256") if k in source},
            "entity_type": "document",
            "identity": {"business_key": doc_no, "version_id": revision},
            "payload": payload,
            "evidence": evidence,
            "review_status": review_status,
        }
        if review_status in {"approved", "rejected"}:
            envelope["reviewed_by"] = str(cand.get("reviewed_by") or "") or (actor or "")
        records.append(envelope)
    summary = {"records": len(candidates or []), "errors": errors}
    return {
        "schema_version": "m0.ingest.adaptation.v1",
        "adapter": "document-candidate",
        "input_schema_version": DOC_CANDIDATE_SCHEMA,
        "canonical_schema_version": "m0.ingest.v1",
        "valid": errors == 0,
        "summary": summary,
        "issues": issues,
        "records": records,
    }


def merge_relation_issues(report: dict[str, Any],
                          issues_by_index: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
    """把派生关系端点检查的错误并入 validate report（改计数与 valid/publishable）。"""
    if not issues_by_index:
        return report
    report = dict(report)
    records = [dict(r) for r in report["records"]]
    added = 0
    for rec in records:
        extra = issues_by_index.get(int(rec["index"]), [])
        if extra:
            rec["issues"] = list(rec["issues"]) + extra
            added += len(extra)
    summary = dict(report["summary"])
    summary["errors"] = int(summary["errors"]) + added
    report["summary"] = summary
    report["valid"] = summary["errors"] == 0
    report["publishable"] = report["valid"] and summary["review_blockers"] == 0
    report["records"] = records
    return report


def document_relation_issues(envelopes: list[dict[str, Any]], *, tenant_id: str,
                             lookup) -> dict[int, list[dict[str, Any]]]:
    """document 候选派生关系（product→has_*）端点存在性检查（fail-closed）。"""
    return product_dependency_issues(envelopes, tenant_id=tenant_id, lookup=lookup)


def product_refs_of(envelope: dict[str, Any]) -> list[str]:
    """各实体类型的产品引用（关系端点用）：order/bom/document/process_route 非可选。"""
    entity_type = str(envelope.get("entity_type") or "")
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    if not isinstance(payload, dict):
        return []
    if entity_type == "order":
        return [str(line.get("product_code")) for line in payload.get("lines") or []
                if isinstance(line, dict) and str(line.get("product_code") or "")]
    if entity_type in {"bom", "process_route"}:
        code = str(payload.get("product_code") or "")
        return [code] if code else []
    if entity_type == "document":
        return [str(p) for p in payload.get("product_codes") or []]
    return []


def product_dependency_issues(envelopes: list[dict[str, Any]], *, tenant_id: str,
                              lookup) -> dict[int, list[dict[str, Any]]]:
    """非可选产品引用端点存在性（order/bom/document/process_route）→ issues by index。

    同批携带仅算 entity_type=product 的记录；其余须已 canonical。
    """
    out: dict[int, list[dict[str, Any]]] = {}
    in_request = {str(e.get("entity_type") or ""): set() for e in envelopes}
    product_batch = in_request.setdefault("product", set())
    for e in envelopes:
        if str(e.get("entity_type") or "") == "product":
            product_batch.add(str((e.get("identity") or {}).get("business_key") or ""))
    for idx, e in enumerate(envelopes):
        missing = []
        for code in product_refs_of(e):
            if code in product_batch:
                continue
            if lookup("default" if not tenant_id else tenant_id, "product", code) is not None:
                continue
            missing.append(code)
        if missing:
            field = "payload.product_code" if str(e.get("entity_type") or "") != "order" else "payload.lines[].product_code"
            out[idx] = [_issue("error", "UNRESOLVED_RELATION_ENDPOINT",
                               f"product 端点未入规范且不在同批: {missing}", field)]
    return out


class CatalogService:
    """进程内 catalog 语义层（canonical-only：db_path 必须指向 M0Store 库）。"""

    canonical = True

    def __init__(self, db_path: str | Path) -> None:
        self.path = str(db_path)
        from .m0_backend import M0Store

        self.store = M0Store(self.path)  # 建 8 表（幂等）
        with self._connect() as db:
            db.executescript(SIDE_TABLE_DDL)

    # ---------- helpers ----------
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def _entity_lookup(self, tenant_id: str, entity_type: str, business_key: str) -> dict[str, Any] | None:
        """按业务身份找实体（canonical_key=business_key 的本层写入）。"""
        with self._connect() as db:
            row = db.execute(
                "SELECT entity_id, canonical_key, current_version, lifecycle_status "
                "FROM canonical_entities WHERE tenant_id=? AND entity_type=? AND canonical_key=?",
                (tenant_id, entity_type, business_key)).fetchone()
        return dict(row) if row else None

    def _idem_row(self, tenant_id: str, task_id: str, key: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM m0_catalog_ingestions WHERE tenant_id=? AND task_id=? AND idempotency_key=?",
                (tenant_id, task_id, key)).fetchone()
        return dict(row) if row else None

    # ---------- 校验（dry-run，无写） ----------
    def validate_records(self, records: list[dict[str, Any]], *, tenant_id: str,
                         task_id: str | None = None,
                         actor: str | None = None) -> dict[str, Any]:
        """m0.ingest.v1 记录集校验，返回 m0.ingest.validation.v1 report（不写库）。

        actor 非空时先做 reviewed_by 权威化（与 publish 同口径），使幂等判定
        与已发布台账可比。
        """
        if actor:
            records = [self._authoritative_reviewed_by(r, actor) for r in records]
        summary = {"records": len(records), "errors": 0, "review_blockers": 0,
                   "duplicates": 0, "new_records": 0, "planned_relations": 0}
        out_records: list[dict[str, Any]] = []
        seen_keys: dict[tuple[str, str], int] = {}        # (tenant, idem) -> index
        seen_identity: dict[tuple[str, str, str, str], int] = {}  # (tenant,type,key,version) -> index
        errors = 0
        blockers = 0

        for index, record in enumerate(records):
            issues: list[dict[str, Any]] = []
            entity_type = ""
            business_key = ""
            version_id = ""
            idem_key = ""
            idem_status = "new"
            planned_relations = 0
            has_contract_error = False

            if not isinstance(record, dict):
                issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR", "记录必须是 JSON 对象", "records"))
                has_contract_error = True
            else:
                if str(record.get("schema_version") or "") != "m0.ingest.v1":
                    issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR",
                                         f"schema_version 必须为 m0.ingest.v1", "schema_version"))
                    has_contract_error = True
                entity_type = str(record.get("entity_type") or "")
                if entity_type not in ENTITY_TYPES:
                    issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR",
                                         f"不支持的 entity_type: {entity_type!r}", "entity_type"))
                    has_contract_error = True
                record_tenant = str(record.get("tenant_id") or "")
                if record_tenant != tenant_id:
                    issues.append(_issue("error", "TENANT_MISMATCH",
                                         f"record.tenant_id={record_tenant!r} ≠ 请求租户 {tenant_id!r}", "tenant_id"))
                idem_key = str(record.get("idempotency_key") or "")
                if not idem_key:
                    issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR", "缺少 idempotency_key", "idempotency_key"))
                    has_contract_error = True
                source = record.get("source")
                if not isinstance(source, dict) or not str(source.get("system") or "") or not str(source.get("external_id") or ""):
                    issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR", "source.system/external_id 必填", "source"))
                    has_contract_error = True
                elif len(str(source.get("sha256") or "")) != 64:
                    issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR", "source.sha256 必须为 64 位 hex", "source.sha256"))
                    has_contract_error = True
                identity = record.get("identity")
                if not isinstance(identity, dict):
                    issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR", "identity{business_key,version_id} 必填", "identity"))
                    has_contract_error = True
                else:
                    business_key = str(identity.get("business_key") or "")
                    version_id = str(identity.get("version_id") or "")
                    if not business_key:
                        issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR", "identity.business_key 必填", "identity.business_key"))
                        has_contract_error = True
                    if entity_type in VERSIONED_ENTITY_TYPES and not version_id:
                        issues.append(_issue("error", "VERSION_REQUIRED",
                                             f"{entity_type} 为版本化实体，identity.version_id 必填", "identity.version_id"))
                    elif entity_type not in VERSIONED_ENTITY_TYPES and version_id:
                        issues.append(_issue("error", "STABLE_IDENTITY_CANNOT_HAVE_VERSION",
                                             f"{entity_type} 为稳定身份实体，禁止携带 version_id", "identity.version_id"))
                if not isinstance(record.get("payload"), dict):
                    issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR", "payload 必须为对象", "payload"))
                    has_contract_error = True
                evidence = record.get("evidence")
                if not isinstance(evidence, list) or not evidence:
                    issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR", "evidence 至少 1 条", "evidence"))
                    has_contract_error = True
                else:
                    ev_keys = [str(e.get("key") or "") for e in evidence if isinstance(e, dict)]
                    if len(ev_keys) != len(set(ev_keys)):
                        issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR", "evidence key 必须唯一", "evidence"))
                        has_contract_error = True
                review_status = str(record.get("review_status") or "")
                if review_status not in REVIEW_STATUSES:
                    issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR",
                                         f"review_status 必须为 {REVIEW_STATUSES}", "review_status"))
                    has_contract_error = True
                elif review_status == "approved" and not str(record.get("reviewed_by") or ""):
                    issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR", "review_status=approved 时 reviewed_by 必填", "reviewed_by"))
                    has_contract_error = True
                # 显式 relations：仅 product 允许
                relations = record.get("relations")
                if relations:
                    if entity_type != "product":
                        issues.append(_issue("error", "INVALID_EXPLICIT_RELATION_SOURCE",
                                             "显式 relations 仅 product 记录允许", "relations"))
                    else:
                        rel_issues, rel_count = self._check_explicit_relations(
                            relations, evidence if isinstance(evidence, list) else [],
                            records, index, tenant_id)
                        issues.extend(rel_issues)
                        planned_relations += rel_count
                # 请求内重复
                if record_tenant == tenant_id and idem_key and not has_contract_error:
                    prior = seen_keys.get((tenant_id, idem_key))
                    if prior is not None:
                        issues.append(_issue("error", "DUPLICATE_IDEMPOTENCY_KEY",
                                             f"请求内重复 idempotency_key（记录 {prior}）", "idempotency_key"))
                    else:
                        seen_keys[(tenant_id, idem_key)] = index
                if entity_type in ENTITY_TYPES and business_key and not has_contract_error:
                    id_tuple = (tenant_id, entity_type, business_key, version_id)
                    prior_id = seen_identity.get(id_tuple)
                    if prior_id is not None:
                        issues.append(_issue("error", "DUPLICATE_ENTITY_VERSION",
                                             f"请求内重复实体身份（记录 {prior_id}）", "identity"))
                    else:
                        seen_identity[id_tuple] = index
                # review 级：candidate/rejected 合规但不可发布
                if review_status in {"candidate", "rejected"} and not has_contract_error:
                    issues.append(_issue("review", "REVIEW_REQUIRED",
                                         "review_status 必须为 approved 才可发布", "review_status"))

            # 幂等三态（只读台账；task_id 为空时不判）
            record_hash = canonical_json(record)
            if not has_contract_error and task_id and entity_type in ENTITY_TYPES and idem_key:
                row = self._idem_row(tenant_id, task_id, idem_key)
                if row is not None:
                    if row["record_hash"] == record_hash:
                        idem_status = "duplicate"
                        summary["duplicates"] += 1
                    else:
                        idem_status = "conflict"
                        issues.append(_issue("error", "IDEMPOTENCY_CONFLICT",
                                             "同 (tenant,task,idempotency_key) 已存在不同载荷", "idempotency_key"))

            errors_in_record = sum(1 for i in issues if i["severity"] == "error")
            blockers_in_record = sum(1 for i in issues if i["severity"] == "review")
            errors += errors_in_record
            blockers += blockers_in_record
            if idem_status == "new" and errors_in_record == 0:
                summary["new_records"] += 1
            summary["planned_relations"] += planned_relations
            out_records.append({
                "index": index, "entity_type": entity_type, "business_key": business_key,
                "version_id": version_id, "idempotency_key": idem_key,
                "idempotency_status": idem_status,
                "issues": issues,
            })

        summary["errors"] = errors
        summary["review_blockers"] = blockers
        return {
            "schema_version": "m0.ingest.validation.v1",
            "valid": errors == 0,
            "publishable": errors == 0 and blockers == 0,
            "summary": summary,
            "records": out_records,
        }

    def _check_explicit_relations(self, relations: list[Any], evidence: list[Any],
                                  records: list[dict[str, Any]], owner_index: int,
                                  tenant_id: str) -> tuple[list[dict[str, Any]], int]:
        """product 显式 relations 校验：target 类型/无 version/禁自指/证据覆盖/端点存在。"""
        issues: list[dict[str, Any]] = []
        count = 0
        ev_keys = {str(e.get("key") or "") for e in evidence if isinstance(e, dict)}
        owner = records[owner_index] if owner_index < len(records) else {}
        owner_identity = owner.get("identity") if isinstance(owner, dict) else None
        owner_key = str((owner_identity or {}).get("business_key") or "") if isinstance(owner_identity, dict) else ""
        for rel in relations:
            if not isinstance(rel, dict):
                issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR", "relation 必须为对象", "relations"))
                continue
            rtype = str(rel.get("relation_type") or "")
            expected_target = PRODUCT_RELATION_TYPES.get(rtype)
            if expected_target is None:
                issues.append(_issue("error", "INVALID_RELATION_TARGET",
                                     f"product 不允许 relation_type={rtype!r}", "relations.relation_type"))
                continue
            target_obj = rel.get("target") if isinstance(rel.get("target"), dict) else {}
            target_type = str(rel.get("target_type") or "")
            target_key = str(target_obj.get("business_key") or "") if target_obj else str(rel.get("target_key") or "")
            target_version = str(target_obj.get("version_id") or "") if target_obj else ""
            if target_type != expected_target:
                issues.append(_issue("error", "INVALID_RELATION_TARGET",
                                     f"relation {rtype} 的 target_type 必须为 {expected_target}", "relations.target_type"))
                continue
            if target_version:
                issues.append(_issue("error", "INVALID_RELATION_TARGET_VERSION",
                                     f"relation {rtype} 的 target 不允许带 version_id", "relations.target.version_id"))
                continue
            if target_key == owner_key:
                issues.append(_issue("error", "SELF_RELATION", "禁止自关联", "relations.target"))
                continue
            rel_ev = rel.get("evidence_keys")
            if rel_ev is not None and not set(rel_ev).issubset(ev_keys):
                issues.append(_issue("error", "CANONICAL_CONTRACT_ERROR",
                                     "relations.evidence_keys 必须 ⊆ evidence keys", "relations.evidence_keys"))
                continue
            in_request = any(
                isinstance(r, dict) and str(r.get("entity_type") or "") == target_type
                and str((r.get("identity") or {}).get("business_key") or "") == target_key
                for r in records)
            in_store = (not in_request) and self._entity_lookup(tenant_id, target_type, target_key) is not None
            if not (in_request or in_store):
                issues.append(_issue("error", "UNRESOLVED_RELATION_ENDPOINT",
                                     f"relation {rtype} 的端点 {target_type}:{target_key} 不在库也不在本请求",
                                     "relations.target"))
                continue
            count += 1
        return issues, count

    # ---------- 发布 ----------
    def publish_records(self, records: list[dict[str, Any]], *, tenant_id: str, task_id: str,
                        actor: str = "operator", human_override: bool = False) -> dict[str, Any]:
        """approved 记录原子发布（等价语义见模块 docstring）。返回 data 信封。"""
        if not task_id:
            raise CatalogValidationError("MISSING_TASK_ID", {}, "task_id（X-Yunpai-Task-ID 等价）必填")
        if not actor:
            raise CatalogValidationError("MISSING_PRINCIPAL", {}, "actor（principal 等价）必填")
        records = [self._authoritative_reviewed_by(r, actor) for r in records]
        report = self.validate_records(records, tenant_id=tenant_id, task_id=task_id)
        if not report["valid"]:
            code = "IDEMPOTENCY_CONFLICT" if any(
                i["code"] == "IDEMPOTENCY_CONFLICT"
                for r in report["records"] for i in r["issues"]) else "VALIDATION_FAILED"
            raise CatalogValidationError(code, report)
        if not report["publishable"]:
            raise CatalogValidationError("REVIEW_REQUIRED", report)

        results: list[dict[str, Any]] = []
        published = 0
        duplicates = 0
        now = utc_now()
        with self._connect() as db:
            for record in records:
                idem_key = str(record.get("idempotency_key") or "")
                row = self._idem_row(tenant_id, task_id, idem_key)
                record_hash = canonical_json(record)
                if row is not None:
                    if row["record_hash"] == record_hash:
                        # duplicate：零重写，返回既有信息
                        duplicates += 1
                        results.append({
                            "idempotency_key": idem_key, "status": "duplicate",
                            "ingestion_id": row["ingestion_id"],
                            "entity_id": row["entity_key"], "entity_version_row_id": "",
                        })
                    else:
                        raise CatalogValidationError("IDEMPOTENCY_CONFLICT", report)
                    continue
                entity_type = str(record.get("entity_type") or "")
                identity = record.get("identity") if isinstance(record, dict) else {}
                business_key = str(identity.get("business_key") or "") if isinstance(identity, dict) else ""
                existing = self._entity_lookup(tenant_id, entity_type, business_key)
                same_content = False
                if existing is not None:
                    cur = db.execute(
                        "SELECT payload_json FROM canonical_entity_versions "
                        "WHERE entity_id=? AND version=?",
                        (existing["entity_id"], existing["current_version"])).fetchone()
                    try:
                        stored = json.loads(cur["payload_json"]) if cur is not None else None
                        same_content = (stored is not None
                                        and isinstance(stored, dict)
                                        and _business_content(stored) == _business_content(record))
                    except ValueError:
                        same_content = False
                if same_content:
                    # 跨 (task,key) 同内容重发：复用版本不追加
                    ingestion_id = uuid4().hex
                    db.execute(
                        "INSERT INTO m0_catalog_ingestions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (ingestion_id, tenant_id, task_id, idem_key, record_hash, "published",
                         entity_type, business_key, int(existing["current_version"]), "", now))
                    published += 1
                    results.append({
                        "idempotency_key": idem_key, "status": "published",
                        "ingestion_id": ingestion_id, "entity_id": existing["entity_id"],
                        "entity_version_row_id": f"{existing['canonical_key']}@{existing['current_version']}",
                    })
                    continue
                # 新实体或新内容 → 单事务写入（批次/候选/审批/实体版本/ledger/outbox）
                batch_id = f"cat-{uuid4().hex[:12]}"
                checksum = _sha256_hex(canonical_json(record))
                payload_text = json.dumps(record, ensure_ascii=False)
                db.execute("INSERT INTO import_batches VALUES (?,?,?,?,?,?)",
                           (batch_id, tenant_id, task_id, "published", now, now))
                document_id = uuid4().hex
                sha256 = _sha256_hex(payload_text)
                db.execute("INSERT INTO source_documents VALUES (?,?,?,?,?,?)",
                           (document_id, batch_id,
                            f"{record.get('source', {}).get('external_id', 'catalog') if isinstance(record.get('source'), dict) else 'catalog'}",
                            sha256, payload_text, now))
                candidate_id = uuid4().hex
                db.execute("INSERT INTO import_candidates VALUES (?,?,?,?,?,?,?)",
                           (candidate_id, batch_id, document_id, entity_type, payload_text, "published", now))
                approval_id = uuid4().hex
                mode = "human_override" if human_override else "standard"
                db.execute("INSERT INTO approval_records VALUES (?,?,?,?,?,?,?,?)",
                           (approval_id, batch_id, candidate_id, actor, "approve", mode,
                            "catalog publish", now))
                if existing is None:
                    entity_id = uuid4().hex
                    db.execute(
                        "INSERT INTO canonical_entities VALUES (?,?,?,?,?,?,?,?)",
                        (entity_id, tenant_id, entity_type, business_key, 1, "active", now, now))
                    version = 1
                else:
                    entity_id = existing["entity_id"]
                    version = int(existing["current_version"]) + 1
                    db.execute(
                        "UPDATE canonical_entities SET current_version=?, updated_at=? WHERE entity_id=?",
                        (version, now, entity_id))
                db.execute("INSERT INTO canonical_entity_versions VALUES (?,?,?,?,?,?)",
                           (entity_id, version, payload_text, checksum, batch_id, now))
                ledger_id = uuid4().hex
                db.execute("INSERT INTO canonical_ledger VALUES (?,?,?,?,?,?,?,?)",
                           (ledger_id, batch_id, entity_id, version, "publish", actor, mode, now))
                db.execute("INSERT INTO canonical_outbox VALUES (?,?,?,?,?,?)",
                           (uuid4().hex, ledger_id, "canonical.entity.published",
                            json.dumps({"entity_id": entity_id, "version": version}, ensure_ascii=False),
                            "pending", now))
                ingestion_id = uuid4().hex
                db.execute("INSERT INTO m0_catalog_ingestions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (ingestion_id, tenant_id, task_id, idem_key, record_hash, "published",
                            entity_type, business_key, version, batch_id, now))
                published += 1
                results.append({
                    "idempotency_key": idem_key, "status": "published",
                    "ingestion_id": ingestion_id, "entity_id": entity_id,
                    "entity_version_row_id": f"{business_key}@{version}",
                })
            relation_count = self._persist_explicit_relations(records, tenant_id, now, db)
        return {
            "status": "published",
            "published": published,
            "duplicates": duplicates,
            "results": results,
            "tracking_task_id": task_id,
            "catalog_counts": self._catalog_counts(tenant_id),
        }

    def publish_documents(self, candidates: list[dict[str, Any]], *, tenant_id: str,
                          task_id: str, actor: str = "operator",
                          human_override: bool = False) -> dict[str, Any]:
        """data_catalog_document_candidate_publish：候选 → document 实体 + 产品文档边。"""
        adaptation = adapt_document_candidates(candidates, tenant_id=tenant_id, actor=actor)
        if not adaptation["valid"]:
            raise CatalogValidationError("VALIDATION_FAILED",
                                         {"adaptation": adaptation, "validation": None,
                                          "publishable": False})
        envelopes = adaptation["records"]
        rel_issues = document_relation_issues(envelopes, tenant_id=tenant_id,
                                              lookup=self._entity_lookup)
        report = self.validate_records(envelopes, tenant_id=tenant_id, task_id=task_id, actor=actor)
        report = merge_relation_issues(report, rel_issues)
        if not report["valid"]:
            flat = [i for r in report["records"] for i in r["issues"]]
            if any(i["code"] == "IDEMPOTENCY_CONFLICT" for i in flat):
                code = "IDEMPOTENCY_CONFLICT"
            elif any(i["code"] == "UNRESOLVED_RELATION_ENDPOINT" for i in flat):
                code = "UNRESOLVED_RELATION_ENDPOINT"
            else:
                code = "VALIDATION_FAILED"
            raise CatalogValidationError(code, {"adaptation": adaptation, "validation": report,
                                                "publishable": False})
        if not report["publishable"]:
            raise CatalogValidationError("REVIEW_REQUIRED", {"adaptation": adaptation,
                                                             "validation": report, "publishable": False})
        publication = self.publish_records(envelopes, tenant_id=tenant_id, task_id=task_id,
                                           actor=actor, human_override=human_override)
        self._persist_doc_relations(envelopes, tenant_id=tenant_id)
        publication = {**publication, "catalog_counts": self._catalog_counts(tenant_id)}
        return {"adaptation": adaptation, "validation": report, "publishable": True,
                "publication": publication}

    def _persist_doc_relations(self, envelopes: list[dict[str, Any]], *, tenant_id: str) -> int:
        """document 候选派生关系（委托通用派生关系写入器）。"""
        return self.persist_derived_relations(envelopes, tenant_id=tenant_id)

    def persist_derived_relations(self, envelopes: list[dict[str, Any]], *, tenant_id: str) -> int:
        """按实体类型派生关系落 m0_catalog_relations（D-2/D-3/D-4 共用）。

        - document：product→has_approval_specification|has_sop|has_engineering_drawing（owner=document）
        - order：product→has_order（owner=order）；bom：product→has_bom（owner=bom）
        - process_route：product→has_route（owner=route）
        - 可选端点关系只在端点已 canonical 时落：bom→contains_material（material）、
          supplier→supplied_by（material）、route 行 has_operation/uses_material/
          uses_equipment/requires_tooling（operation/资源）；未入规范=静默不落（后到由回填补）。
        """
        written = 0
        now = utc_now()
        with self._connect() as db:
            for envelope in envelopes:
                entity_type = str(envelope.get("entity_type") or "")
                identity = envelope.get("identity") if isinstance(envelope, dict) else {}
                owner_key = str(identity.get("business_key") or "") if isinstance(identity, dict) else ""
                if not owner_key:
                    continue
                row = db.execute(
                    "SELECT current_version FROM canonical_entities WHERE tenant_id=? "
                    "AND entity_type=? AND canonical_key=?",
                    (tenant_id, entity_type, owner_key)).fetchone()
                if row is None:
                    continue
                owner_version = int(row["current_version"])
                payload = envelope.get("payload") if isinstance(envelope, dict) else {}
                if not isinstance(payload, dict):
                    continue
                rows: list[tuple[str, str, str, str, str]] = []  # (rtype, stype, skey, ttype, tkey)
                if entity_type == "document":
                    role = str(payload.get("role") or "")
                    rtype = DOC_ROLE_RELATIONS.get(role)
                    if rtype:
                        rows = [(rtype, "product", str(p), "document", owner_key)
                                for p in payload.get("product_codes") or []]
                elif entity_type == "order":
                    seen_p: set[str] = set()
                    for line in payload.get("lines") or []:
                        code = str((line or {}).get("product_code") or "")
                        if code and code not in seen_p:
                            seen_p.add(code)
                            rows.append(("has_order", "product", code, "order", owner_key))
                elif entity_type == "bom":
                    pcode = str(payload.get("product_code") or "")
                    if pcode:
                        rows.append(("has_bom", "product", pcode, "bom", owner_key))
                    seen_m: set[str] = set()
                    for line in payload.get("lines") or []:
                        code = str((line or {}).get("material_code") or "")
                        if code and code not in seen_m:
                            seen_m.add(code)
                            if self._entity_lookup(tenant_id, "material", code) is not None:
                                rows.append(("contains_material", "bom", owner_key, "material", code))
                elif entity_type == "process_route":
                    pcode = str(payload.get("product_code") or "")
                    if pcode:
                        rows.append(("has_route", "product", pcode, "process_route", owner_key))
                    for op in payload.get("operations") or []:
                        op_code = str((op or {}).get("operation_code") or "")
                        if not op_code:
                            continue
                        op_live = self._entity_lookup(tenant_id, "operation", op_code) is not None
                        if op_live:
                            rows.append(("has_operation", "process_route", owner_key, "operation", op_code))
                        res_pairs = (
                            ("uses_material", "material", "material_codes"),
                            ("uses_equipment", "equipment", "equipment_codes"),
                            ("requires_tooling", "tooling", "tooling_codes"),
                        )
                        for rtype, restype, col in res_pairs:
                            for rcode in (op or {}).get(col) or []:
                                rcode = str(rcode)
                                if not rcode:
                                    continue
                                if not op_live:
                                    continue
                                if self._entity_lookup(tenant_id, restype, rcode) is not None:
                                    rows.append((rtype, "operation", op_code, restype, rcode))
                elif entity_type == "supplier":
                    for mcode in payload.get("material_codes") or []:
                        mcode = str(mcode)
                        if mcode and self._entity_lookup(tenant_id, "material", mcode) is not None:
                            rows.append(("supplied_by", "material", mcode, "supplier", owner_key))
                for rtype, stype, skey, ttype, tkey in rows:
                    db.execute(
                        "INSERT OR IGNORE INTO m0_catalog_relations "
                        "(relation_id, owner_entity_key, owner_entity_type, owner_version, relation_type, "
                        " source_type, source_key, target_type, target_key, properties_json, derived_from, "
                        " status, created_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (uuid4().hex, owner_key, entity_type, owner_version, rtype,
                         stype, skey, ttype, tkey, "{}", entity_type, "active", now, ""))
                    written += 1
        return written

    def supersede_active_boms(self, envelopes: list[dict[str, Any]], *, tenant_id: str) -> int:
        """BOM 抢占：发布 active+approved bom 后，同 product_code 其它 active bom
        追加 superseded 版本（版本时间线保留；旧实现 recorded_to 关闭语义的本地等价）。"""
        superseded = 0
        now = utc_now()
        targets: set[str] = set()
        for env in envelopes:
            payload = env.get("payload") if isinstance(env, dict) else {}
            if (str(env.get("entity_type") or "") != "bom"
                    or str(payload.get("status") or "") != "active"
                    or str(env.get("review_status") or "") != "approved"
                    or not isinstance(payload, dict)):
                continue
            pcode = str(payload.get("product_code") or "")
            if pcode:
                targets.add(pcode)
        if not targets:
            return 0
        published_bom_keys = {str((e.get("identity") or {}).get("business_key") or "")
                              for e in envelopes if str(e.get("entity_type") or "") == "bom"}
        with self._connect() as db:
            # 全量 bom 实体扫描（本地量级小），逐条对 product 目标匹配
            rows = db.execute("SELECT entity_id, canonical_key, current_version FROM canonical_entities "
                              "WHERE tenant_id=? AND entity_type='bom'", (tenant_id,)).fetchall()
            for row in rows:
                if row["canonical_key"] in published_bom_keys:
                    continue
                cur = db.execute("SELECT payload_json FROM canonical_entity_versions "
                                 "WHERE entity_id=? AND version=?", (row["entity_id"], row["current_version"])).fetchone()
                if cur is None:
                    continue
                try:
                    payload = json.loads(cur["payload_json"])
                except ValueError:
                    continue
                if (str(payload.get("entity_type") or "") != "bom"
                        or str((payload.get("payload") or {}).get("product_code") or "") not in targets
                        or str((payload.get("payload") or {}).get("status") or "") != "active"
                        or str(payload.get("review_status") or "") != "approved"):
                    continue
                # 追加 superseded 版本（payload 本体置 superseded，信封字段保留）
                next_version = int(row["current_version"]) + 1
                new_payload = dict(payload)
                new_payload["payload"] = {**payload["payload"], "status": "superseded"}
                text = json.dumps(new_payload, ensure_ascii=False)
                checksum = _sha256_hex(canonical_json(new_payload))
                batch_id = f"cat-{uuid4().hex[:12]}"
                db.execute("INSERT INTO import_batches VALUES (?,?,?,?,?,?)",
                           (batch_id, tenant_id, "supersede", "published", now, now))
                db.execute("UPDATE canonical_entities SET current_version=?, updated_at=? WHERE entity_id=?",
                           (next_version, now, row["entity_id"]))
                db.execute("INSERT INTO canonical_entity_versions VALUES (?,?,?,?,?,?)",
                           (row["entity_id"], next_version, text, checksum, batch_id, now))
                ledger_id = uuid4().hex
                db.execute("INSERT INTO canonical_ledger VALUES (?,?,?,?,?,?,?,?)",
                           (ledger_id, batch_id, row["entity_id"], next_version, "supersede",
                            "system", "standard", now))
                db.execute("INSERT INTO canonical_outbox VALUES (?,?,?,?,?,?)",
                           (uuid4().hex, ledger_id, "canonical.entity.superseded",
                            json.dumps({"entity_id": row["entity_id"], "version": next_version},
                                       ensure_ascii=False), "pending", now))
                superseded += 1
        return superseded

    def backfill_declared_relations(self, envelopes: list[dict[str, Any]], *, tenant_id: str) -> int:
        """后到实体回填（旧 service L600-751 等价子集）：

        material 入规范 → 扫描既有 bom（contains_material）与供应商（supplied_by）；
        operation/equipment/tooling 入规范 → 扫描 process_route 当前版本行补
        has_operation/uses_material/uses_equipment/requires_tooling（端点已入规范才落）。
        """
        written = 0
        now = utc_now()
        declared: dict[str, list[str]] = {"material": [], "operation": [], "equipment": [],
                                          "tooling": []}
        for env in envelopes:
            etype = str(env.get("entity_type") or "")
            key = str((env.get("identity") or {}).get("business_key") or "") if isinstance(env, dict) else ""
            if etype in declared and key:
                declared[etype].append(key)
        with self._connect() as db:
            def insert(row_owner_key: str, owner_type: str, owner_version: int,
                       rtype: str, stype: str, skey: str, ttype: str, tkey: str) -> None:
                nonlocal written
                db.execute(
                    "INSERT OR IGNORE INTO m0_catalog_relations "
                    "(relation_id, owner_entity_key, owner_entity_type, owner_version, relation_type, "
                    " source_type, source_key, target_type, target_key, properties_json, derived_from, "
                    " status, created_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid4().hex, row_owner_key, owner_type, owner_version, rtype,
                     stype, skey, ttype, tkey, "{}", "backfill", "active", now, ""))
                written += 1

            if declared["material"]:
                # 既有 bom 引用 → contains_material；既有供应商声明 → supplied_by
                mat_set = set(declared["material"])
                for row in db.execute("SELECT entity_id, canonical_key, current_version FROM canonical_entities "
                                      "WHERE tenant_id=? AND entity_type IN ('bom','supplier')",
                                      (tenant_id,)).fetchall():
                    cur = db.execute("SELECT payload_json FROM canonical_entity_versions WHERE entity_id=? AND version=?",
                                     (row["entity_id"], row["current_version"])).fetchone()
                    if cur is None:
                        continue
                    try:
                        payload = json.loads(cur["payload_json"])
                    except ValueError:
                        continue
                    pl = payload.get("payload") if isinstance(payload, dict) else None
                    if not isinstance(pl, dict):
                        continue
                    etype = str(payload.get("entity_type") or "")
                    if etype == "bom":
                        for line in pl.get("lines") or []:
                            code = str((line or {}).get("material_code") or "")
                            if code in mat_set:
                                insert(row["canonical_key"], "bom", int(row["current_version"]),
                                       "contains_material", "bom", row["canonical_key"], "material", code)
                    elif etype == "supplier":
                        for code in pl.get("material_codes") or []:
                            if str(code) in mat_set:
                                insert(row["canonical_key"], "supplier", int(row["current_version"]),
                                       "supplied_by", "material", str(code), "supplier", row["canonical_key"])
                # 路线行 material_codes 命中且行内 operation 已入规范 → uses_material
                for row in db.execute("SELECT entity_id, canonical_key, current_version FROM canonical_entities "
                                      "WHERE tenant_id=? AND entity_type='process_route'",
                                      (tenant_id,)).fetchall():
                    cur = db.execute("SELECT payload_json FROM canonical_entity_versions WHERE entity_id=? AND version=?",
                                     (row["entity_id"], row["current_version"])).fetchone()
                    if cur is None:
                        continue
                    try:
                        payload = json.loads(cur["payload_json"])
                    except ValueError:
                        continue
                    pl = payload.get("payload") if isinstance(payload, dict) else None
                    if not isinstance(pl, dict):
                        continue
                    for op in pl.get("operations") or []:
                        op_code = str((op or {}).get("operation_code") or "")
                        if not op_code or self._entity_lookup(tenant_id, "operation", op_code) is None:
                            continue
                        for rcode in (op or {}).get("material_codes") or []:
                            if str(rcode) in mat_set:
                                insert(row["canonical_key"], "process_route",
                                       int(row["current_version"]), "uses_material", "operation",
                                       op_code, "material", str(rcode))
            if declared["operation"] or declared["equipment"] or declared["tooling"]:
                op_set = set(declared["operation"])
                for row in db.execute("SELECT entity_id, canonical_key, current_version FROM canonical_entities "
                                      "WHERE tenant_id=? AND entity_type='process_route'", (tenant_id,)).fetchall():
                    cur = db.execute("SELECT payload_json FROM canonical_entity_versions WHERE entity_id=? AND version=?",
                                     (row["entity_id"], row["current_version"])).fetchone()
                    if cur is None:
                        continue
                    try:
                        payload = json.loads(cur["payload_json"])
                    except ValueError:
                        continue
                    pl = payload.get("payload") if isinstance(payload, dict) else None
                    if not isinstance(pl, dict):
                        continue
                    for op in pl.get("operations") or []:
                        op_code = str((op or {}).get("operation_code") or "")
                        if not op_code:
                            continue
                        op_declared = op_code in op_set
                        op_canonical = (op_declared or
                                        self._entity_lookup(tenant_id, "operation", op_code) is not None)
                        if op_declared:
                            insert(row["canonical_key"], "process_route", int(row["current_version"]),
                                   "has_operation", "process_route", row["canonical_key"], "operation", op_code)
                        if not op_canonical:
                            continue
                        for rtype, restype, col in (("uses_material", "material", "material_codes"),
                                                    ("uses_equipment", "equipment", "equipment_codes"),
                                                    ("requires_tooling", "tooling", "tooling_codes")):
                            for rcode in (op or {}).get(col) or []:
                                rcode = str(rcode)
                                if rcode and rcode in declared.get(restype, []):
                                    insert(row["canonical_key"], "process_route",
                                           int(row["current_version"]), rtype, "operation", op_code,
                                           restype, rcode)
        return written

    def _authoritative_reviewed_by(self, record: dict[str, Any], actor: str) -> dict[str, Any]:
        if str(record.get("review_status") or "") in {"approved", "rejected"}:
            record = dict(record)
            record["reviewed_by"] = actor
        return record

    def _persist_explicit_relations(self, records: list[dict[str, Any]], tenant_id: str,
                                    now: str, db: sqlite3.Connection) -> int:
        """product 显式 relations → m0_catalog_relations（owner=product 实体当前版本）。"""
        written = 0
        for record in records:
            if str(record.get("entity_type") or "") != "product":
                continue
            relations = record.get("relations")
            if not isinstance(relations, list) or not relations:
                continue
            identity = record.get("identity") if isinstance(record, dict) else {}
            owner_key = str(identity.get("business_key") or "") if isinstance(identity, dict) else ""
            row = db.execute(
                "SELECT current_version FROM canonical_entities WHERE tenant_id=? AND entity_type='product' AND canonical_key=?",
                (tenant_id, owner_key)).fetchone()
            owner_version = int(row["current_version"]) if row else 0
            for rel in relations:
                if not isinstance(rel, dict):
                    continue
                target_obj = rel.get("target") if isinstance(rel.get("target"), dict) else {}
                target_key = str(target_obj.get("business_key") or "") if target_obj else str(rel.get("target_key") or "")
                rtype = str(rel.get("relation_type") or "")
                target_type = str(rel.get("target_type") or "")
                if not (owner_key and target_key and rtype):
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO m0_catalog_relations "
                    "(relation_id, owner_entity_key, owner_entity_type, owner_version, relation_type, "
                    " source_type, source_key, target_type, target_key, properties_json, derived_from, "
                    " status, created_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid4().hex, owner_key, "product", owner_version, rtype,
                     "product", owner_key, target_type, target_key,
                     json.dumps(rel.get("properties") or {}, ensure_ascii=False),
                     "explicit", "active", now, ""))
                written += 1
        return written

    def _catalog_counts(self, tenant_id: str) -> dict[str, Any]:
        with self._connect() as db:
            ingestions = db.execute("SELECT count(*) AS n FROM m0_catalog_ingestions").fetchone()["n"]
            entities = db.execute("SELECT count(*) AS n FROM canonical_entities WHERE tenant_id=?", (tenant_id,)).fetchone()["n"]
            versions = db.execute(
                "SELECT count(*) AS n FROM canonical_entity_versions v "
                "JOIN canonical_entities e ON e.entity_id=v.entity_id WHERE e.tenant_id=?",
                (tenant_id,)).fetchone()["n"]
            relations = db.execute("SELECT count(*) AS n FROM m0_catalog_relations").fetchone()["n"]
            evidence = db.execute("SELECT count(*) AS n FROM source_documents").fetchone()["n"]
        return {"ingestions": int(ingestions), "entities": int(entities),
                "entity_versions": int(versions), "relations": int(relations),
                "evidence": int(evidence)}


# ---------------------------------------------------------------------------
# 工具契约级共享服务（P0-4）
#
# manifest 声明的 28 个 M0 工具既要能经本地 handler 调用，也要能经 HTTP
# 适配器调用；两条路径必须共用同一份业务判定，否则同一次调用会因为传输方式
# 不同而给出不同结果（盘点的第 6 类问题）。以下函数是本文件的唯一实现，
# HTTP 端点（m0_backend）直接调用；本地 handler（workers）后续改为调用同一
# 函数（见 _repair/REQUESTS-R1.md）。
# ---------------------------------------------------------------------------


def lookup_entity(service: CatalogService, tenant_id: str, entity_type: str,
                  business_key: str) -> dict[str, Any] | None:
    """公开的实体身份查询（供关系端点校验使用，替代直接访问私有方法）。"""
    return service._entity_lookup(tenant_id, entity_type, business_key)


def publish_typed_records(service: CatalogService, records: list[dict[str, Any]], *,
                          expected_type: str, tenant_id: str, task_id: str,
                          actor: str = "operator") -> dict[str, Any]:
    """typed facade 发布语义（单类型闸门 → approved-only → 派生关系/回填/抢占）。

    与 ``workers._facade_publish`` 同语义，返回契约信封
    ``{"success": bool, "code": str | None, "data": dict, "errors": list}``。
    """
    if not isinstance(records, list) or not records:
        raise ValueError(f"{expected_type} facade 需要 records 数组（≥1）")
    if len(records) > 5000:
        raise ValueError("records 超过上限 5000")
    mixed = [str(r.get("entity_type") or "") for r in records if isinstance(r, dict)]
    if any(entity_type != expected_type for entity_type in mixed):
        return {"success": False, "code": "ENTITY_TYPE_MISMATCH",
                "errors": [{"code": "ENTITY_TYPE_MISMATCH",
                            "message": f"混批拒绝：只接受 entity_type={expected_type}",
                            "details": []}],
                "data": {"expected": expected_type}}
    report = service.validate_records(records, tenant_id=tenant_id, task_id=task_id, actor=actor)
    rel = product_dependency_issues(records, tenant_id=tenant_id,
                                    lookup=lambda t, e, k: lookup_entity(service, t, e, k))
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
                "data": {"validation": report}}
    if not report["publishable"]:
        return {"success": False, "code": "REVIEW_REQUIRED",
                "errors": [{"code": "REVIEW_REQUIRED",
                            "message": "facade 只发布 review_status=approved 记录", "details": []}],
                "data": {"validation": report}}
    try:
        data = service.publish_records(records, tenant_id=tenant_id, task_id=task_id, actor=actor)
    except CatalogValidationError as exc:
        return {"success": False, "code": exc.code,
                "errors": [{"code": exc.code, "message": str(exc), "details": []}],
                "data": exc.report}
    service.persist_derived_relations(records, tenant_id=tenant_id)
    service.backfill_declared_relations(records, tenant_id=tenant_id)
    if expected_type == "bom":
        service.supersede_active_boms(records, tenant_id=tenant_id)
    data = {**data, "catalog_counts": service._catalog_counts(tenant_id)}
    return {"success": True, "code": None, "data": data, "errors": []}


def adapt_and_validate_template(service: CatalogService, *, filename: str, data: bytes,
                                template_version: str, source_system: str,
                                source_external_id: str, review_status: str,
                                tenant_id: str, task_id: str,
                                actor: str = "operator") -> dict[str, Any]:
    """版本化文件 → m0.ingest.v1 → dry-run（含产品引用端点检查）。"""
    from .m0_catalog_templates import adapt_tabular_file

    adaptation = adapt_tabular_file(
        filename=filename, data=data, template_version=template_version,
        tenant_id=tenant_id, source_system=source_system,
        source_external_id=source_external_id, review_status=review_status,
        reviewed_by=actor if review_status in ("approved", "rejected") else "")
    validation = None
    if adaptation["valid"]:
        report = service.validate_records(adaptation["records"], tenant_id=tenant_id,
                                          task_id=task_id, actor=actor)
        rel = product_dependency_issues(
            adaptation["records"], tenant_id=tenant_id,
            lookup=lambda t, e, k: lookup_entity(service, t, e, k))
        validation = merge_relation_issues(report, rel)
    return {"adaptation": adaptation, "validation": validation,
            "publishable": bool(validation and validation["publishable"])}


def publish_template_file(service: CatalogService, *, filename: str, data: bytes,
                          template_version: str, source_system: str,
                          source_external_id: str, review_status: str,
                          tenant_id: str, task_id: str,
                          actor: str = "operator") -> dict[str, Any]:
    """已审核版本化文件 → 适配 → 发布（含派生关系）。返回契约信封。"""
    if review_status != "approved":
        return {"success": False, "code": "REVIEW_REQUIRED",
                "errors": [{"code": "REVIEW_REQUIRED",
                            "message": "data_catalog_file_publish 需要 review_status=approved",
                            "details": []}],
                "data": {}}
    result = adapt_and_validate_template(
        service, filename=filename, data=data, template_version=template_version,
        source_system=source_system, source_external_id=source_external_id,
        review_status="approved", tenant_id=tenant_id, task_id=task_id, actor=actor)
    adaptation, report = result["adaptation"], result["validation"]
    if not adaptation["valid"]:
        return {"success": False, "code": "VALIDATION_FAILED",
                "errors": [{"code": "VALIDATION_FAILED",
                            "message": "模板适配失败（见 data.adaptation.issues）", "details": []}],
                "data": {**result, "publishable": False}}
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
                "data": {**result, "publishable": False}}
    try:
        publication = service.publish_records(adaptation["records"], tenant_id=tenant_id,
                                              task_id=task_id, actor=actor)
    except CatalogValidationError as exc:
        return {"success": False, "code": exc.code,
                "errors": [{"code": exc.code, "message": str(exc), "details": []}],
                "data": exc.report}
    service.persist_derived_relations(adaptation["records"], tenant_id=tenant_id)
    publication = {**publication, "catalog_counts": service._catalog_counts(tenant_id)}
    return {"success": True, "code": None, "errors": [],
            "data": {**result, "publishable": True, "publication": publication}}


def adapt_and_validate_document_candidates(service: CatalogService,
                                           candidates: list[dict[str, Any]], *,
                                           tenant_id: str, task_id: str,
                                           actor: str = "operator") -> dict[str, Any]:
    """m0.document-candidate.v1 候选 → dry-run（含产品文档关系端点检查）。"""
    adaptation = adapt_document_candidates(candidates, tenant_id=tenant_id, actor=actor)
    validation = None
    if adaptation["valid"]:
        report = service.validate_records(adaptation["records"], tenant_id=tenant_id,
                                          task_id=task_id, actor=actor)
        rel = document_relation_issues(
            adaptation["records"], tenant_id=tenant_id,
            lookup=lambda t, e, k: lookup_entity(service, t, e, k))
        validation = merge_relation_issues(report, rel)
    return {"adaptation": adaptation, "validation": validation,
            "publishable": bool(validation and validation["publishable"])}


def publish_document_candidates(service: CatalogService, candidates: list[dict[str, Any]], *,
                                tenant_id: str, task_id: str,
                                actor: str = "operator") -> dict[str, Any]:
    """已审核文档候选 → document 实体 + 产品文档边。返回契约信封。"""
    try:
        data = service.publish_documents(candidates, tenant_id=tenant_id,
                                         task_id=task_id, actor=actor)
    except CatalogValidationError as exc:
        return {"success": False, "code": exc.code,
                "errors": [{"code": exc.code, "message": str(exc), "details": []}],
                "data": exc.report}
    return {"success": True, "code": None, "errors": [], "data": data}
