# M6 工具接口（记 · yunpai-m6-ledger）

本文件与 `registry-manifests/m6.json`（工具契约）和 `src/yunpai_orchestrator/m6_tooling.py`（operation 白名单）逐项对齐；v2 只有**单份** manifest（`registry-manifests/`），`scripts/check_contracts.py` 的 H1–H5 是它的等价加载/去重门槛。只在需要选择或调用 M6「记 · yunpai-m6-ledger」侧工具时读取。

## M6 记（成本明细账与台账）

Skill：`yunpai-m6-ledger`。完整 Schema：`registry-manifests/m6.json`（`module=m6`，全部 `local_only=true`——v2 无 M6 HTTP 服务，只经本地 handler 绑定，HTTP 段仅为未来服务化预留）。
operation 白名单见 `m6_tooling.py`；调用方式见同级 `SKILL.md`。

### `list_costing_snapshots`

查询成本快照列表（试算 + 正式都在，含 status 与 cost_incomplete 标记），可按期间/状态/订单精确过滤。当用户问「算过哪些成本」「这个月的成本快照」「某单的试算」时使用。只读：不改任何状态，也不把 trial 当正式成本。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`default`、`snapshots`
- HTTP（预留）：`GET /api/m6/v1/costing-snapshots`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `period` | `string` | 否 | 账期过滤（可选，如 2026-09），精确匹配。 |
| `status` | `string` | 否 | 状态过滤（可选）：trial=试算不进汇总，confirmed=正式成本。 |
| `order_id` | `string` | 否 | 订单号过滤（可选），精确匹配。 |
| `limit` | `integer` | 否 | - |

### `save_costing_snapshot`

算出一版成本并冻结为**试算快照**（propose 段）：按 D5 口径逐行定价（有库存 → 库存成本价；缺料 → 实际采购价；两者都取不到 → 该行标 cost_incomplete、绝不编造），写入本模块自己的 m6 库，status=trial。试算**不构成正式成本**、不进月末汇总，因此本工具刻意无人工门（合同显式 review_gate=none，理由见账本 D-008）；要让成本进账请随后用 confirm_costing_snapshot（finance 门）。入口定位：这是成本计算链的落库入口；纯算数预览用 get_product_cost/audit_order_cost，月末汇总用 list_month_costing。业务键 order_id+batch_no+period 命中已有快照时返回 duplicate_of 提示（新建/覆盖/查看由人决定，本工具不覆盖）；该期间月账已冻结则拒绝（MONTH_CLOSED）。

- 类型：`tool`；执行：`sync`；副作用：`local_write`；**刻意无门**（propose 段，账本 D-008）
- operation：`save_snapshot`
- HTTP（预留）：`POST /api/m6/v1/costing-snapshots`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `period` | `string` | 是 | 账期（YYYY-MM），例如 2026-09。快照的月结与汇总口径。 |
| `snapshot_id` | `string` | 否 | 快照主键。不传时按 period+order_id+batch_no 确定性派生（同键第 N 版）；同键重复计算不覆盖历史快照。 |
| `order_id` | `string` | 否 | 订单号（业务键之一）。 |
| `product_code` | `string` | 否 | 产品编码（成本对象）。 |
| `batch_no` | `string` | 否 | 批次号（业务键之一）。 |
| `quantity` | `number` | 否 | 数量：单台单位成本 × 数量 = 总成本。缺失按 0 计（total_cost=0）。 |
| `bom_lines` | `array` | 否 | BOM 行事实（装配层给，勿手填）：[{material_code, qty_per, unit_price, loss_rate}]。unit_price 是 BOM 价（库存口径用），loss_rate 为损耗率小数（0.05=5%）。 |
| `routing_steps` | `array` | 否 | 工艺路线工序事实：[{operation_id, standard_minutes}]，standard_minutes 以分钟计。缺标准工时计入 incomplete。 |
| `hour_rate` | `number` | 否 | 人工费率（元/小时）。**口径值**：未给出时按缺失处理（标 cost_incomplete），不编造默认费率。 |
| `overhead_rate` | `number` | 否 | 制造费率（元/小时）。口径值，同 hour_rate 口径：缺则标 cost_incomplete。 |
| `inventory` | `array` | 否 | 库存事实（装配层给）：[{material_code, available_qty, stock_class}]。stock_class 为库存四态 raw/finished/semi/wip；态缺失或未知时不猜「有库存」，回落实际采购价。 |
| `purchase_tracking_rows` | `array` | 否 | M4 采购追踪行（装配层从 list_m4_tracking 取，勿手填）：[{id, purchase_order_no, purchase_order_item_id, supplier_name, promised_date, unit_price, currency}]。单价不可解析的行不入价源。 |
| `purchase_order_items` | `array` | 否 | M4 采购单行项（仅用于 purchase_order_item_id → item_code/internal_material_no 映射）。 |
| `purchase_price_facts` | `object` | 否 | 已解析的采购价源（{material_code: {unit_price, source_ref, ...}} 或 purchase_prices_from_tracking 的输出）。给了就直接用，不再解析 tracking 行。 |
| `valuation_price_source` | `string` | 否 | 库存成本价的估价价源口径（默认 bom_price）。未给出时按口径层默认并在 assumptions 标 assumed=true。 |
| `evidence` | `object` | 否 | 调用方附带的证据块（BOM 版本/齐套快照引用等），随快照入库。 |

