"""身份·组织架构·权限正式版（F-013/F-014/F-015，2026-09-07 规范落地）。

规范来源：《组织架构与权限说明交接包（2026-09-07）》——本模块按其
``SPEC_INTAKE_POINTS`` 五接缝填充正式实现，取代占位行为：

- 接缝 1 ``PERMISSION_CATALOG``：v1 冻结既有 11 项（code/gate 不变）+ 新增
  ``worker.view``/``report.view``/``finance.approve``/``cost.view``；查看类权限附数据范围（self/dept/tenant），
  v1 执行层只按 tenant 隔离（dept 过滤等组织树接缝完成后启用）；
- 接缝 2 ``DEFAULT_ROLE_SEEDS``：产线四角色 + 流程五角色共 9 个种子；
  ``roles`` 主键 (tenant_id, role_code)，新租户按需复制种子集；
- 接缝 3 ``derive_org_from_workers``：正式派生规则——部门取花名册 ``shift``
  字段（实态即部门名，如「仓储部」「人事、采购」），按 、，/ 拆分多部门、
  剥「部」后缀归一化合并（仅用于节点合并判断，展示保留原名）；``skill``
  是岗位属性不建组织节点；``line`` 层级暂不派生（数据无此维度）；被手工
  编辑过（source='manual'）的节点派生不覆盖；幂等 source='derived'；
- 接缝 4 ``authorize``：绑定命中 → legacy 过渡（``IDENTITY_LEGACY_ROLES``
  默认 1=等价现状）→ bootstrap 首管（仅当租户无任何 user_binding）→
  fail-closed；每次判定（含 deny）写审计日志；
- 接缝 5 登录 v1 见 ``auth.py``（账号密码 + HS256 签名会话 Cookie），
  ``api.py`` 在受信头之前接受会话 Cookie 注入 principal。

汇报关系 v1 不派生（花名册无上级字段，org_nodes 不加 manager 列，列 v2）。
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .db_utils import connect_sqlite, enable_wal, transactional

# ---------------------------------------------------------------------------
# 接缝 1：业务语言权限清单正式版（v1 = 冻结 11 + 新增 4）。
# code 稳定不变；gate 列是「权限 ↔ 现有审批 Gate」的唯一映射事实源；
# 新增权限必须先在此登记 code/label/gate 再进角色（upsert_role 校验）。
# 查看类权限附 scopes（数据范围维度）；其余默认租户全量。
# ---------------------------------------------------------------------------

#: 数据范围档位（宽度 self < dept < tenant）。v1 执行层只按 tenant 隔离，
#: dept 过滤待组织树手工调整链路稳定后启用（交接包接缝 1 条款）。
PERMISSION_SCOPES: tuple[str, ...] = ("self", "dept", "tenant")

PERMISSION_CATALOG: tuple[dict[str, Any], ...] = (
    {"code": "order.view", "label": "查看订单", "gate": "-", "scopes": PERMISSION_SCOPES},
    {"code": "order.ingest", "label": "上传/入库订单与业务资料", "gate": "candidate"},
    {"code": "candidate.approve", "label": "批准业务资料候选进入 M0", "gate": "candidate"},
    {"code": "sensitive.review", "label": "复核敏感资料（工资/人事）", "gate": "sensitive_data"},
    {"code": "order.review", "label": "复核 M1 订单解析结果", "gate": "review"},
    {"code": "engineering.approve", "label": "工程批准 BOM/SOP 路线", "gate": "engineering"},
    {"code": "procurement.supplement", "label": "补充供应商与交期", "gate": "procurement"},
    {"code": "schedule.solve", "label": "发起排程求解", "gate": "-"},
    {"code": "schedule.release", "label": "发布/生效生产排程", "gate": "apply"},
    {"code": "data.steward", "label": "维护主数据（物料/产品/BOM/route）", "gate": "-"},
    # 新增 2 项（交接包接缝 1）：花名册/日报查看独立于 sensitive.review 授权。
    {"code": "worker.view", "label": "查看人员主数据（花名册）", "gate": "-", "scopes": PERMISSION_SCOPES},
    {"code": "report.view", "label": "查看生产日报", "gate": "-", "scopes": PERMISSION_SCOPES},
    {"code": "finance.approve", "label": "批准财务结果生效", "gate": "finance"},
    {"code": "cost.view", "label": "查看成本与财务数据", "gate": "-", "scopes": PERMISSION_SCOPES},
    {"code": "identity.admin", "label": "管理组织架构与权限分配", "gate": "-"},
)

_CATALOG_BY_CODE: dict[str, dict[str, Any]] = {p["code"]: p for p in PERMISSION_CATALOG}

#: Gate 类型 → 判定用权限 code（业务端点影子/强制模式用；与 catalog gate 列
# 一致性由测试守护）。未映射的 gate（authorization/blocked_input）不走
# identity 判定，维持原 GATE_ALLOWED_ROLES 语义。
GATE_PERMISSION: dict[str, str] = {
    "candidate": "candidate.approve",
    "sensitive_data": "sensitive.review",
    "review": "order.review",
    "engineering": "engineering.approve",
    "procurement": "procurement.supplement",
    "apply": "schedule.release",
    "finance": "finance.approve",
}

#: 受信头旧角色 → 权限的过渡映射（接缝 4：``IDENTITY_LEGACY_ROLES`` 开关
# 控制，默认 1=等价现状；业务端点全量接管并影子验证后默认改 0 再删代码）。
LEGACY_ROLE_GRANTS: dict[str, frozenset[str]] = {
    "admin": frozenset(p["code"] for p in PERMISSION_CATALOG),
    "data-steward": frozenset({"order.view", "order.ingest", "data.steward"}),
    "m0-reviewer": frozenset({"order.view", "candidate.approve", "data.steward"}),
}

# ---------------------------------------------------------------------------
# 接缝 2：种子角色 = 流程五角色 + 产线四角色（0825 roleConfig 体系合并）。
# permissions 元素为 "code" 或 "code@scope"（查看类权限可带范围）；
# 工人角色是 self 范围 order.view（我的订单/报工查看）。
# ---------------------------------------------------------------------------

_FACTORY_DIRECTOR_PERMISSIONS: list[str] = [
    p["code"] for p in PERMISSION_CATALOG if p["code"] != "identity.admin"
]

DEFAULT_ROLE_SEEDS: tuple[dict[str, Any], ...] = (
    {"role_code": "org-admin", "name": "组织管理员",
     "permissions": ["identity.admin", "worker.view"]},
    {"role_code": "data-steward", "name": "主数据管理员",
     "permissions": ["order.view", "order.ingest", "data.steward", "candidate.approve"]},
    {"role_code": "engineer", "name": "工程审批",
     "permissions": ["order.view", "engineering.approve"]},
    {"role_code": "planner", "name": "计划员",
     "permissions": ["order.view", "schedule.solve", "procurement.supplement"]},
    {"role_code": "release-manager", "name": "发布负责人",
     "permissions": ["order.view", "schedule.release"]},
    {"role_code": "factory-director", "name": "厂长",
     "permissions": _FACTORY_DIRECTOR_PERMISSIONS},
    {"role_code": "quality-assurance", "name": "品保监督",
     "permissions": ["order.view", "order.review", "report.view"]},
    {"role_code": "team-leader", "name": "组长",
     "permissions": ["order.view", "report.view", "schedule.solve"]},
    {"role_code": "worker", "name": "工人",
     "permissions": ["order.view@self"]},
    {"role_code": "finance-officer", "name": "财务审批",
     "permissions": ["order.view", "cost.view", "finance.approve"]},
)

_DEPT_SPLIT_RE = re.compile(r"[、，,／/]")


def parse_permission_spec(spec: str) -> tuple[str, str | None]:
    """解析角色权限元素 ``code`` 或 ``code@scope`` → (code, scope|None)。"""
    text = str(spec or "").strip()
    code, _, scope = text.partition("@")
    code = code.strip()
    if not code:
        raise ValueError(f"非法权限元素: {spec!r}")
    scope = scope.strip() or None
    if scope is not None and scope not in PERMISSION_SCOPES:
        raise ValueError(f"非法数据范围 {scope!r}（合法: {'/'.join(PERMISSION_SCOPES)}）")
    return code, scope


def _scope_width(scope: str | None) -> int:
    # None 视为默认 tenant（ widest ），保证多角色并集取最宽范围。
    return PERMISSION_SCOPES.index(scope) if scope in PERMISSION_SCOPES else len(PERMISSION_SCOPES) - 1


def normalize_dept_name(name: str) -> str:
    """部门名归一化（接缝 3）：去空白 + 剥「部」后缀差异，仅用于节点合并判断。"""
    text = re.sub(r"\s+", "", str(name or ""))
    while len(text) > 1 and text.endswith("部"):
        text = text[:-1]
    return text


def split_dept_field(raw: Any) -> list[str]:
    """拆分部门字段（一人兼多部门合法）：按 、，,/ 切分并去空片段。"""
    text = str(raw or "").strip()
    if not text:
        return []
    return [part.strip() for part in _DEPT_SPLIT_RE.split(text) if part.strip()]


def permission_for_gate(gate_type: str) -> str | None:
    """Gate 类型 → 判定用权限 code（GATE_PERMISSION 事实源）。"""
    return GATE_PERMISSION.get(str(gate_type or ""))


def legacy_roles_enabled() -> bool:
    """``IDENTITY_LEGACY_ROLES`` 开关（默认 1=等价现状；退役计划见模块 docstring）。"""
    return os.getenv("IDENTITY_LEGACY_ROLES", "1").strip().lower() not in {"0", "false", "no", "off"}


def plan_org_from_workers(workers: list[dict[str, Any]], *,
                          existing_nodes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """派生规划纯函数（不写库）：花名册 → 计划新建的部门节点 + 花名册对齐。

    供 ``IdentityStore.derive_org_from_workers``（落库）与引导AI 建议端点
    （只读预览，不落任何节点）共用；existing_nodes 传当前租户 org_tree，
    其中的 manual 节点（按 org_id 或归一化名）受保护不覆盖。
    """
    tree = list(existing_nodes or [])
    known_ids = {node.get("org_id") for node in tree}
    existing_by_id = {node.get("org_id"): node for node in tree}
    manual_norm_keys = {
        normalize_dept_name(node.get("name") or "")
        for node in tree if node.get("source") == "manual"
    }
    norm_to_org_id = {
        normalize_dept_name(node.get("name") or ""): node.get("org_id")
        for node in tree if node.get("org_id")
    }
    planned: list[dict[str, Any]] = []
    dept_display: dict[str, str] = {}
    skipped_manual: list[str] = []
    roster: list[dict[str, Any]] = []
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        dept_field = worker.get("shift")
        if dept_field in (None, ""):
            dept_field = worker.get("dept") or worker.get("department") or ""
        fragments = split_dept_field(dept_field)
        primary_id: str | None = None
        for fragment in fragments:
            norm = normalize_dept_name(fragment)
            if not norm:
                continue
            dept_display.setdefault(norm, fragment)
            org_id = f"dept:{norm}"
            if org_id in known_ids:
                if existing_by_id.get(org_id, {}).get("source") == "manual" and norm not in skipped_manual:
                    # 同 org_id 手工节点：不覆盖，留痕（保护信号）。
                    skipped_manual.append(norm)
                if primary_id is None:
                    primary_id = org_id
                continue
            if norm in manual_norm_keys:
                # 同名手工节点已存在（org_id 可能自定义）：不建派生副本，
                # 主部门对齐到该手工节点。
                if norm not in skipped_manual:
                    skipped_manual.append(norm)
                if primary_id is None:
                    primary_id = norm_to_org_id.get(norm)
                continue
            planned.append({"org_id": org_id, "name": fragment})
            known_ids.add(org_id)
            if primary_id is None:
                primary_id = org_id
        roster.append({
            "worker_code": str(worker.get("worker_code") or ""),
            "worker_name": str(worker.get("worker_name") or ""),
            "skill": str(worker.get("skill") or ""),
            "depts": [normalize_dept_name(f) for f in fragments],
            "primary_dept": primary_id,
        })
    return {
        "planned_nodes": planned,
        "skipped_manual": skipped_manual,
        "departments": [
            {"org_id": f"dept:{norm}", "name": display}
            for norm, display in sorted(dept_display.items())
        ],
        "roster": roster,
    }


def _default_db_path() -> str:
    return os.getenv("YUNPAI_IDENTITY_DB", "runtime/yunpai-identity.sqlite")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IdentityStore:
    """组织架构/角色/用户绑定/登录用户 的 SQLite 事实源（db_utils 底座约定）。

    表全部租户化（org_nodes 亦含 tenant_id——双租户各自建树互不可见）；
    ``authz_audit`` 记录每次 authorize 判定（deny 留痕，ledger 审计风格）；
    ``guidance_plans`` 是引导AI 建议方案的 draft→applied 状态机（人工确认
    Gate 前只有方案行、没有任何绑定写入）。
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = str(db_path or _default_db_path())
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = connect_sqlite(self.db_path, foreign_keys=True)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _init(self) -> None:
        with transactional(self._connect()) as db:
            enable_wal(db)
            db.execute(
                """CREATE TABLE IF NOT EXISTS org_nodes (
                    tenant_id TEXT NOT NULL,
                    org_id TEXT NOT NULL,
                    parent_id TEXT,
                    name TEXT NOT NULL,
                    org_type TEXT NOT NULL DEFAULT 'dept',
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, org_id),
                    FOREIGN KEY(tenant_id, parent_id) REFERENCES org_nodes(tenant_id, org_id)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS roles (
                    tenant_id TEXT NOT NULL,
                    role_code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    permissions TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, role_code)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS user_bindings (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    org_id TEXT,
                    role_codes TEXT NOT NULL DEFAULT '[]',
                    skill TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, user_id),
                    FOREIGN KEY(tenant_id, org_id) REFERENCES org_nodes(tenant_id, org_id)
                )"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_user_tenant ON user_bindings(tenant_id)")
            db.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    display_name TEXT,
                    password_hash TEXT NOT NULL,
                    org_id TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, user_id),
                    FOREIGN KEY(tenant_id, org_id) REFERENCES org_nodes(tenant_id, org_id)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS authz_audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    allowed INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS kv_secrets (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )"""
            )
            # 种子角色（幂等）：default 租户开箱即有；其他租户在 ensure_tenant_roles
            # 时按需复制（接缝 2：新租户开通时复制种子集）。
            for role in DEFAULT_ROLE_SEEDS:
                db.execute(
                    "INSERT OR IGNORE INTO roles(tenant_id, role_code, name, permissions, created_at) VALUES(?,?,?,?,?)",
                    ("default", role["role_code"], role["name"],
                     _dump(role["permissions"]), _now()),
                )

    # ------------------------------------------------------------- 组织架构

    def upsert_org(self, *, tenant_id: str, org_id: str, name: str, parent_id: str | None = None,
                   org_type: str = "dept", source: str = "manual") -> dict[str, Any]:
        if parent_id in {"", org_id}:
            parent_id = None
        with self._txn() as db:
            db.execute(
                """INSERT INTO org_nodes(tenant_id, org_id, parent_id, name, org_type, source, created_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(tenant_id, org_id) DO UPDATE SET
                     parent_id=excluded.parent_id, name=excluded.name,
                     org_type=excluded.org_type, source=excluded.source""",
                (tenant_id, org_id, parent_id, name, org_type, source, _now()),
            )
        return {"tenant_id": tenant_id, "org_id": org_id, "name": name, "parent_id": parent_id,
                "org_type": org_type, "source": source}

    def org_tree(self, *, tenant_id: str) -> list[dict[str, Any]]:
        with self._txn() as db:
            rows = db.execute(
                "SELECT org_id, parent_id, name, org_type, source FROM org_nodes WHERE tenant_id=? ORDER BY org_id",
                (tenant_id,)).fetchall()
        return [dict(row) for row in rows]

    def org_path(self, *, tenant_id: str, org_id: str) -> list[str]:
        """从根到该节点的 org_id 路径（环防护：最长 16 层）。"""
        by_id = {node["org_id"]: node for node in self.org_tree(tenant_id=tenant_id)}
        path: list[str] = []
        current = org_id
        while current and current in by_id and len(path) < 16:
            path.append(current)
            current = by_id[current].get("parent_id")
        return list(reversed(path))

    def derive_org_from_workers(self, workers: list[dict[str, Any]], *, tenant_id: str = "default") -> dict[str, Any]:
        """接缝 3 正式派生：花名册 → 部门两级树（company ← dept），落库。

        规划逻辑在纯函数 ``plan_org_from_workers``（不写库），本方法只负责
        按计划建节点：company 根 ← dept 两级；幂等 source='derived'；被手工
        编辑过（source='manual'）的节点不覆盖（规划器已排除）。
        ``line`` 产线层级暂不派生（数据无此维度，等工位绑定数据）。
        """
        tree = self.org_tree(tenant_id=tenant_id)
        plan = plan_org_from_workers(workers, existing_nodes=tree)
        created: list[dict[str, Any]] = []
        if "company" not in {node.get("org_id") for node in tree}:
            created.append(self.upsert_org(tenant_id=tenant_id, org_id="company", name="公司",
                                           org_type="company", source="derived"))
        for dept in plan["planned_nodes"]:
            created.append(self.upsert_org(
                tenant_id=tenant_id, org_id=dept["org_id"], name=dept["name"],
                parent_id="company", org_type="dept", source="derived"))
        return {
            "created": created,
            "skipped_manual": plan["skipped_manual"],
            "departments": plan["departments"],
            "roster": plan["roster"],
        }

    # ---------------------------------------------------------- 角色/绑定

    def ensure_tenant_roles(self, tenant_id: str) -> int:
        """新租户按需复制种子角色集（幂等）；返回本次插入数。"""
        with self._txn() as db:
            inserted = 0
            for role in DEFAULT_ROLE_SEEDS:
                cur = db.execute(
                    "INSERT OR IGNORE INTO roles(tenant_id, role_code, name, permissions, created_at) VALUES(?,?,?,?,?)",
                    (tenant_id, role["role_code"], role["name"],
                     _dump(role["permissions"]), _now()),
                )
                inserted += cur.rowcount
            return inserted

    def upsert_role(self, *, tenant_id: str, role_code: str, name: str,
                    permissions: list[str]) -> dict[str, Any]:
        # 接缝 1：角色里出现未登记 code（或非法 scope）一律 ValueError（保持占位行为）。
        normalized: list[str] = []
        for spec in permissions:
            code, scope = parse_permission_spec(spec)
            entry = _CATALOG_BY_CODE.get(code)
            if entry is None:
                raise ValueError(f"未知权限 code: {spec}（合法集合见 PERMISSION_CATALOG）")
            if scope is not None and "scopes" not in entry:
                raise ValueError(f"权限 {code} 不支持数据范围标注（仅查看类权限可带 @scope）")
            normalized.append(f"{code}@{scope}" if scope else code)
        with self._txn() as db:
            db.execute(
                """INSERT INTO roles(tenant_id, role_code, name, permissions, created_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(tenant_id, role_code) DO UPDATE SET
                     name=excluded.name, permissions=excluded.permissions""",
                (tenant_id, role_code, name, _dump(normalized), _now()),
            )
        return {"tenant_id": tenant_id, "role_code": role_code, "name": name, "permissions": normalized}

    def list_roles(self, *, tenant_id: str) -> list[dict[str, Any]]:
        self.ensure_tenant_roles(tenant_id)
        with self._txn() as db:
            rows = db.execute(
                "SELECT role_code, name, permissions FROM roles WHERE tenant_id=? ORDER BY role_code",
                (tenant_id,)).fetchall()
        return [
            {"role_code": row["role_code"], "name": row["name"],
             "permissions": _load(row["permissions"])}
            for row in rows
        ]

    def bind_user(self, *, tenant_id: str, user_id: str, role_codes: list[str],
                  org_id: str | None = None, skill: str | None = None) -> dict[str, Any]:
        self.ensure_tenant_roles(tenant_id)
        org_id = org_id or None
        with self._txn() as db:
            for role in role_codes:
                exists = db.execute(
                    "SELECT 1 FROM roles WHERE tenant_id=? AND role_code=?", (tenant_id, role)).fetchone()
                if not exists:
                    raise ValueError(f"未注册角色: {role}（租户 {tenant_id}）")
            if org_id:
                node = db.execute(
                    "SELECT 1 FROM org_nodes WHERE tenant_id=? AND org_id=?", (tenant_id, org_id)).fetchone()
                if not node:
                    raise ValueError(f"组织节点不存在: {org_id}（租户 {tenant_id}）")
            db.execute(
                """INSERT INTO user_bindings(tenant_id, user_id, org_id, role_codes, skill, created_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(tenant_id, user_id) DO UPDATE SET
                     org_id=excluded.org_id, role_codes=excluded.role_codes, skill=excluded.skill""",
                (tenant_id, user_id, org_id, _dump(role_codes), skill, _now()),
            )
        return {"tenant_id": tenant_id, "user_id": user_id, "role_codes": role_codes,
                "org_id": org_id, "skill": skill}

    def bind_users_bulk(self, *, tenant_id: str,
                        bindings: list[dict[str, Any]]) -> dict[str, Any]:
        """按部门批量授权入口（接缝 3 阶段③）：单事务原子批量，任何一条
        校验失败整批回滚（与引导方案 apply 的整批语义一致）。"""
        self.ensure_tenant_roles(tenant_id)
        normalized: list[tuple[str, list[str], str | None, str | None]] = []
        for item in bindings:
            user_id = str(item.get("user_id") or "")
            role_codes = [str(r) for r in (item.get("role_codes") or [])]
            if not user_id or not role_codes:
                raise ValueError("批量绑定项缺少 user_id 或 role_codes")
            normalized.append((user_id, role_codes, item.get("org_id") or None, item.get("skill")))
        with self._txn() as db:
            for user_id, role_codes, org_id, _skill in normalized:
                for role in role_codes:
                    exists = db.execute(
                        "SELECT 1 FROM roles WHERE tenant_id=? AND role_code=?", (tenant_id, role)).fetchone()
                    if not exists:
                        raise ValueError(f"未注册角色: {role}（租户 {tenant_id}）")
                if org_id:
                    node = db.execute(
                        "SELECT 1 FROM org_nodes WHERE tenant_id=? AND org_id=?", (tenant_id, org_id)).fetchone()
                    if not node:
                        raise ValueError(f"组织节点不存在: {org_id}（租户 {tenant_id}）")
            for user_id, role_codes, org_id, skill in normalized:
                db.execute(
                    """INSERT INTO user_bindings(tenant_id, user_id, org_id, role_codes, skill, created_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(tenant_id, user_id) DO UPDATE SET
                         org_id=excluded.org_id, role_codes=excluded.role_codes, skill=excluded.skill""",
                    (tenant_id, user_id, org_id, _dump(role_codes), skill, _now()),
                )
        return {"tenant_id": tenant_id, "count": len(normalized),
                "bindings": [
                    {"user_id": user_id, "role_codes": role_codes, "org_id": org_id, "skill": skill}
                    for user_id, role_codes, org_id, skill in normalized
                ]}

    def list_bindings(self, *, tenant_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT user_id, org_id, role_codes, skill FROM user_bindings WHERE tenant_id=?"
        params: list[Any] = [tenant_id]
        if user_id:
            query += " AND user_id=?"
            params.append(user_id)
        with self._txn() as db:
            rows = db.execute(query + " ORDER BY user_id", params).fetchall()
        return [
            {"user_id": row["user_id"], "org_id": row["org_id"],
             "role_codes": _load(row["role_codes"]), "skill": row["skill"]}
            for row in rows
        ]

    def count_bindings(self, *, tenant_id: str) -> int:
        with self._txn() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM user_bindings WHERE tenant_id=?", (tenant_id,)).fetchone()
        return int(row["n"])

    # ------------------------------------------------------ 登录用户（接缝 5）

    def create_user(self, *, tenant_id: str, user_id: str, password_hash: str,
                    display_name: str | None = None, org_id: str | None = None) -> dict[str, Any]:
        org_id = org_id or None
        with self._txn() as db:
            if org_id:
                node = db.execute(
                    "SELECT 1 FROM org_nodes WHERE tenant_id=? AND org_id=?", (tenant_id, org_id)).fetchone()
                if not node:
                    raise ValueError(f"组织节点不存在: {org_id}（租户 {tenant_id}）")
            db.execute(
                """INSERT INTO users(tenant_id, user_id, display_name, password_hash, org_id, created_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(tenant_id, user_id) DO UPDATE SET
                     display_name=excluded.display_name,
                     password_hash=excluded.password_hash, org_id=excluded.org_id""",
                (tenant_id, user_id, display_name, password_hash, org_id, _now()),
            )
        return {"tenant_id": tenant_id, "user_id": user_id,
                "display_name": display_name, "org_id": org_id}

    def get_user(self, *, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        with self._txn() as db:
            row = db.execute(
                "SELECT user_id, display_name, password_hash, org_id FROM users WHERE tenant_id=? AND user_id=?",
                (tenant_id, user_id)).fetchone()
        return dict(row) if row else None

    def session_secret(self) -> str:
        """会话签名密钥：环境变量优先，否则首用生成并持久化在 kv_secrets。"""
        env_secret = os.getenv("YUNPAI_SESSION_SECRET", "").strip()
        if env_secret:
            return env_secret
        with self._txn() as db:
            row = db.execute(
                "SELECT value FROM kv_secrets WHERE key='session_secret'").fetchone()
            if row:
                return str(row["value"])
            value = secrets.token_urlsafe(48)
            db.execute("INSERT INTO kv_secrets(key, value) VALUES('session_secret', ?)", (value,))
            return value

    # ------------------------------------------------------------ 审计

    def record_authz(self, *, tenant_id: str, user_id: str, permission: str,
                     allowed: bool, reason: str, detail: str | None = None) -> None:
        """authorize 判定留痕（deny 必须可追，allow 一并记录，ledger 审计风格）。"""
        with self._txn() as db:
            db.execute(
                "INSERT INTO authz_audit(tenant_id, user_id, permission, allowed, reason, detail, created_at) VALUES(?,?,?,?,?,?,?)",
                (tenant_id, user_id, permission, 1 if allowed else 0, reason, detail, _now()),
            )

    def recent_authz(self, *, tenant_id: str, limit: int = 50,
                     user_id: str | None = None, only_denied: bool = False) -> list[dict[str, Any]]:
        query = "SELECT tenant_id, user_id, permission, allowed, reason, detail, created_at FROM authz_audit WHERE tenant_id=?"
        params: list[Any] = [tenant_id]
        if user_id:
            query += " AND user_id=?"
            params.append(user_id)
        if only_denied:
            query += " AND allowed=0"
        params.append(int(limit))
        with self._txn() as db:
            rows = db.execute(query + " ORDER BY seq DESC LIMIT ?", params).fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------- 解析

    def resolve(self, *, tenant_id: str, user_id: str) -> dict[str, Any]:
        """用户 → 角色/权限/数据范围/组织路径 的解析闭环（F-013 内核）。

        无绑定时返回空权限（fail-closed）；``authorize`` 再决定过渡期行为。
        permissions 是 code 并集；permission_scopes 是查看类权限的显式范围
        （多角色并集取最宽；缺省视为 tenant）。
        """
        with self._txn() as db:
            row = db.execute(
                "SELECT org_id, role_codes, skill FROM user_bindings WHERE tenant_id=? AND user_id=?",
                (tenant_id, user_id)).fetchone()
            role_rows = db.execute(
                "SELECT role_code, name, permissions FROM roles WHERE tenant_id=?",
                (tenant_id,)).fetchall()
        roles_by_code = {r["role_code"]: dict(r) for r in role_rows}
        bound_roles: list[str] = list(_load(row["role_codes"])) if row else []
        permissions: set[str] = set()
        scopes: dict[str, str] = {}
        unscoped: set[str] = set()
        for code in bound_roles:
            for spec in _load(roles_by_code.get(code, {}).get("permissions") or "[]"):
                perm_code, scope = parse_permission_spec(spec)
                permissions.add(perm_code)
                if scope is None:
                    # 未标注 = tenant（最宽）：并集后该权限不再受窄范围约束。
                    unscoped.add(perm_code)
                else:
                    current = scopes.get(perm_code)
                    if current is None or _scope_width(scope) > _scope_width(current):
                        scopes[perm_code] = scope
        for code in unscoped:
            scopes.pop(code, None)
        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "roles": bound_roles,
            "role_names": [roles_by_code.get(c, {}).get("name", c) for c in bound_roles],
            "permissions": sorted(permissions),
            "permission_scopes": scopes,
            "org_id": row["org_id"] if row else None,
            "org_path": self.org_path(tenant_id=tenant_id, org_id=row["org_id"]) if row and row["org_id"] else [],
            "skill": row["skill"] if row else None,
        }


def authorize(store: IdentityStore, *, tenant_id: str, user_id: str, permission: str,
              legacy_roles: list[str] | None = None) -> dict[str, Any]:
    """授权判定正式语义（接缝 4）。

    1. 显式绑定权限优先（resolve 结果含 permission → allow，reason=binding）；
    2. 过渡兼容：``IDENTITY_LEGACY_ROLES``（默认 1=等价现状）开启时，受信头
       旧角色按 ``LEGACY_ROLE_GRANTS`` 授予；开关关 0 后此路失效；
    3. 引导首管：``IDENTITY_BOOTSTRAP_ADMIN`` 命名用户拿 identity.admin——
       **仅当该租户无任何 user_binding**（首个管理员引导通道，租户出现首个
       绑定后即失效，防长期后门）；
    4. 其余 deny（fail-closed，含绑定用户越权请求）。

    每次判定写审计行（deny 留痕：tenant/user/permission/reason/时间）。
    """
    resolved = store.resolve(tenant_id=tenant_id, user_id=user_id)
    decision: dict[str, Any]
    if permission in resolved["permissions"]:
        decision = {"allowed": True, "reason": "binding"}
    elif legacy_roles_enabled():
        matched = None
        for legacy in legacy_roles or []:
            if permission in LEGACY_ROLE_GRANTS.get(legacy, frozenset()):
                matched = legacy
                break
        decision = ({"allowed": True, "reason": f"legacy_role:{matched}"} if matched
                    else {"allowed": False, "reason": "fail_closed"})
    else:
        decision = {"allowed": False, "reason": "fail_closed"}
    if not decision["allowed"]:
        bootstrap = os.getenv("IDENTITY_BOOTSTRAP_ADMIN", "").strip()
        if (bootstrap and user_id == bootstrap and permission == "identity.admin"
                and store.count_bindings(tenant_id=tenant_id) == 0):
            decision = {"allowed": True, "reason": "bootstrap_admin"}
    store.record_authz(tenant_id=tenant_id, user_id=user_id, permission=permission,
                       allowed=decision["allowed"], reason=decision["reason"],
                       detail="legacy_off" if not legacy_roles_enabled() else None)
    decision["resolved"] = resolved
    decision["at"] = _now()
    return decision


# ---------------------------------------------------------------------------
# CLI（接缝 3/5）：python -m yunpai_orchestrator.identity {derive-org,create-admin}
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="yunpai_orchestrator.identity",
                                     description="身份/组织架构管理 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    derive = sub.add_parser("derive-org", help="从 canonical worker 实体派生组织树（不经 LLM）")
    derive.add_argument("--tenant", default="default")
    derive.add_argument("--identity-db", default=None)
    derive.add_argument("--m0-db", default=os.getenv("YUNPAI_M0_DB", "runtime/yunpai-m0.sqlite"))

    admin = sub.add_parser("create-admin", help="创建登录管理员（IDENTITY_BOOTSTRAP_ADMIN 替代通道）")
    admin.add_argument("--tenant", default="default")
    admin.add_argument("--user", required=True)
    admin.add_argument("--password", required=True)
    admin.add_argument("--display-name", default=None)
    admin.add_argument("--identity-db", default=None)

    args = parser.parse_args()
    store = IdentityStore(args.identity_db)
    if args.command == "derive-org":
        from .m0_backend import M0Store

        data = M0Store(args.m0_db).list_entities("worker", args.tenant)
        workers = [entity.get("payload_json") or {} for entity in data.get("entities", [])]
        if not workers:
            print(f"tenant={args.tenant} canonical 无 worker 实体（读取 {args.m0_db}），未派生")
            return 1
        result = store.derive_org_from_workers(workers, tenant_id=args.tenant)
        print(f"worker={len(workers)} 部门={len(result['departments'])} "
              f"新建节点={len(result['created'])} 手工保护跳过={result['skipped_manual']}")
        for dept in result["departments"]:
            print(f"  - {dept['org_id']} ({dept['name']})")
        return 0
    if args.command == "create-admin":
        from .auth import hash_password

        store.create_user(tenant_id=args.tenant, user_id=args.user,
                          password_hash=hash_password(args.password),
                          display_name=args.display_name)
        print(f"已创建管理员 tenant={args.tenant} user={args.user}")
        return 0
    parser.error("未知命令")
    return 2


def _dump(value: list[str]) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


def _load(text: str) -> list[str]:
    import json
    try:
        value = json.loads(text)
        return [str(v) for v in value] if isinstance(value, list) else []
    except ValueError:
        return []



#: 规范落点状态（2026-09-07）：五接缝已按交接包填充正式实现，占位退役。
#: 后续演进（v2）：汇报关系派生（等人员主数据补上级字段或引导AI 采集）、
#: dept 级数据范围过滤启用、LEGACY_ROLE_GRANTS 默认关与删除、0825 BFF
#: 架构复用（SaaS 多租户 OIDC/PKCE/JWKS）届时另立交接包。
SPEC_INTAKE_POINTS: tuple[dict[str, str], ...] = (
    {"point": "PERMISSION_CATALOG", "slot": "已落地：v1 = 11 冻结 + worker.view/report.view，查看类附 data scope"},
    {"point": "DEFAULT_ROLE_SEEDS / upsert_role", "slot": "已落地：流程五角色 + 产线四角色，租户按需复制种子集"},
    {"point": "derive_org_from_workers", "slot": "已落地：shift 拆分/归一化合并/manual 保护；skill 为岗位属性；line 暂不派生"},
    {"point": "authorize（LEGACY_ROLE_GRANTS/bootstrap）", "slot": "已落地：IDENTITY_LEGACY_ROLES 开关 + bootstrap 仅空租户 + deny 审计"},
    {"point": "api.py principal 头", "slot": "已落地：登录 v1（auth.py 会话 Cookie）替换受信头语义（二选一）"},
)


if __name__ == "__main__":
    raise SystemExit(_cli())
