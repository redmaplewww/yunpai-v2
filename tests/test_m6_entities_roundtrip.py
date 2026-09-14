"""B0b 遗留收口：`expense` / `delivery_note` 的 canonical 导入 → 回读往返（F-008）。

B0b 当时只验到接入面（白名单、契约、门、绑定），往返留了缺口：**带完整
``m0.ingest.v1`` 信封**的 canonical 记录要走通「facade 发布 → M0 canonical →
`list_expenses` / `list_delivery_notes` 只读回读」这条链，否则「M6 只读 canonical」
这条 D4／老仓 D-020（v2 记 D-009）口径只是声明。

信封口径（``m0_catalog_ingest.validate_records``）：
``schema_version="m0.ingest.v1"`` / ``entity_type`` / ``tenant_id``（须与请求租户一致）/
``idempotency_key`` / ``source{system,external_id,sha256(64hex)}`` /
``identity{business_key}`` / ``payload``（业务体）/ ``review_status="approved"``。

红线：往返必须**原样照抄事实值**（金额、单号、日期），并在读回侧保持租户隔离——
M6 的成本/对账都建立在这些事实上。
"""

from __future__ import annotations

import hashlib

import pytest

from yunpai_orchestrator.registry import build_default_registry

CTX = {"task_id": "TASK-M6-ROUNDTRIP", "tenant_id": "default", "actor": "tester"}


@pytest.fixture
def canonical_env(tmp_path, monkeypatch):
    """canonical 库 = 临时 sqlite；不配 M0_URL（避免发布走 HTTP）。"""
    db = tmp_path / "m0-canonical.sqlite"
    monkeypatch.setenv("YUNPAI_M0_DB", str(db))
    monkeypatch.delenv("M0_URL", raising=False)
    return db


def _envelope(entity_type: str, business_key: str, payload: dict) -> dict:
    return {
        "schema_version": "m0.ingest.v1",
        "tenant_id": "default",
        "idempotency_key": f"idem-{entity_type}-{business_key}",
        "source": {"system": "m6-roundtrip-test",
                   "external_id": f"ext-{entity_type}-{business_key}",
                   "sha256": hashlib.sha256(f"{entity_type}:{business_key}".encode()).hexdigest()},
        "entity_type": entity_type,
        "identity": {"business_key": business_key},
        "payload": payload,
        "evidence": [{"key": "ev-1", "ref": f"test:{entity_type}:{business_key}"}],
        "review_status": "approved",
        "reviewed_by": "tester",
    }


async def test_expense_import_then_read_back(canonical_env):
    registry = build_default_registry()
    expenses = [
        _envelope("expense", "EXP-2026-09-ELEC",
                  {"category": "electricity", "amount": 12345.67, "period": "2026-09",
                   "note": "9 月电费"}),
        _envelope("expense", "EXP-2026-09-MEAL",
                  {"category": "meals", "amount": 500.5, "period": "2026-09"}),
    ]
    published = await registry.call("m0_expenses_import", {"records": expenses}, CTX)
    assert published["success"] is True, published.get("errors")
    assert published["data"]["published"] == 2

    # 回读：金额/类别/期间原样照抄（不换算、不四舍五入、不丢字段）
    listed = await registry.call("list_expenses", {"period": "2026-09"}, CTX)
    assert listed["success"] is True
    rows = {row["category"]: row for row in listed["data"]["expenses"]}
    assert set(rows) == {"electricity", "meals"}
    assert rows["electricity"]["amount"] == 12345.67
    assert rows["electricity"]["period"] == "2026-09"
    assert rows["meals"]["amount"] == 500.5

    # 过滤面生效：别的期间读不到
    other = await registry.call("list_expenses", {"period": "2026-10"}, CTX)
    assert other["data"]["expenses"] == []


async def test_delivery_note_import_then_read_back_with_signoff_fields(canonical_env):
    registry = build_default_registry()
    note = _envelope("delivery_note", "DN-2026-0007", {
        "note_no": "DN-2026-0007", "counterparty_code": "CUST-01", "note_date": "2026-09-07",
        "direction": "out", "amount": 8800.0,
        "signed_by": "张三", "signed_at": "2026-09-07T10:20:00",
        "warehouse_confirmed_by": "李四", "qc_status": "released",
    })
    published = await registry.call("m0_delivery_notes_import", {"records": [note]}, CTX)
    assert published["success"] is True, published.get("errors")
    assert published["data"]["published"] == 1

    listed = await registry.call("list_delivery_notes", {"counterparty_code": "CUST-01"}, CTX)
    assert listed["success"] is True
    rows = listed["data"]["delivery_notes"]
    assert len(rows) == 1
    row = rows[0]
    assert row["note_no"] == "DN-2026-0007"
    assert row["note_date"] == "2026-09-07"
    assert row["amount"] == 8800.0
    # 签收面字段随单往返（对账依据不能丢）
    assert row["signed_by"] == "张三"
    assert row["warehouse_confirmed_by"] == "李四"
    assert row["qc_status"] == "released"


async def test_readback_is_tenant_scoped(canonical_env):
    """租户隔离：别的租户读不到本租户的费用事实（信封 tenant_id 须与请求一致）。"""
    registry = build_default_registry()
    published = await registry.call(
        "m0_expenses_import",
        {"records": [_envelope("expense", "EXP-T1", {"category": "tax", "amount": 1.0,
                                                     "period": "2026-09"})]},
        CTX)
    assert published["success"] is True

    other = await registry.call("list_expenses", {"period": "2026-09"},
                                {**CTX, "tenant_id": "tenant-b"})
    assert other["data"]["expenses"] == []

    mismatched = await registry.call(
        "m0_expenses_import",
        {"records": [{**_envelope("expense", "EXP-T2", {"category": "tax", "amount": 2.0,
                                                        "period": "2026-09"}),
                      "tenant_id": "tenant-b"}]},
        CTX)
    assert mismatched["success"] is False       # 信封租户与请求租户不一致 → 拒收
