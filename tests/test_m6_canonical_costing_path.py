"""真 canonical → M6 装配 → 成本的**链路**测试（F-008）。

## 为什么单开一个文件

M6 现有的成本测试（`test_m6_cost` / `test_m6_cost_tools` / `test_m6_costing_tools`）
里的 `bom_lines` / `routing_steps` **全是注入的 fixture**——从来没有验证过
「从真 canonical 读出来、再喂给 `_assemble_costing_facts`」这条路。而这条路上的两个
读口在 2026-09-13 被实测出**读不到任何工序**（`route_steps=0`；BOM 的 `lines` 因为在
业务体顶层而正常）。

根因：`fact_gateway.read_entities` 只展开了信封**外层**的 `attributes`，没展开
`payload.attributes`——而 SOP 文档的 `route_steps` 恰好落在后者
（形状见 `business_catalog.py:1134-1139`）。修复在**在途分支**
`feat/migration-integration-20260909`（`read_entities` 增展开内层），**尚未合入 main**。

## 三个测试的分工

- `test_real_canonical_bom_*`：**现在就跑并应通过**。BOM 那条路今天就是通的，它的
  作用是**证明这套夹具确实是"真 canonical"而不是又一份 fixture**——否则下面那条
  skip 掉的测试无从判断对错。
- `test_real_canonical_sop_route_steps_*`：**依赖上面那个修复**，故标 skip；合并后
  去掉 skip 即可验证，**断言无需改动**。
- `test_kernel_*`：钉住"字段拼写读不到时绝不编造"这条红线性质，与依赖无关。

## 不做什么

**不**为了让断言变绿而把 `route_steps` 直接塞在业务体顶层——那会变成"验一个不存在的
形状"，正是本文件要防的事。真实形状必须是 `payload.attributes.route_steps`。
"""

from __future__ import annotations

import hashlib

import pytest

from yunpai_orchestrator.m0_catalog_ingest import CatalogService
from yunpai_orchestrator.m6_cost import compute_process_cost
from yunpai_orchestrator.orchestration_bridge import _assemble_costing_facts

PRODUCT = "TX-001"
TENANT = "default"

#: 依赖：在途分支对 `fact_gateway.read_entities` 的修复（展开 payload.attributes）。
DEPENDENCY_REASON = (
    "依赖在途分支 feat/migration-integration-20260909 对 fact_gateway.read_entities "
    "的修复（展开信封内 payload.attributes，使 SOP 的 route_steps 可读；"
    "该分支尚未合入 main）。合入并 rebase 后删掉本 skip 即可验证，断言无需改动。"
)


def _envelope(entity_type: str, business_key: str, payload: dict,
              *, version_id: str | None = None) -> dict:
    """`m0.ingest.v1` 信封（必填项对齐 `m0_catalog_ingest.validate_records`）。"""
    identity: dict = {"business_key": business_key}
    if version_id:
        identity["version_id"] = version_id
    return {
        "schema_version": "m0.ingest.v1",
        "tenant_id": TENANT,
        "idempotency_key": f"idem-{entity_type}-{business_key}",
        "source": {"system": "m6-canonical-path-test",
                   "external_id": f"ext-{entity_type}-{business_key}",
                   "sha256": hashlib.sha256(f"{entity_type}:{business_key}".encode()).hexdigest()},
        "entity_type": entity_type,
        "identity": identity,
        "payload": payload,
        "evidence": [{"key": "ev-1", "ref": f"test:{entity_type}:{business_key}"}],
        "review_status": "approved",
        "reviewed_by": "tester",
    }


#: BOM：`lines` 在业务体**顶层**（真实形状，`business_catalog.py` 同构）。
BOM_RECORD = _envelope("bom", PRODUCT, {
    "product_code": PRODUCT,
    "lines": [
        {"line_no": 1, "material_code": "MAT-A", "material_name": "A 料",
         "quantity_per": 2, "uom": "pcs", "loss_rate": 0.03},
        {"line_no": 2, "material_code": "MAT-B", "material_name": "B 料",
         "quantity_per": 1, "uom": "pcs", "loss_rate": 0},
    ],
}, version_id="BOM-V1")

