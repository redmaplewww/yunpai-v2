"""M-INFRA 横切基建回归（施工依据：``_migration/INFRA-DECISIONS.md`` v1）。

锁定本次裁决，防止后续分片把结论改回去：

- **裁决 1（ctx 键）**：``tool_context()`` 仍是 7 个请求语义键，**不含** ``_tool_registry``；
  Skill 派发靠 ``SkillRegistry`` 构造注入 + ContextVar，因此 V2 executor 一行不改。
  同时锁定「执行器 Skill 路径真的能派发工具」——V2 原状是 ``RuntimeError``（executor
  从不把 registry 塞进 context），这是本次修复的回归点。
- **裁决 2（模块随迁）**：10 个随迁模块的 import 闭包必须保持可加载；骨架模块必须带
  ``PORT_SYMBOLS`` / ``PORT_HANDLERS`` 清单，供归属分片按 rows 迁入。
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

from yunpai_orchestrator.skills import SkillRegistry, build_default_skill_registry
from yunpai_orchestrator.worker.executor import make_worker_execute, tool_context

MIGRATED_MODULES = (
    "m1_domain", "m2_local", "m3_local", "m4_purchase_local", "m4_supplier_local",
    "m4_tracking_local", "m5_work_local", "m3_store", "m4_store", "m4b_store",
)

SKELETON_MODULES = (
    "m3_local", "m4_purchase_local", "m4_supplier_local",
    "m4_tracking_local", "m5_work_local",
)
# 注：``m2_local`` 已由 M2 分片迁完（INFRA-DECISIONS §2.6.2：把 PORT_HANDLERS 改写成
# 真正的 LOCAL_HANDLERS 并删除 PORT_SYMBOLS），因此不再属于骨架模块。其余骨架由各自
# 分片迁完后同样从这里移除；M-INFRA 可统一改成「有 LOCAL_HANDLERS 即视为已迁完」。


class _FakeRegistry:
    """只记录调用、返回合法信封的 ToolRegistry 替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []

    async def call(self, name: str, payload: dict, context: dict) -> dict:
        self.calls.append((name, payload, context))
        return {"success": True, "data": {"echo": name}}


def _skill_state(tool: str = "yunpai-m2-bom-sop") -> dict:
    return {
        "task_id": "TASK-INFRA-1",
        "run_id": "RUN-INFRA-1",
        "tenant_id": "tenant-a",
        "request": {tool: {"operation": "bom"}},
        "current_step": {"tool": tool, "kind": "skill"},
        "outputs": {}, "errors": [], "trace": [],
    }


def test_tool_context_contract_has_no_registry_key():
    """裁决 1：ctx 只承载请求语义键；registry 不进 context。"""
    ctx = tool_context({"task_id": "T", "run_id": "R", "tenant_id": "x",
                        "request": {"principal": {"user": "u", "role": "admin"}}}, "tool")
    assert set(ctx) == {
        "task_id", "run_id", "tenant_id", "trace_id", "idempotency_key",
        "actor_user", "actor_role", "actor",
    }
    assert "_tool_registry" not in ctx
    assert ctx["actor_user"] == "u" and ctx["actor_role"] == "admin"
    # 键名映射别名：与 actor_user 同源（V2 workers.py:183/187/766/772 读 actor）
    assert ctx["actor"] == ctx["actor_user"] == "u"
    assert ctx["idempotency_key"] == "T:tool"


def test_tool_context_forwards_m6_ledger_path_only_when_supplied():
    """M6 本地账路径属于请求上下文，不能在缺省时污染通用 context。"""
    state = {"task_id": "T", "run_id": "R", "tenant_id": "x",
             "request": {"m6_db_path": "D:/tmp/m6.sqlite"}}
    ctx = tool_context(state, "save_costing_snapshot")
    assert ctx["m6_db_path"] == "D:/tmp/m6.sqlite"


