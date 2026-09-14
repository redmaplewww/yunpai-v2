from __future__ import annotations

import re
from pathlib import Path

import pytest

from yunpai_orchestrator.orchestrator.router import SKILL_USAGE_ORDER
from yunpai_orchestrator.llm import QwenConfig, QwenRouter
from yunpai_orchestrator.m6_tooling import (
    M6_FINANCE_SKILL_OPERATION_MAP,
    M6_LEDGER_SKILL_OPERATION_MAP,
)
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.skills import (
    M1_SKILL_OPERATION_MAP,
    M2_SKILL_OPERATION_MAP,
    M3_SKILL_OPERATION_MAP,
    M4_SKILL_OPERATION_MAP,
    SkillRegistry,
    build_default_skill_registry,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"

#: 真源 operation map（直接 import 常量，消除双份白名单；PROMPT-INT2 §四）。
_REAL_OP_MAPS: dict[str, dict[str, str]] = {
    "yunpai-m1-document-parser": M1_SKILL_OPERATION_MAP,
    "yunpai-m2-bom-sop": M2_SKILL_OPERATION_MAP,
    "yunpai-m3-material-planning": M3_SKILL_OPERATION_MAP,
    "yunpai-m4-procurement": M4_SKILL_OPERATION_MAP,
    # M6 的两个 Skill 从一开始就导出常量（F-008 / B7），不进 _INLINE_OP_MAPS 镜像。
    "yunpai-m6-finance": M6_FINANCE_SKILL_OPERATION_MAP,
    "yunpai-m6-ledger": M6_LEDGER_SKILL_OPERATION_MAP,
}

#: M0/M5 的 operation map 目前仍内联在 handler 里（无导出常量），只能保留镜像；
#: 后续若抽成常量，应一并迁到 ``_REAL_OP_MAPS``。
_INLINE_OP_MAPS: dict[str, dict[str, str]] = {
    "yunpai-m0-data-foundation": {"default": "data_import_run", "ingest": "data_import_run", "preview": "data_import_preview", "resolve": "data_import_resolve", "commit": "data_import_commit"},
    "yunpai-m5-pmc": {"default": "solve_scheduling", "solve": "solve_scheduling", "schedule": "get_m5_schedule", "progress": "get_m5_pmc_progress", "contracts": "get_m5_integration_contracts", "readiness": "get_m5_material_readiness", "knowledge_search": "search_m5_knowledge", "knowledge_record": "record_m5_knowledge", "message_prepare": "prepare_m5_department_message", "message_get": "get_m5_department_message", "message_delivery": "get_m5_department_message_delivery", "advise": "advise_m5_schedule", "intelligent": "run_m5_intelligent_schedule", "procurement": "generate_m5_material_procurement_plan"},
    "yunpai-m5-pmc-lifecycle": {"default": "get_m5_schedule", "snapshot": "ingest_m5_planning_snapshot", "ingest": "ingest_m5_planning_snapshot", "schedule": "get_m5_schedule", "versions": "list_m5_schedules", "progress": "get_m5_pmc_progress", "replan": "replan_m5_schedule", "dispatch": "dispatch_m5_schedule", "execution": "get_m5_execution_summary"},
}


def _frontmatter_name(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^---\n(.*?)\n---", text, flags=re.DOTALL)
    assert match, f"SKILL.md 缺少 frontmatter: {path}"
    name = re.search(r"^name:\s*(.+)$", match.group(1), flags=re.MULTILINE)
    assert name, f"SKILL.md frontmatter 缺少 name: {path}"
    return name.group(1).strip()


def _op_map_tools(skill_name: str, skills: SkillRegistry) -> set[str]:
    """M5-style skills dispatch through operation_map; every mapped tool must
    be visible to validate_tools/catalog so there is no silent whitelist.

    M1–M4 用真源常量（import），M0/M5 仍内联（handler 内无导出常量）。
    """
    op_map = _REAL_OP_MAPS.get(skill_name) or _INLINE_OP_MAPS.get(skill_name) or {}
    return set(op_map.values())


def test_skill_docs_frontmatter_matches_registry_names():
    registry = build_default_skill_registry()
    registered = set(registry.specs)
    for path in sorted(SKILLS_DIR.rglob("SKILL.md")):
        name = _frontmatter_name(path)
        # orchestrator is documentation-only and intentionally not registered.
        if name == "yunpai-orchestrator":
            continue
        assert name in registered, f"SKILL.md name 未注册: {path.name} -> {name}"


def test_every_registered_skill_has_doc_dir():
    registry = build_default_skill_registry()
    registered = set(registry.specs)
    docs = {_frontmatter_name(path) for path in SKILLS_DIR.rglob("SKILL.md")}
    assert registered <= docs | {"yunpai-orchestrator"}


def test_skill_tools_declared_match_operation_map():
    registry = build_default_skill_registry()
    tool_registry = build_default_registry()
    for name, spec in registry.specs.items():
        declared = set(spec.tools)
        op_map = _op_map_tools(name, registry)
        if op_map:
            # Every operation-mapped tool must be declared (no hidden whitelist).
            assert op_map <= declared, f"{name} op_map 工具未声明: {op_map - declared}"
        # Declared tools must exist in the ToolRegistry so validate_tools holds.
        assert declared <= set(tool_registry.specs), f"{name} 声明了未注册工具: {declared - set(tool_registry.specs)}"


def test_skill_usage_order_covers_all_ordered_skills():
    registry = build_default_skill_registry()
    assert set(SKILL_USAGE_ORDER) <= set(registry.specs)


@pytest.mark.asyncio
async def test_qwen_prompt_exposes_full_versioned_skill_catalog(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"intent":"x","route":"chat","tools":[],"confidence":0.5,"answer":"ok"}'}}]}

    class Client:
        def __init__(self, **kwargs):
            captured["options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, body=kwargs["json"])
            return Response()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    router = QwenRouter(QwenConfig(api_key="test-key"))
    skills = build_default_skill_registry()
    router.skills = skills
    result = await router.classify({"message": "test"}, build_default_registry())
    assert result["ok"] is True
    prompt = captured["body"]["messages"][1]["content"]
    for spec in skills.specs.values():
        assert f"{spec.name}@{spec.version}" in prompt
    assert '"version"' in prompt

