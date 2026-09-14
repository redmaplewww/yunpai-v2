"""M6 工具面声明（Skill 层与注册表共用）。

F-008 / D-005：M6 的写工具一律走 **propose → approve → commit 三段式**。两个 Skill
（``yunpai-m6-finance`` 算 / ``yunpai-m6-ledger`` 记）只经本文件的 operation map 派发，
**map 的取值集合即该 Skill 的完整可调用工具面**——调用方即使显式传 ``tool``，只要不在
集合内就被 ``skills._dispatch_registered_tool`` 拒绝（边界见该函数 docstring）。这正是
红线「本 Skill 不写 M0/M5；写自己的 m6 库」的**代码级**落实（回归见
``tests/test_m6_skills.py``），不靠文档自律。

单一来源：``skills.py`` 的 handler 与 ``SkillSpec.tools`` 都从下面的常量派生
（``unique_tools``），``tests/test_skill_registry_consistency.py`` 直接 import 本常量做
对齐校验 → **不存在第二份手写白名单**（老仓 ``registry/tool-manifests/m6.json`` 与
``src/**/manifests/m6.json`` 漂移成两份的教训；v2 已合并为单份
``registry-manifests/``，本文件把 Skill 侧也收成单份）。

两条口径（所有 operation 名都按此设计）：

- **两个 map 的 ``default`` 都是纯读工具**——调用方不写 ``operation`` 时绝不可能触发
  写库或开门（M6 的写工具永远要被显式点名）。
- operation 名是**面向模型的动作词**，不是工具名本身；一个工具可挂多个 operation。
"""
from __future__ import annotations

#: ``yunpai-m6-finance``（算）：纯算数只读——无副作用、无门、不落库。
#: 输入事实（BOM/工艺/库存/费率/M4B 价源/canonical 单据）由装配层给，缺数一律
#: 标 ``cost_incomplete``/``missing``，绝不编造。
M6_FINANCE_SKILL_OPERATION_MAP: dict[str, str] = {
    # 成本（当场算 = D8 的「试算／报价预览」）
    "default": "get_product_cost",
    "product_cost": "get_product_cost",
    "audit": "audit_order_cost",
    # 凭据预览（只算不落库；落库在 ledger 侧）
    "quotation": "generate_quotation",
    "statement": "generate_statement",
    # 人工 / 效益 / 费用
    "piece_pay": "calculate_piece_pay",
    "monthly_pay": "calculate_monthly_pay",
    "asset_benefit": "compute_asset_benefit",
    "allocate_expenses": "allocate_expenses",
    # 财务口径视图（四态分账 + 在途）与订单事实读口（成本审计的输入面）
    "inventory_view": "get_inventory_finance_view",
    "orders": "list_orders",
}

#: ``yunpai-m6-ledger``（记）：成本快照/明细/月结/报价单/对账单/送货单/资产台账。
#: 写操作照三段式——propose 段写草稿（``trial``）或只报待确认事实，生效落在
#: ``graph._apply_m6_*`` 的 commit 段（``finance`` 门批准之后）。
M6_LEDGER_SKILL_OPERATION_MAP: dict[str, str] = {
    # 成本快照：default 是读（见模块 docstring），save=propose 段，
    # confirm/close_month=commit 段的**发起**（只做前置校验 + 报待确认事实，不翻状态）
    "default": "list_costing_snapshots",
    "save_snapshot": "save_costing_snapshot",
    "confirm_snapshot": "confirm_costing_snapshot",
    "close_month": "close_month_costing",
    "snapshots": "list_costing_snapshots",
    "snapshot": "get_costing_snapshot",
    "month": "list_month_costing",
    # 报价单台账
    "save_quotation": "save_quotation",
    "quotations": "list_quotations",
    "quotation": "get_quotation",
    # 对账单台账
    "save_statement": "save_statement",
    "statements": "list_statements",
    # 凭据：送货单（D-009：canonical 是唯一主，M6 只读、不建 create 工具）
    "delivery_note": "get_delivery_note",
    # 资产台账（追加 trial 修订，绝不改写已确认原值）
    "asset": "get_asset_ledger",
    "save_asset": "upsert_asset_ledger",
}
