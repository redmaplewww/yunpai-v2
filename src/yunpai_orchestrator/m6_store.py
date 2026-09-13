"""M6 财务自有存储层（F-008 / 决策 D1）。

照 M3/M4 store 模式：**只读别人，写自己**。

路径约定（与 `m3_local`/`m4_purchase_local`/`m5_tools` 同构）：
``ctx.m6_db_path`` → ``YUNPAI_M6_DB`` → ``runtime/yunpai-m6.sqlite``。

## 两条财务护栏（编在 DB 层，不靠调用方自觉）

1. **试算不污染账本**（计划 §6 的 A 问题）：快照/单据带 ``status``，
   试算写 ``trial``；**月末汇总只认 ``confirmed``**（`month_summary`）。
2. **月结冻结后不得再确认**：`close_month` 落冻结行；此后该期间
   `confirm_snapshot` / `confirm_document` 一律拒绝（``MONTH_CLOSED``），
   除非显式重开（本层不提供重开——重开属财务流程决策，需要独立授权）。

## 三段式（书二 §6.2.1）在本层的落点

- **propose 段**：`save_snapshot` / `save_document` 只写 ``status=trial``；
- **commit 段**：`confirm_snapshot` / `confirm_document` 才翻 ``confirmed``
  （由 `graph.py` 的 `_apply_m6_*` 钩子在人工门批准后调用）。
本层**自身不做授权判断**——授权在审核 Agent（`reviewer/gates.py` 的 `finance` 门）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

SCHEMA_VERSION = "m6.store.v1"

#: 快照/单据状态：试算（不进汇总）/ 正式（进汇总）
STATUS_TRIAL = "trial"
STATUS_CONFIRMED = "confirmed"
SNAPSHOT_STATUSES = (STATUS_TRIAL, STATUS_CONFIRMED)

#: 成本口径（计划 §4 `basis`）
BASIS_VALUES = ("mixed", "stock", "purchase")

#: 成本明细要素（计划 §4 `element`）
ELEMENTS = ("material", "labor", "overhead", "expense")

#: 明细来源类型（计划 §4 `source_kind`；`route` 为 B1 第二批新增：人工/制费的
#: 标准工时来自 canonical 工艺路线，与 BOM 价、库存价、采购价、报工、分摊并列，
#: 不能借 `bom` 之名掩盖真实来源——`source_ref` 同步带 `canonical:route/<product>`）。
SOURCE_KINDS = ("bom", "stock", "purchase", "report", "allocation", "route")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS m6_store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS m6_snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    task_id      TEXT NOT NULL DEFAULT '',
    period       TEXT NOT NULL,
    order_id     TEXT NOT NULL DEFAULT '',
    product_code TEXT NOT NULL DEFAULT '',
    batch_no     TEXT NOT NULL DEFAULT '',
    quantity     REAL,
    unit_cost    REAL,
    total_cost   REAL,
    status       TEXT NOT NULL,
    basis        TEXT NOT NULL DEFAULT 'mixed',
    cost_incomplete INTEGER NOT NULL DEFAULT 0,
    evidence     TEXT NOT NULL DEFAULT '{}',
    computed_at  TEXT NOT NULL,
    confirmed_at TEXT,
    confirmed_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_m6_snapshots_key
    ON m6_snapshots(tenant_id, order_id, batch_no, period);
CREATE INDEX IF NOT EXISTS ix_m6_snapshots_period
    ON m6_snapshots(tenant_id, period, status);

CREATE TABLE IF NOT EXISTS m6_cost_lines (
    line_id      TEXT PRIMARY KEY,
    snapshot_id  TEXT NOT NULL,
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    seq          INTEGER NOT NULL,
    element      TEXT NOT NULL,
    material_code TEXT NOT NULL DEFAULT '',
    asset_code   TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT '',
    quantity     REAL,
    unit_price   REAL,
    amount       REAL,
    source_kind  TEXT NOT NULL DEFAULT '',
    source_ref   TEXT NOT NULL DEFAULT '',
    evidence     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_m6_cost_lines_snapshot
    ON m6_cost_lines(snapshot_id, seq);

CREATE TABLE IF NOT EXISTS m6_documents (
    doc_id        TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    task_id       TEXT NOT NULL DEFAULT '',
    doc_no        TEXT NOT NULL,
    doc_type      TEXT NOT NULL,
    counterparty_code TEXT NOT NULL DEFAULT '',
    doc_date      TEXT NOT NULL DEFAULT '',
    direction     TEXT NOT NULL DEFAULT '',
    amount        REAL,
    status        TEXT NOT NULL,
    lines         TEXT NOT NULL DEFAULT '[]',
    source_ref    TEXT NOT NULL DEFAULT '',
    evidence      TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    confirmed_at  TEXT,
    confirmed_by  TEXT
);
CREATE INDEX IF NOT EXISTS ix_m6_documents_key
    ON m6_documents(tenant_id, doc_type, doc_no);

CREATE TABLE IF NOT EXISTS m6_month_close (
    tenant_id  TEXT NOT NULL DEFAULT 'default',
    period     TEXT NOT NULL,
    totals     TEXT NOT NULL DEFAULT '{}',
    snapshot_count INTEGER NOT NULL DEFAULT 0,
    closed_at  TEXT NOT NULL,
    closed_by  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, period)
);
"""


