# M6 假数据测试接入计划（P-016）

- 任务：M6 财务模块假数据测试接入 + 三项口径与权限收口
- 分支与基线：`feat/m6-finance-20260913`，HEAD `7790e4a`（前置：P-015 收口提交，见 T0）
- 依据：领导 2026-09-15 指令（M6 为独立模块 → 先接一套假数据开测 → 测通再接真实数据库）；用户会话三项裁定（本节一）
- 关联：`PLAN-M6-standard-conformance-20260915.md`（P-015，本任务的基线）、F-008、E-010
- 受众划分：**本文件给修复方（Codex）**——只讲改什么、怎么改、验收命令。**测试用例与验收标准给测试执行方（同事）**，见 `TEST-M6-fixture-pack-20260915.md`；本文件不重复用例清单。

## 目标 / 非目标

**目标**：用一套带来源标识的假数据，在现有三层测试面上把 M6 财务链测通，并把三处已拍口径落成代码 + 回归断言。

**非目标**（本轮明确不做）：

1. 不建 M6 Workflow，不改 `workflows/`、`KNOWN_WORKFLOWS`、router。M6 走 `free` 路由（显式工具/技能清单），门禁与装配均不依赖 workflow。
2. 不把 `permission_for_gate()` 接入 gate 判定（当前 `src/` 无人调用，接线属权限强制执行，超出本轮）。
3. 不改工具面、Tool ID、HTTP 路径、数据库表结构。`registry-manifests/m6.json` 的 24 件、`check_contracts` 的 147/128 计数保持不变。
4. 不迁移 Data Manager / Domain Service / Repository 架构（沿用 P-015 的非目标）。
5. 不接真实数据库。真库切换点在装配层 `_assemble_costing_facts`，本轮只验证该路径在假数据下自洽。

## 一、已拍口径（不变量——实现与断言都按此，不再讨论）

### 2026-09-15 实施前复核

- 按用户本次“核查合理性后修复”的授权实施本地代码、测试与文档；保留 P-015 工作树。T0 的提交与 T5 的 push/PR 不作为本地修复前置，本轮不自动发布。
- 以当前 `7790e4a` 加 P-015 工作树为基线；文档中的测试数量仅为历史记录，本轮以重新执行的结果为准。
- T-34：`authorize()` 单独调用抛 `GateError`；Graph 捕获后发出 `gate_invalid` interrupt，后续合法 resume 必须仍可用。
- T-36：hook 返回 `m6_conflict`，Graph 消费为 `pending_gate.conflict` / trace；冲突后重试仍无法恢复的业务错误可以失败终止，不声称已修复其原因。
- T-01：来源通过 `get_costing_snapshot.data.snapshot.evidence.caller_evidence` 回读验证；不是 save 输出的直接字段。
- L2 使用 `registry.call()` 验证 schema 与 handler；缺必填字段会先被 Registry 拒绝，另测 handler 的 `INVALID_INPUT` 映射。
- 增补 T-44：四个 M6 commit hook 同步更新 `result` 与兼容字段 `data`，防止批准后仍从 `result` 读到 trial。
- 工资整张事实集缺失与逐行缺口分开：报工或单价集缺失为 `facts_present=false`；两者在位但部分记录无匹配单价仍为 `cost_incomplete=true`。消费者必须同时检查两个标志，不能只看 `success` 或 `cost_incomplete`。
- 身份权限只完成登记和新租户绑定到 Gate 的测试桥接，不能据此声明 API 已强制执行 `finance.approve` / `cost.view`。

**口径 1｜缺 quantity 不得编造总成本。**
试算阶段允许"只有单价、还没定数量"，故**不硬报错**；但 `total_cost` 不得落 `0`（0 是一个编造出来的数字）。缺 quantity 时：`missing_inputs` 追加 `{"reason": "missing_quantity"}`、`cost_incomplete=True`、`total_cost=None`。

