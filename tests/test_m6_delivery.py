"""B3：凭据层的两个工具——`get_delivery_note`（读 canonical）+ `generate_statement`（依据生成）。

要点：

1. **拆信封**（本仓踩过的坑）：canonical 实体的读口形状是 ``{canonical_key, **envelope}``，
   业务体嵌套在 ``payload`` 里。B0b 的两个读工具曾因直接按业务字段过滤而**恒返回空**
   （由往返测试抓出）。本文件用**真 canonical 库**跑一遍导入→回读，确保新读口没重蹈。
2. **依据生成不编造**：客户对账依据 canonical 送货单的外发货、供应商对账依据 M4 **已入库**
   追踪行；依据缺失 → `missing` + `basis_source=missing` 并**拒绝出单**，
   绝不给一份"金额为零"的空对账单。
3. **不误用单价**：供应商侧入库行的 `unit_price` 是**单价**不是入库金额，不得当对账金额
   （依据行须自带 `amount`）。
4. **落库仍走三段式**：`generate_statement` 只算不落；`save_statement` 现在可**自动取依据**，
   依旧 propose（trial 草稿）→ finance 门 → commit 翻正。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from test_graph_smoke import _deps

from yunpai_orchestrator.graph import build_graph
from yunpai_orchestrator.m6_store import M6Store, STATUS_CONFIRMED, STATUS_TRIAL
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.reviewer import rules
from yunpai_orchestrator.state import new_state_v2

CTX = {"task_id": "TASK-M6-B3", "tenant_id": "default", "actor": "tester"}


def _ctx(tmp_path, **extra):
    return {"tenant_id": "default", "task_id": "task-m6-b3",
            "m6_db_path": str(tmp_path / "m6.sqlite"), **extra}


@pytest.fixture
def registry():
    return build_default_registry()


@pytest.fixture
def canonical_env(tmp_path, monkeypatch):
    """canonical 库 = 临时 sqlite（真库，不是 stub）。"""
    db = tmp_path / "m0-canonical.sqlite"
    monkeypatch.setenv("YUNPAI_M0_DB", str(db))
    monkeypatch.delenv("M0_URL", raising=False)
    return db


def _envelope(entity_type: str, business_key: str, payload: dict) -> dict:
    return {
        "schema_version": "m0.ingest.v1",
        "tenant_id": "default",
        "idempotency_key": f"idem-{entity_type}-{business_key}",
        "source": {"system": "m6-b3-test", "external_id": f"ext-{business_key}",
                   "sha256": hashlib.sha256(f"{entity_type}:{business_key}".encode()).hexdigest()},
        "entity_type": entity_type,
        "identity": {"business_key": business_key},
        "payload": payload,
        "evidence": [{"key": "ev-1", "ref": f"test:{business_key}"}],
        "review_status": "approved",
        "reviewed_by": "tester",
    }


# ---------------------------------------------------------------------------
# get_delivery_note：真 canonical 往返（拆信封）
# ---------------------------------------------------------------------------

async def test_get_delivery_note_roundtrip_from_canonical(registry, canonical_env, tmp_path,
                                                          monkeypatch):
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "m4b-absent.sqlite"))
    published = await registry.call("m0_delivery_notes_import", {"records": [
        _envelope("delivery_note", "DN-2026-0007", {
            "note_no": "DN-2026-0007", "counterparty_code": "CUST-01", "note_date": "2026-09-07",
            "direction": "out", "amount": 8800.0,
            "signed_by": "张三", "warehouse_confirmed_by": "李四", "qc_status": "released"}),
    ]}, CTX)
    assert published["success"] is True, published.get("errors")

    state = new_state_v2({"message": "查送货单", "note_no": "DN-2026-0007"})
    got = await registry.call("get_delivery_note",
                              _bridge_payload(state, "get_delivery_note"), CTX)
    assert got["success"] is True, got.get("errors")
    note = got["data"]["delivery_note"]
    # 业务体在顶层（信封已拆）——金额/单号不是 None，正是 B0b 那个坑的正面断言
    assert note["note_no"] == "DN-2026-0007"
    assert note["amount"] == 8800.0
    assert note["counterparty_code"] == "CUST-01"
    assert note["signed_by"] == "张三" and note["warehouse_confirmed_by"] == "李四"

    missing = await registry.call("get_delivery_note",
                                  _bridge_payload(new_state_v2({"message": "x",
                                                                "note_no": "DN-NOPE"}),
                                                  "get_delivery_note"), CTX)
    assert missing["success"] is False and missing["code"] == "NOT_FOUND"

    no_key = _bridge_payload(new_state_v2({"message": "x"}), "get_delivery_note")
    assert no_key["code"] == "BLOCKED_INPUT"          # 没单号：装配层就拦下


def _bridge_payload(state, tool):
    from yunpai_orchestrator.orchestration_bridge import bridge_payload

    return bridge_payload(state, tool)


# ---------------------------------------------------------------------------
# generate_statement：依据送货单生成（客户侧）
# ---------------------------------------------------------------------------

async def test_generate_statement_from_delivery_notes(registry, canonical_env, tmp_path,
                                                      monkeypatch):
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "m4b-absent.sqlite"))
    published = await registry.call("m0_delivery_notes_import", {"records": [
        _envelope("delivery_note", "DN-1", {"note_no": "DN-1", "counterparty_code": "CUST-01",
                                            "note_date": "2026-09-07", "direction": "out",
                                            "amount": 8800.0}),
        _envelope("delivery_note", "DN-2", {"note_no": "DN-2", "counterparty_code": "CUST-01",
                                            "note_date": "2026-09-20", "direction": "out",
                                            "amount": 1200.0}),
        _envelope("delivery_note", "DN-3", {"note_no": "DN-3", "counterparty_code": "CUST-99",
                                            "note_date": "2026-09-21", "direction": "out",
                                            "amount": 9999.0}),
        _envelope("delivery_note", "DN-IN", {"note_no": "DN-IN", "counterparty_code": "CUST-01",
                                             "note_date": "2026-09-22", "direction": "in",
                                             "amount": 500.0}),
    ]}, CTX)
    assert published["success"] is True

    state = new_state_v2({"message": "和 CUST-01 对账", "statement_type": "customer",
                          "counterparty_code": "CUST-01", "opening_balance": 1000.0})
    result = await registry.call("generate_statement",
                                 _bridge_payload(state, "generate_statement"), CTX)
    data = result["data"]
    assert result["success"] is True, result.get("errors")
    assert data["basis_source"] == "delivery_notes"
    # 只取该客户、只取外发货（DN-3 是别人、DN-IN 是入库单）：1000 + 8800 + 1200
    assert data["inflow"] == 10000.0 and data["outflow"] == 0.0
    assert data["closing_balance"] == 11000.0
    assert [tx["ref"] for tx in data["transactions"]] == ["DN-1", "DN-2"]
    assert all(tx["direction"] == "in" for tx in data["transactions"])   # 发货 → 应收增加


async def test_generate_statement_refuses_without_basis(registry, canonical_env, tmp_path,
                                                        monkeypatch):
    """没有任何依据时**拒出单**（不给"金额为零"的空对账单）。"""
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "m4b-absent.sqlite"))
    state = new_state_v2({"message": "对账", "statement_type": "customer",
                          "counterparty_code": "CUST-NONE"})
    result = await registry.call("generate_statement",
                                 _bridge_payload(state, "generate_statement"), CTX)
    assert result["success"] is False and result["code"] == "INVALID_INPUT"
    assert result["data"]["basis_source"] == "missing"
    assert result["data"]["missing"][0]["reason"] == "missing_delivery_notes"

    # 送货单缺金额 → 该单不进明细（不按 0 计）
    await registry.call("m0_delivery_notes_import", {"records": [
        _envelope("delivery_note", "DN-NOAMT", {"note_no": "DN-NOAMT",
                                                "counterparty_code": "CUST-02",
                                                "note_date": "2026-09-07",
                                                "direction": "out"})]}, CTX)
    no_amount = await registry.call("generate_statement", _bridge_payload(
        new_state_v2({"message": "对账", "counterparty_code": "CUST-02"}),
        "generate_statement"), CTX)
    assert no_amount["success"] is False
    assert no_amount["data"]["missing"][0]["reason"] == "missing_delivery_amount"


async def test_generate_statement_supplier_never_uses_unit_price_as_amount(registry, tmp_path,
                                                                          monkeypatch):
    """供应商侧：入库行的 `unit_price` 是单价，**不得**当对账金额（须自带 amount）。"""
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0-absent.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)
    state = new_state_v2({
        "message": "和供应商对账", "statement_type": "supplier",
        "counterparty_code": "供应商一", "opening_balance": 0.0,
        "purchase_tracking_rows": [
            # 已入库但只有单价（旧口径会把它当金额 → 编造）
            {"id": 1, "purchase_order_no": "PO-1", "supplier_name": "供应商一",
             "arrival_status": "received", "unit_price": "8.5"},
            # 已入库且带金额（可用）
            {"id": 2, "purchase_order_no": "PO-2", "supplier_name": "供应商一",
             "arrival_status": "received", "unit_price": "3.2", "amount": "320.0"},
            # 未入库 → 不是对账依据
            {"id": 3, "purchase_order_no": "PO-3", "supplier_name": "供应商一",
             "arrival_status": "not_received", "amount": "700.0"},
        ]})
    result = await registry.call("generate_statement",
                                 _bridge_payload(state, "generate_statement"), CTX)
    data = result["data"]
    assert data["basis_source"] == "purchase_receipts"
    assert data["closing_balance"] == 320.0            # 只认已入库 + 自带金额的那条
    assert [tx["ref"] for tx in data["transactions"]] == ["PO-2"]
    assert data["missing"] == [{"line": 1, "reason": "missing_receipt_amount", "ref": "PO-1"}]


# ---------------------------------------------------------------------------
# save_statement：可自动取依据，落库仍走 propose → finance 门 → commit
# ---------------------------------------------------------------------------

def test_save_statement_auto_basis_then_finance_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0-absent.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)
    monkeypatch.setenv("YUNPAI_M6_DB", str(tmp_path / "m6.sqlite"))
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "m4b-absent.sqlite"))
    db = tmp_path / "m6.sqlite"

    graph = build_graph(_deps(), checkpointer=MemorySaver())
    state = new_state_v2({
        "message": "出对账单", "tools": ["save_statement"],
        "statement_type": "supplier", "counterparty_code": "供应商一",
        "opening_balance": 1000.0, "doc_date": "2026-09-30",
        "purchase_tracking_rows": [
            {"id": 1, "purchase_order_no": "PO-1", "supplier_name": "供应商一",
             "arrival_status": "received", "amount": "320.0"},
            {"id": 2, "purchase_order_no": "PO-2", "supplier_name": "供应商一",
             "arrival_status": "received", "amount": "180.0"},
        ]})
    config = {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 64}

    out = asyncio.run(graph.ainvoke(state, config))
    saved = out["outputs"]["save_statement"]["data"]
    assert saved["status"] == STATUS_TRIAL               # propose：草稿
    assert saved["pending_document_commit"] is True
    assert saved["totals_source"] == "purchase_receipts"  # **依据是自动取来的**
    assert saved["closing_balance"] == 1500.0             # 1000 + 320 + 180
    assert saved["direction"] == "in"                     # supplier → 我方对供应商
    gate = out["__interrupt__"][0]
    gate = gate.value if hasattr(gate, "value") else gate
    assert gate["gate"]["type"] == "finance"
    assert M6Store(str(db)).list_documents(status=STATUS_CONFIRMED) == []

    resumed = asyncio.run(graph.ainvoke(
        Command(resume={"decision": "approve", "actor": "fin-01",
                        "roles": ["finance-officer"]}), config))
    assert resumed["status"] == "completed", f"errors={resumed.get('errors')}"
    docs = M6Store(str(db)).list_documents(doc_type="statement")
    assert [doc["status"] for doc in docs] == [STATUS_CONFIRMED]
    assert docs[0]["amount"] == 1500.0


def test_save_statement_still_blocks_when_no_basis_and_no_detail(tmp_path, monkeypatch):
    """依据与明细都没有 → 仍 BLOCKED_INPUT（不落一张空对账单）。"""
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0-absent.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "m4b-absent.sqlite"))
    payload = _bridge_payload(
        new_state_v2({"message": "对账", "counterparty_code": "CUST-01"}),
        "save_statement")
    assert payload["code"] == "BLOCKED_INPUT"
    assert payload["data"]["missing_fields"] == [
        "对账明细（transactions/lines 或可用的送货单/入库依据）"]


def test_save_statement_rejects_explicit_transaction_without_amount(tmp_path):
    """显式对账明细缺金额不得按 0 生成草稿。"""
    from yunpai_orchestrator.m6_tools import m6_save_statement

    payload = {
        "doc_no": "ST-MISSING-AMOUNT",
        "counterparty_code": "CUST-01",
        "transactions": [{"direction": "in"}],
    }
    result = asyncio.run(m6_save_statement(payload, {
        "m6_db_path": str(tmp_path / "m6.sqlite"), "tenant_id": "default",
        "task_id": "missing-amount",
    }))
    assert result["success"] is False
    assert result["code"] == "INVALID_INPUT"
    assert result["data"]["missing"][0]["reason"] == "missing_transaction_amount"


# ---------------------------------------------------------------------------
# 契约 ↔ 规则
# ---------------------------------------------------------------------------

def test_b3_read_tools_are_ungated(registry):
    for tool in ("get_delivery_note", "generate_statement"):
        spec = registry.specs[tool]
        assert spec.module == "m6", tool
        assert spec.side_effect == "none", tool
        assert spec.review_gate == "none", tool
        assert rules.gate_type_for(tool, spec) == "", tool
        assert tool not in rules.RULES, tool
        assert rules.evaluate(tool, {"success": True, "data": {}}) == [], tool
        # 依据不足是业务状态（basis_source=missing），不开补数门
        assert rules.evaluate(tool, {"success": False, "code": "INVALID_INPUT"}) == [], tool
