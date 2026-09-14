# M6 工具接口（算 · yunpai-m6-finance）

本文件与 `registry-manifests/m6.json`（工具契约）和 `src/yunpai_orchestrator/m6_tooling.py`（operation 白名单）逐项对齐；v2 只有**单份** manifest（`registry-manifests/`），`scripts/check_contracts.py` 的 H1–H5 是它的等价加载/去重门槛。只在需要选择或调用 M6「算 · yunpai-m6-finance」侧工具时读取。

## M6 算（成本与财务核算）

Skill：`yunpai-m6-finance`。完整 Schema：`registry-manifests/m6.json`（`module=m6`，全部 `local_only=true`——v2 无 M6 HTTP 服务，只经本地 handler 绑定，HTTP 段仅为未来服务化预留）。
operation 白名单见 `m6_tooling.py`；调用方式见同级 `SKILL.md`。

### `get_product_cost`

当场算出一个产品的**单台成本**（材料 + 人工 + 制费），不落库、不进账——即 D8 的「试算／报价预览」。与 save_costing_snapshot 共用同一套 D5 口径（有库存走库存成本价、缺料走实际采购价、两者都无则标 cost_incomplete 不编造），所以预览数与随后的试算快照一致。当用户问「这个产品做一台要多少钱」「报个价之前先算成本」时使用；要落成一版可追溯的试算快照请用 save_costing_snapshot，要正式成本请用 confirm_costing_snapshot。入口定位：纯算数读工具（无副作用、无门），行级价格来源（哪行走库存/哪行走采购）在 lines 里逐行给出。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`default`、`product_cost`
- HTTP（预留）：`POST /api/m6/v1/product-cost`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `product_code` | `string` | 是 | 产品编码（成本对象）。 |
| `bom_lines` | `array` | 否 | BOM 行事实（装配层给，勿手填）：[{material_code, qty_per, unit_price, loss_rate}]。 |
| `routing_steps` | `array` | 否 | 工艺路线工序事实：[{operation_id, standard_minutes}]（分钟）。 |
| `hour_rate` | `number` | 否 | 人工费率（元/小时）。口径值：缺失即标 cost_incomplete，不编造默认费率。 |
| `overhead_rate` | `number` | 否 | 制造费率（元/小时）。口径值，同 hour_rate。 |
| `inventory` | `array` | 否 | 库存事实：[{material_code, available_qty, stock_class}]；态缺失/未知时不猜「有库存」，回落采购价。 |
| `purchase_tracking_rows` | `array` | 否 | M4 采购追踪行（装配层从 list_m4_tracking 取）：实际单价事实。 |
| `purchase_order_items` | `array` | 否 | M4 采购单行项（仅用于 purchase_order_item_id → 料号映射）。 |
| `purchase_price_facts` | `object` | 否 | 已解析的采购价源（{料号: {unit_price, source_ref}} 或解析器输出）。 |
| `valuation_price_source` | `string` | 否 | 库存成本价的估价价源口径（默认 bom_price，未给出即标 assumed=true）。 |

### `audit_order_cost`

订单成本审计与毛利核算：按订单行滚动收入、直接材料、直接人工、制造费用，算出毛利与毛利率，判定 passed / low_profit / loss / cost_incomplete。当用户问「这单赚不赚」「毛利多少」「这个价能做吗」时使用。单位成本两级来源：unit_costs 显式给出（如已确认的账上成本）优先；未覆盖的产品据 products 里的 BOM/工艺/费率当场滚动。缺数量/单价/单位成本的行进 incomplete 且 status 取 cost_incomplete——**「算不全」绝不当成「盈利」**。纯算数读工具，无副作用、无门；要进账的成本请走 confirm_costing_snapshot。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`audit`
- HTTP（预留）：`POST /api/m6/v1/order-cost-audit`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `order_id` | `string` | 否 | 订单号（回显与证据引用）。 |
| `order_lines` | `array` | 是 | 订单行：必填 product_code/qty/unit_price（unit_price = 对客单价）。缺任一即该行进 incomplete。 |
| `unit_costs` | `object` | 否 | 产品单位成本映射 {product_code: {material, labor, overhead}}；缺某产品时据 products 滚动。 |
| `products` | `object` | 否 | 产品成本事实 {product_code: {bom_lines, routing_steps, hour_rate, overhead_rate}}（装配层给）。 |
| `min_margin_rate` | `number` | 否 | 最低毛利率门槛（默认取口径层 DEFAULT_MIN_MARGIN_RATE）。 |