**口径 2｜空工资输入判为事实缺失，用显式在位标志表达。**
`report_events` / `piece_rates` / `salary_standards` 为空或未给 → 判为**事实缺失**，以显式在位标志（`facts_present=false`）+ `missing=[{"reason": "missing_salary_facts"}]` 表达。`cost_incomplete` 语义固定为"输入在位但存在无法计算的缺口"，**空输入时不置 True**——否则"本月确实没人报工"这一正常状态会天天误报。

**口径 3｜新增 `finance-officer` 角色 + `finance.approve` / `cost.view` 权限。**
复用 `factory-director` 的方案否掉。`reviewer/gates.py` 已声明 finance 门角色为 `("finance-officer", "admin")`，本任务只补 identity 侧登记，角色名对齐即可。

## 二、任务表

### 表 A：任务总览

| 任务 ID | 工作域 | 任务（可验收粒度） | 优先级 | 现状 | 依赖 | 负责人 | 工期 |
|---|---|---|---|---|---|---|---|
| T0 | 治理 | P-015 收口：提交今日未提交改动，落干净的 P-016 基线 | P0 | 6 源文件 + 2 账本 + 1 新 PLAN + 未跟踪 README 在工作树；全量 pytest 826 passed/5 skipped 已实测绿 | 无 | Agent | 0.5h |
| T1 | 成本口径 | 缺 quantity → `missing_quantity` + `cost_incomplete=True` + `total_cost=None` | P0 | `m6_tools.py:291-292` 现为 `total_cost = round(unit_cost * (quantity or 0.0), 4)`，缺数量静默得 0 且 `cost_incomplete=False`（已实测复现） | T0 | Agent | 2h |
| T2 | 工资口径 | 空工资输入 → 显式在位标志 + `missing_salary_facts`，`cost_incomplete` 不误报 | P0 | `m6_cost.py:325` / `:372` 空输入返回 `cost_incomplete=False, missing=[]`，与"有数据无缺口"形状无法区分（已实测复现） | T0 | Agent | 3h |
| T3 | 身份权限 | `finance-officer` 角色 + `finance.approve` / `cost.view` 权限登记（含 3 处断言同步） | P1 | `gates.py:19` 已声明该角色；`identity.py` 的 catalog / GATE_PERMISSION / 种子角色三处**均无**财务条目 | T0 | Agent | 3h |
| T4 | 测试基建 | 按 `TEST-M6-fixture-pack-20260915.md` §二 的 fixture 规格落地假数据包，并把 §三 的 T-xx 逐条写成可执行用例 | P0 | 三层能力均已具备（166 个 M6 测试 + `m6_db` 隔离 fixture）；预期数值已在 TEST 文档实算登记，无需重算 | T1、T2 | Agent | 6h |
| T5 | 治理 | 收口：账本 / docs / 全量测试 / PR | P0 | 本次新增 PLAN 未提交；D-013~D-015 待登 | T1–T4 | Agent | 2h |

### 表 B：任务内容、交接与交付要求

