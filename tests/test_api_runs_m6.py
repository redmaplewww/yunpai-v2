"""P-017 T7：M6 通过真实 create_app 的 runs/resume API 验收。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yunpai_orchestrator.api.app import create_app
from yunpai_orchestrator.config import OrchestratorConfig, StorageConfig
from yunpai_orchestrator.m6_store import M6Store
from yunpai_orchestrator.repository import SQLiteRunRepository


FACTS = {
    "period": "2026-09", "order_id": "SO-API-001", "product_code": "P1",
    "batch_no": "B1", "quantity": 10,
    "bom_lines": [{"material_code": "M1", "qty_per": 2, "unit_price": 5.0, "loss_rate": 0.1}],
    "routing_steps": [{"operation_id": "OP10", "standard_minutes": 60}],
    "inventory": [{"material_code": "M1", "available_qty": 100, "stock_class": "raw"}],
    "hour_rate": 50, "overhead_rate": 20,
}


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "missing-m0.sqlite"))
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "missing-m4b.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)
    config = OrchestratorConfig(
        storage=StorageConfig(
            runtime_dir=str(tmp_path), run_db=str(tmp_path / "runs.sqlite"),
            evolution_db=str(tmp_path / "evolution.sqlite"),
            identity_db=str(tmp_path / "identity.sqlite"),
            manifest_dir="registry-manifests"),
        evolution_enabled=False,
    )
    repository = SQLiteRunRepository(tmp_path / "runs.sqlite")
    with TestClient(create_app(repository=repository, config=config)) as client:
        yield client, tmp_path


def _start(client: TestClient, tmp_path: Path) -> dict:
    response = client.post("/runs", json={
        "message": "按财务口径确认成本", "tools": [
            "save_costing_snapshot", "confirm_costing_snapshot"],
        "m6_db_path": str(tmp_path / "m6.sqlite"), **FACTS})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pending_gate"]["type"] == "finance"
    assert "finance-officer" in body["pending_gate"]["allowed_roles"]
    return body


def test_api_create_run_exposes_finance_gate(api):
    client, tmp_path = api
    body = _start(client, tmp_path)
    assert body["status"] == "waiting_human"
    assert body["pending_gate"]["tool"] == "confirm_costing_snapshot"


def test_api_resume_approve_persists_confirmed_snapshot(api):
    client, tmp_path = api
    created = _start(client, tmp_path)
    run_id = created["run_id"]
    response = client.post(f"/runs/{run_id}/resume", json={
        "decision": "approve", "actor": "fin-api", "roles": ["finance-officer", "admin"]})
    assert response.status_code == 200, response.text
    state = client.get(f"/runs/{run_id}").json()
    assert state["outputs"]["confirm_costing_snapshot"]["data"]["status"] == "confirmed"
    assert state["outputs"]["confirm_costing_snapshot"]["data"]["committed_by"] == "finance_gate"
    assert state["approvals"][-1]["decision"] == "approve"
    snapshot_id = state["outputs"]["confirm_costing_snapshot"]["data"]["snapshot_id"]
    snapshot = M6Store(str(tmp_path / "m6.sqlite")).get_snapshot(snapshot_id)
    assert snapshot["status"] == "confirmed"


def test_api_resume_reject_keeps_trial_snapshot(api):
    client, tmp_path = api
    created = _start(client, tmp_path)
    run_id = created["run_id"]
    response = client.post(f"/runs/{run_id}/resume", json={
        "decision": "reject", "actor": "fin-api", "roles": ["finance-officer"]})
    assert response.status_code == 200, response.text
    state = client.get(f"/runs/{run_id}").json()
    snapshot_id = state["outputs"]["save_costing_snapshot"]["data"]["snapshot_id"]
    assert M6Store(str(tmp_path / "m6.sqlite")).get_snapshot(snapshot_id)["status"] == "trial"
    assert state["approvals"][-1]["decision"] == "reject"


def test_api_resume_operator_is_rejected_without_bypassing_gate(api):
    client, tmp_path = api
    created = _start(client, tmp_path)
    run_id = created["run_id"]
    response = client.post(f"/runs/{run_id}/resume", json={
        "decision": "approve", "actor": "operator", "roles": ["operator"]})
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["status"] == "waiting_human"
    assert state["pending_gate"]["type"] == "finance"
    assert state["outputs"]["confirm_costing_snapshot"]["data"]["status"] == "trial"


def test_api_resume_accepts_trusted_role_headers(api):
    client, tmp_path = api
    created = _start(client, tmp_path)
    run_id = created["run_id"]
    response = client.post(
        f"/runs/{run_id}/resume", json={"decision": "approve"},
        headers={"X-Actor-User": "fin-header", "X-Actor-Roles": "finance-officer"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"


def test_api_public_state_contains_gate_outputs_and_redacts_content(api):
    client, tmp_path = api
    created = _start(client, tmp_path)
    state = client.get(f"/runs/{created['run_id']}").json()
    assert {"pending_gate", "outputs", "approvals", "status"} <= set(state)
    assert "__interrupt__" not in state
    assert state["pending_gate"]["review"]["total_cost"] == 810.0