### `generate_quotation`

算一版报价（R-QT-1：行报价 =(材料+人工+制费)×(1+加价率)，行小计 ×数量 汇总）——**纯算数、不落库**，即「报价预览」。当用户问「这个价报多少」「按 10% 加价算一下」时使用。要落成可追溯的客户台账（并可被财务确认生效）请用 save_quotation。入口定位：与 get_product_cost 同一关系——先看数、再决定落不落。缺 product_code/qty 的行计入 missing，不编造金额。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`quotation`
- HTTP（预留）：`POST /api/m6/v1/quotations/preview`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `quote_no` | `string` | 否 | 报价单号（仅回显；落库时才需要唯一）。 |
| `customer_code` | `string` | 否 | 客户编码（回显）。 |
| `doc_date` | `string` | 否 | 报价日期（YYYY-MM-DD），不给取当日。 |
| `markup_rate` | `number` | 否 | 全局加价率（行未自带时用它；缺省 0 且标 markup_assumed）。 |
| `lines` | `array` | 是 | 报价行：[{product_code, qty, unit_material, unit_labor, unit_overhead, markup_rate?}]。前三个成本项来自成本链（可先用 get_product_cost 算），markup_rate 是加价率小数（0.1=加 10%）；未给出时按 0 算并标 markup_assumed=true——**0 加价不等于谈好的报价**。 |
| `products` | `object` | 否 | 成本事实（装配层给）：{product_code: {bom_lines, routing_steps, hour_rate, overhead_rate, inventory, purchase_tracking_rows...}}；行内未给 unit_material/labor/overhead 时据此按 D5 口径滚动（缺成本的行不给报价，进 missing）。 |

### `generate_statement`

**依据送货单/采购入库事实生成**对账明细并算期末（期初 + Σin − Σout）——纯算数、不落库，即「对账单预览」。当用户问「和这个客户对一下账」「这个月该收多少」时使用。客户对账取 canonical 送货单的外发货（→ 明细 in，增加应收）；供应商对账取 M4 已入库追踪行（→ 明细 in，增加应付）；也可显式给 transactions 覆盖。**依据不足时明示 missing 并拒（basis_source=missing），绝不出具一份金额为零的空对账单**。要看数用本工具，要落成可确认的凭据用 save_statement（那条链才有 finance 门）。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`statement`
- HTTP（预留）：`POST /api/m6/v1/generate_statement`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `counterparty_code` | `string` | 是 | 往来单位编码（客户或供应商）。 |
| `supplier_code` | `string` | 否 | 供应商编码（counterparty_code 的别名）。 |
| `statement_type` | `string` | 否 | 对账类型（默认 customer）：customer 取送货单、supplier 取已入库采购追踪。 |
| `period` | `string` | 否 | 账期（可选，如 2026-09，供装配层过滤依据）。 |
| `date_from` | `string` | 否 | 起始日期（可选，回显与证据用）。 |
| `date_to` | `string` | 否 | 截止日期（可选，回显与证据用）。 |
| `opening_balance` | `number` | 否 | 期初余额（元）。 |
| `transactions` | `array` | 否 | 显式对账明细（覆盖依据生成）：[{direction(in\|out), amount, ref?}]。 |
| `delivery_notes` | `array` | 否 | 送货单依据（装配层给，勿手填）。 |
| `purchase_receipts` | `array` | 否 | 采购入库依据（装配层给，勿手填；须自带 amount，追踪行的 unit_price 是单价不是入库金额）。 |

### `calculate_piece_pay`