| 任务 ID | 分支与基线 | 任务内容 | 交接内容（前置文档 + 基线 SHA + 数据与人员卡点） | 交付要求（DoD + 测试命令） |
|---|---|---|---|---|
| T0 | `feat/m6-finance-20260913` @ `7790e4a` | 把工作树里的 P-015 改动提交；只暂存本任务文件 | 前置：`PLAN-M6-standard-conformance-20260915.md`；卡点：`.project-to-act/docs/README.md` 当前**未跟踪**，须随 T0 一并纳入，否则后续 docs 索引无基线 | `git status` 干净；全量 `python -m pytest -q` 826 passed/5 skipped；`python scripts/check_contracts.py` exit 0 |
| T1 | 同 T0 | 在 `m6_save_costing_snapshot` 内（**不改共用 `_cost_breakdown`**）加缺数量判定；同步契约描述与 ledger skill 文档 | 前置：`m6_tools.py:267`（save）、`:219`（共用预算，**勿动**）、`m6_store.py:61-79`（`quantity`/`total_cost` 已是可空 `REAL`，无需改表）；卡点：`get_product_cost` 是"单台成本"预览，**不需要** quantity，检查若放进 `_cost_breakdown` 会误报预览 | 新增回归：缺 quantity（其余齐全）→ `cost_incomplete=True` + `missing_inputs` 含 `missing_quantity` + `total_cost is None`；有 quantity 路径数值不变（81.0 元/台不变）；`python -m pytest tests/ -q -k m6`；`check_contracts` 计数不变 |
| T2 | 同 T0 | 两个内核函数加在位标志与缺失 reason；两个 handler 透出该标志 | 前置：`m6_cost.py:271`（piece_pay）、`:328`（monthly_pay）、`m6_tools.py:1272`、`:1300`；卡点：`test_m6_payroll.py` 现有断言依赖 `cost_incomplete` 当前取值，改语义前先跑该文件确认影响面 | 新增回归两条：①空输入 → `facts_present=false` + `missing` 含 `missing_salary_facts` 且 `cost_incomplete=false`；②有报工有单价 → `facts_present=true` 且 `cost_incomplete=false`（防误报）；`python -m pytest tests/test_m6_payroll.py tests/test_m6_cost.py -q` |
| T3 | 同 T0 | `PERMISSION_CATALOG` 加两项；`GATE_PERMISSION` 加 `finance`；`DEFAULT_ROLE_SEEDS` 加 `finance-officer`；同步 3 处断言 | 前置：`identity.py:47`（catalog）、`:69`（GATE_PERMISSION）、`:96`（种子）、`gates.py:19`；卡点见下方"撞车点"——3 条断言是**精确相等**，不同步改必红 | 新增回归：绑定 `finance-officer` 的用户可过 finance 门、`operator` 仍被拒；`python -m pytest tests/test_identity_placeholder.py tests/test_m6_finance_gate.py tests/test_m6_store.py -q` |
| T4 | 同 T0 | 按 TEST 文档落地假数据包与 T-xx 用例（用例清单不在本文件重复） | 前置：`TEST-M6-fixture-pack-20260915.md` §二（fixture 规格 + 实算预期数值）、`tests/test_m6_finance_gate.py:44-56`（`m6_db` 隔离 fixture 与 `FACTS` 形状，直接复用）；卡点：假数据须带 `source_ref: fixture:<ID>` 且**不得**伪装成真实 M0/M4 数据 | TEST 文档 §三 的 T-xx **每条都有对应可执行用例**（含 G1 的三条人工核查项的取证方式）；用例可单条运行；`python -m pytest tests/ -q -k m6` 全绿 |
| T5 | 同 T0 | 账本 F-008 / P-016 / E-010 / D-013~D-015；docs 索引；全量测试；同 PR | 前置：`PROJECT_PROGRESS.md`（P-014/P-015 行格式）、`PROJECT_OVERVIEW.md`（D 行格式与号段）；卡点：D-012 已被"老仓引用先例"占用，新决策从 D-013 起编 | `python -m pytest -q` 全绿（退出码 0）；`python scripts/check_contracts.py` exit 0；账本行 ≤300 字符；docs README 索引 ↔ 文件双向一致 |

## 三、撞车点（会变红的现有断言，必须同步改）

新增权限与新增角色会撞上三条**精确相等**断言，同属 T3 范围：

| 位置 | 现有断言 | 处置 |
|---|---|---|
| `tests/test_identity_placeholder.py:43` | `codes == _FROZEN_11 \| {"worker.view", "report.view"}` | 并集补入 `finance.approve`、`cost.view` |
| `tests/test_identity_placeholder.py:47` | `scoped == {"order.view", "report.view", "worker.view"}` | 补入 `cost.view`（查看类权限带 scopes，与既有三项同形） |
| `tests/test_identity_placeholder.py:61` | `test_default_role_seeds_nine_roles`：集合精确等于 9 个角色码 | 改十角色并改测试名（`..._ten_roles`） |

