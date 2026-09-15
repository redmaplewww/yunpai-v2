# M6 运行逻辑、IO 契约与独立测试指南（SPEC）

- 用途：**给测试执行方**。讲清 M6 在整条链路里怎么跑、每件工具吃什么吐什么、门开在哪、哪些内容能独立测、假数据怎么接、输入输出怎么才算合规。
- 与其它文档的分工：**本文件讲模型与契约（为什么这么测、数据放哪一层）**；**独立验证的动作见 `TEST-M6-independent-verification-20260915.md`**（换输入手算 / 人工三条 / 异机冒烟）；基线回归用例与判据见 `TEST-M6-fixture-pack-20260915.md`；改动依据与撞车点见 `PLAN-M6-fixture-test-20260915.md`。
- 基线：分支 `feat/m6-finance-20260913`，HEAD = P-017 提交（交接时以实际 SHA 为准）。
- 依据：F-008 / D-005~D-015 / E-008~E-011。

---

## 一、M6 在整条链路中的位置

### 1.1 执行路径：`free` 路由，不是 workflow

M6 **不走 workflow**，全仓只有两条 workflow（`m1_m5_document_to_plan`、`canonical_to_m5`），M6 不在 `KNOWN_WORKFLOWS` 里。M6 通过 `request["tools"]` 给出一串工具名，由 Planner **平铺**成步骤（每步 `depends_on=[]`，顺序即列表顺序）。M6 的两个 Skill（`yunpai-m6-finance` 算 10 件 / `yunpai-m6-ledger` 记 14 件）每次也只派发**一个**工具。

**对测试的含义**：不需要 workflow 文件也能完整测 M6；门禁与装配都不依赖 workflow。反过来，本轮也证明不了"M1→M5→M6 全链路"（没有 M6 的 workflow 入口）。

### 1.2 一次运行的完整时序

```
request（tools + 事实）
  → Router        free 路由：校验工具已绑定/可见
  → Planner       平铺步骤
  → Assembler     三层装配（见 §2.1）——缺 required 则产出 BLOCKED_INPUT
  → Registry.call handler 执行（m6_tools.py → m6_cost.py 纯函数 → m6_store.py 落库）
  → Reviewer      rules.evaluate 判定：fail（终态）或 gate:<类型>（开门）
  → 有门          interrupt 挂起，等人 resume
  → approve       graph._apply_m6_* commit 钩子：trial → confirmed（生效）
  → reject        不产生生效行，草稿保留
```

**关键**：v2 图内**唯一开门点**在 `worker_execute` **之后**。所以 M6 的写工具分成两半——工具只做 propose（写 `trial` 草稿），**生效只发生在 approve 之后的 commit 钩子**。这就是 D-005 的三段式。

### 1.3 M6 的内部结构（读代码时的入口）

| 文件 | 职责 |
|---|---|
| `m6_cost.py` | 算法内核（纯函数，不碰库）：材料/工序/报价/审计/分摊/对账/工资/库存视图 |
| `m6_defaults.py` | 口径层：默认费率、加班倍数、计薪天数、`PENDING_FINANCE_CONFIRMATION` |
| `m6_price_source.py` | 缺料价源解析（D5：有库存走库存成本价，缺料走 M4B 采购价，两缺→missing） |
| `m6_store.py` | 存储层：快照/明细/单据/月结/资产；`store_path` 决定库路径 |
| `m6_tools.py` | handler：信封、落库、错误码、`review_summary` |
| `m6_tooling.py` | 两个 Skill 的 operation map（**白名单即全部可调用面**） |
| `graph.py` 的 `_apply_m6_*` | commit 段四个钩子（成本确认 / 月结 / 单据 / 资产） |
| `reviewer/rules.py` | M6 各工具的门禁规则（`gate:finance` / `fail`） |

---

## 二、输入依赖

### 2.1 载荷有**两条路径**——这是最容易写错的地方

装配器 `worker/assembler.py` 严格按三层合成最终 payload：

1. `bridged = bridge_payload(state, tool)`——**桥接读**，从 `request` 的**顶层键**或 canonical 库取事实；
2. `explicit = request[工具名]`——**显式参数**，非空值**覆盖**桥接值；
3. 合同 `required` 校验——缺任一必填字段 → 产出 `BLOCKED_INPUT`。

**24 件工具里只有 16 件写了桥接规则**：