def now_iso() -> str:
    """当前时间（UTC，秒级 ISO8601）。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def store_path(ctx: dict[str, Any] | None) -> str:
    """M6 库路径：``ctx.m6_db_path`` → ``YUNPAI_M6_DB`` → ``runtime/yunpai-m6.sqlite``。"""
    return str(
        (ctx or {}).get("m6_db_path")
        or os.getenv("YUNPAI_M6_DB")
        or "runtime/yunpai-m6.sqlite"
    )


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _loads(text: Any, fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except ValueError:
        return fallback


class M6Store:
    """M6 成本账 sqlite 存储（每 handler 一实例，操作自带事务提交）。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._ensure_schema()

    # ---------- 基础设施 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA_SQL)
            row = conn.execute("SELECT value FROM m6_store_meta WHERE key='schema_version'").fetchone()
            if row is not None and row["value"] != SCHEMA_VERSION:
                raise ValueError(
                    f"SCHEMA_VERSION_MISMATCH: m6 store schema {row['value']} != 本模块 {SCHEMA_VERSION}；"
                    "需要统一迁移，禁止静默升级"
                )
            conn.execute(
                "INSERT OR IGNORE INTO m6_store_meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            conn.commit()
        finally:
            conn.close()

    def _row(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    # ---------- 月度冻结 ----------

    def get_month_close(self, period: str, tenant_id: str = "default") -> dict[str, Any] | None:
        return self._row(
            "SELECT * FROM m6_month_close WHERE tenant_id=? AND period=?",
            (tenant_id, period),
        )

    def close_month(self, *, period: str, totals: dict[str, Any], snapshot_count: int,
                    actor: str = "", tenant_id: str = "default",
                    closed_at: str | None = None) -> dict[str, Any]:
        """冻结月账（月末结账）。重复结账同一期间 → ``MONTH_ALREADY_CLOSED``。"""
        if self.get_month_close(period, tenant_id) is not None:
            return {"success": False, "code": "MONTH_ALREADY_CLOSED", "period": period}
        closed_at = closed_at or now_iso()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO m6_month_close(tenant_id, period, totals, snapshot_count, closed_at, closed_by)"
                " VALUES(?,?,?,?,?,?)",
                (tenant_id, period, _dumps(totals), int(snapshot_count), closed_at, actor),
            )
            conn.commit()
        finally:
            conn.close()
        return {"success": True, "period": period, "closed_at": closed_at,
                "snapshot_count": int(snapshot_count), "totals": totals}

    def _month_closed(self, period: str, tenant_id: str) -> bool:
        return self.get_month_close(period, tenant_id) is not None

    # ---------- 成本快照 ----------

    def find_snapshot(self, *, order_id: str, batch_no: str, period: str,
                      tenant_id: str = "default") -> dict[str, Any] | None:
        """查重（计划 §6 的 B 问题）：业务键 = ``order_id + batch_no + period``。"""
        return self._row(
            "SELECT * FROM m6_snapshots WHERE tenant_id=? AND order_id=? AND batch_no=? AND period=?"
            " ORDER BY computed_at DESC, snapshot_id DESC LIMIT 1",
            (tenant_id, order_id, batch_no, period),
        )

    def save_snapshot(self, *, snapshot_id: str, period: str, order_id: str = "",
                      product_code: str = "", batch_no: str = "",
                      quantity: Any = None, unit_cost: Any = None, total_cost: Any = None,
                      basis: str = "mixed", cost_incomplete: bool = False,
                      lines: Any = None, evidence: Any = None, task_id: str = "",
                      tenant_id: str = "default",
                      computed_at: str | None = None) -> dict[str, Any]:
        """**propose 段**：写入 ``status=trial`` 的快照 + 明细（不构成正式成本）。

        月账已冻结的期间拒绝写入（``MONTH_CLOSED``）——冻结后不得再产出新成本。
        """
        if self._month_closed(period, tenant_id):
            return {"success": False, "code": "MONTH_CLOSED", "period": period}
        computed_at = computed_at or now_iso()
        line_rows = self._normalize_lines(lines, snapshot_id, tenant_id)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO m6_snapshots(snapshot_id, tenant_id, task_id, period, order_id,"
                " product_code, batch_no, quantity, unit_cost, total_cost, status, basis,"
                " cost_incomplete, evidence, computed_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (snapshot_id, tenant_id, task_id, period, order_id, product_code, batch_no,
                 _num(quantity), _num(unit_cost), _num(total_cost), STATUS_TRIAL, basis,
                 1 if cost_incomplete else 0, _dumps(evidence), computed_at),
            )
            for row in line_rows:
                conn.execute(
                    "INSERT INTO m6_cost_lines(line_id, snapshot_id, tenant_id, seq, element,"
                    " material_code, asset_code, category, quantity, unit_price, amount,"
                    " source_kind, source_ref, evidence)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["line_id"], snapshot_id, tenant_id, row["seq"], row["element"],
                     row["material_code"], row["asset_code"], row["category"], row["quantity"],
                     row["unit_price"], row["amount"], row["source_kind"], row["source_ref"],
                     row["evidence"]),
                )
            conn.commit()
        finally:
            conn.close()
        return {"success": True, "snapshot_id": snapshot_id, "status": STATUS_TRIAL,
                "line_count": len(line_rows), "duplicate_of": self._duplicate_hint(
                    order_id, batch_no, period, tenant_id, snapshot_id)}

    def _duplicate_hint(self, order_id: str, batch_no: str, period: str,
                        tenant_id: str, exclude_id: str) -> dict[str, Any] | None:
        """落库后回读：同一业务键若还有别的快照，提示"已存在同号"（让人选新建/覆盖/查看）。"""
        rows = self._rows(
            "SELECT snapshot_id, status, computed_at FROM m6_snapshots"
            " WHERE tenant_id=? AND order_id=? AND batch_no=? AND period=? AND snapshot_id != ?"
            " ORDER BY computed_at DESC",
            (tenant_id, order_id, batch_no, period, exclude_id),
        )
        if not rows:
            return None
        return {"other_count": len(rows), "latest": rows[0]}

    def _normalize_lines(self, lines: Any, snapshot_id: str,
                         tenant_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        items = lines if isinstance(lines, list) else []
        for index, raw in enumerate(items, start=1):
            item = raw if isinstance(raw, dict) else {}
            rows.append({
                "line_id": f"{snapshot_id}::L{index:04d}",
                "seq": index,
                "element": str(item.get("element") or "material"),
                "material_code": str(item.get("material_code") or ""),
                "asset_code": str(item.get("asset_code") or ""),
                "category": str(item.get("category") or ""),
                "quantity": _num(item.get("quantity")),
                "unit_price": _num(item.get("unit_price")),
                "amount": _num(item.get("amount")),
                "source_kind": str(item.get("source_kind") or ""),
                "source_ref": str(item.get("source_ref") or ""),
                "evidence": _dumps(item.get("evidence")),
            })
        return rows

    def get_snapshot(self, snapshot_id: str, tenant_id: str = "default") -> dict[str, Any] | None:
        """快照 + 明细（只读回读）。"""
        snapshot = self._row(
            "SELECT * FROM m6_snapshots WHERE snapshot_id=? AND tenant_id=?",
            (snapshot_id, tenant_id),
        )
        if snapshot is None:
            return None
        snapshot["evidence"] = _loads(snapshot.get("evidence"), {})
        snapshot["cost_incomplete"] = bool(snapshot.get("cost_incomplete"))
        snapshot["lines"] = [
            {**row, "evidence": _loads(row.get("evidence"), {})}
            for row in self._rows(
                "SELECT * FROM m6_cost_lines WHERE snapshot_id=? ORDER BY seq",
                (snapshot_id,),
            )
        ]
        return snapshot

    def list_snapshots(self, *, period: str | None = None, status: str | None = None,
                       order_id: str | None = None,
                       tenant_id: str = "default") -> list[dict[str, Any]]:
        where = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if period:
            where.append("period = ?")
            params.append(period)
        if status:
            where.append("status = ?")
            params.append(status)
        if order_id:
            where.append("order_id = ?")
            params.append(order_id)
        rows = self._rows(
            f"SELECT * FROM m6_snapshots WHERE {' AND '.join(where)}"
            " ORDER BY computed_at DESC, snapshot_id DESC",
            tuple(params),
        )
        for row in rows:
            row["evidence"] = _loads(row.get("evidence"), {})
            row["cost_incomplete"] = bool(row.get("cost_incomplete"))
        return rows

    def confirm_snapshot(self, snapshot_id: str, *, actor: str = "",
                         tenant_id: str = "default",
                         confirmed_at: str | None = None) -> dict[str, Any]:
        """**commit 段**：``trial → confirmed``（由门批准后的 `_apply_m6_*` 调用）。

        - 快照不存在 → ``NOT_FOUND``；
        - 已是 ``confirmed`` → 幂等返回（``changed=False``）；
        - 该期间月账已冻结 → ``MONTH_CLOSED``（冻结后不得再确认）。
        """
        snapshot = self._row(
            "SELECT * FROM m6_snapshots WHERE snapshot_id=? AND tenant_id=?",
            (snapshot_id, tenant_id),
        )
        if snapshot is None:
            return {"success": False, "code": "NOT_FOUND", "snapshot_id": snapshot_id}
        if snapshot["status"] == STATUS_CONFIRMED:
            return {"success": True, "snapshot_id": snapshot_id, "status": STATUS_CONFIRMED,
                    "changed": False}
        if self._month_closed(str(snapshot["period"]), tenant_id):
            return {"success": False, "code": "MONTH_CLOSED", "snapshot_id": snapshot_id,
                    "period": snapshot["period"]}
        confirmed_at = confirmed_at or now_iso()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE m6_snapshots SET status=?, confirmed_at=?, confirmed_by=?"
                " WHERE snapshot_id=? AND tenant_id=? AND status=?",
                (STATUS_CONFIRMED, confirmed_at, actor, snapshot_id, tenant_id, STATUS_TRIAL),
            )
            conn.commit()
        finally:
            conn.close()
        return {"success": True, "snapshot_id": snapshot_id, "status": STATUS_CONFIRMED,
                "changed": True, "confirmed_at": confirmed_at, "period": snapshot["period"]}

    def list_periods(self, tenant_id: str = "default") -> list[str]:
        """已有账期的期间列表（快照期间 ∪ 已结账期间），倒序——供月度汇总列表用。"""
        rows = self._rows(
            "SELECT period FROM m6_snapshots WHERE tenant_id=?"
            " UNION SELECT period FROM m6_month_close WHERE tenant_id=?"
            " ORDER BY period DESC",
            (tenant_id, tenant_id),
        )
        return [str(row["period"]) for row in rows]

    def month_summary(self, period: str, tenant_id: str = "default") -> dict[str, Any]:
        """月末汇总：**只加 ``confirmed``**（试算不进账，计划 §6 的 A 问题）。

        月账已冻结时返回冻结快照（不再随新确认变动）。
        """
        frozen = self.get_month_close(period, tenant_id)
        if frozen is not None:
            return {"period": period, "frozen": True,
                    "closed_at": frozen["closed_at"], "closed_by": frozen["closed_by"],
                    "snapshot_count": int(frozen["snapshot_count"]),
                    "totals": _loads(frozen["totals"], {}),
                    "trial_count": len(self.list_snapshots(period=period, status=STATUS_TRIAL,
                                                           tenant_id=tenant_id))}
        confirmed = self.list_snapshots(period=period, status=STATUS_CONFIRMED,
                                       tenant_id=tenant_id)
        trial = self.list_snapshots(period=period, status=STATUS_TRIAL, tenant_id=tenant_id)
        total_cost = sum(_num(row.get("total_cost")) or 0.0 for row in confirmed)
        return {
            "period": period,
            "frozen": False,
            "snapshot_count": len(confirmed),
            "total_cost": round(total_cost, 4),
            "trial_count": len(trial),
            "trial_excluded": True,
            "by_order": _group_by_order(confirmed),
        }

    # ---------- 单据（报价单 / 送货单 / 对账单）----------

    def find_document(self, *, doc_type: str, doc_no: str,
                      tenant_id: str = "default") -> dict[str, Any] | None:
        """查重：单据侧业务键 = ``doc_type + doc_no``。"""
        return self._row(
            "SELECT * FROM m6_documents WHERE tenant_id=? AND doc_type=? AND doc_no=?"
            " ORDER BY created_at DESC, doc_id DESC LIMIT 1",
            (tenant_id, doc_type, doc_no),
        )

    def save_document(self, *, doc_id: str, doc_no: str, doc_type: str,
                      counterparty_code: str = "", doc_date: str = "", direction: str = "",
                      amount: Any = None, lines: Any = None, source_ref: str = "",
                      evidence: Any = None, task_id: str = "",
                      tenant_id: str = "default",
                      created_at: str | None = None) -> dict[str, Any]:
        """**propose 段**：写入 ``status=trial`` 单据（不构成生效凭据）。"""
        created_at = created_at or now_iso()
        existing = self.find_document(doc_type=doc_type, doc_no=doc_no, tenant_id=tenant_id)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO m6_documents(doc_id, tenant_id, task_id, doc_no, doc_type,"
                " counterparty_code, doc_date, direction, amount, status, lines, source_ref,"
                " evidence, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, tenant_id, task_id, doc_no, doc_type, counterparty_code, doc_date,
                 direction, _num(amount), STATUS_TRIAL, _dumps(lines if lines is not None else []),
                 source_ref, _dumps(evidence), created_at),
            )
            conn.commit()
        finally:
            conn.close()
        return {"success": True, "doc_id": doc_id, "status": STATUS_TRIAL,
                "duplicate_of": ({"doc_id": existing["doc_id"], "status": existing["status"]}
                                 if existing else None)}

    def confirm_document(self, doc_id: str, *, actor: str = "", tenant_id: str = "default",
                         confirmed_at: str | None = None) -> dict[str, Any]:
        """**commit 段**：单据 ``trial → confirmed``（生效凭据）。"""
        doc = self._row(
            "SELECT * FROM m6_documents WHERE doc_id=? AND tenant_id=?", (doc_id, tenant_id)
        )
        if doc is None:
            return {"success": False, "code": "NOT_FOUND", "doc_id": doc_id}
        if doc["status"] == STATUS_CONFIRMED:
            return {"success": True, "doc_id": doc_id, "status": STATUS_CONFIRMED,
                    "changed": False}
        confirmed_at = confirmed_at or now_iso()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE m6_documents SET status=?, confirmed_at=?, confirmed_by=?"
                " WHERE doc_id=? AND tenant_id=? AND status=?",
                (STATUS_CONFIRMED, confirmed_at, actor, doc_id, tenant_id, STATUS_TRIAL),
            )
            conn.commit()
        finally:
            conn.close()
        return {"success": True, "doc_id": doc_id, "status": STATUS_CONFIRMED, "changed": True}

    def list_documents(self, *, doc_type: str | None = None, status: str | None = None,
                       counterparty_code: str | None = None,
                       tenant_id: str = "default") -> list[dict[str, Any]]:
        where = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if doc_type:
            where.append("doc_type = ?")
            params.append(doc_type)
        if status:
            where.append("status = ?")
            params.append(status)
        if counterparty_code:
            where.append("counterparty_code = ?")
            params.append(counterparty_code)
        rows = self._rows(
            f"SELECT * FROM m6_documents WHERE {' AND '.join(where)}"
            " ORDER BY created_at DESC, doc_id DESC",
            tuple(params),
        )
        for row in rows:
            row["lines"] = _loads(row.get("lines"), [])
            row["evidence"] = _loads(row.get("evidence"), {})
        return rows


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _group_by_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按订单加总（月末汇总的明细面）。"""
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        key = str(row.get("order_id") or "")
        if key not in grouped:
            order.append(key)
            grouped[key] = {"order_id": key, "snapshot_count": 0, "total_cost": 0.0,
                            "products": []}
        entry = grouped[key]
        entry["snapshot_count"] += 1
        entry["total_cost"] = round(entry["total_cost"] + (_num(row.get("total_cost")) or 0.0), 4)
        entry["products"].append(str(row.get("product_code") or ""))
    return [grouped[key] for key in order]