### `confirm_costing_snapshot`

发起**成本确认**（trial → confirmed，即「正式成本」落账）：三段式的 commit 段。本工具自身只做前置校验（快照存在、当前为 trial、该期间月账未冻结）并请求人工确认，**不翻状态**；批准后由 _apply_m6_costing_confirm 在 commit 段翻正。开 finance 门（角色 finance-officer/admin，仅 approve/reject），LLM 置信度任何阈值都不得放行。入口定位：试算快照 → 本工具 → 月末汇总只认 confirmed。月账已冻结的期间拒绝确认（MONTH_CLOSED），冻结前遗留的 trial 也不得被翻正。

- 类型：`tool`；执行：`sync`；副作用：`local_write`；门：`finance`
- operation：`confirm_snapshot`
- HTTP（预留）：`POST /api/m6/v1/costing-snapshots/{snapshot_id}/confirm`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `snapshot_id` | `string` | 是 | 待确认的快照主键（trial）。 |
| `note` | `string` | 否 | 确认说明（随门决策留痕）。 |

### `close_month_costing`

发起**月末结账**（冻结该期间月账）：三段式的 commit 段。本工具自身只汇总该期间**已确认**成本并回报待冻结的合计与快照数（trial 不计入，仅作为 trial_count 附注），**不写冻结行**；批准后由 _apply_m6_close_month 用被审阅的这组合计落冻结行。开 finance 门（finance-officer/admin，仅 approve/reject）。冻结后该期间不得再产出新成本、也不得再确认（MONTH_CLOSED）；重复结账被拒（MONTH_ALREADY_CLOSED）。

- 类型：`tool`；执行：`sync`；副作用：`local_write`；门：`finance`
- operation：`close_month`
- HTTP（预留）：`POST /api/m6/v1/month-close`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `period` | `string` | 是 | 待结账期间（YYYY-MM）。 |
| `note` | `string` | 否 | 结账说明（随门决策留痕）。 |

### `get_costing_snapshot`

按快照号读回一版成本的**明细**（快照头 + 成本明细行，每行带 element/source_kind/source_ref/evidence）。当用户问「这版成本怎么算出来的」「某个快照的明细/依据」时使用。只读；快照不存在时返回 NOT_FOUND（不编造）。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`snapshot`
- HTTP（预留）：`GET /api/m6/v1/costing-snapshots/{snapshot_id}`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `snapshot_id` | `string` | 是 | 快照主键。 |

### `list_month_costing`