def test_build_default_skill_registry_injects_tool_registry():
    """裁决 1：registry 在**构造时**注入，不进 context。"""
    fake = _FakeRegistry()
    skills = build_default_skill_registry(fake)
    assert isinstance(skills, SkillRegistry)
    assert skills.tool_registry is fake
    # 不传时保持旧行为（Skill 派发会在运行时给出可读错误，而不是静默降级）
    assert build_default_skill_registry().tool_registry is None


async def test_executor_skill_path_dispatches_tool():
    """裁决 1 的回归点：executor 的 skill 分支现在能真正派发工具。"""
    fake = _FakeRegistry()
    skills = build_default_skill_registry(fake)
    deps = SimpleNamespace(skills=skills, registry=fake, assembler=None)
    out = await make_worker_execute(deps)(_skill_state())
    step = out["current_step"]
    assert step["status"] == "completed", out
    assert fake.calls and fake.calls[0][0] == "generate_m2_bom_controlled"
    assert out["outputs"]["yunpai-m2-bom-sop"]["skill"] == "yunpai-m2-bom-sop"
    # 派发时传给工具的 ctx 仍不含 registry
    assert "_tool_registry" not in fake.calls[0][2]


async def test_executor_skill_path_without_registry_fails_loudly():
    """未注入 registry 时必须显式失败（不得静默当作成功）。"""
    deps = SimpleNamespace(skills=build_default_skill_registry(), registry=None, assembler=None)
    out = await make_worker_execute(deps)(_skill_state())
    assert out["current_step"]["status"] == "failed"
    assert out["errors"][0]["code"] == "UPSTREAM_UNAVAILABLE"


def test_migrated_modules_import_and_declare_checklists():
    """裁决 2：随迁模块可加载；骨架模块带待迁清单；**已迁完的模块**改校验 ``LOCAL_HANDLERS``。

    分片按 ``INFRA-DECISIONS.md §2.6.2`` 完成迁入后会删除 ``PORT_SYMBOLS`` 并改写为
    ``LOCAL_HANDLERS``（如 M2 已完成），此时不应再要求骨架清单存在——否则每个分片都要
    各自改这一处共享断言。父会话统一口径：**二者其一**，且迁完后不得残留 PORT_SYMBOLS。
    """
    for name in MIGRATED_MODULES:
        module = importlib.import_module(f"yunpai_orchestrator.{name}")
        assert module.__file__
    for name in SKELETON_MODULES:
        module = importlib.import_module(f"yunpai_orchestrator.{name}")
        symbols = getattr(module, "PORT_SYMBOLS", None)
        handlers = getattr(module, "PORT_HANDLERS", None)
        local_handlers = getattr(module, "LOCAL_HANDLERS", None)
        if local_handlers is not None:
            assert isinstance(local_handlers, dict) and local_handlers, \
                f"{name} 的 LOCAL_HANDLERS 不能为空"
            assert not symbols, f"{name} 迁完后不应再保留 PORT_SYMBOLS"
            continue
        assert symbols, f"{name} 缺 PORT_SYMBOLS 清单"
        assert isinstance(handlers, dict), f"{name} 缺 PORT_HANDLERS 清单"
        for symbol, kind, lineno in symbols:
            assert isinstance(symbol, str) and kind in {"def", "async def", "class", "assign"}
            assert lineno > 0


def test_migrated_store_modules_are_importable_contract_surfaces():
    """裁决 2：三个纯依赖 store 完整迁入，关键符号可解析。"""
    from yunpai_orchestrator.m3_store import M3FeedbackConflictError, M3Store
    from yunpai_orchestrator.m4_store import M4Store, parse_suggestions_csv
    from yunpai_orchestrator.m4b_store import M4BStore, payload_checksum

    assert all(obj is not None for obj in
               (M3Store, M3FeedbackConflictError, M4Store, parse_suggestions_csv, M4BStore, payload_checksum))
