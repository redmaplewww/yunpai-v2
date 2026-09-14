from __future__ import annotations

import base64
import hashlib
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .business_catalog import ingest_tree
from .m1_tooling import (
    M1_SKILL_OPERATION_MAP,
    unique_tools as m1_unique_tools,
)
from .m3_m4_tooling import (
    M3_SKILL_OPERATION_MAP,
    M4_SKILL_OPERATION_MAP,
    unique_tools,
)
from .m6_tooling import (
    M6_FINANCE_SKILL_OPERATION_MAP,
    M6_LEDGER_SKILL_OPERATION_MAP,
)
from .contracts import normalize_contract_result


SkillHandler = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]

#: Skill 内部派发工具所需的 ToolRegistry。由 ``SkillRegistry`` **构造时注入**
#: （``build_default_skill_registry(registry)``），在 ``SkillRegistry.call`` 执行
#: handler 期间以 ContextVar 暴露给 handler——因此 **不占用 tool context 的键**
#: （V2 的 ``tool_context()`` 契约保持干净，worker/executor.py:15-26 无需改动）。
_TOOL_REGISTRY: ContextVar[Any | None] = ContextVar("yunpai_skill_tool_registry", default=None)


def current_tool_registry() -> Any | None:
    """当前 Skill 调用可用的 ToolRegistry（无则 None，由调用方给出可读错误）。"""
    return _TOOL_REGISTRY.get()


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    handler: SkillHandler
    tags: tuple[str, ...] = field(default_factory=tuple)
    tools: tuple[str, ...] = field(default_factory=tuple)
    version: str = "1.0.0"
    # 与 registry 工具/上游 Skill 的契约版本，用于回归兼容与证据引用
    contract_version: str = "yunpai.skill-contract.v1"


class SkillRegistry:
    """总规划 Agent 可见的高阶能力；Skill 内部可以编排多个工具或数据处理步骤。

    ``tool_registry`` 在构造时注入（见 ``build_default_skill_registry``）：Skill 需要
    经同一个已校验的 ToolRegistry 派发工具，但 V2 的 tool context 只承载请求语义键，
    因此注入不走 context 字典，而由本类在 ``call`` 期间以 ContextVar 暴露。
    """

    def __init__(self, tool_registry: Any | None = None) -> None:
        self.specs: dict[str, SkillSpec] = {}
        self.tool_registry = tool_registry

    def register(self, spec: SkillSpec) -> None:
        if spec.name in self.specs:
            raise ValueError(f"duplicate skill: {spec.name}")
        self.specs[spec.name] = spec

    def validate_tools(self, available_tools: Any) -> None:
        """Fail fast when a declared Skill mapping drifts from Tool manifests."""
        names = set(available_tools)
        missing = {
            skill.name: sorted(set(skill.tools) - names)
            for skill in self.specs.values()
            if set(skill.tools) - names
        }
        if missing:
            raise ValueError(f"skill tool mappings are not registered: {missing}")

    async def call(self, name: str, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if name not in self.specs:
            raise KeyError(f"unknown skill: {name}")
        spec = self.specs[name]
        token = _TOOL_REGISTRY.set(self.tool_registry)
        try:
            result = await spec.handler(payload, context)
        finally:
            _TOOL_REGISTRY.reset(token)
        if isinstance(result, dict):
            result = normalize_contract_result(result, source=f"skill:{name}", invoked_tools=result.get("invoked_tools", []))
            result = {
                **result,
                "skill": name,
                "skill_version": spec.version,
                "skill_contract": spec.contract_version,
                "evidence": [
                    *([item for item in result.get("evidence", [])] if isinstance(result.get("evidence"), list) else []),
                    {"module": "orchestrator", "source_ref": name, "evidence_ref": f"skill:{name}@{spec.version}", "detail": f"Skill 调用 {name}@{spec.version} 已执行"},
                ],
            }
        return result

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "skill_id": f"{spec.name}@{spec.version}",
                "version": spec.version,
                "contract_version": spec.contract_version,
                "description": spec.description,
                "tags": list(spec.tags),
                "tools": list(spec.tools),
            }
            for spec in self.specs.values()
        ]


def _safe_name(filename: str) -> str:
    name = Path(filename).name
    return name if name and name not in {".", ".."} else "upload.bin"