#: SOP：`route_steps` 在 `payload.attributes` 下（**这是需要那层展开的原因**），
#: 工时字段拼写为 `standard_time`（真实产物，`business_catalog.py:1123-1132`）。
SOP_RECORD = _envelope("document", f"{PRODUCT}-sop", {
    "role": "sop", "title": f"{PRODUCT} SOP", "product_codes": [PRODUCT],
    "content_uri": f"sha256:{'0' * 64}", "status": "active",
    "attributes": {
        "route_steps": [
            {"sequence_no": 1, "operation_code": "TX-001-OP-01", "operation_name": "排卡",
             "standard_time": "90", "station_code": "排卡",
             "equipment_codes": [], "tooling_codes": []},
            {"sequence_no": 2, "operation_code": "TX-001-OP-02", "operation_name": "组装",
             "standard_time": "30", "station_code": "组装",
             "equipment_codes": [], "tooling_codes": []},
        ],
        "time_source": "source_not_provided", "source_batch": "B-A1",
    },
}, version_id="SOP-TX001-A1")


@pytest.fixture
def canonical_env(tmp_path, monkeypatch):
    """canonical 库 = 临时 sqlite；不配 `M0_URL`（读口走本地库）。"""
    db = tmp_path / "m0-canonical.sqlite"
    monkeypatch.setenv("YUNPAI_M0_DB", str(db))
    monkeypatch.delenv("M0_URL", raising=False)
    return db


def _publish(db, *records: dict) -> None:
    """用**真实发布入口**写库（与 G3 候选发布同一入口，非测试自建表）。"""
    result = CatalogService(str(db)).publish_records(
        list(records), tenant_id=TENANT, task_id="TASK-M6-CANONICAL-PATH", actor="tester")
    assert result["published"] == len(records), result


def _state() -> dict:
    return {"tenant_id": TENANT, "request": {"product_code": PRODUCT}}


def test_real_canonical_bom_feeds_costing_assembly(canonical_env):
    """BOM 行能从**真 canonical** 读进装配（这条路今天就是通的）。

    本测试的存在意义：证明 `canonical_env` + `CatalogService.publish_records` +
    `_assemble_costing_facts` 这套夹具确实走的是真库真读口——否则下面那条被 skip 的
    SOP 测试就算将来红了，也分不清是"依赖没到"还是"夹具本身就不对"。
    """
    _publish(canonical_env, BOM_RECORD)

    facts = _assemble_costing_facts(_state(), PRODUCT)
    assert facts is not None
    assert [line["material_code"] for line in facts["bom_lines"]] == ["MAT-A", "MAT-B"]
    assert facts["bom_lines"][0]["quantity_per"] == 2
    assert facts["bom_lines"][0]["loss_rate"] == 0.03


@pytest.mark.skip(reason=DEPENDENCY_REASON)
def test_real_canonical_sop_route_steps_feed_costing_assembly(canonical_env):
    """工序能从真 canonical 读进装配（`payload.attributes.route_steps` 那层展开）。

    这是 A1 的核心验收点：`_route_steps_from_entities` 读 `payload.route_steps`，而真实
    SOP 把它放在 `payload.attributes` 下——只有 `read_entities` 展开内层之后才读得到。
    修复前这里会是 `[]`（实测 `route_steps=0`）。
    """
    _publish(canonical_env, SOP_RECORD)

    facts = _assemble_costing_facts(_state(), PRODUCT)
    assert facts is not None
    assert [step["operation_code"] for step in facts["routing_steps"]] == [
        "TX-001-OP-01", "TX-001-OP-02",
    ]


def test_kernel_never_fabricates_hours_when_canonical_spelling_differs():
    """真实字段拼写读不到时，**绝不把工时当 0 静默算过去**。

    真实 SOP 工序的工时拼写是 `standard_time`（秒），而 `compute_process_cost` 只认
    `standard_minutes` / `standard_time_minutes`。两者对不上时该工序必须进
    `incomplete_steps`、整项标 `cost_incomplete`——**不许编造工时**（红线：缺数标不完整）。

    这条性质与上一条依赖**无关**：无论将来是否在装配层接工时归一化，只要字段取不到，
    结论都必须仍是"算不全"。仓库里已有现成的归一化函数
    （`orchestration_bridge._standard_minutes_of`，含"按秒 /60"口径，M5 路径在用），
    若日后要接线，应在此测试**之外**新增断言，而不要放松本测试。
    """
    step = {
        "sequence_no": 1, "operation_code": "TX-001-OP-01", "operation_name": "排卡",
        "standard_time": "90",                      # ← canonical SOP 的真实拼写
        "station_code": "排卡", "equipment_codes": [], "tooling_codes": [],
    }
    result = compute_process_cost([step], hour_rate=60, overhead_rate=20)

    assert result["total_standard_hours"] == 0.0
    assert result["unit_labor_cost"] == 0.0
    assert result["cost_incomplete"] is True
    assert [item["reason"] for item in result["incomplete_steps"]] == ["missing_standard_minutes"]