算**计件工资**（R-PAY-1）：Σ(合格数量 × 单价)，合格数量 = quantity_report − scrap；单价按 (station_code, product_code) 匹配并取报工日生效的版本。当用户问「某工人/某工站这个月的计件工资」时使用。⚠️ **v2 没有 canonical 工资事实面**——老仓的四张 canonical 面（`usage_log` 报工 / `piece_rate` 计件单价 / `salary_standard` 月薪 / `attendance_summary` 考勤）在 v2 **全部不存在**，所以 `report_events` 与 `piece_rates` **都必须显式给出**。缺单价的报工进 missing（missing_piece_rate），**不按 0 计、也不编造单价**（编错单价 = 多发/少发工资），**更不拿别的实体顶替**（老仓明确禁止把资产使用数量 `quantity` 当报工数量）。纯算数只读、无门、不落库。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`piece_pay`
- HTTP（预留）：`POST /api/m6/v1/piece-pay`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `report_events` | `array` | 否 | 报工事实（**v2 无 canonical 来源，必须显式给**）：[{worker_id, station_code, product_code, quantity_report, scrap?, report_date}]。 |
| `piece_rates` | `array` | 否 | 计件单价（**无 canonical 来源，必须显式给**）：[{station_code, product_code, unit_rate, effective_from?, effective_to?}]。不给 → 每条报工进 missing_piece_rate。 |

### `calculate_monthly_pay`

算**月度工资**（R-PAY-2，月度/混合制）：月薪标准 + 加班费 − 缺勤扣款 + 计件工资；时薪 = 月薪 ÷ 计薪天数 ÷ 每日工时，加班费 = 时薪 × 加班倍数 × 加班工时，缺勤扣款 = 时薪 × 缺勤工时。当用户问「某人这个月工资多少」「月薪加加班费」时使用。事实值：salary_standards（工人→月薪）、attendance（工人→overtime_hours/absence_hours）、piece_pay（工人→计件合计，通常取 calculate_piece_pay 的 totals）——⚠️ 这三类在 v2 **也没有 canonical 来源**（老仓分别取 `salary_standard` / `attendance_summary`），必须显式给出。口径值：**加班倍数/计薪天数/每日工时未显式给时取口径层默认**并在 assumptions 标 assumed=true（这三项在 PENDING_FINANCE_CONFIRMATION 里待工厂财务确认）。缺月薪的工人进 missing（不编造）。纯算数只读、无门、不落库。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`monthly_pay`
- HTTP（预留）：`POST /api/m6/v1/monthly-pay`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `salary_standards` | `object` | 否 | 工人→月薪标准（事实值）：{worker_id: 月薪}。 |
| `attendance` | `object` | 否 | 工人→考勤（事实值）：{worker_id: {overtime_hours, absence_hours}}。 |
| `piece_pay` | `object` | 否 | 工人→计件工资合计（事实值）：{worker_id: 金额}；通常取 calculate_piece_pay 的 totals。 |
| `overtime_multiplier` | `number` | 否 | 加班倍数（口径值；默认 1.5，未给出即标 assumed=true 并附待确认清单）。 |
| `work_days` | `number` | 否 | 月计薪天数（口径值；默认 21.75，未给出即标 assumed=true）。 |
| `hours_per_day` | `number` | 否 | 每日工时（口径值；默认 8，未给出即标 assumed=true）。 |

### `compute_asset_benefit`

把模具/机器成本按口径分摊到产品（DEV-15）：口径 quantity/labor_hours/order_count（默认 quantity，待财务确认）。当用户问「这台模具的成本摊到哪些产品上」「模具投入产出」时使用。资产成本两级来源：显式 asset_cost 优先；未给则**取台账生效行的原值**（只看 effective，不看草稿），来源以 asset_cost_source 明示。台账里没有、也没显式成本的资产进 missing_asset_cost（不编造）。纯算数只读、无门、不落库。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`asset_benefit`
- HTTP（预留）：`POST /api/m6/v1/compute_asset_benefit`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `asset_usage` | `array` | 是 | 资产使用记录：[{asset_code, product_code, quantity?, labor_hours?, order_id?}]。 |
| `asset_cost` | `object` | 否 | 资产成本映射 {asset_code: 总成本}（可选；不给则取台账生效原值）。 |
| `allocation_basis` | `string` | 否 | 分摊口径（默认取口径层 DEFAULT_ALLOCATION_BASIS，未给出即标 assumed=true）。 |

### `allocate_expenses`

