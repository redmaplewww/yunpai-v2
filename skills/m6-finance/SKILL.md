---
name: yunpai-m6-finance
description: Compute product and order cost, audit order profitability, preview quotations and statements, calculate piece-rate and monthly payroll, allocate mold/machine benefit, and read the finance view of inventory. Use for M6 cost and finance calculations.
---

# M6 算（成本与财务核算）

## 职责

以 M0 canonical 的 BOM/工艺/库存/主数据事实与 M5 报工事实为输入，**确定性**核算成本、利润、
报价、对账、工资与模具/机器效益分摊。事实值照抄，缺数据显式标 `cost_incomplete` / `missing`，
**绝不编造**；计算不 agent 化。

本 Skill **只管算**（D8 的「试算／报价预览」）：所有工具 `side_effect=none`、无门、不落库、
不改任何库。要让成本进账、单据生效，走 `yunpai-m6-ledger`。

## 工具映射

| 操作 | Tool | 说明 |
| --- | --- | --- |
| `product_cost`（default） | `get_product_cost` | 产品单台成本（当场算 = 试算／报价预览） |
| `audit` | `audit_order_cost` | 订单成本审计与毛利（`passed` / `low_profit` / `loss` / `cost_incomplete`） |
| `quotation` | `generate_quotation` | 报价预览（不落库；落库见 ledger 的 `save_quotation`） |
| `statement` | `generate_statement` | 对账明细预览（客户=送货单外发货 / 供应商=M4 已入库行） |
| `piece_pay` | `calculate_piece_pay` | 计件工资 |
| `monthly_pay` | `calculate_monthly_pay` | 月薪工资 |
| `asset_benefit` | `compute_asset_benefit` | 模具/机器效益分摊 |
| `allocate_expenses` | `allocate_expenses` | 费用按产量/工时/人数/订单数分摊 |
| `inventory_view` | `get_inventory_finance_view` | 库存财务视图（四态分账 + 在途） |
| `orders` | `list_orders` | canonical 订单列表（订单成本审计的输入面） |

调用示例（业务参数放 `tool_payload`，也可直接平铺在载荷里）：

```json
{
  "skill": "yunpai-m6-finance",
  "skill_payload": {
    "operation": "audit",
    "tool_payload": {"period": "2026-09", "order_id": "SO-001"}
  }
}
```

不写 `operation` 时走 default（`get_product_cost`）；两个 Skill 的 default 都是**纯读工具**，
所以漏写 operation 绝不会触发写库或开门。

## 载荷契约（对齐 G2 实装 `worker/executor.py:skill_payload`）

派发 Skill 时，顶层 `message` / `product_code` / `product_name` / `attachments` / `documents`
**仅在缺键时**并入载荷，并补 `files` 别名（= `documents` 或 `attachments`）：调用方显式写进
`request[技能名]` 的值永远优先。因此**产品编码放顶层 `product_code` 即可**（例如「算一下 W-H913
一台多少钱」），其余业务参数放 `skill_payload`（或 `skill_payload.tool_payload`）。

## 规则（红线）

- **本 Skill 不写 M0/M5；写自己的 m6 库**——「算」这一侧连自己的库也不写：全部工具纯算数，
  不落库、无门。可调用工具面由 operation map 白名单在**代码级**兜住（显式传越界的 `tool` 即被
  拒绝，见 `src/yunpai_orchestrator/m6_tooling.py`），不靠文档自律。
- **缺数不编造**：缺单价/用量/工时/费率 → 该行标 `cost_incomplete`，`audit_order_cost` 不得据此
  判 `passed`；口径值取自 `m6_defaults.py` 时必须带 `assumed=true` 痕并附
  `PENDING_FINANCE_CONFIRMATION`。
- **单位/拼写不确定时不猜**（猜单位＝静默倍数误差，例如 SOP 工序工时的单位口径未定）。
- 敏感值（单价/成本/工资/报价金额）查询需 `cost.view` / `finance.view` 授权，不进普通查询载荷。
- 工资与效益分摊口径（加班倍数/计薪天数/分摊基准）为可配默认值，**最终以工厂财务确认口径为准**。

完整接口、字段与 operation 对照见 [references/tools.md](references/tools.md)。
