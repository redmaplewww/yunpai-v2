"""B6 `list_orders` + 工资两件（`calculate_piece_pay` / `calculate_monthly_pay`）验收测试。

## 本文件锁什么

- **订单**：canonical `order` 读得到（**拆信封**）＋ 过滤面生效 ＋ 空集合不编造；
- **计件**：Σ(合格数 × 单价)，`quantity_report − scrap`；缺单价的报工进 `missing`
  （**不按 0 计、不编造单价**）；
- **不拿别的实体顶替报工**：老仓明确禁止把资产使用数量 `quantity` 当报工数量 →
  本文件锁「canonical `production_daily_report` **不会**被当作报工事实」；
- **月工资口径痕**：加班倍数/计薪天数/每日工时未给时取口径层默认并标 `assumed=true`；
- **口径缺省不静默归零**（回归）：未给口径时**必须**用默认值算，不能因为传了 `None`
  让内核把时薪算成 0（那会让加班费/缺勤扣款静默变成 0 = 工资算错）。
"""

from __future__ import annotations

import hashlib

import pytest

from yunpai_orchestrator.m0_catalog_ingest import CatalogService
from yunpai_orchestrator.orchestration_bridge import bridge_payload
from yunpai_orchestrator.registry import build_default_registry

CTX = {"task_id": "TASK-M6-PAYROLL", "tenant_id": "default", "actor": "tester"}

EVENTS = [
    {"worker_id": "W-01", "station_code": "排卡", "product_code": "TX-001",
     "quantity_report": 100, "scrap": 4, "report_date": "2026-09-10"},
]
RATES = [
    {"station_code": "排卡", "product_code": "TX-001", "unit_rate": 2.5,
     "effective_from": "2026-09-01"},
]


def _envelope(entity_type: str, business_key: str, payload: dict,
              *, version_id: str | None = None) -> dict:
    identity: dict = {"business_key": business_key}
    if version_id:
        identity["version_id"] = version_id
    return {
        "schema_version": "m0.ingest.v1",
        "tenant_id": "default",
        "idempotency_key": f"idem-{entity_type}-{business_key}",
        "source": {"system": "m6-payroll-test",
                   "external_id": f"ext-{entity_type}-{business_key}",
                   "sha256": hashlib.sha256(f"{entity_type}:{business_key}".encode()).hexdigest()},
        "entity_type": entity_type,
        "identity": identity,
        "payload": payload,
        "evidence": [{"key": "ev-1", "ref": f"test:{entity_type}:{business_key}"}],
        "review_status": "approved",
        "reviewed_by": "tester",
    }


@pytest.fixture
def canonical_env(tmp_path, monkeypatch):
    db = tmp_path / "m0-canonical.sqlite"
    monkeypatch.setenv("YUNPAI_M0_DB", str(db))
    monkeypatch.delenv("M0_URL", raising=False)
    return db


# ---------------------------------------------------------------------------
# 计件工资
# ---------------------------------------------------------------------------

async def test_piece_pay_is_good_quantity_times_rate():
    registry = build_default_registry()
    result = await registry.call("calculate_piece_pay",
                                 {"report_events": EVENTS, "piece_rates": RATES}, CTX)
    assert result["success"] is True, result.get("errors")
    data = result["data"]
    # 合格数 = 100 − 4 = 96；96 × 2.5 = 240
    assert data["details"][0]["good_qty"] == 96.0
    assert data["details"][0]["amount"] == 240.0
    assert data["totals"] == [{"worker_id": "W-01", "piece_pay": 240.0}]
    assert data["cost_incomplete"] is False
    assert data["piece_rate_source"] == "explicit"


async def test_piece_pay_marks_missing_rate_instead_of_counting_it_as_zero():
    """缺单价的报工进 missing，且**不混进合计**（不是"按 0 元发"）。"""
    registry = build_default_registry()
    events = EVENTS + [
        {"worker_id": "W-02", "station_code": "不存在的工站", "product_code": "TX-001",
         "quantity_report": 50, "scrap": 0, "report_date": "2026-09-10"},
    ]
    result = await registry.call("calculate_piece_pay",
                                 {"report_events": events, "piece_rates": RATES}, CTX)
    data = result["data"]
    assert data["cost_incomplete"] is True
    assert [row["reason"] for row in data["missing"]] == ["missing_piece_rate"]
    assert data["missing"][0]["worker_id"] == "W-02"
    # W-02 没有按 0 元进合计——合计里只有 W-01
    assert [row["worker_id"] for row in data["totals"]] == ["W-01"]


