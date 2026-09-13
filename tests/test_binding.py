"""书二 §7.2：绑定状态表——路由目录只含已绑定工具（治痛点 5/7）。"""
import pytest

from yunpai_orchestrator.binding import (
    DEPRECATED_TOOLS,
    INTENTIONALLY_UNBOUND,
    ORCHESTRATION_INTERNAL,
    BindingStatus,
    CatalogView,
    compute_bindings,
    describe,
    visible_tool_names,
)
from yunpai_orchestrator.registry import build_default_registry


@pytest.fixture(scope="module")
def registry():
    return build_default_registry()


def test_specs_loaded_count(registry):
    """工具总数锁：115 manifest（V2-M3 注册基线）+ 4 本地识别件套 = 119；
    F-008（M6 财务，2026-09-13）B0b 新增 4 件（m0_expenses_import / m0_delivery_notes_import /
    list_expenses / list_delivery_notes）→ 123；B1 第二批新增 6 件成本账工具
    （save_costing_snapshot / confirm_costing_snapshot / close_month_costing /
    list_costing_snapshots / get_costing_snapshot / list_month_costing）→ 129；
    B1 第三批新增 3 件内核读工具（get_product_cost / audit_order_cost / allocate_expenses，
    纯算数不写库）→ 132；B2 新增 6 件单据台账工具（报价单 generate_quotation / save_quotation /
    list_quotations / get_quotation + 对账单 save_statement / list_statements）→ 138；
    B3 新增 2 件凭据工具（get_delivery_note / generate_statement）→ 140；
    B4 新增 3 件资产台账工具（get_asset_ledger / upsert_asset_ledger / compute_asset_benefit）→ 143。"""
    assert len(registry.specs) == 143


def test_deprecated_and_unbound_never_visible(registry):
    visible = set(visible_tool_names(registry))
    assert not (DEPRECATED_TOOLS & visible)
    assert not (INTENTIONALLY_UNBOUND & visible)
    assert not (ORCHESTRATION_INTERNAL & visible)
    assert "run_mrp_procurement_plan" not in visible  # legacy 名不得再误导 LLM


def test_local_only_tools_bound_local(registry):
    bindings = compute_bindings(registry)
    for name in ("sample_file", "ingest_recognized", "query_recognized_table", "ingest_canonical"):
        assert registry.specs.get(name) is not None, f"{name} 必须有合同（local.json）"
        assert bindings[name] == BindingStatus.BOUND_LOCAL


def test_sandbox_marked_and_counted(registry):
    """SANDBOX 名单已清空：M4 import_json 换成真实实现后回归 BOUND_LOCAL（P0）。

    依据：rows-S5.md:67（V2 `binding.py:34` 列 SANDBOX → 替换后须移出并回归
    BOUND_LOCAL）；INFRA-DECISIONS §6「M4 换成真实实现后**必须**同步改
    `binding.py:34` 与该断言」。
    """
    bindings = compute_bindings(registry)
    assert bindings["import_m4_purchase_suggestions_json"] == BindingStatus.BOUND_LOCAL
    summary = {k: len(v) for k, v in describe(registry).items()}
    assert summary["sandbox"] == 0  # 无本地假实现残留
    assert summary["bound_local"] >= 100  # 集成终值 100（M0–M5 全部分片落地，见 REPORT-MIG-INTEGRATION.md）
    assert summary["unbound"] == 6        # 终值 6（DEPRECATED 1 + INTENTIONALLY_UNBOUND 3 + ORCHESTRATION_INTERNAL 2）


def test_catalog_view_only_exposes_visible(registry):
    visible = set(visible_tool_names(registry))
    view = CatalogView(registry)
    assert set(view.specs) == visible
    assert "report_workload" not in view.specs
