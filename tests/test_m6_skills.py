"""B7：M6 两个 Skill（`yunpai-m6-finance` 算 / `yunpai-m6-ledger` 记）。

本文件锁住五条，都是"文档写着但没人验"的地方：

1. **覆盖与归属**：两个 operation map 的并集 == `registry-manifests/m6.json` 的 24 件
   （无孤儿工具、无幻觉工具），且两个 map **不相交**（算/记是划分，不是重叠）；
2. **default 必为纯读**：漏写 `operation` 时绝不可能触发写库或开门（写工具只能被显式点名）；
3. **红线在代码级**：`yunpai-m6-finance` 连自己侧的写工具都调不到，两个 Skill 都调不到
   M0/M5 的工具——白名单外的 `tool` 直接 `ValueError`（不靠文档自律）；
4. **G2 载荷口径**：顶层 `product_code`（以及 `files` 别名）按 G2 并入后能一路到工具入参，
   业务参数走 `tool_payload`／平铺都行；
5. **文档不漂移**：每个 map 里的工具与 operation 都必须出现在同级
   `references/tools.md` 里（v2 的单份 manifest 没有老仓那套溯源哈希，这条是它的等价漂移门）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from yunpai_orchestrator.m6_tooling import (
    M6_FINANCE_SKILL_OPERATION_MAP,
    M6_LEDGER_SKILL_OPERATION_MAP,
)
from yunpai_orchestrator.m6_store import M6Store
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.skills import build_default_skill_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"
PERIOD = "2026-09"

#: 与 `test_m6_cost_tools.py` 同一组事实：材料 5*2*1.1 + 采购 8.5*1 + 采购 3.2*4 = 32.3，
#: 人工 1h*50 + 制费 1h*20 = 70 → 单台 102.3（跨文件同数：同口径换条路走仍得同一数）。
BOM_LINES = [
    {"material_code": "MAT-A", "qty_per": 2, "unit_price": 5.0, "loss_rate": 0.1},
    {"material_code": "MAT-B", "qty_per": 1, "unit_price": 8.0, "loss_rate": 0.0},
    {"material_code": "MAT-C", "qty_per": 4, "unit_price": 3.0, "loss_rate": 0.0},
]
ROUTING_STEPS = [{"operation_id": "OP-10", "standard_minutes": 60}]
INVENTORY = [{"material_code": "MAT-A", "available_qty": 100, "stock_class": "raw"}]
TRACKING_ROWS = [
    {"id": 7, "purchase_order_no": "PO-1", "purchase_order_item_id": 21,
     "supplier_name": "供应商一", "promised_date": "2026-09-01", "unit_price": "8.5",
     "currency": "CNY"},
    {"id": 8, "purchase_order_no": "PO-2", "purchase_order_item_id": 22,
     "promised_date": "2026-09-02", "unit_price": "3.2", "currency": "CNY"},
]
ORDER_ITEMS = [{"id": 21, "item_code": "MAT-B", "internal_material_no": "IM-B"},
               {"id": 22, "item_code": "MAT-C", "internal_material_no": "IM-C"}]


def _ctx(tmp_path, **extra):
    return {"tenant_id": "default", "task_id": "task-m6-skill",
            "m6_db_path": str(tmp_path / "m6.sqlite"), **extra}


def _cost_payload(**overrides):
    payload = {"product_code": "W-H913", "bom_lines": BOM_LINES,
               "routing_steps": ROUTING_STEPS, "inventory": INVENTORY,
               "hour_rate": 50, "overhead_rate": 20,
               "purchase_tracking_rows": TRACKING_ROWS,
               "purchase_order_items": ORDER_ITEMS}
    payload.update(overrides)
    return payload


class _CapturingRegistry:
    """真 registry 的 specs + 只记录调用的假 call（不执行 handler）。"""

    def __init__(self, specs: dict) -> None:
        self.specs = specs
        self.calls: list[tuple[str, dict, dict]] = []

    async def call(self, name: str, payload: dict, context: dict) -> dict:
        self.calls.append((name, dict(payload), dict(context)))
        return {"success": True, "status": "ok", "code": "", "data": {},
                "errors": [], "invoked_tools": [name]}


def _skill_dir(skill_name: str) -> Path:
    return SKILLS_DIR / skill_name.replace("yunpai-", "")


# ---------------------------------------------------------------------------
# 1. 覆盖与归属
# ---------------------------------------------------------------------------

def _manifest_tools() -> set[str]:
    data = json.loads((REPO_ROOT / "registry-manifests" / "m6.json").read_text(encoding="utf-8"))
    return {str(item["name"]) for item in data["tools"]}


def test_two_m6_skills_registered_with_mapped_tool_faces():
    registry = build_default_skill_registry(build_default_registry())
    finance = registry.specs["yunpai-m6-finance"]
    ledger = registry.specs["yunpai-m6-ledger"]
    assert set(finance.tools) == set(M6_FINANCE_SKILL_OPERATION_MAP.values())
    assert set(ledger.tools) == set(M6_LEDGER_SKILL_OPERATION_MAP.values())
    # 声明的工具必须真在 ToolRegistry 里（否则 validate_tools 会抛）
    registry.validate_tools(build_default_registry().specs)


def test_operation_maps_cover_every_m6_manifest_tool_without_overlap():
    """并集 == m6.json 的 24 件；两 map 不相交（算/记是划分）。"""
    finance = set(M6_FINANCE_SKILL_OPERATION_MAP.values())
    ledger = set(M6_LEDGER_SKILL_OPERATION_MAP.values())
    assert finance & ledger == set(), f"同一工具被两个 Skill 都暴露：{finance & ledger}"
    assert finance | ledger == _manifest_tools()


def test_modules_partition_matches_skill_purpose():
    """算侧全是 `side_effect=none` 的纯算数读；写工具只能落在记侧。"""
    specs = build_default_registry().specs
    for tool in M6_FINANCE_SKILL_OPERATION_MAP.values():
        assert str(getattr(specs[tool], "side_effect", "") or "none") == "none", tool
    assert "save_costing_snapshot" in M6_LEDGER_SKILL_OPERATION_MAP.values()
    assert "upsert_asset_ledger" in M6_LEDGER_SKILL_OPERATION_MAP.values()


# ---------------------------------------------------------------------------
# 2. default 必为纯读
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op_map", [M6_FINANCE_SKILL_OPERATION_MAP, M6_LEDGER_SKILL_OPERATION_MAP])
def test_default_operation_is_read_only(op_map):
    specs = build_default_registry().specs
    default_tool = op_map["default"]
    assert str(getattr(specs[default_tool], "side_effect", "") or "none") == "none", (
        f"default 落到有副作用的工具上：{default_tool}——漏写 operation 就可能写库/开门")


async def test_ledger_without_operation_only_reads(tmp_path):
    """不点名 operation → 只读，库里一行不写（写工具必须被显式点名）。"""
    ctx = _ctx(tmp_path)
    registry = build_default_skill_registry(build_default_registry())
    result = await registry.call("yunpai-m6-ledger", {}, ctx)
    assert result["invoked_tool"] == "list_costing_snapshots"
    assert M6Store(ctx["m6_db_path"]).list_snapshots() == []


# ---------------------------------------------------------------------------
# 3. 红线在代码级（白名单外一律拒绝）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("skill_name,foreign_tool", [
    # 算侧调不到自己侧的写工具（"算"根本不写库；要写请走 ledger）
    ("yunpai-m6-finance", "save_costing_snapshot"),
    # 记侧调不到算侧工具（两侧是划分，不互相兜底）
    ("yunpai-m6-ledger", "get_product_cost"),
    # 两个 Skill 都不写 M0/M5 —— 红线「本 Skill 不写 M0/M5；写自己的 m6 库」
    ("yunpai-m6-finance", "m0_expenses_import"),
    ("yunpai-m6-ledger", "m0_delivery_notes_import"),
    ("yunpai-m6-finance", "solve_scheduling"),
    ("yunpai-m6-ledger", "dispatch_m5_schedule"),
    # 也不碰 M0 的读面（送货单只经 M6 自己的 get_delivery_note 读）
    ("yunpai-m6-ledger", "list_expenses"),
])
async def test_skill_refuses_any_tool_outside_its_map(skill_name, foreign_tool):
    registry = build_default_skill_registry(build_default_registry())
    with pytest.raises(ValueError, match="not allowed"):
        await registry.call(skill_name, {"tool": foreign_tool}, _ctx(Path(".")))


async def test_unknown_operation_is_refused():
    registry = build_default_skill_registry(build_default_registry())
    with pytest.raises(ValueError, match="not allowed"):
        await registry.call("yunpai-m6-finance", {"operation": "no_such_operation"}, _ctx(Path(".")))


# ---------------------------------------------------------------------------
# 4. G2 载荷口径
# ---------------------------------------------------------------------------

async def test_finance_dispatches_by_operation_and_forwards_business_args():
    fake = _CapturingRegistry(build_default_registry().specs)
    skills = build_default_skill_registry(fake)
    result = await skills.call(
        "yunpai-m6-finance",
        {"operation": "audit", "tool_payload": {"order_id": "SO-1", "order_lines": []}},
        {"task_id": "T-1", "tenant_id": "tenant-main"})

    tool, payload, _ctx = fake.calls[0]
    assert tool == "audit_order_cost"
    assert payload["order_id"] == "SO-1"
    assert result["invoked_tool"] == "audit_order_cost"
    assert result["skill"] == "yunpai-m6-finance"


async def test_g2_top_level_keys_reach_the_tool(tmp_path):
    """G2（在途 `worker/executor.py:skill_payload`）把顶层 product_code 并入载荷后，
    本 Skill 必须一路把它送到工具入参——否则「算一下 W-H913 一台多少钱」会缺参。"""
    registry = build_default_skill_registry(build_default_registry())
    ctx = _ctx(tmp_path)
    # 顶层 product_code 由 G2 并入 skill 载荷；业务事实平铺在载荷里（等价 tool_payload）
    payload = {"product_code": "W-H913", **_cost_payload()}
    result = await registry.call("yunpai-m6-finance",
                                 {"operation": "product_cost", **payload}, ctx)
    assert result["success"] is True, result.get("errors")
    assert result["data"]["product_code"] == "W-H913"
    assert result["data"]["unit_cost"] == 102.3


async def test_g2_files_alias_and_message_do_not_break_m6_tools(tmp_path):
    """G2 还会并入 `message`／`files`（附件别名）——M6 工具合同没有
    `additionalProperties:false`，这些键必须被容忍而不是把调用顶成 VALIDATION 失败。"""
    registry = build_default_skill_registry(build_default_registry())
    ctx = _ctx(tmp_path)
    payload = {"operation": "product_cost", **_cost_payload(),
               "message": "算一下这个产品一台多少钱", "files": [{"filename": "bom.xlsx"}]}
    result = await registry.call("yunpai-m6-finance", payload, ctx)
    assert result["success"] is True, result.get("errors")
    assert result["data"]["unit_cost"] == 102.3


async def test_ledger_save_runs_the_three_stage_propose_段(tmp_path):
    """经 Skill 走 propose 段：只写 trial 快照，且**刻意无门**（D-008，不报待确认事实）。"""
    registry = build_default_skill_registry(build_default_registry())
    ctx = _ctx(tmp_path)
    result = await registry.call("yunpai-m6-ledger",
                                 {"operation": "save_snapshot",
                                  **_cost_payload(), "period": PERIOD, "quantity": 1}, ctx)
    assert result["success"] is True, result.get("errors")
    assert result["data"]["status"] == "trial"
    assert "pending_confirmation" not in result["data"]     # 试算无门，不挂待确认（D-008）
    rows = M6Store(ctx["m6_db_path"]).list_snapshots()
    assert [row["status"] for row in rows] == ["trial"]      # 生效行只有 approve 之后才出现


async def test_ledger_confirm_only_requests_and_never_flips_status(tmp_path):
    """三段式的 commit **发起**：`confirm` 只回报 `pending_confirmation`，
    库里仍是 `trial`——翻正只在 `finance` 门批准后的 `_apply_m6_costing_confirm`。"""
    registry = build_default_skill_registry(build_default_registry())
    ctx = _ctx(tmp_path)
    saved = await registry.call("yunpai-m6-ledger",
                                {"operation": "save_snapshot",
                                 **_cost_payload(), "period": PERIOD, "quantity": 1}, ctx)
    snapshot_id = saved["data"]["snapshot_id"]

    confirm = await registry.call("yunpai-m6-ledger",
                                  {"operation": "confirm_snapshot",
                                   "snapshot_id": snapshot_id}, ctx)
    assert confirm["success"] is True, confirm.get("errors")
    assert confirm["data"]["pending_confirmation"] is True
    assert confirm["data"]["status"] == "trial"
    assert confirm["invoked_tool"] == "confirm_costing_snapshot"
    store = M6Store(ctx["m6_db_path"])
    assert store.get_snapshot(snapshot_id, "default")["status"] == "trial"
    summary = store.month_summary(PERIOD, "default")
    assert summary["snapshot_count"] == 0 and summary["total_cost"] == 0.0   # 试算不进汇总
    assert summary["trial_count"] == 1 and summary["trial_excluded"] is True


# ---------------------------------------------------------------------------
# 5. 文档不漂移（v2 的等价漂移门；老仓那套溯源哈希在 v2 不存在）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("skill_name,op_map", [
    ("yunpai-m6-finance", M6_FINANCE_SKILL_OPERATION_MAP),
    ("yunpai-m6-ledger", M6_LEDGER_SKILL_OPERATION_MAP),
])
def test_reference_docs_document_every_mapped_tool_and_operation(skill_name, op_map):
    text = (_skill_dir(skill_name) / "references" / "tools.md").read_text(encoding="utf-8")
    for tool in set(op_map.values()):
        assert f"### `{tool}`" in text, f"{skill_name}/references/tools.md 缺工具：{tool}"
    for operation in op_map:
        assert f"`{operation}`" in text, f"{skill_name}/references/tools.md 缺 operation：{operation}"


@pytest.mark.parametrize("skill_name", ["yunpai-m6-finance", "yunpai-m6-ledger"])
def test_skill_doc_declares_red_line_and_matches_registry_name(skill_name):
    skill_md = (_skill_dir(skill_name) / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(r"^---\n(.*?)\n---", skill_md, flags=re.DOTALL)
    assert match, "SKILL.md 缺少 frontmatter"
    name = re.search(r"^name:\s*(.+)$", match.group(1), flags=re.MULTILINE)
    assert name and name.group(1).strip() == skill_name
    assert "本 Skill 不写 M0/M5；写自己的 m6 库" in skill_md      # 红线逐字写进文档
    assert "references/tools.md" in skill_md
    assert "${" not in skill_md        # 模板占位符没被替换掉
