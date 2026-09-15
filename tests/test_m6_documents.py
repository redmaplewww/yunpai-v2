"""B2：M6 单据台账（报价单 / 对账单）——三段式的第二个落点。

要点（与 B1 第二批同标准）：

1. **propose 段**：`save_quotation` / `save_statement` 只写 ``status=trial`` 草稿，
   并回报 ``pending_document_commit``；**生效**（``trial → confirmed``）由
   `graph._apply_m6_document_commit` 在 finance 门 approve 后执行——这是该钩子的
   第一个真实用户（B1 第二批只锁了它的约定）；
2. **reject 不生效**：拒绝后草稿保留（可追溯）但库内 ``confirmed`` 计数为 0；
3. **单号不静默覆盖**：同号单据显式拒绝（``DOC_NO_EXISTS``），让人选新建/覆盖/查看；
4. **缺成本不报价**：行内没给三项单位成本且 `products[code]` 也滚不出来 → 该行进
   `missing` 且**不计入合计**（缺成本算出来的"报价"就是编造）；加价率未给按 0 算但标
   ``markup_assumed=True``；对账单没有对账明细即拒（不以空明细出账）。
"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from test_graph_smoke import _deps

from yunpai_orchestrator.graph import build_graph
from yunpai_orchestrator.m6_store import M6Store, STATUS_CONFIRMED, STATUS_TRIAL
from yunpai_orchestrator.repository import InMemoryRunRepository
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.reviewer import rules
from yunpai_orchestrator.state import new_state_v2

FINANCE_ROLES = ("finance-officer", "admin")

#: 与 B1 同源的成本事实：单台 102.3（材料 32.3 + 人工 50 + 制费 20）。
BOM_LINES = [
    {"material_code": "MAT-A", "qty_per": 2, "unit_price": 5.0, "loss_rate": 0.1},
    {"material_code": "MAT-B", "qty_per": 1, "unit_price": 8.0, "loss_rate": 0.0},
    {"material_code": "MAT-C", "qty_per": 4, "unit_price": 3.0, "loss_rate": 0.0},
]
ROUTING_STEPS = [{"operation_id": "OP-10", "standard_minutes": 60}]
INVENTORY = [{"material_code": "MAT-A", "available_qty": 100, "stock_class": "raw"}]
TRACKING_ROWS = [
    {"id": 7, "purchase_order_no": "PO-1", "purchase_order_item_id": 21,
     "promised_date": "2026-09-01", "unit_price": "8.5", "currency": "CNY"},
    {"id": 8, "purchase_order_no": "PO-2", "purchase_order_item_id": 22,
     "promised_date": "2026-09-02", "unit_price": "3.2", "currency": "CNY"},
]
ORDER_ITEMS = [{"id": 21, "item_code": "MAT-B"}, {"id": 22, "item_code": "MAT-C"}]


def _ctx(tmp_path, **extra):
    return {"tenant_id": "default", "task_id": "task-m6-doc",
            "m6_db_path": str(tmp_path / "m6.sqlite"), **extra}


def _product_facts(**overrides):
    facts = {"bom_lines": BOM_LINES, "routing_steps": ROUTING_STEPS,
             "inventory": INVENTORY, "hour_rate": 50, "overhead_rate": 20,
             "purchase_tracking_rows": TRACKING_ROWS,
             "purchase_order_items": ORDER_ITEMS}
    facts.update(overrides)
    return facts


@pytest.fixture
def registry():
    return build_default_registry()


# ---------------------------------------------------------------------------
# generate_quotation：报价预览（纯算数，不落库）
# ---------------------------------------------------------------------------

async def test_generate_quotation_computes_from_facts_without_writing(registry, tmp_path):
    ctx = _ctx(tmp_path)
    result = await registry.call("generate_quotation", {
        "customer_code": "CUST-01", "markup_rate": 0.2,
        "lines": [{"product_code": "W-H913", "qty": 10}],
        "products": {"W-H913": _product_facts()},
    }, ctx)
    assert result["success"] is True, result.get("errors")
    line = result["data"]["lines"][0]
    assert line["base_cost"] == 102.3                 # 成本按 D5 口径滚动出来
    assert line["quote_price"] == 122.76              # ×1.2 加价
    assert line["line_total"] == 1227.6
    assert result["data"]["total"] == 1227.6
    assert result["data"]["assumptions"]["markup_rate_assumed"] is False

    # 纯算数：一行都不落库
    store = M6Store(ctx["m6_db_path"])
    assert store.list_documents() == []


async def test_generate_quotation_refuses_to_price_unknown_cost(registry, tmp_path):
    """缺成本的行**不进报价**（老仓把缺成本当 0 算，会报出"成本为零"的价）。"""
    ctx = _ctx(tmp_path)
    result = await registry.call("generate_quotation", {
        "lines": [{"product_code": "W-H913", "qty": 10}],   # 无 products 事实
    }, ctx)
    assert result["success"] is False and result["code"] == "INVALID_INPUT"
    assert result["data"]["missing"][0]["reason"] == "missing_unit_cost"

    # 部分可算：可算的行照常报价，缺成本的行进 missing 且不计入合计
    mixed = await registry.call("generate_quotation", {
        "markup_rate": 0.1,
        "lines": [{"product_code": "W-H913", "qty": 1},
                  {"product_code": "W-UNKNOWN", "qty": 5}],
        "products": {"W-H913": _product_facts()},
    }, ctx)
    data = mixed["data"]
    assert len(data["lines"]) == 1 and data["lines"][0]["product_code"] == "W-H913"
    assert data["total"] == 112.53                       # 102.3 × 1.1，未把未知产品的 0 混进来
    assert data["missing"] == [{"line": 2, "reason": "missing_unit_cost",
                                "product_code": "W-UNKNOWN"}]


async def test_generate_quotation_flags_assumed_markup(registry, tmp_path):
    """加价率没给 → 按 0 算但**必须留痕**（0 加价不能冒充谈好的报价）。"""
    ctx = _ctx(tmp_path)
    result = await registry.call("generate_quotation", {
        "lines": [{"product_code": "W-H913", "qty": 1}],
        "products": {"W-H913": _product_facts()},
    }, ctx)
    assert result["data"]["lines"][0]["quote_price"] == 102.3     # 未加价
    assert result["data"]["assumptions"]["markup_rate_assumed"] is True
    assert result["data"]["assumptions"]["pending_finance_confirmation"]


# ---------------------------------------------------------------------------
# save_quotation / save_statement：propose 段（只落 trial 草稿）
# ---------------------------------------------------------------------------

async def test_save_quotation_writes_draft_and_asks_for_commit(registry, tmp_path):
    ctx = _ctx(tmp_path)
    result = await registry.call("save_quotation", {
        "customer_code": "CUST-01", "doc_date": "2026-09-14", "markup_rate": 0.2,
        "lines": [{"product_code": "W-H913", "qty": 10}],
        "products": {"W-H913": _product_facts()},
    }, ctx)
    assert result["success"] is True, result.get("errors")
    data = result["data"]
    assert data["status"] == STATUS_TRIAL                # propose 段只落草稿
    assert data["pending_document_commit"] is True       # 约定字段：等 finance 门
    assert data["doc_no"] == "QT-20260914-001"
    assert data["direction"] == "out"
    assert data["total"] == 1227.6
    assert data["duplicate_of"] is None

    store = M6Store(ctx["m6_db_path"])
    doc = store.get_document(data["doc_id"])
    assert doc["status"] == STATUS_TRIAL                 # 库里是草稿，不是生效凭据
    assert doc["lines"][0]["quote_price"] == 122.76
    assert doc["evidence"]["total"] == 1227.6


async def test_save_quotation_refuses_duplicate_doc_no(registry, tmp_path):
    ctx = _ctx(tmp_path)
    payload = {"customer_code": "CUST-01", "doc_no": "QT-0001",
               "lines": [{"product_code": "W-H913", "qty": 1}],
               "products": {"W-H913": _product_facts()}}
    first = await registry.call("save_quotation", payload, ctx)
    assert first["success"] is True
    second = await registry.call("save_quotation", payload, ctx)
    assert second["success"] is False and second["code"] == "DOC_NO_EXISTS"
    # 派生单号则各成一张（历史不覆盖）
    derived = await registry.call("save_quotation",
                                  {**payload, "doc_no": ""}, ctx)
    assert derived["data"]["doc_no"] != first["data"]["doc_no"]


async def test_save_statement_computes_balance_and_direction(registry, tmp_path):
    ctx = _ctx(tmp_path)
    result = await registry.call("save_statement", {
        "counterparty_code": "CUST-01", "statement_type": "customer",
        "doc_date": "2026-09-30", "opening_balance": 1000.0,
        "transactions": [{"direction": "in", "amount": 500.0, "ref": "DN-1"},
                         {"direction": "out", "amount": 200.0, "ref": "收款-1"}],
    }, ctx)
    data = result["data"]
    assert data["status"] == STATUS_TRIAL
    assert data["pending_document_commit"] is True
    assert data["opening_balance"] == 1000.0
    assert data["inflow"] == 500.0 and data["outflow"] == 200.0
    assert data["closing_balance"] == 1300.0
    assert data["amount"] == 1300.0
    assert data["direction"] == "out"                    # customer → 我方对外
    # B3 起明细来源统一由 `_statement_transactions` 判定：显式给的就是 "explicit"
    # （依据自动生成时为 delivery_notes / purchase_receipts）
    assert data["totals_source"] == "explicit"

    # supplier 方向相反
    supplier = await registry.call("save_statement", {
        "counterparty_code": "SUP-01", "statement_type": "supplier",
        "opening_balance": 0.0, "transactions": [{"direction": "in", "amount": 300.0}],
    }, ctx)
    assert supplier["data"]["direction"] == "in"


async def test_save_statement_requires_reconciliation_detail(registry, tmp_path):
    ctx = _ctx(tmp_path)
    result = await registry.call("save_statement",
                                 {"counterparty_code": "CUST-01"}, ctx)
    assert result["success"] is False and result["code"] == "INVALID_INPUT"
    assert M6Store(ctx["m6_db_path"]).list_documents() == []


# ---------------------------------------------------------------------------
# 读工具：台账与单张回读
# ---------------------------------------------------------------------------

async def test_list_and_get_documents(registry, tmp_path):
    ctx = _ctx(tmp_path)
    await registry.call("save_quotation", {
        "customer_code": "CUST-01", "doc_date": "2026-09-14",
        "lines": [{"product_code": "W-H913", "qty": 1, "unit_material": 10.0,
                   "unit_labor": 5.0, "unit_overhead": 5.0, "markup_rate": 0.5}],
    }, ctx)
    store = M6Store(ctx["m6_db_path"])
    store.confirm_document("QUOTATION-QT-20260914-001", actor="finance-officer")

    listed = await registry.call("list_quotations", {"customer_code": "CUST-01"}, ctx)
    assert listed["data"]["count"] == 1
    assert listed["data"]["quotations"][0]["status"] == STATUS_CONFIRMED
    trial_only = await registry.call("list_quotations", {"status": "trial"}, ctx)
    assert trial_only["data"]["quotations"] == []

    got = await registry.call("get_quotation", {"doc_no": "QT-20260914-001"}, ctx)
    assert got["data"]["quotation"]["doc_no"] == "QT-20260914-001"
    assert got["data"]["quotation"]["lines"][0]["quote_price"] == 30.0
    assert (await registry.call("get_quotation", {"doc_no": "QT-NOPE"}, ctx))["code"] == "NOT_FOUND"
    assert (await registry.call("get_quotation", {}, ctx))["code"] == "INVALID_INPUT"

    statements = await registry.call("list_statements", {}, ctx)
    assert statements["data"]["statements"] == []        # 单据类型隔离


# ---------------------------------------------------------------------------
# 三段式端到端：propose → finance 门 → approve 才生效 / reject 不生效
# ---------------------------------------------------------------------------

def _run(graph, payload, *, threads, limit=96):
    thread_id = (payload.get("thread_id") if isinstance(payload, dict) else None) or threads[id(graph)]
    return asyncio.run(graph.ainvoke(
        payload, {"configurable": {"thread_id": thread_id}, "recursion_limit": limit}))


def _pending(out: dict) -> dict:
    assert out.get("__interrupt__"), "应挂起在人工门"
    info = out["__interrupt__"][0]
    return info.value if hasattr(info, "value") else info


def _graph_state(tmp_path, monkeypatch, tools, repository=None, **request_extra):
    monkeypatch.setenv("YUNPAI_M6_DB", str(tmp_path / "m6.sqlite"))
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0-absent.sqlite"))
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "m4b-absent.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)
    graph = build_graph(_deps(repository), checkpointer=MemorySaver())
    request = {"message": "出张报价单", "tools": tools,
               "customer_code": "CUST-01", "doc_date": "2026-09-14",
               "markup_rate": 0.2, "bom_lines": BOM_LINES,
               "routing_steps": ROUTING_STEPS, "inventory": INVENTORY,
               "hour_rate": 50, "overhead_rate": 20,
               "purchase_tracking_rows": TRACKING_ROWS,
               "purchase_order_items": ORDER_ITEMS,
               **request_extra}
    if "save_quotation" in tools:
        # 报价需要行（产品 + 数量）；对账单靠 transactions，不注入报价行
        request.setdefault("lines", [{"product_code": "W-H913", "qty": 10}])
    state = new_state_v2(request)
    return graph, state


def _confirmed_docs(db) -> list[dict]:
    return M6Store(str(db)).list_documents(status=STATUS_CONFIRMED)


def test_quotation_propose_then_finance_gate_commit(tmp_path, monkeypatch):
    repo = InMemoryRunRepository()
    graph, state = _graph_state(tmp_path, monkeypatch, ["save_quotation"], repository=repo)
    threads: dict[int, str] = {id(graph): state["thread_id"]}
    db = tmp_path / "m6.sqlite"

    out = _run(graph, state, threads=threads)
    saved = out["outputs"]["save_quotation"]["data"]
    assert saved["status"] == STATUS_TRIAL               # propose：库里是草稿
    gate = _pending(out)["gate"]
    assert gate["type"] == "finance" and gate["tool"] == "save_quotation"
    assert M6Store(str(db)).get_document(saved["doc_id"])["status"] == STATUS_TRIAL
    assert _confirmed_docs(db) == []                      # 未批准前不得生效
    assert repo.get(state["run_id"])["pending_gate"]["type"] == "finance"

    resumed = _run(graph, Command(resume={"decision": "approve", "actor": "fin-01",
                                          "roles": list(FINANCE_ROLES)}), threads=threads)
    assert resumed["status"] == "completed", f"errors={resumed.get('errors')}"
    doc = M6Store(str(db)).get_document(saved["doc_id"])
    assert doc["status"] == STATUS_CONFIRMED              # commit 钩子翻正
    assert doc["confirmed_by"] == "fin-01"
    assert resumed["outputs"]["save_quotation"]["data"]["pending_document_commit"] is False
    assert resumed["outputs"]["save_quotation"]["result"] == resumed["outputs"]["save_quotation"]["data"]
    assert any(e.get("event") == "m6.document_confirmed" for e in resumed.get("trace") or [])
    assert [row["action"] for row in resumed.get("review_applied") or []] == ["commit"]


def test_quotation_reject_keeps_draft_and_no_effective_doc(tmp_path, monkeypatch):
    graph, state = _graph_state(tmp_path, monkeypatch, ["save_quotation"])
    threads: dict[int, str] = {id(graph): state["thread_id"]}
    db = tmp_path / "m6.sqlite"

    out = _run(graph, state, threads=threads)
    doc_id = out["outputs"]["save_quotation"]["data"]["doc_id"]
    assert _pending(out)["gate"]["type"] == "finance"

    resumed = _run(graph, Command(resume={"decision": "reject", "actor": "fin-01",
                                          "roles": list(FINANCE_ROLES),
                                          "note": "价格待确认"}), threads=threads)
    assert resumed["status"] == "failed"
    assert _confirmed_docs(db) == []                      # reject：库内 confirmed 计数 0
    assert M6Store(str(db)).get_document(doc_id)["status"] == STATUS_TRIAL   # 草稿留着可追溯
    assert not resumed.get("review_applied")


def test_statement_writes_only_after_finance_approve(tmp_path, monkeypatch):
    graph, state = _graph_state(
        tmp_path, monkeypatch, ["save_statement"], statement_type="customer",
        opening_balance=1000.0,
        transactions=[{"direction": "in", "amount": 500.0, "ref": "DN-1"}])
    threads: dict[int, str] = {id(graph): state["thread_id"]}
    db = tmp_path / "m6.sqlite"

    out = _run(graph, state, threads=threads)
    assert out["outputs"]["save_statement"]["data"]["pending_document_commit"] is True
    assert _pending(out)["gate"]["tool"] == "save_statement"
    assert M6Store(str(db)).list_documents(status=STATUS_CONFIRMED) == []

    resumed = _run(graph, Command(resume={"decision": "approve", "actor": "fin-01",
                                          "roles": list(FINANCE_ROLES)}), threads=threads)
    assert resumed["status"] == "completed", f"errors={resumed.get('errors')}"
    docs = M6Store(str(db)).list_documents(doc_type="statement")
    assert [doc["status"] for doc in docs] == [STATUS_CONFIRMED]
    assert docs[0]["amount"] == 1500.0
    assert docs[0]["direction"] == "out"


def test_finance_gate_roles_still_enforced_for_documents(tmp_path, monkeypatch):
    graph, state = _graph_state(tmp_path, monkeypatch, ["save_quotation"])
    threads: dict[int, str] = {id(graph): state["thread_id"]}
    db = tmp_path / "m6.sqlite"
    out = _run(graph, state, threads=threads)
    doc_id = out["outputs"]["save_quotation"]["data"]["doc_id"]

    denied = _run(graph, Command(resume={"decision": "approve", "actor": "op-01",
                                         "roles": ["operator"]}), threads=threads)
    refused = _pending(denied)
    assert refused["type"] == "gate_invalid"              # 越权不得静默通过
    assert _confirmed_docs(db) == []

    allowed = _run(graph, Command(resume={"decision": "approve", "actor": "fin-01",
                                          "roles": list(FINANCE_ROLES)}), threads=threads)
    assert allowed["status"] == "completed"
    assert M6Store(str(db)).get_document(doc_id)["status"] == STATUS_CONFIRMED


# ---------------------------------------------------------------------------
# 契约 ↔ 规则一致性
# ---------------------------------------------------------------------------

def test_document_contracts_and_rules_agree(registry):
    expected = {
        "generate_quotation": ("none", "none", ""),
        "save_quotation": ("local_write", "finance", "finance"),
        "list_quotations": ("none", "none", ""),
        "get_quotation": ("none", "none", ""),
        "save_statement": ("local_write", "finance", "finance"),
        "list_statements": ("none", "none", ""),
    }
    for tool, (side_effect, review_gate, gate) in expected.items():
        spec = registry.specs[tool]
        assert spec.module == "m6", tool
        assert spec.side_effect == side_effect, tool
        assert spec.review_gate == review_gate, tool
        assert rules.gate_type_for(tool, spec) == gate, tool
    assert rules.AUTO_APPROVE_ALLOWED is False
    # 草稿声明必须真的触发门：没有 pending_document_commit 就不开（例如幂等重放）
    assert rules.evaluate("save_quotation",
                          {"success": True, "data": {"pending_document_commit": False}}) == []
    assert [f["gate"] for f in rules.evaluate(
        "save_quotation", {"success": True, "data": {"pending_document_commit": True}})] == \
        ["finance"]
