---
name: yunpai-m6-ledger
description: Record and query the M6 costing ledger: cost snapshots and lines, month closing and monthly summary, quotation and statement registers, delivery notes, and mold/machine asset ledger. Writes follow propose-approve-commit and only touch the M6 database.
---

# M6 记（成本明细账与台账）

## 职责

把「算」出来的成本与凭据**记进账**：成本快照与明细、月结汇总、报价单台账、对账单台账、
送货单凭据、模具/机器资产台账。查询与写入都在本 Skill 内，但两者段位不同——见下。

## 工具映射

| 操作 | Tool | 段 | 说明 |
| --- | --- | --- | --- |
| `snapshots`（default） | `list_costing_snapshots` | 读 | 快照列表（按期间/订单/产品过滤） |
| `snapshot` | `get_costing_snapshot` | 读 | 单张快照 + 明细（`NOT_FOUND` 显式返回） |
| `month` | `list_month_costing` | 读 | 月末汇总（**只认 `confirmed`**，并明示 `trial_excluded`） |
| `save_snapshot` | `save_costing_snapshot` | propose | 算一版成本并冻结为 `trial` 快照（**刻意无门**，D-008） |
| `confirm_snapshot` | `confirm_costing_snapshot` | commit 发起 | 报待确认事实 → `finance` 门 |
| `close_month` | `close_month_costing` | commit 发起 | 报待结账期 → `finance` 门 |
| `quotations` | `list_quotations` | 读 | 报价单台账 |
| `quotation` | `get_quotation` | 读 | 单张报价单 |
| `save_quotation` | `save_quotation` | propose | 写 `trial` 草稿 + `pending_document_commit` → `finance` 门 |
| `statements` | `list_statements` | 读 | 对账单台账 |
| `save_statement` | `save_statement` | propose | 写 `trial` 草稿 + `pending_document_commit` → `finance` 门 |
| `delivery_note` | `get_delivery_note` | 读 | canonical 送货单（**只读，不建 create**） |
| `asset` | `get_asset_ledger` | 读 | 资产台账（生效值与待确认草稿**分列**） |
| `save_asset` | `upsert_asset_ledger` | propose | 追加 `trial` 修订 + `pending_asset_commit` → `finance` 门 |

调用示例：

```json
{
  "skill": "yunpai-m6-ledger",
  "skill_payload": {
    "operation": "save_snapshot",
    "tool_payload": {"period": "2026-09", "order_id": "SO-001", "batch_no": "B-01", "quantity": 500}
  }
}
```

不写 `operation` 时走 default（`list_costing_snapshots`）；两个 Skill 的 default 都是**纯读工具**，
所以漏写 operation 绝不会触发写库或开门。

## 三段式（D-005 的约定字段）

propose 段只写**草稿**（`status=trial`）或只回报待确认事实；**生效**只在 `finance` 门批准后由
`graph._apply_m6_*` 的 commit 段落库：

| propose 工具 | 约定字段 | commit 钩子 |
| --- | --- | --- |
| `save_costing_snapshot` | —（试算快照无门，D-008） | — |
| `confirm_costing_snapshot` | `data.pending_confirmation` | `_apply_m6_costing_confirm` |
| `close_month_costing` | `data.pending_close` | `_apply_m6_close_month` |
| `save_quotation` / `save_statement` | `data.pending_document_commit` + `doc_id` | `_apply_m6_document_commit` |
| `upsert_asset_ledger` | `data.pending_asset_commit` + `asset_id` | `_apply_m6_asset_commit` |

**拒批（reject）不落任何生效行**：草稿保留可追溯，但不进月末汇总、不成为「正式成本」。
门批准前库里仍是 `trial`——这正是本 Skill 与存量 M0–M5 写工具的区别所在。

## 载荷契约（对齐 G2 实装 `worker/executor.py:skill_payload`）

派发 Skill 时，顶层 `message` / `product_code` / `product_name` / `attachments` / `documents`
**仅在缺键时**并入载荷，并补 `files` 别名（= `documents` 或 `attachments`）：调用方显式写进
`request[技能名]` 的值永远优先。业务参数放 `skill_payload`（或 `skill_payload.tool_payload`）。

## 规则（红线）

- **本 Skill 不写 M0/M5；写自己的 m6 库**（`YUNPAI_M6_DB` / `runtime/yunpai-m6.sqlite`）。
  送货单是唯一例外且是**只读**例外：D-009 规定 canonical 是送货单唯一主，M6 **只读、不提供
  create 工具**（写入面在 M0 侧导入）。可调用工具面由 operation map 白名单在**代码级**兜住
  （显式传越界的 `tool` 即被拒绝，见 `src/yunpai_orchestrator/m6_tooling.py`）。
- **试算不污染账**：`trial` 不进月末汇总（月结只认 `confirmed`）；月账冻结后该期间拒绝再写
  （`MONTH_CLOSED`）、重复结账 `MONTH_ALREADY_CLOSED`。这两条护栏编在存储层，不靠调用方自觉。
- **同号单据拒绝**（`DOC_NO_EXISTS`），单号确定性派生，本 Skill 不覆盖已有单据。
- **资产台账只追加修订**（`asset_code` + `revision`），**绝不改写已确认原值**；已有待确认修订时
  拒堆草稿（`ASSET_PENDING_EXISTS`）。
- **缺数不编造**：依据缺失即拒出单（不以空明细出账），缺数标 `missing`，绝不报「金额为零」。

完整接口、字段与 operation 对照见 [references/tools.md](references/tools.md)。