| 有桥接规则（16 件，事实可放 `request` 顶层） | 无桥接规则（8 件，**只能**放 `request[工具名]`） |
|---|---|
| `save_costing_snapshot`、`confirm_costing_snapshot`、`get_costing_snapshot`、`close_month_costing`、`get_product_cost`、`audit_order_cost`、`allocate_expenses`、`generate_quotation`、`save_quotation`、`save_statement`、`get_delivery_note`、`generate_statement`、`get_inventory_finance_view`、`list_orders`、`calculate_piece_pay`、`calculate_monthly_pay` | `list_costing_snapshots`、`list_month_costing`、`list_quotations`、`get_quotation`、`list_statements`、`get_asset_ledger`、`upsert_asset_ledger`、`compute_asset_benefit` |

**实测对照**（同一件 `upsert_asset_ledger`，只改事实放置位置）：

| 放法 | 结果 |
|---|---|
| `asset_code` 放 `request` **顶层** | 挂起在 **`blocked_input` 门**（`reason="装配缺口需补数据"`），步骤 `blocked`；工具**没被执行** |
| 放 `request["upsert_asset_ledger"]` | 正常执行 → 挂起在 **`finance` 门**，`review` 已填 `{asset_code, revision, original_value, effective_date}` |

> ⚠️ **放错位置不会报"参数错误"，而是开一道补数门**。测试时看到 `blocked_input` 而非预期结果，先检查事实是不是放错了层级，而不是当成缺陷报。

### 2.2 上游事实的具体读口

| 事实 | 首选来源 | 回落 | 缺省行为 |
|---|---|---|---|
| BOM 行 | `run_bom_sop_workflow` 的 `bom_generation`，要求 `approval_status == "approved"`（即 M2 工程门已批） | canonical 实体 `bom`/`product`；再回落 `request.bom_lines` | 空 → `blocked`（要求先批 BOM） |
| 工艺工序 | `read_approved_route` | canonical `document` 中 `role ∈ {SOP, INSTRUCTION}` 的 `payload.route_steps` | 空 → 不影响落库，但人工/制费缺 → `cost_incomplete` |
| 库存 | `request.inventory_snapshot` / `request.inventory` | canonical 实体 `inventory`（**必须带 `stock_class`**，否则态未知 → 不走库存口径） | 空 → 走采购价；两缺 → `missing` |
| 采购单价 | `request.purchase_tracking_rows` | M4B 库（`request.m4b_db_path` → `YUNPAI_M4B_DB`；**库不存在即返回空，不建库**） | 空 → 缺料行 `missing_purchase_price` + `cost_incomplete` |
| 费用 | canonical 实体 `expense` | `request.expenses` | 空 → `missing_expenses`（**空集合不当零分摊**） |
| 订单 | canonical 实体 `order` | `request.order_lines` | 空 → `blocked`（审计需订单行） |
| 送货单 | canonical `delivery_note`（D-009：唯一主，M6 只读） | — | 空 → 对账单拒出（`basis_source=missing`） |
| 工资四类事实 | **只能显式给**（`report_events`/`piece_rates`/`salary_standards`/`attendance`） | **无 canonical 面** | 空 → `facts_present=false` + `missing_salary_facts` |
| 资产用量/成本 | `request.asset_usage` / 台账生效原值 | 台账（`get_asset_ledger`） | 两缺 → `missing_asset_cost` + `cost_incomplete` |

**工资是唯一没有 canonical 来源的一块**——老仓的四张面（`usage_log`/`piece_rate`/`salary_standard`/`attendance_summary`）在 v2 一张都没有。而且代码**明确禁止**用 `production_daily_report` 顶替报工（老仓禁止把资产使用数量 `quantity` 当报工数量）。测试时工资必须显式注入，这不是缺陷。

---

## 三、输出契约与合规要求

### 3.1 信封形状

所有 M6 工具返回同一形状：

```json
{
  "success": true,
  "result": { ... },          // 与 data 同值；commit 后两者一起刷新
  "data":   { ... },
  "error":  null,
  "business_status": "completed",
  "errors": [],
  "trace_id": "<task_id>:<suffix>",
  "evidence": [ { "module": "m6", "source_ref": ..., "evidence_ref": ..., "detail": ... } ]
}
```

失败时：`success=false`、`code` 必填、`business_status="failed"`、`errors[0]` 带 code/message。**不得出现 `success=true` 携带错误码。**

### 3.2 缺数与口径的表达（合规的核心）

M6 有三层严格分离，测试断言必须能区分它们：