**不需要改**：`reviewer/gates.py:19` 的 `("finance-officer", "admin")` 已是目标值；`tests/test_m6_store.py:199` 断言同一取值，保持通过。

**另需注意**：`m6-ledger` 的 `references/tools.md` 与 `registry-manifests/m6.json` 逐项对齐，且由 `tests/test_skill_registry_consistency.py` 守护漂移。T1 若改动契约描述，必须同步该文档同段落。

## 四、T4 详细要求

### 假数据来源标识（红线：不得伪装成真实事实）

每条假数据带 `source_ref: "fixture:M6-XXX"` 与 `evidence: {fixture_id, observed_at}`。代码侧已有落点：`m6_save_costing_snapshot` 会把 `payload["evidence"]` 原样收进 `resolved_evidence.caller_evidence`，无需新增机制。

### 三层测试面

| 层 | 验证内容 | 是否经 Gate | 运行方式 |
|---|---|---|---|
| L1 纯函数 | 成本 / 工资 / 分摊 / 对账公式 | 否 | 直接调 `m6_cost` 函数，不落库 |
| L2 Registry Tool | Tool 入参、`M6Store` 落库、业务错误码、trial 状态 | 否 | 直接调 handler（ctx 给 `m6_db_path`） |
| L3 Graph | 装配、interrupt / resume、finance 门、commit 钩子 | 是 | `build_graph` + resume approve / reject |

L3 复用 `tests/test_m6_finance_gate.py` 的 `m6_db` fixture 模式（`tmp_path` + `YUNPAI_M6_DB`，并把 `YUNPAI_M0_DB`/`YUNPAI_M4B_DB` 指向不存在的库以隔离上游）。

### 干净成本用例的载荷形状（实测确认，写错会得到 `cost_incomplete=True`）

有库存的行**必须由 BOM 行自带的 `unit_price` 定价**（口径 `bom_price`），库存行本身不提供价格。可用形状：

```json
{
  "period": "2026-09", "order_id": "SO-XXX", "product_code": "P1", "quantity": 10,
  "bom_lines": [{"material_code": "M1", "qty_per": 2, "unit_price": 5.0, "loss_rate": 0.1}],
  "routing_steps": [{"operation_id": "OP10", "standard_minutes": 60}],
  "inventory": [{"material_code": "M1", "available_qty": 100, "stock_class": "raw"}],
  "hour_rate": 50, "overhead_rate": 20
}
```

实测：`unit_cost=81.0`、`total_cost=810.0`、`cost_incomplete=False`。（若库存行给 `unit_cost` 而不给 BOM 行 `unit_price`，会得到 `missing_stock_price` → `cost_incomplete=True`。）

### 预期数值必须入库文档

假数据对应的预期数值已在本轮**实算并登记**在 `TEST-M6-fixture-pack-20260915.md` §二（FX-COST-001/002、FX-PAY-001/002、FX-EXP-001、FX-ASSET-001、FX-STMT-001、FX-INV-001），T4 直接引用，不必重算；若新增 fixture，须按同格式补登预期数值后再写用例。

## 五、收口清单（T5）

1. 全量测试：`python -m pytest -q` 退出码 0。
2. 契约校验：`python scripts/check_contracts.py` exit 0；工具计数保持 147 / handler 128 / m6 24 / bound_local 128 / unbound 6（两条 W2 为 D-008 刻意接受项，本轮不变更判定）。
3. 账本：`F-008` 功能行更新 + `P-016` 进度行 + `E-010` 验收证据（行 ≤300 字符）+ `D-013`（缺 quantity）/ `D-014`（空工资事实缺失）/ `D-015`（finance 角色权限）决策行。
4. docs：本文件 + `TEST-M6-fixture-pack-20260915.md` + `README.md` 索引同步，索引与实际文件双向一致。
5. 同一分支、同一次 push、同一个 PR（base = 干线）。

## 六、本轮实施结果（2026-09-15）