async def test_piece_pay_without_rates_returns_nothing_but_missing():
    """单价事实集缺失时：不编造、不给 0 工资，显式标记事实缺失。"""
    registry = build_default_registry()
    result = await registry.call("calculate_piece_pay", {"report_events": EVENTS}, CTX)
    data = result["data"]
    assert data["totals"] == []
    assert data["piece_rate_source"] == "missing"
    assert data["cost_incomplete"] is False
    assert data["facts_present"] is False
    assert data["missing"] == [{"reason": "missing_salary_facts"}]


async def test_canonical_daily_report_is_not_substituted_as_report_events(monkeypatch):
    """**不拿别的实体顶替**：即便 canonical 里存在 `production_daily_report`（带 quantity），
    也**不得**把它当报工事实——那是**生产日报的数量**，不是报工数量（老仓
    `_m6_report_events_from_usage_logs` 原文：「usage_log 的普通 quantity 是资产使用数量，
    不能被当作报工数量」）。v2 既没有 `usage_log` 面，就不许从日报推断，否则会算出错误的
    计件工资（错发/少发工资）。

    喂的是 canonical 读口的真实产出形状；`production_daily_report` 同样不在
    `m0.ingest.v1` 的 `ENTITY_TYPES` 白名单里，只能由 M0 导入批次路径写入。
    """
    registry = build_default_registry()
    canned = [{"canonical_key": "PDR-2026-09-10-TX001",
               "payload": {"date": "2026-09-10", "product_code": "TX-001",
                           "quantity": 100, "total_quantity": 100}}]
    monkeypatch.setattr("yunpai_orchestrator.orchestration_bridge._read_m0_entities",
                        lambda state, entity_type: canned)

    state = {"tenant_id": "default", "request": {}}
    payload = bridge_payload(state, "calculate_piece_pay")
    # 装配层**根本不读** canonical 报工（键缺省 = 没有事实），更不会拿日报顶替
    assert payload.get("report_events") in (None, []), \
        "报工事实不得从 canonical 生产日报推断"
    assert "report_events" not in payload, "装配产出不该带 None 键（契约是 array）"

    result = await registry.call("calculate_piece_pay",
                                 {**payload, "piece_rates": RATES}, CTX)
    assert result["data"]["totals"] == []
    assert result["data"]["report_event_count"] == 0


# ---------------------------------------------------------------------------
# 月工资
# ---------------------------------------------------------------------------

async def test_monthly_pay_uses_default_rates_with_assumed_marks():
    registry = build_default_registry()
    result = await registry.call("calculate_monthly_pay", {
        "salary_standards": {"W-01": 8700},
        "attendance": {"W-01": {"overtime_hours": 10, "absence_hours": 2}},
        "piece_pay": {"W-01": 500},
    }, CTX)
    row = result["data"]["rows"][0]
    # 时薪 = 8700 ÷ 21.75 ÷ 8 = 50；加班费 = 50 × 1.5 × 10 = 750；扣款 = 50 × 2 = 100
    assert row["overtime_pay"] == 750.0
    assert row["absence_deduct"] == 100.0
    assert row["piece_pay"] == 500.0
    assert row["gross_pay"] == 8700.0 + 750.0 - 100.0 + 500.0
    assumptions = result["data"]["assumptions"]
    assert assumptions["overtime_multiplier_assumed"] is True
    assert assumptions["work_days_assumed"] is True
    assert assumptions["hours_per_day_assumed"] is True
    assert assumptions["pending_finance_confirmation"]


async def test_monthly_pay_never_silently_zeroes_the_hourly_rate():
    """**回归锁**：口径未给时必须用默认值算，**不能传 None 进去**——那会让内核把时薪
    算成 0（`days`/`hours` 为 0 时 `hourly=0`），于是加班费与缺勤扣款静默全为 0，
    工资被算错却标着"成功"。"""
    registry = build_default_registry()
    result = await registry.call("calculate_monthly_pay", {
        "salary_standards": {"W-01": 8700},
        "attendance": {"W-01": {"overtime_hours": 10, "absence_hours": 0}},
    }, CTX)
    row = result["data"]["rows"][0]
    assert row["overtime_pay"] > 0, "口径缺省被静默归零：加班费算成了 0"
    assert row["overtime_pay"] == 750.0
    assert result["data"]["overtime_multiplier"] == 1.5
    assert result["data"]["work_days"] == 21.75
    assert result["data"]["hours_per_day"] == 8