| 情形 | 表达 | 合规要求 |
|---|---|---|
| **事实值缺失**（取不到单价/用量/工时） | 该行进 `missing[]` + `cost_incomplete=true` | **绝不编造**：不得用 0 或默认价冒充 |
| **口径值缺省**（费率、加班倍数、分摊基准没给） | 用 `m6_defaults` 的值，并在 `assumptions` 标 `*_assumed=true`，同时附 `PENDING_FINANCE_CONFIRMATION` | 口径值**必须留痕**，不能静默采用 |
| **整张事实集缺席** | 工资：`facts_present=false` + `missing_salary_facts`，**`cost_incomplete` 保持 false** | 区分"本期无报工"（正常）与"算不全" |
| **缺生产数量** | `missing_inputs` 含 `missing_quantity` + `cost_incomplete=true` + **`total_cost=null`** | 0 与"未知"语义不同，**不得以 0 冒充总量** |

### 3.3 来源标识（假数据的硬要求）

- 每条假数据带 `source_ref: "fixture:<ID>"` 与 `evidence: {fixture_id, observed_at}`；
- 经 `save_costing_snapshot` 等写工具落库后，可在 `evidence.caller_evidence` 里回读到；
- **禁止把测试数据伪装成真实 M0/M4/财务系统数据**——`source_ref` 前缀必须是 `fixture:`；
- 金额口径：**未税、CNY**；比较容差 `≤ 0.0001`（内部保留 4 位小数）。

---

## 四、门禁逻辑

### 4.1 六类人工门 + `finance` 门

`reviewer/gates.py` 的门型与角色矩阵：

| 门型 | 允许角色 | 决策 |
|---|---|---|
| candidate | data-steward / m0-reviewer / admin | approve / reject |
| sensitive_data | data-steward / hr-officer / admin | approve / reject |
| review | document-reviewer / data-steward / admin | approve / reject |
| engineering | engineering-manager / admin | approve / reject |
| procurement | procurement-manager / purchase-reviewer / admin | supplement / supplier_by_material / retry / reject |
| apply | production-manager / admin | approve / reject |
| authorization | operator / admin | approve / reject |
| blocked_input | data-steward / engineering-manager / production-manager / admin | retry / supplement / reject |
| **finance** | **`finance-officer` / `admin`** | **approve / reject** |

`finance` 是 M6 新增的门型（D-005），令牌表在 `identity.py`：权限 `finance.approve`（catalog 的 `gate` 列 = `finance`）+ 种子角色 `finance-officer`。

> ⚠️ **`authorize()` 取的是 `resume` 载荷里的 `roles`**，不等于 API 已强制校验身份（D-015 已明确记为"登记不等于运行时授权"）。测试越权用例时按"载荷里的 roles 被正确校验"来断言，**不要据此宣称 API 层已强制鉴权**。

### 4.2 三段式时序

| 阶段 | 发生什么 | 库状态 |
|---|---|---|
| propose | 工具写草稿 | `trial` |
| gate | reviewer 开 `finance` 门，run 挂起 | 仍是 `trial`（**未生效**） |
| approve | `graph._apply_m6_*` 执行 commit | `trial → confirmed` |
| reject | 不产生生效行；草稿保留 | 仍是 `trial` |

**关键断言点**：门挂起时库里必须是 `trial`（"批准前不得生效"）；approve 后 `confirmed` + `confirmed_by`/`confirmed_at` + 输出 `committed_by="finance_gate"`；reject 后无 confirmed 行。

### 4.3 逐工具门禁矩阵（24 件）

| 工具 | 读写 | 门 | 说明 |
|---|---|---|---|
| `save_costing_snapshot` | 写 | **无门**（D-008） | 只写 `trial`、不进月末汇总，刻意不开门 |
| `confirm_costing_snapshot` | 写 | `finance` | commit 段的**发起**：只校验并报待确认，不翻状态 |
| `close_month_costing` | 写 | `finance` | 同上：只报待冻结月账；冻结由 `_apply_m6_close_month` 做 |
| `save_quotation` | 写 | `finance` | propose 写草稿 → approve 后 `_apply_m6_document_commit` |
| `save_statement` | 写 | `finance` | 同上 |
| `upsert_asset_ledger` | 写 | `finance` | 追加 trial 修订，**绝不改写已确认原值** |
| 其余 18 件 | **读** | 无门、不落库 | 纯算数（当场算 = D8 试算/报价预览） |

**区分三种"不成功"**（测试时最容易混淆）：

