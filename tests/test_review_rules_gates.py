"""书二 §6：审查规则与 Gate——合同驱动 + 决策语义矩阵 + 永不自动放行。"""
import pytest

from yunpai_orchestrator.reviewer import rules
from yunpai_orchestrator.reviewer.gates import (
    GATE_ALLOWED_ROLES,
    GateError,
    apply_decision,
    authorize,
    make_gate,
    validate_resume_decision,
)
from yunpai_orchestrator.state import new_state_v2


def test_ingest_document_low_confidence_opens_review_gate():
    findings = rules.evaluate("ingest_document", {"data": {"overall_confidence": 0.5}})
    assert any(f["gate"] == "review" for f in findings)


def test_ingest_document_needs_review_flag():
    findings = rules.evaluate("ingest_document", {"data": {"needs_review": True}})
    assert any(f["gate"] == "review" for f in findings)


def test_ingest_document_clean_passes():
    assert rules.evaluate("ingest_document", {"data": {"overall_confidence": 0.95, "needs_review": False}}) == []


def test_solve_draft_opens_apply_gate():
    findings = rules.evaluate("solve_scheduling", {"data": {"lifecycle_status": "draft"}})
    assert any(f["gate"] == "apply" for f in findings)


def test_solve_released_no_gate():
    assert rules.evaluate("solve_scheduling", {"data": {"lifecycle_status": "released"}}) == []


def test_blocked_input_maps_to_data_gate():
    findings = rules.evaluate("any_tool", {"code": "BLOCKED_INPUT", "data": {}})
    assert findings and findings[0]["gate"] == "blocked_input"


def test_review_summary_is_carried_only_when_present():
    result = {"success": True, "data": {"review_summary": {"total_cost": 810.0,
                                                               "cost_incomplete": False}}}
    finding = rules.evaluate("confirm_costing_snapshot", {**result, "data": {
        **result["data"], "pending_confirmation": True}})[0]
    assert finding["review"] == {"total_cost": 810.0, "cost_incomplete": False}
    legacy = make_gate("candidate", "ingest_canonical", "reason")
    assert set(legacy) == {"type", "tool", "step_id", "reason", "allowed_roles",
                           "payload_digest", "opened_at"}


def test_no_auto_approve():
    """红线：任何路径都不得自动放行人工门（旧审计 P0 教训）。"""
    assert rules.AUTO_APPROVE_ALLOWED is False


def test_gate_role_matrix_complete():
    for gate in ("candidate", "review", "engineering", "procurement", "apply", "blocked_input"):
        assert gate in GATE_ALLOWED_ROLES and "admin" in GATE_ALLOWED_ROLES[gate]


def test_resume_decision_matrix():
    assert validate_resume_decision("apply", "approve") == "approve"
    assert validate_resume_decision("apply", "allow") == "approve"     # approve 族归一
    assert validate_resume_decision("blocked_input", "retry") == "retry"
    assert validate_resume_decision("blocked_input", "supplement") == "supplement"
    assert validate_resume_decision("procurement", "supplier_by_material") == "supplier_by_material"
    with pytest.raises(GateError):
        validate_resume_decision("apply", "retry")  # apply 不接受 retry（engineering 同理禁当 retry 用）
    with pytest.raises(GateError):
        validate_resume_decision("review", "supplement")


def test_authorize_requires_allowed_role():
    gate = make_gate("apply", "solve_scheduling", "draft")
    with pytest.raises(GateError):
        authorize(gate, ["document-reviewer"])
    authorize(gate, ["production-manager"])
    authorize(gate, ["admin"])


def test_apply_decision_supplement_merges_into_request():
    state = new_state_v2({"message": "m"})
    gate = make_gate("blocked_input", "m5_snapshot", "缺快照")
    updates = apply_decision(state, gate, {
        "decision": "supplement", "actor": "u", "roles": ["data-steward"],
        "supplement": {"snapshot_kind": "orders"}})
    assert updates["request"]["m5_snapshot"]["snapshot_kind"] == "orders"
    assert updates["pending_gate"] is None
    assert updates["approvals"][0]["decision"] == "supplement"


def test_apply_decision_approve_authorizes_tool():
    state = new_state_v2({"message": "m"})
    gate = make_gate("apply", "solve_scheduling", "draft")
    updates = apply_decision(state, gate, {"decision": "approve", "actor": "pm", "roles": ["production-manager"]})
    assert "solve_scheduling" in updates["authorized_steps"]
