"""identity 模块回归测试（2026-09-07 规范落地版，原占位 7 项显式演进）。

行为演进均对应《组织架构与权限说明交接包》条款：
- 接缝 1：PERMISSION_CATALOG v1 = 冻结 11 + worker.view/report.view/finance.approve/cost.view（15 项），
  查看类权限附数据范围 self|dept|tenant；
- 接缝 2：种子角色 = 流程五角色 + 产线四角色（9 个）；
- 接缝 3：派生规则正式版——部门取 shift 字段、拆分多部门、剥「部」归一化、
  skill 不建节点、line 暂不派生、manual 保护；
- 接缝 4：authorize=绑定→legacy（IDENTITY_LEGACY_ROLES 开关）→bootstrap
  （仅空租户）→fail-closed；deny 留痕 authz_audit。
"""
from __future__ import annotations

import pytest

from yunpai_orchestrator.identity import (
    DEFAULT_ROLE_SEEDS,
    GATE_PERMISSION,
    LEGACY_ROLE_GRANTS,
    PERMISSION_CATALOG,
    IdentityStore,
    authorize,
    legacy_roles_enabled,
    permission_for_gate,
)

_FROZEN_11 = {
    "order.view", "order.ingest", "candidate.approve", "sensitive.review",
    "order.review", "engineering.approve", "procurement.supplement",
    "schedule.solve", "schedule.release", "data.steward", "identity.admin",
}


@pytest.fixture()
def store(tmp_path):
    return IdentityStore(str(tmp_path / "identity.sqlite"))


def test_permission_catalog_matches_gate_semantics():
    """接缝 1：v1 清单 = 冻结 11 + worker.view/report.view；gate 映射一致。"""
    codes = {p["code"] for p in PERMISSION_CATALOG}
    assert codes == _FROZEN_11 | {"worker.view", "report.view", "finance.approve", "cost.view"}
    gates = {p["gate"] for p in PERMISSION_CATALOG}
    assert {"candidate", "review", "engineering", "apply", "sensitive_data", "procurement", "finance"} <= gates
    assert all(p["label"] for p in PERMISSION_CATALOG)
    # 查看类权限附数据范围，其余不带
    scoped = {p["code"] for p in PERMISSION_CATALOG if "scopes" in p}
    assert scoped == {"order.view", "report.view", "worker.view", "cost.view"}
    # GATE_PERMISSION 是 catalog gate 列的一致映射
    catalog_gate_by_code = {}
    for p in PERMISSION_CATALOG:
        if p["gate"] != "-":
            catalog_gate_by_code.setdefault(p["code"], p["gate"])
    for gate, code in GATE_PERMISSION.items():
        assert code in codes, f"gate {gate} 映射了未登记权限 {code}"
        assert catalog_gate_by_code.get(code) == gate
    assert permission_for_gate("engineering") == "engineering.approve"
    assert permission_for_gate("finance") == "finance.approve"
    assert permission_for_gate("authorization") is None


def test_default_role_seeds_ten_roles():
    """接缝 2：流程五角色 + 产线四角色；厂长=事实 admin 不含 identity.admin；
    工人是 self 范围 order.view。"""
    by_code = {role["role_code"]: role for role in DEFAULT_ROLE_SEEDS}
    assert set(by_code) == {
        "org-admin", "data-steward", "engineer", "planner", "release-manager",
        "factory-director", "quality-assurance", "team-leader", "worker", "finance-officer",
    }
    assert "identity.admin" not in by_code["factory-director"]["permissions"]
    assert "identity.admin" in by_code["org-admin"]["permissions"]
    assert "worker.view" in by_code["org-admin"]["permissions"]
    assert by_code["worker"]["permissions"] == ["order.view@self"]
    assert set(by_code["finance-officer"]["permissions"]) == {
        "order.view", "cost.view", "finance.approve"}
    assert set(by_code["quality-assurance"]["permissions"]) == {
        "order.view", "order.review", "report.view"}


def test_org_tree_upsert_and_path(store):
    store.upsert_org(tenant_id="default", org_id="company", name="公司", org_type="company")
    store.upsert_org(tenant_id="default", org_id="dept:押出", name="押出部", parent_id="company")
    store.upsert_org(tenant_id="default", org_id="line:押出:1L", name="一楼产线",
                     parent_id="dept:押出", org_type="line")
    assert store.org_path(tenant_id="default", org_id="line:押出:1L") == [
        "company", "dept:押出", "line:押出:1L"]
    assert store.org_path(tenant_id="default", org_id="company") == ["company"]
    assert store.org_path(tenant_id="default", org_id="不存在") == []
    # 组织树租户隔离（接缝 2：org_nodes 租户化）
    assert store.org_tree(tenant_id="other") == []