| 表现 | 含义 | 门 |
|---|---|---|
| `success=false` + `code`（`INVALID_INPUT`/`NOT_FOUND`/`MONTH_CLOSED`/`SNAPSHOT_EXISTS`/`MONTH_ALREADY_CLOSED`） | 业务硬失败，**终态** | 走 `fail`，**不开门** |
| `code=BLOCKED_INPUT` | 装配缺权威输入 | 开 **`blocked_input` 门**，可补数重试 |
| 挂起 + `gate.type=finance` | 需要人工批准 | **`finance` 门** |

### 4.4 门的对象形状（P-017 之后）

审批人（或任何前端）在门里能看到的全部内容：

```json
{
  "type": "gate_pending",
  "gate": {
    "type": "finance",
    "tool": "confirm_costing_snapshot",
    "step_id": "tool-confirm_costing_snapshot",
    "reason": "成本确认 trial→confirmed 是生效写，须 finance 门人工批准后由 _apply_m6_costing_confirm 落库",
    "allowed_roles": ["finance-officer", "admin"],
    "payload_digest": "...",
    "opened_at": "...",
    "review": {                      // ← P-017 新增：被审阅的合计
      "snapshot_id": "...", "period": "2026-09", "order_id": "...",
      "product_code": "...", "batch_no": "...", "quantity": 10,
      "unit_cost": 81.0, "total_cost": 810.0,
      "basis": "stock", "cost_incomplete": false
    }
  }
}
```

`review` 的合规要求：**只能抄 propose 阶段已产出的结果**（不得 commit 时重算，否则会出现"批了 A 冻了 B"）；**必须含 `cost_incomplete`**（批准一份算不全的成本是业务决策，审批人有权看见）；**不参与任何授权判断**，是只读展示字段。字段**非空才写入**——不传时门仍是旧的 7 键形状。

**门的对外出口是 `pending_gate`，不是 `__interrupt__`**（`public_state` 的键是固定白名单，不含 `__interrupt__`）。经 API 测试时用 `pending_gate`。

---

## 五、独立功能测试：能做到什么、缺什么

### 5.1 三层测试面

| 层 | 验证内容 | 经门 | 怎么跑 |
|---|---|---|---|
| L1 纯函数 | 成本/工资/分摊/对账公式 | 否 | 直接调 `m6_cost` 函数，不落库 |
| L2 Registry Tool | 入参、落库、错误码、trial、Skill 白名单 | 否 | `registry.call(tool, payload, ctx)`，直接给完整 payload |
| L3 Graph | 装配、interrupt/resume、`finance` 门、commit 钩子 | 是 | `build_graph` + resume；或经真实 `create_app` 走 API |

### 5.2 做"相对独立"测试时**缺少**的内容（诚实清单）

| 缺口 | 影响本轮什么 | 处置 |
|---|---|---|
| **M6 无 workflow** | 证明不了 M1→M5→M6 全链路 | 本轮不覆盖；M6 走 `free` 路由，不影响独立测试 |
| **v2 无 canonical 工资事实面** | 工资不能从库读，只能显式注入 | 显式注入即合规；接真库前须先立实体 |
| **`standard_time` 单位未定** | 工序链路一条用例 skip；按秒/分钟猜会差 60 倍 | 挂起，勿擅自接线（见 §5.3） |
| **`loss_rate` 隐式 0.0** | 与 D-006 不一致 | 已记账；接真库前对齐 |
| **供应商报价 2 件工具** | ⛔ 阻塞 R1b（需先定 `supplier_quote` 实体 + 财务口径） | 本轮不在范围内 |
| **T-51 租户隔离无用例** | M6 从未断言跨租户不可见 | 已知缺口（TEST 附录 C），补或挂起 |
| **身份登记 ≠ 运行时授权** | 不能宣称 API 已强制鉴权 | 已记 D-015 |
| **`inventory`/`production_daily_report` 等 6 类不在 `ENTITY_TYPES`** | 库存等能否真落 canonical 待 M0 口径 | 装配层已支持，数据侧待定 |

### 5.3 独立测试时怎么绕开上游

- **不必跑上游链**：把事实直接放 `request` 顶层（16 件有桥接规则的）或 `request[工具名]`（8 件无桥接规则的），装配层就会用它们；
- **隔离上游库**：把 `YUNPAI_M0_DB`/`YUNPAI_M4B_DB` 指向**不存在**的库，让装配读空、用例自洽（`tests/test_m6_finance_gate.py` 的 `m6_db` fixture 就是这么做的）；
- **工序/人工/制费**：用显式 `routing_steps` + `hour_rate`/`overhead_rate`，不要依赖 canonical 的 `standard_time`（单位未定）。

---

## 六、假数据怎么接

### 6.1 已有夹具