async def identify_business_data(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """识别外部资料或上传文件，并写入可审核候选库。

    上传文件逐个返回状态（accepted/needs_review/unsupported/parse_failed/
    skipped），缺失 content_b64 的文件标记 skipped 而不是静默 continue；
    没有任何 accepted 文件时返回失败而非 candidate_created 空批次。
    """
    from .uploads import UPLOAD_MODES, UploadSummary, require_content_b64, to_attachment_record, validate_mode

    db_path = payload.get("db_path") or "runtime/yunpai-business-catalog.sqlite"
    root_path = payload.get("root_path") or payload.get("business_data_root")
    mode = validate_mode(payload.get("mode") or "master_data")
    summary = UploadSummary(mode=mode)
    if root_path:
        result = ingest_tree(
            root_path,
            db_path,
            batch_id=f"batch-{context.get('task_id', 'skill')}",
            parse_xlsx=True,
            deep_limit_bytes=40_000_000,
        )
        total = int(result.get("file_count") or 0)
        for item in result.get("errors", []):
            summary.add(to_attachment_record(
                {"filename": str(item.get("path") or "upload")}, mode=mode,
                status="parse_failed", reason=str(item.get("error") or "ingest error"),
            ))
        summary.accepted = max(0, total - summary.parse_failed)
        summary.total = max(summary.total, total)
        batch_result = result
    else:
        files = payload.get("files") or []
        if not isinstance(files, list) or not files:
            raise ValueError("业务资料 Skill 需要 root_path 或 files")
        staging = Path(payload.get("staging_dir") or "runtime/business-upload-staging") / str(context.get("task_id", "skill"))
        staging.mkdir(parents=True, exist_ok=True)
        accepted = 0
        for index, item in enumerate(files, start=1):
            filename = str(item.get("filename") or "upload.bin") if isinstance(item, dict) else "upload.bin"
            try:
                raw = require_content_b64(item, filename=filename)
            except ValueError as exc:
                summary.add(to_attachment_record(
                    item if isinstance(item, dict) else {}, mode=mode,
                    status="skipped", reason=str(exc),
                ))
                continue
            digest = hashlib.sha256(raw).hexdigest()
            # Include the upload index so same-name/same-content files do not
            # overwrite one another in the staging batch.
            (staging / f"{digest[:16]}-{index:03d}-{_safe_name(filename)}").write_bytes(raw)
            summary.add(to_attachment_record(
                {**(item if isinstance(item, dict) else {}), "sha256": digest},
                mode=mode, status="accepted",
            ))
            accepted += 1
        if accepted == 0:
            return {
                "skill": "business-data-identification",
                "status": "failed",
                "code": "NO_ACCEPTED_FILES",
                "message": "上传批次中没有可识别文件；缺失 content_b64、格式不支持或超限文件已逐文件跳过",
                "schema_version": "yunpai.business-catalog.v2",
                "upload_summary": summary.as_dict(),
                "evidence": [{"module": "orchestrator", "source_ref": "files", "evidence_ref": f"business-catalog:{context.get('task_id', 'skill')}", "detail": "无 accepted 文件，未创建候选"}],
            }
        batch_result = ingest_tree(staging, db_path, batch_id=f"batch-{context.get('task_id', 'skill')}", parse_xlsx=True, deep_limit_bytes=40_000_000)
        for item in batch_result.get("errors", []):
            summary.add(to_attachment_record(
                {"filename": str(item.get("path") or "upload")}, mode=mode,
                status="parse_failed", reason=str(item.get("error") or "ingest error"),
            ))
    sensitivity_summary = _candidate_sensitivity_summary(db_path)
    # Product identity must be explicit or recoverable from the request's
    # semantic text; never bind an arbitrary workbook to a product by path.
    import re

    identity_text = " ".join(str(payload.get(key) or "") for key in ("product_code", "product_name", "message"))
    product_match = re.search(r"\b[A-Z]{1,4}-[A-Z0-9]{2,}\b", identity_text)
    product_code = str(payload.get("product_code") or (product_match.group(0) if product_match else "")).strip()
    from .business_catalog import canonical_records_from_batch

    product_name = str(payload.get("product_name") or payload.get("message") or "")
    canonical_records = canonical_records_from_batch(
        db_path,
        batch_id=str(batch_result.get("batch_id") or ""),
        tenant_id=str(context.get("tenant_id") or "default"),
        product_code=product_code,
        product_name=product_name,
        reviewed_by=str(context.get("actor_user") or context.get("principal_id") or "operator"),
    ) if product_code else []
    return {
        "skill": "business-data-identification",
        "skill_mode": mode,
        "status": "candidate_created",
        "schema_version": "yunpai.business-catalog.v2",
        "upload_summary": summary.as_dict(),
        "sensitivity_summary": sensitivity_summary,
        "batch": batch_result,
        "product_code": product_code,
        "m0_candidate_records": canonical_records,
        "m0_candidate_record_count": len(canonical_records),
        "available_next_actions": [
            "review_candidates",
            "publish_canonical",
            "run_m1",
        ],
        "next_actions": [
            "review_candidates",
            "publish_canonical",
            "run_m1",
        ],
        "evidence": [{"module": "orchestrator", "source_ref": batch_result["root_path"], "evidence_ref": f"business-catalog:{batch_result['batch_id']}", "detail": "文件哈希、分类和字段观察已写入候选库"}],
    }


def _candidate_sensitivity_summary(db_path: str) -> dict[str, int]:
    """从候选库读取 sensitivity 计数（普通/内部/HR/财务），供 Reviewer 决定 Gate。"""
    import sqlite3

    try:
        with sqlite3.connect(db_path) as db:
            rows = db.execute("SELECT sensitivity_classification, count(*) FROM document_candidates GROUP BY sensitivity_classification").fetchall()
        return {str(kind): int(count) for kind, count in rows}
    except Exception:
        return {}


async def _dispatch_registered_tool(
    skill_name: str,
    payload: dict[str, Any],
    context: dict[str, Any],
    operation_map: dict[str, str],
) -> dict[str, Any]:
    """Dispatch a high-level Skill through the same validated ToolRegistry.

    The registry is injected into ``SkillRegistry`` at construction time and exposed
    through a ContextVar for the duration of the handler call, so it never enters the
    tool context (``tool_context()`` stays a pure request-semantics dict). Module
    handlers therefore cannot see the registry and cannot bypass Tool contracts or
    HTTP adapters. ``context["_tool_registry"]`` is still honoured for callers that
    pass it explicitly (legacy tests). ``tool_payload`` is explicit; remaining fields
    are a convenience for direct Skill calls and are filtered only for Skill control
    fields.
    """
    registry = context.get("_tool_registry") or _TOOL_REGISTRY.get()
    if registry is None:
        raise RuntimeError("skill execution requires a ToolRegistry")
    operation = str(payload.get("operation") or "default")
    tool = str(payload.get("tool") or operation_map.get(operation) or "")
    allowed = set(operation_map.values())
    if tool not in allowed:
        raise ValueError(f"skill operation is not allowed: {skill_name}/{operation}")
    tool_payload = payload.get("tool_payload")
    if not isinstance(tool_payload, dict):
        tool_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"operation", "tool", "tool_payload"}
        }
    tool_context = {key: value for key, value in context.items() if key != "_tool_registry"}
    # R16（父会话裁决）：Skill 派发只取 payload，不经 orchestration_bridge 装配，而
    # ``get_material_readiness_snapshot`` / ``export_m3_procurement_suggestions`` /
    # ``query_m4_material_supply_snapshot`` 的 ``input_schema.required`` 含 ``tenant_id``
    # → 经 Skill 调用直接 ``'tenant_id' is a required property``。此处**只注入该键**
    # （契约不改），边界：
    #   * 调用方已显式给出 tenant_id → 不覆盖（调用方优先）；
    #   * 仅当该工具合同 required 含 tenant_id 时注入（不给 additionalProperties:false
    #     的工具塞未声明字段，避免入参校验失败）；
    #   * ctx 无 tenant_id → 不注入（让合同校验如实报缺参，不伪造租户）。
    if "tenant_id" not in tool_payload:
        spec = getattr(registry, "specs", {}).get(tool)
        required = (getattr(spec, "input_schema", None) or {}).get("required") or []
        if "tenant_id" in required:
            tenant_id = str(tool_context.get("tenant_id") or "").strip()
            if tenant_id:
                tool_payload = {**tool_payload, "tenant_id": tenant_id}
    result = await registry.call(tool, tool_payload, tool_context)
    if isinstance(result, dict):
        output = dict(result)
    else:
        output = {"result": result}
    output.update({"skill": skill_name, "skill_operation": operation, "invoked_tool": tool, "invoked_tools": [tool]})
    return output