- T1–T4 已完成：`m6_save_costing_snapshot` 对缺 quantity 保留单位成本并返回 `total_cost=null`；计件/月薪空事实显式返回 `facts_present=false`；身份目录加入 `finance-officer`、`finance.approve`、`cost.view`；四个 commit hook 同步 `data` 与 `result`；新增 7 组 JSON 夹具及可单条运行的 L1/L2 回归。
- 证据：`python -m pytest tests -q` → **834 passed, 5 skipped**；`python -m pytest tests -q -k m6` → **173 passed, 1 skipped**；`python scripts/check_contracts.py` → exit 0（工具 147 / handler 128 / M6 24）；`python -m compileall -q src tests` → exit 0；身份/门禁/夹具聚焦 → **24 passed**。
- 收口状态（2026-09-15 复核后更新）：T0/T5 已完成——全部改动已随 `ce61cae`（`fix: 收敛 M6 财务规范并补齐夹具测试`）提交，账本 P-015/P-016 状态已改为「已完成」。**推送与 PR 尚未执行**，按项目流程由负责人安排。
- 复核实测（独立于 Codex 自报）：`python -m pytest -q` → 834 passed, 5 skipped, exit 0；`python -m pytest tests/ -q -k m6` → 173 passed, 1 skipped；`python scripts/check_contracts.py` → exit 0；`python scripts/check_contracts.py --strict` → **exit 1**（D-008 刻意接受的 2 条 W2：`ingest_recognized`、`save_costing_snapshot`，非缺陷，验收只认默认模式）。
- 复核发现的遗留（本轮未修，已立 P-017 或在测试文档登记）：finance 门载荷无金额（→ T6）；`/runs` 与 resume 端点全仓零覆盖（→ T7）；T-51 租户隔离无用例（见 `TEST-M6-fixture-pack-20260915.md` 附录 C）。

## 七、验收边界（诚实标注）

- 本轮证明的是：M6 财务链在假数据下自洽，契约与门禁行为符合已拍口径。
- 本轮**不能**证明：真实 canonical 数据齐备性、M1→M5→M6 全链路、生产环境验收。这三项属"接真实数据库"之后的独立验收轮次。
- 接真库时会撞上的两个已记账口径缺口（`PROJECT_PROGRESS.md` P-014 已记录，不在本轮范围）：`standard_time` 单位未定（`_standard_minutes_of` 按秒 /60，M6 未接线，猜单位 = 静默 60 倍误差）；`loss_rate` 在 `m6_cost.py:65` 走 `_num` 隐式 0.0 与 D-006 不一致。

---

# 补充轮次（P-017）：审批可见性与 API 层覆盖

- 立项：P-016 完成后复核发现两处与"财务可信"直接相关的缺口，用户授权立补充轮次
- 基线：`feat/m6-finance-20260913` @ `ce61cae`（P-016 交付点）
- 与 P-016 的关系：**独立轮次**，不改 P-016 的口径与夹具；T6 只补门载荷内容，T7 只补测试覆盖面
- 非目标：仍不建 M6 Workflow、不引前端/新架构、不接真实数据库

## 表 A：任务总览

| 任务 ID | 工作域 | 任务（可验收粒度） | 优先级 | 现状 | 依赖 | 负责人 | 工期 |
|---|---|---|---|---|---|---|---|
| T6 | 门载荷 | finance 门携带"被审阅的合计"，审批人可见金额与缺口标志 | P0 | 门对象实测只有 7 个键（type/tool/step_id/reason/allowed_roles/payload_digest/opened_at），**无任何金额** | 无 | Agent | 3h |
| T7 | 测试覆盖 | 补 `/runs` 与 `/runs/{run_id}/resume` 的 API 层用例（含门可见、approve、reject、角色） | P0 | **全仓 0 覆盖**：`grep -rl "/runs\|resume" tests/*.py` 无命中；`test_evolution.py` 用裸 `FastAPI()`，真 `create_app` 从未被测试 | T6（仅 review 断言部分） | Agent | 4h |

