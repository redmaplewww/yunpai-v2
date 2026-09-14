from __future__ import annotations

import json

import pytest

from yunpai_orchestrator.agents import PlannerAgent
from yunpai_orchestrator.graph import YunpaiGraph
from yunpai_orchestrator.llm import QwenConfig, QwenRouter
from yunpai_orchestrator.models import new_state
from yunpai_orchestrator.m3_m4_tooling import M3_ADAPTER_TOOL_NAMES, M4_ADAPTER_TOOL_NAMES
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.skills import build_default_skill_registry


def test_planner_selects_business_data_skill_for_upload_intent():
    planner = PlannerAgent(QwenRouter(QwenConfig(enabled=False)), build_default_skill_registry())
    decision = planner.plan({"message": "识别并落库业务资料", "documents": []}, build_default_registry())
    assert decision["route"] == "free"
    assert decision["steps"][0]["tool"] == "business-data-identification"
    assert decision["steps"][0]["kind"] == "skill"


def test_master_data_attachment_does_not_force_identification_skill_without_intent():
    planner = PlannerAgent(QwenRouter(QwenConfig(enabled=False)), build_default_skill_registry())
    decision = planner.plan({
        "message": "请看看这些文件",
        "attachments": [{"kind": "master_data", "filename": "设备台账.xlsx", "content_b64": "AA=="}],
    }, build_default_registry())
    assert decision["route"] in {"chat", "free"}
    assert all(step["tool"] != "business-data-identification" for step in decision["steps"])


def test_unknown_workflow_is_structured_and_does_not_downgrade():
    planner = PlannerAgent(QwenRouter(QwenConfig(enabled=False)), build_default_skill_registry())
    decision = planner.plan({"workflow": "not_registered", "message": "执行流程"}, build_default_registry())
    assert decision["error"]["code"] == "UNKNOWN_WORKFLOW"
    assert decision["route"] == "chat"
    assert decision["steps"] == []


def test_order_attachment_does_not_bind_identification_skill_by_kind():
    planner = PlannerAgent(QwenRouter(QwenConfig(enabled=False)), build_default_skill_registry())
    decision = planner.plan({
        "message": "根据订单附件执行订单到排程",
        "attachments": [{"kind": "order", "filename": "order.xlsx", "content_b64": "AA=="}],
        "workflow": "m1_m5_document_to_plan",
    }, build_default_registry())
    assert decision["route"] == "workflow"
    assert decision["steps"][0]["tool"] == "ingest_document"


def test_skill_catalog_exposes_versioned_skill_ids():
    catalog = {item["name"]: item for item in build_default_skill_registry().catalog()}
    expected = {
        "business-data-identification",
        "yunpai-m0-data-foundation",
        "yunpai-m1-document-parser",
        "yunpai-m2-bom-sop",
        "yunpai-m3-material-planning",
        "yunpai-m4-procurement",
        "yunpai-m5-pmc",
        "yunpai-m5-pmc-lifecycle",
        "yunpai-m6-finance",
        "yunpai-m6-ledger",
    }
    assert set(catalog) == expected
    assert "solve_scheduling" in catalog["yunpai-m5-pmc"]["tools"]
    assert "dispatch_m5_schedule" in catalog["yunpai-m5-pmc-lifecycle"]["tools"]
    assert set(M3_ADAPTER_TOOL_NAMES).issubset(catalog["yunpai-m3-material-planning"]["tools"])
    assert set(catalog["yunpai-m4-procurement"]["tools"]) == set(M4_ADAPTER_TOOL_NAMES)
    assert all(item["description"] for item in catalog.values())
    assert all(item["version"] for item in catalog.values())
    assert all(item["skill_id"] == f"{item['name']}@{item['version']}" for item in catalog.values())


def test_skill_execution_records_versioned_evidence_and_contract():
    from yunpai_orchestrator.contracts import ToolSpec
    from yunpai_orchestrator.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(ToolSpec("get_m5_schedule", "m5", "查询排程", {"type": "object"}, {"type": "object"}), None)
    result = build_default_skill_registry().specs["yunpai-m5-pmc-lifecycle"]
    assert result.version >= "1.0.0"
    assert result.contract_version == "yunpai.skill-contract.v1"


def test_planner_routes_explicit_and_semantic_pmc_lifecycle_skill():
    planner = PlannerAgent(QwenRouter(QwenConfig(enabled=False)), build_default_skill_registry())
    registry = build_default_registry()
    explicit = planner.plan({"skill": "yunpai-m5-pmc-lifecycle", "skill_payload": {"operation": "versions"}}, registry)
    semantic = planner.plan({"message": "查看排程版本和生产执行回传"}, registry)
    assert explicit["steps"][0]["kind"] == "skill"
    assert explicit["steps"][0]["tool"] == "yunpai-m5-pmc-lifecycle"
    assert semantic["steps"][0]["tool"] == "yunpai-m5-pmc-lifecycle"


