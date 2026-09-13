"""B0b（F-008）：expense / delivery_note 实体的接入面——白名单、契约、门、注册与绑定。

来源：老仓 `tests/test_m6_expense.py` 的 registry/M0Store 端到端用例
（`test_expense_import_validates_required_fields` / `test_delivery_note_import_accepts_signoff_fields`）
在 v2 的等价实现。**本文件覆盖"接入面"**；带 `m0.ingest.v1` 完整信封的 canonical
导入→回读往返（publish + list_expenses 读回）属下一步，见文件末尾说明。

红线：manifest 声明与 RULES 表必须一一对应——只声明不登记会变成「有门不生效」
（reviewer_check_node 只消费 RULES 表，见 rules.py 顶部注释）。
"""

from __future__ import annotations

import json
from pathlib import Path

from yunpai_orchestrator import binding
from yunpai_orchestrator.canonical_schema import CANONICAL_SCHEMA
from yunpai_orchestrator.m0_catalog_ingest import ENTITY_TYPES
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.reviewer import rules as rules_mod

M0_MANIFEST = Path(__file__).resolve().parents[1] / "registry-manifests" / "m0.json"

#: 写入面：工具 → canonical entity_type（facade 单类型闸门）
WRITE_TOOLS: dict[str, str] = {
    "m0_expenses_import": "expense",
    "m0_delivery_notes_import": "delivery_note",
}

#: 查询面（只读）
READ_TOOLS: tuple[str, ...] = ("list_expenses", "list_delivery_notes")


def _manifest_tool(name: str) -> dict:
    data = json.loads(M0_MANIFEST.read_text(encoding="utf-8"))
    return next(tool for tool in data["tools"] if tool["name"] == name)


def test_ingest_whitelist_and_schema_include_m6_entities():
    """老仓把 expense/delivery_note 留在 facade 层（源分裂）——两处白名单都要收口。"""
    for entity_type in WRITE_TOOLS.values():
        assert entity_type in ENTITY_TYPES, f"{entity_type} 不在 m0.ingest.v1 白名单"
        assert entity_type in CANONICAL_SCHEMA, f"{entity_type} 不在 CANONICAL_SCHEMA"


def test_manifest_declares_write_tools_with_candidate_gate():
    for tool, entity_type in WRITE_TOOLS.items():
        contract = _manifest_tool(tool)
        assert contract["side_effect"] == "external_write", tool
        assert contract["review_gate"] == "candidate", tool
        assert contract["execution"] == "sync", tool


def test_manifest_declares_read_tools_as_sync_reads():
    for tool in READ_TOOLS:
        contract = _manifest_tool(tool)
        assert contract["execution"] == "sync", tool
        # 读工具不得声明写副作用（否则 check_contracts W1/W2 会抓）
        assert contract.get("side_effect") in (None, "none"), tool
        assert "limit" in contract["input_schema"]["properties"], tool


def test_candidate_gate_registered_in_rules_table():
    """门不是只写进 manifest：必须在 RULES 表登记，否则「有门不生效」。"""
    for tool in WRITE_TOOLS:
        checks = rules_mod.RULES.get(tool)
        assert checks, f"{tool} 未登记 RULES 条目（有门不生效）"
        assert any(check.action == "gate:candidate" for check in checks), tool


def test_m6_tools_registered_and_bound_local():
    registry = build_default_registry()
    bindings = binding.compute_bindings(registry)
    for tool in (*WRITE_TOOLS, *READ_TOOLS):
        assert tool in registry.specs, f"{tool} 未注册"
        assert tool in registry.handlers, f"{tool} 无本地 handler"
        assert bindings[tool] == binding.BindingStatus.BOUND_LOCAL, f"{tool} 绑定态={bindings[tool]}"


async def test_facade_rejects_mixed_entity_type_batch():
    """facade 单类型闸门：混批必须在装配前被拒（不写半批）。"""
    registry = build_default_registry()
    result = await registry.call(
        "m0_expenses_import",
        {"records": [
            {"entity_type": "expense", "category": "electricity", "amount": 1.0, "period": "2026-09"},
            {"entity_type": "delivery_note", "note_no": "DN-1"},
        ]},
        {"task_id": "T-M6-B0B", "tenant_id": "default"},
    )
    assert result["success"] is False
    assert result["code"] == "ENTITY_TYPE_MISMATCH"


# 下一步（B0b 收口）：带 `m0.ingest.v1` 完整信封
# （schema_version/entity_type/tenant_id/idempotency_key/source{system,external_id,sha256}/identity.business_key）
# 的 canonical 导入 → candidate 门批准 → `list_expenses` / `list_delivery_notes` 回读往返。
# 该往返需要 v2 的 publish 链（CatalogService.validate_records → publish），
# 与 B1 的 finance 门接线同批完成，届时补 `test_m6_entities_roundtrip.py`。