## T6｜finance 门携带被审阅的合计

### 现象（实测，2026-09-15）

把三段式跑到 finance 门挂起，审批人（或任何前端）能看到的**全部**内容：

```json
{
  "type": "gate_pending",
  "gate": {
    "type": "finance",
    "tool": "confirm_costing_snapshot",
    "step_id": "tool-confirm_costing_snapshot",
    "reason": "成本确认 trial→confirmed 是生效写，须 finance 门人工批准后由 _apply_m6_costing_confirm 落库",
    "allowed_roles": ["finance-officer", "admin"],
    "payload_digest": "{'success': True, 'error': None, 'business_status': 'completed', ...",
    "opened_at": "2026-09-15T05:09:35+00:00"
  }
}
```

看不到总额、单台成本、账期、订单号、快照号，也看不到 `cost_incomplete` 是否为真。

### 为什么这是问题而不是体验问题

D-005 立三段式的理由原文是"财务模块不可接受'成本已标正式/月账已冻结后才等人批'"；`_apply_m6_close_month` 的注释进一步写明"冻结的是**被审阅的那组合计**……审阅什么就冻结什么，避免'批了 A 冻了 B'"。但**没有任何东西把被审阅的那组数放到审批人眼前**——形式上是"先批后落库"，实质上审批人只能盲批。金额确实已在 `outputs.<tool>.data` 里（`public_state` 会吐出 `outputs`），缺的是"这份数据的哪几个字段是给人审的"这一声明。

### 设计（沿用既有模式：工具声明意图，rules 抽取，graph 建门）

| 步骤 | 位置 | 改动 |
|---|---|---|
| 1 | `m6_tools.py` 四个"commit 段发起"工具 | 在成功返回的 `data` 里加 `review_summary`（字段见表下） |
| 2 | `reviewer/rules.py:428` `_diagnostics()` | 增加一项 `review`：取 `data.review_summary`（**唯一抽取点**，与 `code`/`message`/`missing_fields` 同处） |
| 3 | `reviewer/gates.py:43` `make_gate()` | 增加可选 `review` 形参，**非空才写入**（与 `code`/`message`/`missing_fields` 的既有向后兼容写法一致） |
| 4 | `graph.py:534`（唯一开门点） | 传 `review=gate_finding.get("review")` |

`review_summary` 应含字段：

| 工具 | 必含字段 |
|---|---|
| `confirm_costing_snapshot` | `snapshot_id`、`period`、`order_id`、`product_code`、`batch_no`、`quantity`、`unit_cost`、`total_cost`、`basis`、**`cost_incomplete`** |
| `close_month_costing` | `period`、`total_cost`（被审阅合计）、`snapshot_count`、`trial_count`、`trial_excluded` |
| `save_quotation` / `save_statement` | `doc_no`、`doc_type`、`counterparty_code`、`line_count`、`totals`、`basis_source` |
| `upsert_asset_ledger` | `asset_code`、`revision`、`original_value`（生效原值口径）、`effective_date` |

### 红线

1. `review_summary` **只能抄 propose 阶段已产出的结果**，不得在 commit 时重算（承 D-005"审阅什么就冻结什么"）。
2. **必须含 `cost_incomplete`**：批准一份算不全的成本是业务决策，审批人有权看到"这个数不全"，不得隐藏。
3. 不得改动 `payload_digest` 的语义、`allowed_roles`、`GATE_DECISIONS`；门型与决策矩阵不变。
4. 不得用 `review_summary` 参与任何授权判断——它是**只读展示字段**。

### 撞车点（必须处理，否则变红）

