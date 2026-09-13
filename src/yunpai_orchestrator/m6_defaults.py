"""M6 财务口径默认值集中配置（单一事实源）。

领导财务思维导图（财务.xmind）要求的业务口径尚未经工厂财务确认。按
"默认值 + 接口"模式开发（v2 账本 F-008 / 决策 D-006）：口径类默认值集中在本模块，
财务确认后只改此处或调用入参，不改业务代码；事实类数据（实际金额/单价）永远不设
默认值，缺失时由计算层标 ``cost_incomplete``。

每个默认值的使用情况都会经 ``expense_assumptions`` 出现在
``allocate_expenses`` 输出的 ``assumptions`` 块中（assumed=True 表示
该值来自默认口径而非调用方显式给定），供审计回溯哪些结果建立在假设上。

来源：老仓 ``yunpai-39092`` ``src/yunpai_langgraph/m6_defaults.py``（83 行，原样搬运，
仅文档引用改为 v2 号段；代码逻辑不变）。本模块**不 import 任何 v2 模块**，以保持
"跟基线无关"（计划 §13）。
"""

from __future__ import annotations

from typing import Any

#: 费用类别规范名（财务.xmind："社保，税费，电费，运费，伙食费，杂项支出"）。
#: 开放枚举：其他类别字符串允许入库（杂项为兜底类别），规范名用于报表归集。
EXPENSE_CATEGORIES: tuple[str, ...] = (
    "social_insurance",  # 社保（导图口径：按人数固定）
    "tax",               # 税费
    "electricity",       # 电费
    "freight",           # 运费
    "meals",             # 伙食费
    "misc",              # 杂项支出
)

#: 分摊口径选项：键为口径名，值为分摊基准行（basis_rows）上的取数字段。
ALLOCATION_BASIS_KEYS: dict[str, str] = {
    "quantity": "quantity",        # 产量（默认；与 compute_asset_benefit 默认一致）
    "labor_hours": "labor_hours",  # 工时
    "headcount": "headcount",      # 人数（导图口径：社保按人数固定）
    "order_count": "order_count",  # 订单数
}

#: 默认分摊口径（待财务确认）。
DEFAULT_ALLOCATION_BASIS = "quantity"

#: 工资/审计默认口径集中收口（原先散在工具默认参数中，此处集中后口径含义不变）。
DEFAULT_OVERTIME_MULTIPLIER = 1.5
DEFAULT_WORK_DAYS = 21.75
DEFAULT_HOURS_PER_DAY = 8.0
DEFAULT_MIN_MARGIN_RATE = 0.15

#: 库存估价单价来源（待财务确认）：BOM 价优先，其次最近采购价。
VALUATION_PRICE_SOURCES: tuple[str, ...] = ("bom_price", "latest_purchase_price")
DEFAULT_VALUATION_PRICE_SOURCE = "bom_price"

#: 车间维度（财务.xmind"一车间/二车间"）在 tooling/equipment 上的承载字段。
#: 口径未确认前为自由文本（如"一车间"），只随实体入库、不参与计算；
#: 财务确认结构化编码后再迁移，不影响既有字段。
WORKSHOP_FIELD = "workshop"

#: 待工厂财务确认的口径清单——防止默认值被误当已确认事实（红线：不编造）。
PENDING_FINANCE_CONFIRMATION: tuple[str, ...] = (
    "费用分摊基准（默认 quantity；导图口径社保按人数 headcount）",
    "加班倍数 1.5 / 计薪天数 21.75 / 每日工时 8",
    "最低毛利率 0.15",
    "库存估价单价来源（默认 bom_price）",
    "车间维度：workshop 自由文本 vs 结构化编码",
)


def expense_assumptions(
    *,
    allocation_basis: str | None = None,
    basis_source: str = "explicit",
    valuation_price_source: str | None = None,
) -> dict[str, Any]:
    """生成费用分摊输出附带的 assumptions 块。

    ``allocation_basis``/``valuation_price_source`` 为 None 时取默认口径并标
    ``assumed=True``；``basis_source`` ∈ explicit/canonical/missing 记录分摊
    基准数据来源（调用方显式传入 / M0 canonical 自动聚合 / 缺失）。
    """
    return {
        "allocation_basis": allocation_basis or DEFAULT_ALLOCATION_BASIS,
        "allocation_basis_assumed": allocation_basis is None,
        "basis_source": basis_source,
        "valuation_price_source": valuation_price_source or DEFAULT_VALUATION_PRICE_SOURCE,
        "valuation_price_source_assumed": valuation_price_source is None,
        "pending_finance_confirmation": list(PENDING_FINANCE_CONFIRMATION),
    }