def test_derive_org_from_workers_formal_rule(store):
    """接缝 3：部门取 shift（数据实态）、拆分多部门、skill 不建节点、line 不派生。"""
    result = store.derive_org_from_workers([
        {"worker_code": "W-1", "worker_name": "张三", "shift": "仓储部", "skill": "仓管"},
        {"worker_code": "W-2", "worker_name": "李四", "shift": "人事、采购", "skill": "办公室文员"},
        {"worker_code": "W-3", "worker_name": "王五", "shift": "仓储", "skill": "质检员"},
        "garbage",
    ], tenant_id="default")
    created_ids = {node["org_id"] for node in result["created"]}
    # 「人事、采购」拆两节点；「仓储部」与「仓储」归一化合并为一个节点
    assert created_ids == {"company", "dept:仓储", "dept:人事", "dept:采购"}
    # line 不派生、skill 不建节点
    assert all(node["org_type"] in {"company", "dept"} for node in result["created"])
    # 幂等：重跑零新增
    again = store.derive_org_from_workers(
        [{"worker_code": "W-9", "worker_name": "赵六", "shift": "仓储部"}], tenant_id="default")
    assert again["created"] == []
    assert again["skipped_manual"] == []
    # 花名册对齐：主部门取第一片段
    roster = {item["worker_code"]: item for item in again["roster"]}
    assert roster["W-9"]["primary_dept"] == "dept:仓储"
    assert roster["W-9"]["skill"] == ""
    # 展示名保留首见原名（「仓储部」）
    names = {node["org_id"]: node["name"] for node in store.org_tree(tenant_id="default")}
    assert names["dept:仓储"] == "仓储部"


def test_derive_org_manual_nodes_protected(store):
    """接缝 3：手工调整优先——manual 节点不被派生覆盖。"""
    store.upsert_org(tenant_id="default", org_id="dept:仓储", name="成品仓（手工）", source="manual")
    result = store.derive_org_from_workers(
        [{"worker_code": "W-1", "worker_name": "张三", "shift": "仓储部"}], tenant_id="default")
    # company 根节点正常创建；dept:仓储 是手工节点，未被派生覆盖
    assert {node["org_id"] for node in result["created"]} == {"company"}
    assert result["skipped_manual"] == ["仓储"]
    names = {node["org_id"]: node["name"] for node in store.org_tree(tenant_id="default")}
    assert names["dept:仓储"] == "成品仓（手工）"


def test_role_and_binding_resolve_roundtrip(store):
    store.bind_user(tenant_id="default", user_id="u-planner",
                    role_codes=["planner"], org_id=None)
    resolved = store.resolve(tenant_id="default", user_id="u-planner")
    assert resolved["roles"] == ["planner"]
    assert "schedule.solve" in resolved["permissions"]
    assert "schedule.release" not in resolved["permissions"]  # 越权不可见

    store.bind_user(tenant_id="default", user_id="u-eng", role_codes=["engineer"])
    eng = store.resolve(tenant_id="default", user_id="u-eng")
    assert "engineering.approve" in eng["permissions"]

    # 接缝 1：数据范围解析（self 范围 order.view；多角色并集取最宽）
    store.bind_user(tenant_id="default", user_id="u-worker", role_codes=["worker"])
    worker = store.resolve(tenant_id="default", user_id="u-worker")
    assert worker["permissions"] == ["order.view"]
    assert worker["permission_scopes"] == {"order.view": "self"}
    store.bind_user(tenant_id="default", user_id="u-mix", role_codes=["worker", "team-leader"])
    mixed = store.resolve(tenant_id="default", user_id="u-mix")
    assert mixed["permission_scopes"] == {}  # team-leader 的 order.view 无标注（=tenant，最宽）

    # skill 岗位属性（接缝 3：作为绑定属性留存）
    store.bind_user(tenant_id="default", user_id="u-skill", role_codes=["worker"], skill="仓管")
    assert store.resolve(tenant_id="default", user_id="u-skill")["skill"] == "仓管"


def test_finance_officer_seed_resolves_finance_permissions(store):
    """财务角色已进入租户种子并能解析审批/成本查看权限。"""
    store.ensure_tenant_roles("finance-tenant")
    store.bind_user(tenant_id="finance-tenant", user_id="u-finance",
                    role_codes=["finance-officer"])
    resolved = store.resolve(tenant_id="finance-tenant", user_id="u-finance")
    assert resolved["roles"] == ["finance-officer"]
    assert {"finance.approve", "cost.view"} <= set(resolved["permissions"])
    assert permission_for_gate("finance") == "finance.approve"