| 位置 | 现有断言 | 处置 |
|---|---|---|
| `tests/test_migration_m0.py:446` | `set(legacy) == {"type","tool","step_id","reason","allowed_roles","payload_digest","opened_at"}`，且注释写明"不传诊断字段时 Gate 形状与旧版逐字一致" | **`review` 必须非空才写入**。若写成无条件 `review={}`，该测试必红 |
| `tests/test_review_rules_gates.py:51`、`test_review_rules_parity.py:100` | 只断言 `allowed_roles` | 不受影响，跑一遍确认 |
| `tests/test_m6_finance_gate.py:122` | `gate["allowed_roles"] == list(FINANCE_ROLES)` | 不受影响 |

### 交付要求（DoD）

- 新增回归：门里 `review["total_cost"]` 与 propose 阶段 `outputs.confirm_costing_snapshot.data.total_cost` **相等**（防"批 A 冻 B"漂移）；`review` 含 `cost_incomplete`。
- 新增回归：不传 `review_summary` 时门键集仍为 7 个（锁住向后兼容，正对 `test_migration_m0.py:446`）。
- 命令：`python -m pytest tests/ -q -k "m6 or review or migration_m0"`；全量 `python -m pytest -q` 退出码 0。
- 文档：`PLAN` 的 T6 行 + `TEST-M6-fixture-pack-20260915.md` 增补一条"审批可见性"人工核查项（审批人能看见金额与 `cost_incomplete`）。

## T7｜补 `/runs` 与 `/runs/{run_id}/resume` 的 API 层用例

### 现象

`grep -rl "/runs\|resume" tests/*.py` **零命中**——`api/app.py` 的 11 个路由（含整条门 resume 面）全仓无测试。`test_evolution.py` 里的 `TestClient` 用的是裸 `FastAPI()`，因此**真正的 `create_app` 工厂从未被测试过**。M6 的 173 条用例全部停在 Graph 层（直接 `build_graph` + `graph.ainvoke` + `MemorySaver`），从未经过 HTTP、`public_state` 序列化与 `SQLiteRunRepository` 往返。

### 构造方式

```python
from fastapi.testclient import TestClient
from yunpai_orchestrator.api.app import create_app
from yunpai_orchestrator.repository import SQLiteRunRepository
from yunpai_orchestrator.config import OrchestratorConfig

# repository / run_db / m6_db_path 全部指向 tmp_path，禁止落 runtime
client = TestClient(create_app(repository=SQLiteRunRepository(tmp_path / "runs.sqlite"),
                              config=OrchestratorConfig(...)))
```

- `create_app(*, repository=None, registry=None, identity_store=None, evolution=None, config=None)`——**repository 可注入**，用它做隔离。
- `m6_db_path` 放在 `POST /runs` 的 body 顶层（经 `worker/executor.tool_context` 进工具 ctx，P-015 已接线）。
- 角色两条路都要覆盖：body 的 `roles` 与 `X-Actor-Roles` 请求头（后者是真前端的用法）。
- ⚠️ `create_app` 内部有 `_LoopRunner`（专属后台事件循环线程）。若同步 `TestClient` 出现挂起或跨循环错误，须查明循环归属后修正，**不得用 `time.sleep` 硬等**绕过。

### 用例清单

| ID | 请求 | 断言 |
|---|---|---|
| T-60 | `POST /runs`，body 含 `tools=[save_costing_snapshot, confirm_costing_snapshot]` + FX-COST-001 事实 + `m6_db_path` | HTTP 200；响应 `pending_gate.type == "finance"`；`allowed_roles` 含 `finance-officer`；`status` 为等待人工态 |
| T-61 | 承 T-60，`POST /runs/{run_id}/resume` body `{decision:"approve", actor, roles:["finance-officer","admin"]}` | `200`；`GET /runs/{run_id}` 可见 `outputs` 中 `confirm_costing_snapshot.data.status == "confirmed"`；`committed_by == "finance_gate"`；`approvals` 留痕 |
| T-62 | revert 场景：同 T-60，resume `{decision:"reject"}` | `200`；库中快照仍 `status=trial`；**无 confirmed 行**；`approvals` 保留 reject 记录 |
| T-63 | 同 T-60，resume `roles:["operator"]` | 授权被拒（gate 仍待人工，或应答含 `GateError` 语义的 4xx/错误体）；`allowed_roles` 未被绕过 |
| T-64 | 同 T-61 但角色走 `X-Actor-Roles: finance-officer` 请求头、`actor` 走 `X-Actor-User` | 与 T-61 同等通过（证明头路径可用） |
| T-65 | 承 T-61，`GET /runs/{run_id}` 序列化检查 | `public_state` 含 `pending_gate`/`outputs`/`approvals`/`status`；`content_b64` 被脱敏为 `[omitted]`（若存在） |
| T-66（依赖 T6） | 承 T-60 | `pending_gate.review.total_cost == 810.0` 且含 `cost_incomplete` |