#: M2 Skill operation → tool 映射（单一来源：handler 与 SkillSpec.tools 同源，
#: 修 REQUESTS-R3 §2.2「白名单与 operation map 脱钩」）。
#: ``onboard``/``runs`` 两个 operation 原先缺失 → ``onboard_m2_bom_template`` /
#: ``list_m2_runs`` 经 Skill 不可达（rows-S3.md 备注）。
M2_SKILL_OPERATION_MAP: dict[str, str] = {
    "default": "run_bom_sop_workflow",
    "generate": "run_bom_sop_workflow",
    "history": "search_m2_bom_history",
    "bom": "generate_m2_bom_controlled",
    "sop": "generate_m2_sop",
    "run": "get_m2_run",
    "onboard": "onboard_m2_bom_template",
    "runs": "list_m2_runs",
}


async def m0_governance(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return await _dispatch_registered_tool(
        "yunpai-m0-data-foundation", payload, context,
        {"default": "data_import_run", "ingest": "data_import_run", "preview": "data_import_preview", "resolve": "data_import_resolve", "commit": "data_import_commit"},
    )


async def m1_document_intelligence(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return await _dispatch_registered_tool(
        "yunpai-m1-document-parser", payload, context,
        M1_SKILL_OPERATION_MAP,
    )


async def m2_engineering_control(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return await _dispatch_registered_tool(
        "yunpai-m2-bom-sop", payload, context,
        M2_SKILL_OPERATION_MAP,
    )


async def m3_material_planning(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return await _dispatch_registered_tool(
        "yunpai-m3-material-planning", payload, context,
        M3_SKILL_OPERATION_MAP,
    )


async def m4_procurement_control(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return await _dispatch_registered_tool(
        "yunpai-m4-procurement", payload, context,
        M4_SKILL_OPERATION_MAP,
    )


async def m5_pmc_control(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return await _dispatch_registered_tool(
        "yunpai-m5-pmc", payload, context,
        {
            "default": "solve_scheduling",
            "solve": "solve_scheduling",
            "schedule": "get_m5_schedule",
            "progress": "get_m5_pmc_progress",
            "contracts": "get_m5_integration_contracts",
            "readiness": "get_m5_material_readiness",
            "knowledge_search": "search_m5_knowledge",
            "knowledge_record": "record_m5_knowledge",
            "message_prepare": "prepare_m5_department_message",
            "message_get": "get_m5_department_message",
            "message_delivery": "get_m5_department_message_delivery",
            "advise": "advise_m5_schedule",
            "intelligent": "run_m5_intelligent_schedule",
            "procurement": "generate_m5_material_procurement_plan",
        },
    )


async def m5_lifecycle_control(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return await _dispatch_registered_tool(
        "yunpai-m5-pmc-lifecycle", payload, context,
        {
            "default": "get_m5_schedule",
            "snapshot": "ingest_m5_planning_snapshot",
            "ingest": "ingest_m5_planning_snapshot",
            "schedule": "get_m5_schedule",
            "versions": "list_m5_schedules",
            "progress": "get_m5_pmc_progress",
            "replan": "replan_m5_schedule",
            "dispatch": "dispatch_m5_schedule",
            "execution": "get_m5_execution_summary",
        },
    )


async def m6_finance_control(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """M6「算」：成本/利润审计、报价与对账预览、工资、效益分摊、库存财务视图。

    全部是**纯算数只读**（无副作用、无门、不落库）：输入事实由装配层给，缺数一律标
    ``cost_incomplete``/``missing``，绝不编造。落库/门在 ``yunpai-m6-ledger`` 侧。
    """
    return await _dispatch_registered_tool(
        "yunpai-m6-finance", payload, context, M6_FINANCE_SKILL_OPERATION_MAP,
    )


async def m6_ledger_control(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """M6「记」：成本快照/明细/月结/报价单台账/对账单/送货单/资产台账。

    写操作照 **三段式**（D-005）：propose 段只写草稿（``status=trial``）或只回报待确认
    事实，生效落在 ``finance`` 门批准之后的 ``graph._apply_m6_*`` commit 段。本 Skill
    **不写 M0/M5**，只写自己的 m6 库（红线由 operation map 白名单在代码级兜住）。
    """
    return await _dispatch_registered_tool(
        "yunpai-m6-ledger", payload, context, M6_LEDGER_SKILL_OPERATION_MAP,
    )


def build_default_skill_registry(tool_registry: Any | None = None) -> SkillRegistry:
    """构建默认 Skill 面；``tool_registry`` 为 Skill 内部派发工具所用（见 §裁决 1）。

    ``default_deps()`` 传入已构建的 ToolRegistry；不传时行为与旧版一致（Skill 内部
    派发会抛 ``RuntimeError: skill execution requires a ToolRegistry``）。
    """
    registry = SkillRegistry(tool_registry)
    registry.register(SkillSpec(
        name="business-data-identification",
        description="识别云湃业务资料，抽取订单/BOM/工程文档字段，保留文件哈希和字段级证据，并写入可审核候选库。",
        handler=identify_business_data,
        tags=("upload", "m0", "m1", "evidence", "catalog"),
    ))
    registry.register(SkillSpec(
        name="yunpai-m0-data-foundation",
        description="治理 M0 导入、候选审核、canonical 发布、版本和证据；缺事实时停在可恢复 Gate。",
        handler=m0_governance,
        tags=("m0", "canonical", "evidence", "governance"),
        tools=("data_import_run", "data_import_preview", "data_import_resolve", "data_import_commit"),
    ))
    registry.register(SkillSpec(
        name="yunpai-m1-document-parser",
        description="解析订单、图纸、表格、CAD 与归档并保留字段级证据；支持任务/批次轮询、订单与文档检索、导出、人工审核队列与提交、报告和 Governed Wiki 知识查询。低置信度结果送人工复核，不拥有 M0 canonical 事实。",
        handler=m1_document_intelligence,
        tags=("m1", "ocr", "document", "review", "knowledge", "archive"),
        tools=m1_unique_tools(M1_SKILL_OPERATION_MAP),
    ))
    registry.register(SkillSpec(
        name="yunpai-m2-bom-sop",
        description="生成和审查版本化 BOM/SOP 草稿，连接历史检索、工程审核和工艺制品查询。",
        handler=m2_engineering_control,
        tags=("m2", "bom", "sop", "engineering"),
        tools=unique_tools(M2_SKILL_OPERATION_MAP),
    ))
    registry.register(SkillSpec(
        name="yunpai-m3-material-planning",
        description="计算 MRP、物料缺口和齐套快照，输出可审计的 M3→M4 采购需求，不预占库存。",
        handler=m3_material_planning,
        tags=("m3", "mrp", "material", "readiness"),
        tools=unique_tools(M3_SKILL_OPERATION_MAP),
    ))
    registry.register(SkillSpec(
        name="yunpai-m4-procurement",
        description="管理采购建议、采购单审核、供应商回复、供应快照、ETA 跟踪和预警；副作用仍需人工授权。",
        handler=m4_procurement_control,
        tags=("m4", "procurement", "supplier", "tracking"),
        tools=unique_tools(M4_SKILL_OPERATION_MAP),
    ))
    registry.register(SkillSpec(
        name="yunpai-m5-pmc",
        description="统一 M5 PMC 求解、WIP/资源检查、计划版本、重排、派工、报工和执行摘要；生产发布必须通过 Gate。",
        handler=m5_pmc_control,
        tags=("m5", "pmc", "wip", "schedule", "execution"),
        tools=("solve_scheduling", "get_m5_schedule", "get_m5_pmc_progress",
               "get_m5_integration_contracts", "get_m5_material_readiness",
               "search_m5_knowledge", "record_m5_knowledge",
               "prepare_m5_department_message", "get_m5_department_message",
               "get_m5_department_message_delivery",
               "advise_m5_schedule", "run_m5_intelligent_schedule",
               "generate_m5_material_procurement_plan"),
    ))
    registry.register(SkillSpec(
        name="yunpai-m5-pmc-lifecycle",
        description="面向 M5 Flow Board 的版本历史、重排、派工和执行回传操作；只调用已注册 M5 Tool，不伪造生产状态。",
        handler=m5_lifecycle_control,
        tags=("m5", "lifecycle", "flow-board", "dispatch", "execution"),
        tools=("ingest_m5_planning_snapshot", "get_m5_schedule", "list_m5_schedules",
               "replan_m5_schedule", "get_m5_pmc_progress", "dispatch_m5_schedule",
               "get_m5_execution_summary"),
    ))
    registry.register(SkillSpec(
        name="yunpai-m6-finance",
        description="M6 算：产品/订单成本核算、利润审计、报价与对账预览、计件/月薪工资、模具机器效益分摊、库存财务视图。纯确定性计算，缺数据标 cost_incomplete，不编造、不落库、不写 M0/M5。",
        handler=m6_finance_control,
        tags=("m6", "finance", "cost", "quotation", "statement", "payroll", "inventory"),
        tools=unique_tools(M6_FINANCE_SKILL_OPERATION_MAP),
    ))
    registry.register(SkillSpec(
        name="yunpai-m6-ledger",
        description="M6 记：成本快照与明细、月结汇总、报价单/对账单台账、送货单凭据与资产台账。写操作照三段式（先批准后落库），只写自己的 m6 库，不写 M0/M5。",
        handler=m6_ledger_control,
        tags=("m6", "finance", "ledger", "costing", "snapshot", "document", "asset"),
        tools=unique_tools(M6_LEDGER_SKILL_OPERATION_MAP),
    ))
    return registry
