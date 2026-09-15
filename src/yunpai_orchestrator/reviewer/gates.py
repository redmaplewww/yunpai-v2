"""Gate 生命周期（书二 §6.2）——角色矩阵与 resume 决策语义〔迁〕legacy graph.py:456-492。"""
from __future__ import annotations

from typing import Any

from ..state import now_iso

#: 六类人工门 + 授权/阻断门的角色矩阵（legacy 原值迁移）。
GATE_ALLOWED_ROLES: dict[str, tuple[str, ...]] = {
    "candidate": ("data-steward", "m0-reviewer", "admin"),
    "sensitive_data": ("data-steward", "hr-officer", "admin"),
    "review": ("document-reviewer", "data-steward", "admin"),
    "engineering": ("engineering-manager", "admin"),
    "procurement": ("procurement-manager", "purchase-reviewer", "admin"),
    "apply": ("production-manager", "admin"),
    "authorization": ("operator", "admin"),
    "blocked_input": ("data-steward", "engineering-manager", "production-manager", "admin"),
    # finance（F-008 / D-005）：成本确认（trial→confirmed）、月末结账、单据落库。
    # 不复用 authorization：那支角色是通用 operator，与"财务口径须财务确认"不符。
    "finance": ("finance-officer", "admin"),
}

#: resume 决策语义矩阵（书二 §6.2；业务语义沿用旧系统）。
GATE_DECISIONS: dict[str, tuple[str, ...]] = {
    "review": ("approve", "reject"),
    "candidate": ("approve", "reject"),
    "engineering": ("approve", "reject"),
    "apply": ("approve", "reject"),
    "sensitive_data": ("approve", "reject"),
    "authorization": ("approve", "reject"),
    "procurement": ("retry", "supplier_by_material", "reject"),
    "blocked_input": ("retry", "supplement", "reject"),
    "finance": ("approve", "reject"),
}

_APPROVE_LIKE = {"approve", "allow", "continue"}


class GateError(ValueError):
    pass


def make_gate(gate_type: str, tool: str, reason: str, *, step_id: str = "",
              payload_digest: str = "", code: str = "", message: str = "",
              missing_fields: list[Any] | None = None,
              review: dict[str, Any] | None = None) -> dict[str, Any]:
    """建门；``code``/``message``/``missing_fields`` 为可诊断字段（非空才写入）。

    这三项由 ``rules.evaluate`` 从工具结果抽取后经 graph 传入：没有它们，
    ``blocked_input`` 门只有静态 ``reason``，前端无法回答「缺什么、怎么补」。
    不传时 Gate 形状与旧版逐字一致（向后兼容）。
    """
    gate: dict[str, Any] = {
        "type": gate_type,
        "tool": tool,
        "step_id": step_id,
        "reason": reason,
        "allowed_roles": list(GATE_ALLOWED_ROLES.get(gate_type, ("admin",))),
        "payload_digest": payload_digest,
        "opened_at": now_iso(),
    }
    if code:
        gate["code"] = code
    if message:
        gate["message"] = message
    if missing_fields:
        gate["missing_fields"] = list(missing_fields)
    if review:
        gate["review"] = dict(review)
    return gate


def authorize(gate: dict[str, Any], roles: list[str]) -> None:
    """角色校验（不通过抛 GateError）；admin 恒可。"""
    allowed = GATE_ALLOWED_ROLES.get(str(gate.get("type")), ("admin",))
    principal_roles = [str(r) for r in roles] if isinstance(roles, list) else []
    if not principal_roles or not (set(principal_roles) & set(allowed)):
        raise GateError(
            f"gate {gate.get('type')} 需要角色 {'/'.join(allowed)}；当前 roles={principal_roles or ['(none)']}"
        )


def validate_resume_decision(gate_type: str, decision: str) -> str:
    """校验决策合法性并归一化（approve/allow/continue → approve 族）。"""
    decision = str(decision or "").strip().lower()
    allowed = GATE_DECISIONS.get(gate_type, ("approve", "reject"))
    if decision in _APPROVE_LIKE and "approve" in allowed:
        return "approve"
    if decision in allowed:
        return decision
    raise GateError(f"gate {gate_type} 不接受决策 {decision!r}（合法：{list(allowed)}）")


def apply_decision(state: dict[str, Any], gate: dict[str, Any],
                   decision: dict[str, Any]) -> dict[str, Any]:
    """应用 Gate 决策，返回状态增量 dict（approve/reject/retry 族）。

    decision: {"decision": str, "actor": str, "roles": [str], "note": str,
               "supplement": dict}（supplement 供 blocked_input 补数）。
    """
    gate_type = str(gate.get("type"))
    normalized = validate_resume_decision(gate_type, str(decision.get("decision") or ""))
    authorize(gate, list(decision.get("roles") or []))
    tool = str(gate.get("tool") or "")
    at = now_iso()
    audit = {
        "gate_type": gate_type, "tool": tool, "decision": normalized,
        "actor": str(decision.get("actor") or "human"),
        "roles": list(decision.get("roles") or []),
        "note": str(decision.get("note") or ""),
        "at": at,
    }
    approvals = list(state.get("approvals") or []) + [audit]
    trace = list(state.get("trace") or [])
    trace.append({"event": "gate.decided", "decision": normalized, "actor": audit["actor"],
                  "tool": tool, "at": at})
    updates: dict[str, Any] = {"approvals": approvals, "pending_gate": None, "trace": trace}
    if normalized == "approve":
        authorized = list(state.get("authorized_steps") or [])
        if tool and tool not in authorized:
            authorized.append(tool)
        updates["authorized_steps"] = authorized
    elif normalized == "reject":
        errors = list(state.get("errors") or []) + [{
            "tool": tool, "code": "GATE_REJECTED",
            "message": f"{gate_type} Gate 被拒绝：{audit['note'] or '无说明'}"}]
        updates["errors"] = errors
    elif normalized in ("retry", "supplement", "supplier_by_material"):
        # data/procurement 族：补数重试。supplement 数据由 request 注入后重装配。
        request = dict(state.get("request") or {})
        supplement = decision.get("supplement")
        if isinstance(supplement, dict):
            request.setdefault(tool, {})
            request[tool] = {**request[tool], **supplement}
            # K2/K3：装配层读的是**顶层** request 键（orchestration_bridge 的
            # request["inventory_snapshot"]/request["supplier_by_material"]），
            # 工具载荷兼容路径读 request[tool]（assembler 的显式参数覆盖）。
            # 两条键路径都写入，门里补的数据才能被装配看见（None 不写入，
            # 不覆盖已有事实）。
            for key, value in supplement.items():
                if value is not None:
                    request[key] = value
        request["gate_retry"] = {"tool": tool, "mode": normalized, "at": at}
        updates["request"] = request
    return updates