async def test_monthly_pay_explicit_rates_clear_the_assumed_marks():
    registry = build_default_registry()
    result = await registry.call("calculate_monthly_pay", {
        "salary_standards": {"W-01": 8700},
        "attendance": {"W-01": {"overtime_hours": 10, "absence_hours": 0}},
        "overtime_multiplier": 2.0,
        "work_days": 20,
        "hours_per_day": 10,
    }, CTX)
    row = result["data"]["rows"][0]
    # 时薪 = 8700 ÷ 20 ÷ 10 = 43.5；加班费 = 43.5 × 2 × 10 = 870
    assert row["overtime_pay"] == 870.0
    assumptions = result["data"]["assumptions"]
    assert assumptions["overtime_multiplier_assumed"] is False
    assert assumptions["work_days_assumed"] is False
    assert assumptions["hours_per_day_assumed"] is False


async def test_monthly_pay_marks_missing_salary_instead_of_guessing():
    registry = build_default_registry()
    result = await registry.call("calculate_monthly_pay", {
        "salary_standards": {"W-01": 8700, "W-02": None},
        "attendance": {"W-01": {"overtime_hours": 0, "absence_hours": 0}},
    }, CTX)
    data = result["data"]
    assert data["cost_incomplete"] is True
    assert data["incomplete"] == [{"worker_id": "W-02", "reason": "missing_salary"}]
    assert [row["worker_id"] for row in data["rows"]] == ["W-01"]


# ---------------------------------------------------------------------------
# 订单列表
# ---------------------------------------------------------------------------

async def test_list_orders_reads_canonical_order_through_the_read_path(canonical_env):
    registry = build_default_registry()
    published = CatalogService(str(canonical_env)).publish_records([
        _envelope("order", "SO-2026-001",
                  {"order_id": "SO-2026-001", "product_code": "TX-001", "product_name": "HDMI 线",
                   "quantity": 1000, "due_date": "2026-09-30", "customer_name": "客户甲"}),
        _envelope("order", "SO-2026-002",
                  {"order_id": "SO-2026-002", "product_code": "TX-002", "product_name": "排卡",
                   "quantity": 500, "due_date": "2026-10-15", "customer_name": "客户乙"}),
    ], tenant_id="default", task_id="TASK-M6-PAYROLL", actor="tester")
    assert published["published"] == 2, published

    state = {"tenant_id": "default", "request": {}}
    payload = bridge_payload(state, "list_orders")
    result = await registry.call("list_orders", payload, CTX)
    assert result["success"] is True, result.get("errors")
    data = result["data"]
    assert data["count"] == 2
    assert data["source"] == "canonical"
    by_id = {row["order_id"]: row for row in data["orders"]}
    assert by_id["SO-2026-001"]["quantity"] == 1000
    assert by_id["SO-2026-001"]["customer_name"] == "客户甲"
    # canonical 订单没有独立行实体 → line_count 恒 0（不假装有行）
    assert by_id["SO-2026-001"]["line_count"] == 0


async def test_list_orders_filters_and_flags_empty_canonical(canonical_env):
    registry = build_default_registry()
    CatalogService(str(canonical_env)).publish_records([
        _envelope("order", "SO-2026-001",
                  {"order_id": "SO-2026-001", "product_code": "TX-001", "quantity": 1000,
                   "due_date": "2026-09-30", "customer_name": "客户甲"}),
    ], tenant_id="default", task_id="TASK-M6-PAYROLL", actor="tester")

    state = {"tenant_id": "default", "request": {}}
    base = bridge_payload(state, "list_orders")

    by_product = await registry.call("list_orders", {**base, "product_code": "TX-999"}, CTX)
    assert by_product["data"]["count"] == 0

    by_period = await registry.call("list_orders", {**base, "period": "2026-10"}, CTX)
    assert by_period["data"]["count"] == 0

    hit_period = await registry.call("list_orders", {**base, "period": "2026-09"}, CTX)
    assert hit_period["data"]["count"] == 1

    # 空 canonical：返回空 + missing（不编造订单）
    empty = await registry.call("list_orders", {"orders": []}, CTX)
    assert empty["data"]["orders"] == []
    assert empty["data"]["missing"] == [{"reason": "missing_orders"}]