把费用（社保/税费/电费/运费/伙食费/杂项）按口径分摊到产品：Σ(费用 × 产品基准 ÷ 基准合计)。当用户问「这些费用怎么摊到产品上」「按产量/工时/人数摊一下」时使用。事实来源：expenses（默认由装配层从 M0 canonical 的 expense 实体读入）与 basis_rows（产量/工时/人数/订单数基准）；分摊口径未显式指定时取口径层默认并在 assumptions 标 assumed=true，基准数据来源以 basis_source（explicit/canonical/missing）明示。只读、不落库——分摊结果仅作分析，要进账的成本走 save/confirm_costing_snapshot；缺金额或未知口径计入 missing，不用 0 冒充。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`allocate_expenses`
- HTTP（预留）：`POST /api/m6/v1/expense-allocation`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `expenses` | `array` | 否 | 费用事实：[{category, amount, period, allocation_basis?}]（装配层从 canonical expense 读入；单条可自带口径覆盖全局）。 |
| `basis_rows` | `array` | 否 | 分摊基准行：[{product_code, quantity, labor_hours, headcount, order_count}]（装配层从报工事实/订单行聚合）。 |
| `period` | `string` | 否 | 账期过滤（可选，如 2026-09）：只分摊该期间的费用。 |
| `allocation_basis` | `string` | 否 | 全局分摊口径（默认取口径层 DEFAULT_ALLOCATION_BASIS，未给出即标 assumed=true）。 |
| `basis_source` | `string` | 否 | 基准数据来源（装配层写入，用于口径溯源）。 |

### `get_inventory_finance_view`

财务口径的**库存视图**（B5）：按库存四态（原料在库 raw / 成品在库 finished / 半成品在库 semi / 在制 wip）分账，并给出**在途**（M4 采购追踪里尚未入库的行）。当用户问「现在库存值多少钱」「原料/成品各结存多少」「在途还有多少」时使用。三条口径全部 fail-closed：① 库存态缺失/未知 → 单列 unknown 一栏，**绝不归到任何一态**（不猜「有库存」）；② **金额只在能取到单价时给**——v2 的 inventory 实体**没有价格字段**，金额必须由 unit_costs（材料编码→单价，来自 BOM 价/资产台账）带入，取不到即该行进 missing 且整项 cost_incomplete（**绝不编造成本**）；③ 追踪行 arrival_status 为空 → 单列 unknown_status（**不猜它在路上**）。by_class.finished 即「成品账」。纯算数只读、无门、不落库。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`inventory_view`
- HTTP（预留）：`POST /api/m6/v1/inventory-finance-view`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `inventory` | `array` | 否 | 库存事实（装配层给，勿手填）：[{material_code, available_qty, stock_class?}]；stock_class ∈ raw/finished/semi/wip，缺失或未知按「分不清态」处理。 |
| `purchase_tracking_rows` | `array` | 否 | M4B 采购追踪行（装配层从 list_m4_tracking 取）：用于判在途。arrival_status=received 为已入库（不算在途）、其他非空值为在途、空为状态未知。 |
| `unit_costs` | `object` | 否 | 材料编码→单价映射（事实值，用于算金额）。v2 的 inventory 无价格字段，故金额只能由此带入；不给则该行进 missing + cost_incomplete（绝不编造成本）。 |
| `valuation_price_source` | `string` | 否 | 库存估价单价来源（默认取口径层 DEFAULT_VALUATION_PRICE_SOURCE=bom_price，未给出即标 assumed=true）。 |

### `list_orders`

列出 M0 canonical 里**已落库的订单**（B6）：字段照 canonical `order`（order_id / product_code / product_name / quantity / due_date / customer_name / unit_price / total_amount）+ 订单行数 line_count。当用户问「有哪些订单」「这个产品有哪些单」「某客户/某交期的单」时使用。过滤面：product_code / customer_name 精确匹配、period 按 due_date 前缀匹配（canonical 的 order **没有** order_date 字段，**不拿别的字段冒充下单日期**）；limit 默认 50。canonical 里没有订单时返回空列表 + missing（不编造订单）。纯算数只读、无门、不落库。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`orders`
- HTTP（预留）：`POST /api/m6/v1/orders`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `orders` | `array` | 否 | 订单事实（装配层从 canonical `order` 读入并拆信封，勿手填）。 |
| `product_code` | `string` | 否 | 按产品编码精确过滤。 |
| `customer_name` | `string` | 否 | 按客户名精确过滤。 |
| `period` | `string` | 否 | 按 due_date 前缀过滤（如 2026-09）。注意是**交期**不是下单日。 |
| `limit` | `integer` | 否 | 返回条数上限（默认 50，最大 200）。 |