### 撞车点与注意

- `public_state` 的键是**固定白名单**（`state.py:144`），**不含 `__interrupt__`**。门的对外出口是 `pending_gate`，断言不要写 `__interrupt__`。
- `POST /runs` 会走 `_principal(request)` 注入 `principal`；断言时注意 `run_id`/`thread_id` 来自响应，resume 用响应里的 `run_id`。
- T-60～T-65 与 T6 解耦，可在 T6 之前先做；只有 T-66 依赖 T6。

### 交付要求（DoD）

- 新增用例建议落 `tests/test_api_runs_m6.py`，**每条可单条运行**（`-k` 可选）。
- 命令：`python -m pytest tests/test_api_runs_m6.py -q`；全量 `python -m pytest -q` 退出码 0；`python scripts/check_contracts.py`（默认模式）exit 0，工具计数不变。
- 账本：`P-017` 进度行 + `E-011` 证据行；若 T6 的字段设计形成新口径，登记决策行（`D-016`）。
- 测试文档同步：`TEST-M6-fixture-pack-20260915.md` 增补"G7 API 层"组（T-60～T-66）与执行命令，避免该组游离在验收标准之外。

## 表 B：补充轮次的交接与交付

| 任务 ID | 分支与基线 | 任务内容 | 交接内容（防返工关键列） | 交付要求（DoD） |
|---|---|---|---|---|
| T6 | `feat/m6-finance-20260913` @ `ce61cae` | 四工具加 `review_summary`；`_diagnostics` 抽取；`make_gate` 加可选 `review`；`graph.py:534` 传参 | 前置：`reviewer/rules.py:428`（唯一抽取点）、`reviewer/gates.py:43`、`graph.py:534`（唯一开门点）；卡点：`review` 必须非空才写入，否则 `test_migration_m0.py:446` 变红 | 见 T6 DoD；`-k "m6 or review or migration_m0"` + 全量绿 |
| T7 | 同上 | 补 API 层用例 T-60～T-66 | 前置：`api/app.py:34`（`create_app` 可注入 repository）、`api/app.py:212/318`（`/runs`、resume）、`worker/executor.py`（`m6_db_path` 透传）；卡点：`public_state` 不含 `__interrupt__`；`_LoopRunner` 循环归属 | 见 T7 DoD；`tests/test_api_runs_m6.py` 全绿 + 全量绿 + 契约 exit 0 |

## P-017 实施结果（2026-09-15）

- T6 已完成：`confirm_costing_snapshot`、`close_month_costing`、`save_quotation`、`save_statement`、`upsert_asset_ledger` 在成功结果中提供 `review_summary`；规则层仅抽取已有摘要，Gate 仅在非空时携带，审批判断不读取该字段。旧无诊断 Gate 的 7 键形状保持兼容。
- T7 已完成：新增 `tests/test_api_runs_m6.py`，通过真实 `create_app`、隔离 `SQLiteRunRepository` 与临时 `m6_db_path` 覆盖 T-60～T-66。实测 API 的 `pending_gate` 直接承载门对象，与 `state.public_state` 既有契约一致；计划表中原 `pending_gate.gate` 写法已按实际契约修正。
- 验证：T6/T7 聚焦 46 passed；完整回归与契约检查以本轮收口命令结果为准。
