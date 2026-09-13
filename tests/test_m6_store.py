"""B1 第一批：`m6_store` 存储层 + `finance` 门型（F-008 / D-005 / D-006）。

两条财务护栏是本文件的验收核心：
1. **试算不污染账本**：`trial` 快照不进月末汇总，只有 `confirmed` 计入；
2. **月结冻结后不得再确认**（也不得再产出新成本）。
三段式的 propose/commit 分界也在这里验证：`save_*` 写 trial，`confirm_*` 才翻 confirmed。
"""

from __future__ import annotations

import pytest

from yunpai_orchestrator.m6_store import (
    SCHEMA_VERSION,
    STATUS_CONFIRMED,
    STATUS_TRIAL,
    M6Store,
    store_path,
)
from yunpai_orchestrator.reviewer import gates


@pytest.fixture
def store(tmp_path) -> M6Store:
    return M6Store(str(tmp_path / "m6.sqlite"))


def _save(store: M6Store, snapshot_id: str, *, period: str = "2026-09",
          order_id: str = "SO-1", batch: str = "B1", total: float = 100.0,
          status_lines: bool = True) -> dict:
    lines = ([{"element": "material", "material_code": "M-1", "quantity": 2,
               "unit_price": 10.0, "amount": 20.0, "source_kind": "stock",
               "source_ref": "canonical:bom", "evidence": {"sheet": "BOM", "row": 3}}]
             if status_lines else [])
    return store.save_snapshot(snapshot_id=snapshot_id, period=period, order_id=order_id,
                               product_code="W-H913", batch_no=batch, quantity=10,
                               unit_cost=10.0, total_cost=total, lines=lines,
                               evidence={"bom_version": "v1"})


# ── 护栏 1：试算不进账本 ────────────────────────────────────────────────
def test_trial_snapshot_not_counted_in_month_summary(store):
    _save(store, "S-TRIAL", total=999.0)
    _save(store, "S-CONFIRM", batch="B2", total=100.0)
    store.confirm_snapshot("S-CONFIRM", actor="finance")

    summary = store.month_summary("2026-09")
    assert summary["snapshot_count"] == 1            # 只算 confirmed
    assert summary["total_cost"] == 100.0            # 试算的 999 不计入
    assert summary["trial_count"] == 1
    assert summary["trial_excluded"] is True
    # 试算仍在库里可追溯（不删、不藏）
    assert store.get_snapshot("S-TRIAL")["status"] == STATUS_TRIAL


def test_propose_writes_trial_and_commit_flips_confirmed(store):
    saved = _save(store, "S-1")
    assert saved["status"] == STATUS_TRIAL
    assert store.get_snapshot("S-1")["status"] == STATUS_TRIAL

    result = store.confirm_snapshot("S-1", actor="finance-officer")
    assert result["success"] is True
    assert result["changed"] is True
    snapshot = store.get_snapshot("S-1")
    assert snapshot["status"] == STATUS_CONFIRMED
    assert snapshot["confirmed_by"] == "finance-officer"
    assert snapshot["confirmed_at"] == result["confirmed_at"]


def test_confirm_is_idempotent_and_not_found_is_explicit(store):
    _save(store, "S-1")
    assert store.confirm_snapshot("S-1")["changed"] is True
    again = store.confirm_snapshot("S-1")
    assert again["success"] is True and again["changed"] is False
    missing = store.confirm_snapshot("S-NOPE")
    assert missing["success"] is False and missing["code"] == "NOT_FOUND"


# ── 护栏 2：月结冻结 ────────────────────────────────────────────────────
def test_closed_month_blocks_new_writes_and_confirms(store):
    _save(store, "S-1", total=100.0)
    store.confirm_snapshot("S-1", actor="finance")
    closed = store.close_month(period="2026-09", totals={"total_cost": 100.0},
                               snapshot_count=1, actor="finance")
    assert closed["success"] is True

    # 冻结后不得再产出新成本
    blocked_save = _save(store, "S-2", batch="B9", total=50.0)
    assert blocked_save["success"] is False and blocked_save["code"] == "MONTH_CLOSED"
    assert store.get_snapshot("S-2") is None      # 确实没落库，不是仅返回错误

    # 汇总走冻结快照（不再随新确认变动）
    summary = store.month_summary("2026-09")
    assert summary["frozen"] is True
    assert summary["totals"]["total_cost"] == 100.0
    assert summary["closed_by"] == "finance"


def test_closed_month_blocks_confirm_of_existing_trial(store):
    """冻结前已存在的 trial 快照，冻结后也不得确认（月账口径锁定）。"""
    _save(store, "S-1")
    store.close_month(period="2026-09", totals={}, snapshot_count=0, actor="finance")
    result = store.confirm_snapshot("S-1")
    assert result["success"] is False and result["code"] == "MONTH_CLOSED"
    assert store.get_snapshot("S-1")["status"] == STATUS_TRIAL   # 未被翻正


def test_close_month_twice_is_rejected(store):
    store.close_month(period="2026-09", totals={}, snapshot_count=0)
    again = store.close_month(period="2026-09", totals={}, snapshot_count=0)
    assert again["success"] is False and again["code"] == "MONTH_ALREADY_CLOSED"