def test_authorize_formal_semantics(store, monkeypatch):
    """接缝 4：绑定→legacy（开关）→bootstrap（仅空租户）→fail-closed + deny 留痕。"""
    store.bind_user(tenant_id="default", user_id="u-planner", role_codes=["planner"])
    ok = authorize(store, tenant_id="default", user_id="u-planner", permission="schedule.solve")
    assert ok["allowed"] and ok["reason"] == "binding"
    # 无绑定用户 + 无旧角色 → fail-closed
    denied = authorize(store, tenant_id="default", user_id="stranger", permission="schedule.release")
    assert not denied["allowed"] and denied["reason"] == "fail_closed"
    # 绑定用户越权 → fail-closed
    overreach = authorize(store, tenant_id="default", user_id="u-planner", permission="schedule.release")
    assert not overreach["allowed"]
    # 过渡兼容：受信头旧角色授予（IDENTITY_LEGACY_ROLES 默认 1=等价现状）
    assert legacy_roles_enabled() is True
    legacy = authorize(store, tenant_id="default", user_id="stranger",
                       permission="candidate.approve", legacy_roles=["m0-reviewer"])
    assert legacy["allowed"] and legacy["reason"] == "legacy_role:m0-reviewer"
    # 接缝 4：IDENTITY_LEGACY_ROLES=0 后旧角色通道失效（退役开关）
    monkeypatch.setenv("IDENTITY_LEGACY_ROLES", "0")
    legacy_off = authorize(store, tenant_id="default", user_id="stranger",
                           permission="candidate.approve", legacy_roles=["m0-reviewer"])
    assert not legacy_off["allowed"]
    monkeypatch.delenv("IDENTITY_LEGACY_ROLES")
    # 接缝 4：bootstrap 收紧——仅当租户无任何 user_binding 时生效
    monkeypatch.setenv("IDENTITY_BOOTSTRAP_ADMIN", "boot-admin")
    boot = authorize(store, tenant_id="fresh-tenant", user_id="boot-admin", permission="identity.admin")
    assert boot["allowed"] and boot["reason"] == "bootstrap_admin"
    # default 租户已有绑定 → bootstrap 失效（防长期后门）
    boot_dead = authorize(store, tenant_id="default", user_id="boot-admin", permission="identity.admin")
    assert not boot_dead["allowed"]
    # 首个绑定出现后 bootstrap 立即失效
    store.bind_user(tenant_id="fresh-tenant", user_id="u-first", role_codes=["org-admin"])
    boot_after = authorize(store, tenant_id="fresh-tenant", user_id="boot-admin", permission="identity.admin")
    assert not boot_after["allowed"]
    # deny 留痕：审计行存在（tenant/user/permission/reason）
    audit = store.recent_authz(tenant_id="default", only_denied=True)
    rows = [row for row in audit if row["user_id"] == "u-planner" and row["permission"] == "schedule.release"]
    assert rows and rows[0]["reason"] == "fail_closed" and rows[0]["created_at"]


def test_role_validation_and_tenant_isolation(store):
    with pytest.raises(ValueError, match="未知权限"):
        store.upsert_role(tenant_id="default", role_code="bad", name="坏", permissions=["no.such"])
    # 接缝 1：查看类权限才能带 @scope；scope 值必须合法
    with pytest.raises(ValueError, match="不支持数据范围"):
        store.upsert_role(tenant_id="default", role_code="bad2", name="坏",
                          permissions=["schedule.solve@self"])
    with pytest.raises(ValueError, match="非法数据范围"):
        store.upsert_role(tenant_id="default", role_code="bad3", name="坏",
                          permissions=["order.view@everywhere"])
    with pytest.raises(ValueError, match="未注册角色"):
        store.bind_user(tenant_id="default", user_id="u", role_codes=["ghost"])
    # 租户隔离：租户 A 的角色对租户 B 不可用
    store.upsert_role(tenant_id="A", role_code="custom", name="A 专属", permissions=["order.view"])
    with pytest.raises(ValueError, match="未注册角色"):
        store.bind_user(tenant_id="B", user_id="u", role_codes=["custom"])
    # 跨租户 resolve 为空绑定（fail-closed）
    stranger = store.resolve(tenant_id="B", user_id="u")
    assert stranger["permissions"] == []
    # 新租户按需复制种子集（接缝 2）
    roles_b = store.list_roles(tenant_id="B")
    assert {role["role_code"] for role in roles_b} >= {"worker", "org-admin", "factory-director"}


def test_legacy_grants_subset_of_catalog():
    catalog = {p["code"] for p in PERMISSION_CATALOG}
    for grants in LEGACY_ROLE_GRANTS.values():
        assert grants <= catalog