查询月度成本汇总（**只加 confirmed**，试算不计入并以 trial_count 明示）：给定期间返回该期间合计、快照数、是否已结账；不给期间则列出各期间的汇总。当用户问「这个月成本多少」「月账结了吗」「各月成本对比」时使用。只读；月账已冻结的期间返回冻结快照（不随新确认变动）。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`month`
- HTTP（预留）：`GET /api/m6/v1/month-costing`，超时 `30s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `period` | `string` | 否 | 账期（可选，如 2026-09）。不给则列出所有已有快照或已结账的期间。 |
| `limit` | `integer` | 否 | - |

### `save_quotation`

落一版**报价单草稿**（propose 段）并请财务确认：算报价 → 写 status=trial 草稿（客户台账，可追溯但**不构成对外承诺**）→ 开 finance 门；批准后由 _apply_m6_document_commit 翻 confirmed（生效凭据）。当用户说「给客户出张报价单」时使用。同号单据不静默覆盖（DOC_NO_EXISTS，由人决定新建/覆盖/查看）；单号不给时按 QT-日期-流水 确定性派生。缺行/缺价计入 missing，不编造金额。

- 类型：`tool`；执行：`sync`；副作用：`local_write`；门：`finance`
- operation：`save_quotation`
- HTTP（预留）：`POST /api/m6/v1/save_quotation`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `doc_no` | `string` | 否 | 报价单号；不给则按 QT-YYYYMMDD-流水 派生。同号已存在即显式拒绝。 |
| `customer_code` | `string` | 是 | 客户编码（往来单位）。 |
| `doc_date` | `string` | 否 | 报价日期（YYYY-MM-DD），不给取当日。 |
| `direction` | `string` | 否 | 单据方向，报价默认 out（我方对外）。 |
| `markup_rate` | `number` | 否 | 全局加价率（行未自带时用它）。 |
| `lines` | `array` | 是 | 报价行：[{product_code, qty, unit_material, unit_labor, unit_overhead, markup_rate?}]。前三个成本项来自成本链（可先用 get_product_cost 算），markup_rate 是加价率小数（0.1=加 10%）；未给出时按 0 算并标 markup_assumed=true——**0 加价不等于谈好的报价**。 |
| `source_ref` | `string` | 否 | 来源引用（如订单号/询价单号），随单入库供追溯。 |
| `products` | `object` | 否 | 成本事实（装配层给）：{product_code: {bom_lines, routing_steps, hour_rate, overhead_rate, inventory, purchase_tracking_rows...}}；行内未给 unit_material/labor/overhead 时据此按 D5 口径滚动（缺成本的行不给报价，进 missing）。 |

### `list_quotations`

查询报价单台账（trial 草稿与 confirmed 生效都列出并明示状态）。当用户问「给某客户报过哪些价」「报价单列表」时使用。只读；可按客户或状态过滤。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`quotations`
- HTTP（预留）：`GET /api/m6/v1/quotations`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `customer_code` | `string` | 否 | 客户编码过滤（可选，精确匹配）。 |
| `counterparty_code` | `string` | 否 | 往来单位编码过滤（customer_code 的别名）。 |
| `status` | `string` | 否 | 状态过滤（可选）：trial=草稿未生效，confirmed=已生效。 |
| `limit` | `integer` | 否 | - |

### `get_quotation`

按单号或主键读回一张报价单（含明细行）。当用户问「这张报价单里有什么」时使用。只读；不存在返回 NOT_FOUND（不编造）。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`quotation`
- HTTP（预留）：`GET /api/m6/v1/quotations/{doc_no}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `doc_no` | `string` | 否 | 报价单号。 |
| `quote_no` | `string` | 否 | 报价单号别名。 |
| `doc_id` | `string` | 否 | 单据主键（与单号二选一）。 |

### `save_statement`

落一版**对账单草稿**（propose 段）并请财务确认：期末 = 期初 + Σin − Σout（compute_statement 确定性累加）→ 写 status=trial 草稿 → 开 finance 门。当用户说「和这个客户/供应商对一下账」时使用。statement_type 决定方向：customer=我方对客户（out）、supplier=对供应商（in）。明细缺失（既无 transactions 也无 lines）即拒（对账单必须有对账依据，不编造）。同号单据不覆盖；单号不给时按 ST-日期-流水 派生。

- 类型：`tool`；执行：`sync`；副作用：`local_write`；门：`finance`
- operation：`save_statement`
- HTTP（预留）：`POST /api/m6/v1/save_statement`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `doc_no` | `string` | 否 | 对账单号；不给则按 ST-YYYYMMDD-流水 派生。 |
| `counterparty_code` | `string` | 是 | 往来单位编码（客户或供应商）。 |
| `statement_type` | `string` | 否 | 对账类型（决定 direction：customer→out、supplier→in）。 |
| `direction` | `string` | 否 | 单据方向（显式覆盖 statement_type 的推导）。 |
| `doc_date` | `string` | 否 | 对账日期（YYYY-MM-DD），不给取当日。 |
| `opening_balance` | `number` | 否 | 期初余额（元）。 |
| `transactions` | `array` | 否 | 对账明细：[{direction(in\|out), amount, ref?}]；direction in=增加余额（客户发货确认/供应商入库）、out=减少（客户回款/供应商付款）。 |
| `lines` | `array` | 否 | 已定稿的对账行（与 transactions 二选一，原样采信不二次推算）。 |
| `amount` | `number` | 否 | 对账金额/期末余额（只给 lines 时用）。 |
| `source_ref` | `string` | 否 | 来源引用（如订单号），随单入库供追溯。 |

