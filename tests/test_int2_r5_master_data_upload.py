"""INT2 第五轮（G1–G4）：基础资料（BOM/SOP）上传 → canonical 发布接线回归。

对应 `_migration/exec/PROMPT-INT2-R5.md`：
- G1 路由：附件**全部** ``kind=master_data`` 且无订单附件 → 确定性回退到
  ``business-data-identification``（否则被规则 1 吃成 ``m1_m5_document_to_plan``）；
- G2 技能载荷：顶层 ``message``/``product_code``/``product_name``/``attachments``/
  ``documents`` 仅在缺键时并入技能载荷（显式值优先；``documents``→``files`` 别名）；
- G3 候选门 + 批准后发布：``m0_candidate_records`` 非空 → candidate 门；批准即
  ``CatalogService.publish_records`` 发布 canonical，计数写回 + trace 留痕；
- G4 缺产品编码 fail-closed：``status=needs_product_code`` + ``missing_fields``
  → blocked_input 门（补 product_code 重试），绝不发布空批次。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from io import BytesIO

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from yunpai_orchestrator.checkpointer import memory_checkpointer
from yunpai_orchestrator.config import OrchestratorConfig
from yunpai_orchestrator.graph import (
    GraphDeps,
    _publish_business_canonical,
    build_graph,
    default_deps,
)
from yunpai_orchestrator.llm import QwenConfig, QwenRouter
from yunpai_orchestrator.m0_backend import M0Store
from yunpai_orchestrator.orchestrator.router import Router
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.reviewer import rules
from yunpai_orchestrator.reviewer.gates import make_gate
from yunpai_orchestrator.skills import build_default_skill_registry, identify_business_data
from yunpai_orchestrator.state import new_state_v2
from yunpai_orchestrator.worker.executor import skill_payload

SKILL = "business-data-identification"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

def _bom_xlsx_bytes() -> bytes:
    """最小合法 BOM 工作簿（分类为 bom，含 1 行可量化物料）。"""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "中性系列"
    sheet.append(["物料编码", "材料名称", "用量", "单位"])
    sheet.append(["YA.G.10.002", "护套", 2, "PCS"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _master_data_attachment(filename: str = "中性系列-成品BOM表.xlsx") -> dict:
    return {
        "kind": "master_data",
        "filename": filename,
        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "content_b64": base64.b64encode(_bom_xlsx_bytes()).decode("ascii"),
    }


def _record(entity_type: str = "bom", business_key: str = "W-H913",
            version_id: str = "upload") -> dict:
    digest = hashlib.sha256(b"bom-W-H913").hexdigest()
    return {
        "schema_version": "m0.ingest.v1",
        "tenant_id": "default",
        "idempotency_key": f"upload:batch-r5:{entity_type}:{business_key}:{version_id}",
        "source": {"system": "yunpai-business-upload",
                   "external_id": f"bom-W-H913.xlsx::{entity_type}::{business_key}::{version_id}",
                   "sha256": digest},
        "identity": {"business_key": business_key, "version_id": version_id},
        "evidence": [{"key": "source",
                      "locator": {"filename": "bom-W-H913.xlsx", "sha256": digest},
                      "excerpt": "bom-W-H913.xlsx"}],
        "review_status": "approved",
        "reviewed_by": "operator",
        "entity_type": entity_type,
        "payload": {"product_code": business_key, "status": "active",
                    "lines": [{"line_no": "1", "material_code": "YA.G.10.002",
                               "material_name": "护套", "quantity": 2, "uom": "PCS"}]},
    }


def _candidate_gate(tool: str = SKILL) -> dict:
    return make_gate("candidate", tool, "业务资料候选须人工审核后发布 canonical")


def _skill_state(records: list[dict] | None = None, *, tool: str = SKILL,
                 tenant_id: str = "default") -> dict:
    return {
        "task_id": "task-r5", "tenant_id": tenant_id, "trace": [],
        "outputs": {tool: {"skill": SKILL, "status": "candidate_created",
                           "m0_candidate_records": records if records is not None else [_record()]}},
    }


def _deps(repository=None) -> GraphDeps:
    cfg = OrchestratorConfig()
    reg = build_default_registry()
    skills = build_default_skill_registry()
    base = default_deps(cfg, registry=reg, skills=skills)
    return GraphDeps(
        registry=reg, skills=skills,
        router=Router(QwenRouter(QwenConfig(enabled=False)), reg, skills, base.engine),
        planner=base.planner, engine=base.engine, assembler=base.assembler,
        config=cfg, repository=repository,
    )


# ---------------------------------------------------------------------------
# G1 · 路由：全 master_data 附件 → 业务资料识别技能
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_g1_all_master_data_documents_route_to_identification_skill():
    router = Router(QwenRouter(QwenConfig(enabled=False)), build_default_registry(),
                    build_default_skill_registry())
    state = new_state_v2({
        "message": "识别并落库 W-H913 的基础资料（BOM/SOP）",
        "documents": [_master_data_attachment("中性系列-成品BOM表.xlsx"),
                      _master_data_attachment("HDTV 作业指导书.xls")],
    })

    decision = await router.decide(state)

    assert decision.route == "free"
    assert decision.source == "deterministic_fallback"
    assert decision.skills == [SKILL]
    assert decision.tools == []
    assert decision.workflow_id is None
    assert "master_data" in decision.reason


@pytest.mark.asyncio
async def test_g1_order_attachment_keeps_document_workflow_unchanged():
    """含订单附件的既有行为不得改变（继续走 m1_m5_document_to_plan）。"""
    router = Router(QwenRouter(QwenConfig(enabled=False)), build_default_registry(),
                    build_default_skill_registry())
    order = {"kind": "order", "filename": "order-w913.xlsx", "content_b64": "QUJD"}
    state = new_state_v2({"message": "处理订单", "documents": [_master_data_attachment(), order]})

    decision = await router.decide(state)

    assert (decision.route, decision.workflow_id, decision.source) == (
        "workflow", "m1_m5_document_to_plan", "deterministic_fallback")


@pytest.mark.asyncio
async def test_g1_rule_reads_request_documents_when_state_attachments_missing():
    """直接调 ``_fallback``（无 state.attachments）时也要认顶层 ``documents``。"""
    router = Router(QwenRouter(QwenConfig(enabled=False)), build_default_registry(),
                    build_default_skill_registry())
    decision = router._fallback({"request": {"message": "落库基础资料",
                                             "documents": [_master_data_attachment()]}})

    assert (decision.route, decision.skills) == ("free", [SKILL])


@pytest.mark.asyncio
async def test_g1_attachment_without_explicit_kind_not_diverted_to_skill():
    """未声明 kind 的附件不触发规则 0（判定只认显式 kind，不猜文件名）。"""
    router = Router(QwenRouter(QwenConfig(enabled=False)), build_default_registry(),
                    build_default_skill_registry())
    decision = router._fallback({"request": {"message": "x"},
                                 "attachments": [{"filename": "设备台账.xlsx"}]})

    assert decision.route == "chat"
    assert decision.skills == []


class _FakeLLM:
    """不走网络的 LLM 桩：classify 固定返回给定决策（test_router.py 同款）。"""

    def __init__(self, decision: dict):
        self._decision = decision

    async def classify(self, request, registry, skills=None):
        return {"ok": True, "status": "ok", "decision": self._decision}


@pytest.mark.asyncio
async def test_g1_llm_workflow_proposal_vetoed_for_master_data_only_request():
    """G1 深根因①：LLM 同时提案 workflow+skills 时，全 master_data 请求必须落到技能。

    实测（父会话）：prompt 曾同时命中「上传原始业务文件→workflow」与「上传基础资料
    →skills」两句，LLM 的 raw 同时给出 ``workflow_id=m1_m5_document_to_plan`` 与
    ``skills=["business-data-identification"]``；``_validate`` 又丢掉 skills →
    基础资料永远进订单链。这里锁定：矛盾提案不让生效，确定性规则 0 接管。
    """
    router = Router(_FakeLLM({"intent": "document_to_plan", "route": "workflow",
                              "workflow_id": "m1_m5_document_to_plan", "tools": [],
                              "skills": [SKILL], "confidence": 0.95, "reason": "上传资料"}),
                    build_default_registry(), build_default_skill_registry())
    state = new_state_v2({"message": "识别并落库 W-H913 的基础资料（BOM/SOP）",
                          "documents": [_master_data_attachment()]})

    decision = await router.decide(state)

    assert decision.route == "free"
    assert decision.skills == [SKILL]
    assert decision.workflow_id is None
    assert decision.source == "deterministic_fallback"


@pytest.mark.asyncio
async def test_g1_llm_workflow_proposal_with_order_attachment_still_wins():
    """含订单附件时 LLM 的 workflow 提案照常生效（矛盾判据不误伤订单链）。"""
    router = Router(_FakeLLM({"intent": "document_to_plan", "route": "workflow",
                              "workflow_id": "m1_m5_document_to_plan", "tools": [],
                              "skills": [], "confidence": 0.9, "reason": "订单全链"}),
                    build_default_registry(), build_default_skill_registry())
    state = new_state_v2({"message": "订单文件做全链处理",
                          "documents": [{"kind": "order", "filename": "order-w913.xlsx",
                                         "content_b64": "QUJD"},
                                        _master_data_attachment()]})

    decision = await router.decide(state)

    assert (decision.route, decision.source, decision.workflow_id) == (
        "workflow", "llm", "m1_m5_document_to_plan")


@pytest.mark.asyncio
async def test_g1_validate_keeps_llm_skills_on_workflow_route_with_reason():
    """G1 深根因②：``route=workflow`` 不再静默丢弃 LLM 提案的 skills（留痕+说明）。"""
    router = Router(_FakeLLM({"route": "workflow", "workflow_id": "m1_m5_document_to_plan",
                              "tools": [], "skills": [SKILL], "confidence": 0.9,
                              "reason": "订单全链"}),
                    build_default_registry(), build_default_skill_registry())
    # 带 order 附件 → 矛盾判据不触发，走 LLM 提案分支
    state = new_state_v2({"message": "订单全链",
                          "documents": [{"kind": "order", "filename": "order.xlsx",
                                         "content_b64": "QUJD"}]})

    decision = await router.decide(state)

    assert decision.route == "workflow"
    assert decision.skills == [SKILL]           # 不再静默丢失
    assert "技能不派发" in decision.reason      # 明确说明只留痕不派发


def test_g1_system_prompt_master_data_and_order_judgements_are_exclusive():
    """prompt 两条判据互斥：不再出现「BOM/SOP 同时命中 workflow 与 skills」。"""
    prompt = QwenRouter._system_prompt()

    assert 'kind="order"' in prompt
    assert 'kind="master_data"' in prompt
    assert "不含 order 附件" in prompt
    assert "两类同时出现时以订单主链为准" in prompt
    assert "business-data-identification" in prompt


# ---------------------------------------------------------------------------
# G2 · 技能载荷：顶层键并入（显式优先）
# ---------------------------------------------------------------------------

def test_g2_skill_payload_merges_top_level_message_and_documents():
    state = {
        "request": {
            "message": "识别并落库 W-H913 的基础资料（BOM/SOP）",
            "product_code": "W-H913", "product_name": "中性系列",
            "documents": [_master_data_attachment()],
            SKILL: {"mode": "master_data"},
        },
    }

    payload = skill_payload(state, SKILL)

    assert payload["mode"] == "master_data"
    assert payload["product_code"] == "W-H913"
    assert payload["product_name"] == "中性系列"
    assert payload["message"].startswith("识别并落库 W-H913")
    assert payload["documents"] == state["request"]["documents"]
    # documents → files 别名（legacy `_payload_for` 同口径，技能读 files）
    assert payload["files"] == state["request"]["documents"]


def test_g2_skill_payload_explicit_values_win_and_files_not_overwritten():
    explicit_files = [{"filename": "explicit.json", "content_b64": "e30="}]
    state = {
        "request": {
            "message": "顶层文案", "product_code": "TOP-CODE",
            "documents": [_master_data_attachment()],
            SKILL: {"product_code": "EXPLICIT-CODE", "files": explicit_files},
        },
    }

    payload = skill_payload(state, SKILL)

    assert payload["product_code"] == "EXPLICIT-CODE"  # 显式值不被覆盖
    assert payload["files"] == explicit_files
    assert payload["message"] == "顶层文案"


def test_g2_skill_payload_does_not_mutate_request():
    attachment = _master_data_attachment()
    request = {"message": "文案", "documents": [attachment]}
    state = {"request": request}

    payload = skill_payload(state, SKILL)
    payload["message"] = "changed"

    assert request == {"message": "文案", "documents": [attachment]}
    assert SKILL not in request


# ---------------------------------------------------------------------------
# G3 · 候选门 + 批准后发布 canonical
# ---------------------------------------------------------------------------

def test_g3_rules_open_candidate_gate_for_candidate_records():
    result = {"status": "candidate_created", "m0_candidate_records": [_record()],
              "m0_candidate_record_count": 1}

    findings = rules.evaluate(SKILL, result, None)
    gate = next((f for f in findings if f.get("gate")), None)

    assert gate is not None, findings
    assert gate["gate"] == "candidate"
    assert "canonical" in gate["reason"]


def test_g3_gate_type_for_business_data_is_candidate():
    assert rules.gate_type_for(SKILL, None) == "candidate"


def test_g3_publish_business_canonical_publishes_and_reads_back(tmp_path, monkeypatch):
    db = tmp_path / "m0.sqlite"
    monkeypatch.setenv("YUNPAI_M0_DB", str(db))

    updates = _publish_business_canonical(_skill_state(), _candidate_gate(), actor="steward")

    assert updates is not None
    envelope = updates["outputs"][SKILL]
    publication = envelope["m0_catalog_publish"]
    assert publication["status"] == "published"
    assert publication["published"] == 1
    assert envelope["review_applied"]["actor"] == "steward"
    assert envelope["review_applied"]["published"] == 1
    # trace 留痕：发布计数（不是「批准即完成」的静默成功）
    assert any(entry.get("event") == "m0.canonical_publish"
               and entry.get("published") == 1 for entry in updates["trace"])
    # canonical 真落库（唯一读口 M0Store.list_entities）
    entities = M0Store(str(db)).list_entities("bom", tenant_id="default")
    assert entities["count"] == 1
    assert entities["entities"][0]["canonical_key"] == "W-H913"


def test_g3_publish_skipped_without_m0_db_never_fakes_counts(monkeypatch):
    monkeypatch.delenv("YUNPAI_M0_DB", raising=False)

    updates = _publish_business_canonical(_skill_state(), _candidate_gate(), actor="steward")

    publication = updates["outputs"][SKILL]["m0_catalog_publish"]
    assert publication == {"status": "skipped", "published": 0,
                           "reason": "未配置 YUNPAI_M0_DB；模块 HTTP canonical 发布已废弃"}


def test_g3_publish_failure_returns_conflict_without_fake_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0.sqlite"))
    broken = _record()
    broken.pop("evidence")  # evidence 至少 1 条 → 校验失败

    updates = _publish_business_canonical(_skill_state([broken]), _candidate_gate(),
                                          actor="steward")

    publication = updates["outputs"][SKILL]["m0_catalog_publish"]
    assert publication["status"] == "failed"
    assert publication["published"] == 0
    assert updates["publish_conflict"]["code"] == "M0_PUBLISH_FAILED"
    assert not (tmp_path / "m0.sqlite").exists() or M0Store(
        str(tmp_path / "m0.sqlite")).list_entities("bom", tenant_id="default")["count"] == 0


def test_g3_r2_read_entities_merges_business_body_attributes(tmp_path, monkeypatch):
    """R2 归一化补第 2 层：m0.ingest.v1 业务体 ``payload.attributes`` 的键并入业务体顶层。

    实测缺口（第五轮验收步骤 4）：SOP 的 ``route_steps`` 落在
    ``record["payload"]["attributes"]``（BOM 的 ``lines`` 在业务体顶层），只归一化
    信封层会让 M2 ``_route_steps_from_entities`` 读到 0 道工序 → 工程事实不完整。
    """
    from yunpai_orchestrator.fact_gateway import read_entities
    from yunpai_orchestrator.m0_catalog_ingest import CatalogService
    from yunpai_orchestrator.orchestration_bridge import _route_steps_from_entities

    db = tmp_path / "m0.sqlite"
    monkeypatch.setenv("YUNPAI_M0_DB", str(db))
    record = _record("document", "W-H913-sop", "SOP-TX001-A1")
    record["payload"] = {
        "role": "sop", "product_codes": ["W-H913"], "status": "active",
        # 顶层优先：attributes 里的同名键不得覆盖业务体已有值
        "attributes": {"route_steps": [{"sequence_no": 1, "operation_code": "OP-1",
                                        "operation_name": "脱皮"}],
                       "status": "should-not-win", "source_batch": "batch-r5"},
    }
    publication = CatalogService(str(db)).publish_records(
        [record], tenant_id="default", task_id="task-r5", actor="steward")
    assert publication["published"] == 1

    items = read_entities("document", "default", str(db))
    body = items[0]["payload"]
    assert body["route_steps"][0]["operation_code"] == "OP-1"
    assert body["source_batch"] == "batch-r5"
    assert body["status"] == "active"          # 顶层优先

    state = {"tenant_id": "default", "request": {}, "outputs": {}}
    steps = _route_steps_from_entities(state, "W-H913")
    assert [step["operation_code"] for step in steps] == ["OP-1"]


def test_g3_publish_ignores_other_tools_and_empty_records(tmp_path, monkeypatch):
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0.sqlite"))

    assert _publish_business_canonical(
        _skill_state(tool="data_import_run"),
        _candidate_gate("data_import_run"), actor="steward") is None
    assert _publish_business_canonical(
        _skill_state([]), _candidate_gate(), actor="steward") is None
    assert _publish_business_canonical(
        {"outputs": {}}, _candidate_gate(), actor="steward") is None
    assert _publish_business_canonical(
        _skill_state(), make_gate("engineering", SKILL, "x"), actor="steward") is None


def test_g3_graph_end_to_end_master_data_upload_publishes_canonical(tmp_path, monkeypatch):
    """端到端：全 master_data 附件 → G1 路由 → G2 载荷 → 候选门 → 批准发布 canonical。

    这是「先上传基础资料再上传订单」的第一步在 V2 真正可用的回归：候选门批准后
    canonical 必须真实落库（``bom W-H913``），供 M2 读取。
    """
    db = tmp_path / "m0.sqlite"
    monkeypatch.setenv("YUNPAI_M0_DB", str(db))
    monkeypatch.delenv("M0_URL", raising=False)

    graph = build_graph(_deps(), checkpointer=MemorySaver())
    state = new_state_v2({
        "message": "识别并落库 W-H913 的基础资料（BOM/SOP）",
        "documents": [_master_data_attachment()],
        SKILL: {"db_path": str(tmp_path / "catalog.sqlite"),
                "staging_dir": str(tmp_path / "staging")},
    })
    config = {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 48}

    out = asyncio.run(graph.ainvoke(state, config))

    # G1：确定性回退到技能（不是 m1_m5_document_to_plan）
    assert out["route"] == "free"
    assert out["route_decision"]["skills"] == [SKILL]
    # G2 + 技能结果：产品编码从顶层 message 正则取出，候选非空
    envelope = out["outputs"][SKILL]
    assert envelope["status"] == "candidate_created"
    assert envelope["product_code"] == "W-H913"
    assert envelope["m0_candidate_record_count"] > 0
    # G3：候选门挂起（不是静默 completed）
    gate = getattr(out["__interrupt__"][0], "value", out["__interrupt__"][0])["gate"]
    assert gate["type"] == "candidate" and gate["tool"] == SKILL
    assert M0Store(str(db)).list_entities("bom", tenant_id="default")["count"] == 0

    resumed = asyncio.run(graph.ainvoke(
        Command(resume={"decision": "approve", "actor": "steward",
                        "roles": ["m0-reviewer"]}), config))

    assert resumed["status"] == "completed"
    publication = resumed["outputs"][SKILL]["m0_catalog_publish"]
    assert publication["status"] == "published"
    assert publication["published"] > 0
    entities = M0Store(str(db)).list_entities("bom", tenant_id="default")
    assert [item["canonical_key"] for item in entities["entities"]] == ["W-H913"]
    assert any(entry.get("event") == "m0.canonical_publish"
               for entry in resumed.get("trace") or [])


def test_g3_graph_candidate_gate_requires_data_steward_role(tmp_path, monkeypatch):
    """candidate 门不得被普通操作员/LLM 置信度绕过（角色矩阵）。"""
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0.sqlite"))
    graph = build_graph(_deps(), checkpointer=memory_checkpointer())
    state = new_state_v2({
        "message": "识别并落库 W-H913 的基础资料",
        "documents": [_master_data_attachment()],
        SKILL: {"db_path": str(tmp_path / "catalog.sqlite"),
                "staging_dir": str(tmp_path / "staging")},
    })
    config = {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 48}
    asyncio.run(graph.ainvoke(state, config))

    out = asyncio.run(graph.ainvoke(
        Command(resume={"decision": "approve", "actor": "who", "roles": ["operator"]}), config))

    assert "__interrupt__" in out, "角色不足应再次挂起而非放行"
    value = getattr(out["__interrupt__"][0], "value", out["__interrupt__"][0])
    assert value.get("type") == "gate_invalid"
    assert "m0-reviewer" in str(value.get("error"))


# ---------------------------------------------------------------------------
# G4 · 缺产品编码 fail-closed 可诊断
# ---------------------------------------------------------------------------

def _skill_payload_for(tmp_path, **extra) -> dict:
    payload = {
        "mode": "master_data",
        "files": [_master_data_attachment()],
        "staging_dir": str(tmp_path / "staging"),
        "db_path": str(tmp_path / "catalog.sqlite"),
    }
    payload.update(extra)
    return payload


@pytest.mark.asyncio
async def test_g4_missing_product_code_returns_diagnosable_result(tmp_path):
    result = await identify_business_data(_skill_payload_for(tmp_path),
                                          {"task_id": "TASK-G4", "tenant_id": "default"})

    assert result["status"] == "needs_product_code"
    assert result["code"] == "NEEDS_PRODUCT_CODE"
    assert result["missing_fields"] == ["product_code"]
    assert "W-H913" in result["message"]
    assert result["m0_candidate_records"] == []
    assert result["m0_candidate_record_count"] == 0
    assert result["product_code"] == ""
    assert result["upload_summary"]["accepted"] == 1


def test_g4_missing_product_code_opens_blocked_input_gate():
    result = {"status": "needs_product_code", "code": "NEEDS_PRODUCT_CODE",
              "message": "缺少产品编码：请提供产品编码（如 W-H913）后重试",
              "missing_fields": ["product_code"], "m0_candidate_records": []}

    findings = rules.evaluate(SKILL, result, None)
    gate = next((f for f in findings if f.get("gate")), None)

    assert gate is not None, findings
    assert gate["gate"] == "blocked_input"
    assert gate["missing_fields"] == ["product_code"]
    assert "W-H913" in gate["message"]
    assert gate["code"] == "NEEDS_PRODUCT_CODE"


@pytest.mark.asyncio
async def test_g4_explicit_product_code_still_creates_canonical_candidates(tmp_path):
    result = await identify_business_data(
        _skill_payload_for(tmp_path, product_code="W-H913"),
        {"task_id": "TASK-G4-OK", "tenant_id": "default"})

    assert result["status"] == "candidate_created"
    assert result["product_code"] == "W-H913"
    assert result["m0_candidate_record_count"] > 0
    assert {item["entity_type"] for item in result["m0_candidate_records"]} >= {"bom"}