`tests/fixtures/m6/fixture_pack.json` 已含 8 组夹具：`FX-COST-001/002`、`FX-PAY-001/002`、`FX-EXP-001`、`FX-ASSET-001`、`FX-STMT-001`、`FX-INV-001`，每组带 `source_ref` + `evidence` + `expected`（预期数值）。预期数值与 `TEST-M6-fixture-pack-20260915.md` §二 的实算值一致。

### 6.2 新增夹具的合规要求

1. 带来源标识：`source_ref: "fixture:<ID>"`、`evidence: {fixture_id, observed_at}`，逐行事实也带 `fixture:<ID>:<行标识>`；
2. **先在文档登记预期数值再写用例**（否则用例只是自证）；
3. 不含真实客户/供应商/员工/物料名称；
4. 与该组夹具一一对应的用例可单条运行。

### 6.3 干净成本用例的最小合规载荷（实测）

```json
{
  "period": "2026-09", "order_id": "SO-FX-001", "product_code": "P1",
  "quantity": 10,
  "bom_lines": [{"material_code": "M1", "qty_per": 2, "unit_price": 5.0, "loss_rate": 0.1}],
  "routing_steps": [{"operation_id": "OP10", "standard_minutes": 60}],
  "inventory": [{"material_code": "M1", "available_qty": 100, "stock_class": "raw"}],
  "hour_rate": 50, "overhead_rate": 20
}
```

实测结果：`unit_cost=81.0`、`total_cost=810.0`、`cost_incomplete=false`、`basis=stock`。

### 6.4 两个最常见的写错点

1. **有库存的行必须由 BOM 行自带的 `unit_price` 定价**（口径 `bom_price`）。库存行里给 `unit_cost` **无效**，会得到 `missing_stock_price` → `cost_incomplete=true`。看似缺陷，实为载荷写错。
2. **无桥接规则的 8 件工具必须用 `request[工具名]`**（§2.1），否则得到 `blocked_input` 门而不是结果。

### 6.5 隔离要求（红线）

- 每用例自建临时库（`tmp_path` + `YUNPAI_M6_DB`），**禁止**指向 `runtime/yunpai-m6.sqlite`；
- 上游库指向不存在的路径，避免用例读真库；
- 工资四类事实与费用/订单等如无 canonical 面，显式注入并标 `fixture:`。

---

## 七、输入输出合规检查清单（逐条勾）

**输入侧**

- [ ] 事实放在了正确的层：有桥接规则的放 `request` 顶层，无桥接规则的放 `request[工具名]`
- [ ] 该工具合同的 `required` 字段全部给齐（缺则 `blocked_input` 门）
- [ ] 库存行的定价来自 BOM 行 `unit_price`，且 `stock_class="raw"` + `available_qty>0` 才算"有库存"
- [ ] 工资四类事实显式注入（v2 无 canonical 面）
- [ ] 工序用显式 `standard_minutes`，不依赖 `standard_time`（单位未定）
- [ ] 假数据带 `source_ref: fixture:<ID>`，未伪装成真实 M0/M4 数据

**输出侧**

- [ ] 信封含 `success/result/data/error/business_status/errors/trace_id/evidence`
- [ ] 失败时 `success=false` 且有 `code`；无 `success=true` 携带错误
- [ ] 缺事实 → `missing[]` + `cost_incomplete=true`；**无 0/默认值冒充**
- [ ] 口径值缺省 → `assumptions.*_assumed=true` + `pending_finance_confirmation`
- [ ] 缺数量 → `missing_quantity` + `cost_incomplete=true` + `total_cost=null`
- [ ] 空工资 → `facts_present=false` + `missing_salary_facts`，且 `cost_incomplete=false`
- [ ] 金额未税 CNY，比较容差 ≤0.0001

**门禁侧**

- [ ] 门挂起时库中为 `trial`（批准前未生效）
- [ ] approve 后 `confirmed` + `confirmed_by`/`confirmed_at` + `committed_by="finance_gate"`
- [ ] reject 后无 confirmed 行、草稿保留、`approvals` 留痕
- [ ] 非财务角色（`operator`）被拒；`finance-officer`/`admin` 可通过
- [ ] `blocked_input` 门与 `fail` 不混淆（前者可补数，后者终态）
- [ ] 门的 `review` 含被审阅合计与 `cost_incomplete`，且**非空才存在**
- [ ] 经 API 断言时用 `pending_gate`，不要断言 `__interrupt__`

**合规声明**

- [ ] 结论限定在"假数据下自洽"，未声称为生产验收
- [ ] 未把"身份登记"当成"API 已强制授权"