### `list_statements`

查询对账单台账（双向：customer 出 / supplier 入；trial 草稿与 confirmed 生效都列出）。当用户问「对账单列表」「和某供应商对过账吗」时使用。只读；可按往来单位或状态过滤。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`statements`
- HTTP（预留）：`GET /api/m6/v1/statements`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `counterparty_code` | `string` | 否 | 往来单位编码过滤（可选）。 |
| `status` | `string` | 否 | 状态过滤（可选）。 |
| `limit` | `integer` | 否 | - |

### `get_delivery_note`

按单号读回一张送货单（canonical `delivery_note`，唯一主——M6 侧只读、不建 create）。当用户问「这张送货单的内容」「某单号签收了没」时使用。返回单号、往来单位、日期、方向、金额、明细与签收面字段（signed_by/signed_at/warehouse_confirmed_by/qc_status），原样照抄不加工。只读、无门；不存在返回 NOT_FOUND（不编造）。入口定位：对账依据的单据级读口；要按客户/订单批量筛选用 list_delivery_notes，要据送货单出对账单用 generate_statement。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`delivery_note`
- HTTP（预留）：`GET /api/m6/v1/delivery-notes/{note_no}`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `note_no` | `string` | 是 | 送货单号（canonical 业务键）。 |
| `counterparty_code` | `string` | 否 | 往来单位编码（可选，用于收窄装配范围）。 |

### `get_asset_ledger`

读资产台账（模具/机器）：**生效值与待确认草稿分开列**——effective 才是账上事实，pending 只是等人批的草稿（三段式台账读口，成本/效益分摊只认 effective）。当用户问「这台模具的台账」「资产原值多少」时使用。给了 asset_code 就返回该资产的生效值/待确认值/全部修订历史；不给则列出全部资产的生效与待确认状态。只读、无门；不在台账返回 NOT_FOUND。

- 类型：`tool`；执行：`sync`；副作用：`none`；无副作用、无门
- operation：`asset`
- HTTP（预留）：`GET /api/m6/v1/asset-ledger`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `asset_code` | `string` | 否 | 资产编码（可选；不给则列出全部资产）。 |

### `upsert_asset_ledger`

登记/更新一条资产台账（propose 段）：追加一条 **status=trial 的修订草稿** 并请财务确认——**不覆盖**已确认的原值（台账按 asset_code+revision 存多版，生效行 = 最大的已确认修订）。当用户说「这台设备原值 5 万，登记一下」时使用。已有待确认修订时拒绝再叠一版（ASSET_PENDING_EXISTS）——先让人把上一版批了或撤了。缺原值即不登金额（`acquisition_cost` 可空，不编造）。入口定位：资产档案的唯一写口；查台账用 get_asset_ledger，分摊效益用 compute_asset_benefit。

- 类型：`tool`；执行：`sync`；副作用：`local_write`；门：`finance`
- operation：`save_asset`
- HTTP（预留）：`POST /api/m6/v1/upsert_asset_ledger`，超时 `60s`

输入：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `asset_code` | `string` | 是 | 资产编码（模具/机器编号）。 |
| `asset_name` | `string` | 否 | 资产名称。 |
| `category` | `string` | 否 | 资产类别（如 mold/machine；开放文本）。 |
| `acquisition_cost` | `number` | 否 | 原值/购置成本（元）。**事实值**：不知道就别给（不编造），效益分摊时会标 missing_asset_cost。 |
| `acquired_at` | `string` | 否 | 购入日期（YYYY-MM-DD）。 |
| `useful_life_months` | `integer` | 否 | 预计使用月数（折旧口径，待财务确认）。 |
| `salvage_value` | `number` | 否 | 残值（元）。 |
| `source_ref` | `string` | 否 | 来源引用（如采购单号/发票号），随台账入库供追溯。 |
| `evidence` | `object` | 否 | 调用方附带的证据块（如凭证扫描件引用）。 |