@pytest.mark.asyncio
async def test_skill_dispatches_through_registry_and_preserves_gate_semantics():
    import base64

    graph = YunpaiGraph()
    state = await graph.run(new_state({
        "skill": "yunpai-m0-data-foundation",
        "skill_payload": {
            "operation": "ingest",
            "files": [{"filename": "order.json", "content_b64": base64.b64encode(b'{"records":[{"kind":"order"}]}').decode()}],
        },
    }))
    assert state["pending_gate"]["type"] == "authorization"
    state = await graph.resume(state, "approve", actor="skill-test")
    result = state["outputs"]["yunpai-m0-data-foundation"]
    assert state["pending_gate"]["type"] == "candidate"
    assert result["invoked_tool"] == "data_import_run"
    assert result["skill_operation"] == "ingest"
    assert any(event["event"] == "react.action" and event["tool"] == "yunpai-m0-data-foundation" for event in state["trace"])


@pytest.mark.asyncio
async def test_skill_tool_whitelist_rejects_cross_module_tool():
    graph = YunpaiGraph()
    state = new_state({
        "skill": "yunpai-m5-pmc-lifecycle",
        "skill_payload": {"operation": "versions", "tool": "data_import_commit", "tool_payload": {}},
    })
    state = await graph.run(state)
    assert state["status"] == "failed"
    assert "not allowed" in state["errors"][0]["message"] or "not allowed" in str(state["errors"])


@pytest.mark.asyncio
async def test_skill_operation_maps_to_registered_tool_handler():
    from yunpai_orchestrator.contracts import ToolSpec
    from yunpai_orchestrator.registry import ToolRegistry

    calls = []

    async def fake_schedule(payload, context):
        calls.append((payload, context))
        return {"plan_version": "pv-test"}

    registry = ToolRegistry()
    registry.register(ToolSpec("get_m5_schedule", "m5", "查询排程", {"type": "object"}, {"type": "object"}), fake_schedule)
    skills = build_default_skill_registry()
    result = await skills.call(
        "yunpai-m5-pmc-lifecycle",
        {"operation": "schedule", "tool_payload": {"plan_version": "pv-test"}},
        {"task_id": "TASK-SKILL-MAP", "_tool_registry": registry},
    )
    assert result["invoked_tool"] == "get_m5_schedule"
    assert result["plan_version"] == "pv-test"
    assert calls == [({"plan_version": "pv-test"}, {"task_id": "TASK-SKILL-MAP"})]


@pytest.mark.asyncio
async def test_business_data_skill_runs_after_planner_and_opens_review_gate(tmp_path):
    root = tmp_path / "business"
    root.mkdir()
    (root / "sample.json").write_text(json.dumps({"records": [{"kind": "order"}]}), encoding="utf-8")
    graph = YunpaiGraph()
    graph.planner = PlannerAgent(QwenRouter(QwenConfig(enabled=False)), graph.skills)
    state = await graph.run(new_state({"message": "识别并落库业务资料", "business_data_root": str(root), "business_catalog_db": str(tmp_path / "catalog.sqlite")}))
    assert state["status"] == "waiting_human"
    assert state["plan"][0]["tool"] == "business-data-identification"
    assert state["outputs"]["business-data-identification"]["status"] == "candidate_created"
    assert state["outputs"]["business-data-identification"]["available_next_actions"] == [
        "review_candidates", "publish_canonical", "run_m1",
    ]
    assert state["pending_gate"]["type"] == "candidate"
    assert any(event.get("event") == "agent.intent" for event in state["trace"])
    assert any(event.get("event") == "agent.route" and "business-data-identification" in event.get("selected_tools", []) for event in state["trace"])


@pytest.mark.asyncio
async def test_worker_data_opens_sensitive_data_gate(tmp_path):
    from openpyxl import Workbook

    root = tmp_path / "hr"
    root.mkdir()
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["工号", "姓名", "技能", "班次"])
    sheet.append(["E-01", "张三", "焊接", "白班"])
    workbook.save(root / "员工技能表.xlsx")
    graph = YunpaiGraph()
    graph.planner = PlannerAgent(QwenRouter(QwenConfig(enabled=False)), graph.skills)
    state = await graph.run(new_state({"message": "识别并落库业务资料", "business_data_root": str(root), "business_catalog_db": str(tmp_path / "catalog.sqlite")}))
    assert state["status"] == "waiting_human"
    assert state["pending_gate"]["type"] == "sensitive_data"
    assert "敏感" in state["pending_gate"]["message"]


async def test_m4_read_skill_operation_runs_without_authorization_gate():
    graph = YunpaiGraph()

    async def list_orders(payload, context):
        return {"items": [], "page": 1, "page_size": 20, "total": 0}

    graph.registry.handlers["list_m4_purchase_orders"] = list_orders
    state = await graph.run(new_state({
        "skill": "yunpai-m4-procurement",
        "skill_payload": {"operation": "orders", "tool_payload": {}},
    }))
    assert state["status"] == "completed"
    result = state["outputs"]["yunpai-m4-procurement"]
    assert result["invoked_tool"] == "list_m4_purchase_orders"


@pytest.mark.asyncio
async def test_m4_write_skill_operation_requires_authorization_before_http():
    graph = YunpaiGraph()
    state = await graph.run(new_state({
        "skill": "yunpai-m4-procurement",
        "skill_payload": {
            "operation": "approve",
            "tool_payload": {
                "purchase_order_id": 1,
                "expected_revision": 1,
                "expected_checksum": "a" * 64,
            },
        },
    }))
    assert state["status"] == "waiting_human"
    assert state["pending_gate"]["type"] == "authorization"
    assert state["outputs"] == {}