# ── 查重（计划 §6 的 B 问题）与回读 ─────────────────────────────────────
def test_business_key_dedup_hint_and_lookup(store):
    first = _save(store, "S-1")
    assert first["duplicate_of"] is None
    second = _save(store, "S-2")          # 同 order+batch+period
    assert second["duplicate_of"]["other_count"] == 1
    assert second["duplicate_of"]["latest"]["snapshot_id"] == "S-1"

    found = store.find_snapshot(order_id="SO-1", batch_no="B1", period="2026-09")
    assert found["snapshot_id"] == "S-2"   # 取最新
    assert store.find_snapshot(order_id="SO-1", batch_no="B1", period="2026-10") is None


def test_snapshot_readback_includes_lines_and_evidence(store):
    _save(store, "S-1")
    snapshot = store.get_snapshot("S-1")
    assert snapshot["evidence"] == {"bom_version": "v1"}
    assert snapshot["cost_incomplete"] is False
    assert len(snapshot["lines"]) == 1
    line = snapshot["lines"][0]
    assert line["line_id"] == "S-1::L0001"
    assert line["element"] == "material"
    assert line["source_ref"] == "canonical:bom"
    assert line["evidence"] == {"sheet": "BOM", "row": 3}


def test_month_summary_groups_by_order(store):
    _save(store, "S-1", order_id="SO-1", total=100.0)
    _save(store, "S-2", order_id="SO-2", batch="B2", total=25.5)
    store.confirm_snapshot("S-1")
    store.confirm_snapshot("S-2")
    summary = store.month_summary("2026-09")
    assert summary["total_cost"] == 125.5
    by_order = {row["order_id"]: row for row in summary["by_order"]}
    assert by_order["SO-1"]["total_cost"] == 100.0
    assert by_order["SO-2"]["snapshot_count"] == 1


# ── 单据（凭据）──────────────────────────────────────────────────────────
def test_document_trial_confirm_and_dedup(store):
    saved = store.save_document(doc_id="D-1", doc_no="DN-001", doc_type="delivery_note",
                                counterparty_code="CUST-01", doc_date="2026-09-07",
                                direction="out", amount=1000.0,
                                lines=[{"item": "W-H913", "qty": 10}])
    assert saved["status"] == STATUS_TRIAL and saved["duplicate_of"] is None

    dup = store.save_document(doc_id="D-2", doc_no="DN-001", doc_type="delivery_note")
    assert dup["duplicate_of"]["doc_id"] == "D-1"

    assert store.find_document(doc_type="delivery_note", doc_no="DN-001")["doc_id"] == "D-2"
    confirmed = store.confirm_document("D-1", actor="finance")
    assert confirmed["status"] == STATUS_CONFIRMED

    docs = {row["doc_id"]: row for row in store.list_documents(doc_type="delivery_note")}
    assert set(docs) == {"D-1", "D-2"}
    assert docs["D-1"]["status"] == STATUS_CONFIRMED      # 只有被确认的那张翻正
    assert docs["D-2"]["status"] == STATUS_TRIAL
    assert docs["D-1"]["lines"] == [{"item": "W-H913", "qty": 10}]


# ── 路径约定与 schema 守卫 ──────────────────────────────────────────────
def test_store_path_resolution_order(tmp_path, monkeypatch):
    monkeypatch.setenv("YUNPAI_M6_DB", str(tmp_path / "env.sqlite"))
    assert store_path({"m6_db_path": "ctx.sqlite"}) == "ctx.sqlite"       # ctx 优先
    assert store_path({}) == str(tmp_path / "env.sqlite")                 # 其次 env
    monkeypatch.delenv("YUNPAI_M6_DB")
    assert store_path(None) == "runtime/yunpai-m6.sqlite"                 # 最后默认


def test_schema_version_guard(tmp_path):
    db = str(tmp_path / "m6.sqlite")
    M6Store(db)
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("UPDATE m6_store_meta SET value='m6.store.v0' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="SCHEMA_VERSION_MISMATCH"):
        M6Store(db)
    assert SCHEMA_VERSION == "m6.store.v1"


# ── finance 门型（D-005）────────────────────────────────────────────────
def test_finance_gate_type_registered_with_roles_and_decisions():
    assert gates.GATE_ALLOWED_ROLES["finance"] == ("finance-officer", "admin")
    assert gates.GATE_DECISIONS["finance"] == ("approve", "reject")


def test_finance_gate_authorizes_only_finance_roles():
    gate = gates.make_gate("finance", "confirm_costing_snapshot", "成本确认需财务审批")
    assert gate["type"] == "finance"
    gates.authorize(gate, ["finance-officer"])   # 合法角色
    gates.authorize(gate, ["admin"])
    with pytest.raises(gates.GateError):
        gates.authorize(gate, ["operator"])       # 通用 operator 不得审财务门
    assert gates.validate_resume_decision("finance", "approve") == "approve"
    with pytest.raises(gates.GateError):
        gates.validate_resume_decision("finance", "retry")   # finance 门只有 approve/reject
