# M6 假数据测试项目与验收标准（TEST）

- 用途：**测试执行方（同事）**据此跑测并判定通过与否；修复方（Codex）任务见 `PLAN-M6-fixture-test-20260915.md`
- 前置：PLAN 的 T0（基线收口）、T1（缺数量口径）、T2（空工资口径）、T3（finance 角色）、T4（假数据包与自动化用例落地）**全部完成**后方可开测
- 依据：领导 2026-09-15 指令（M6 独立模块，先假数据开测，测通再接真实数据库）；三项已拍口径见 PLAN §一
- 关联：F-008 / E-010 / `PLAN-M6-fixture-test-20260915.md` / `PLAN-M6-standard-conformance-20260915.md`
- ⚠️ **本文件的用例是"跑一遍"，而跑一遍的增量价值有限**（预期值与代码同源）。真正的独立验证动作（换输入手算探针、人工三条、异机冒烟）见 **`TEST-M6-independent-verification-20260915.md`**——先做那份，本文件的 G1–G7 作为基线回归。

## 范围

**在范围内**：M6 财务模块在**假数据**下的确定性核算、落库三段式、finance 门禁、隔离、API 状态序列化与红线。
**不在范围内**：真实 canonical 数据齐备性、M1→M5→M6 全链路、M6 Workflow、生产环境验收、存量 M0–M5 写工具改造。

## 一、测试环境与运行方式

```bash
# 仓库根，分支 feat/m6-finance-20260913（基线 SHA 见 PLAN 表 A 的 T0 交付）
python -m pytest tests/ -q -k m6        # M6 聚焦面
python -m pytest tests/test_api_runs_m6.py -q  # create_app + runs/resume API 面
python -m pytest -q                     # 全量回归
python scripts/check_contracts.py       # 契约加载/去重/对齐
```

隔离要求：每个用例自建临时库（`tmp_path` + `YUNPAI_M6_DB`，并把 `YUNPAI_M0_DB`/`YUNPAI_M4B_DB` 指向不存在的库）。**禁止**指向 `runtime/yunpai-m6.sqlite` 等真库。参照 `tests/test_m6_finance_gate.py:44-56` 的 `m6_db` fixture。

## 二、假数据包规格（预期数值为实算结果，容差 0.0001）

金额一律为**未税**口径、保留 4 位小数。每条假数据带来源标识：`source_ref: "fixture:<ID>"`、`evidence: {fixture_id, observed_at}`。

### FX-COST-001｜干净成本（齐全输入）

| 项 | 值 |
|---|---|
| 输入 | `period=2026-09`、`order_id=SO-FX-001`、`product_code=P1`、`quantity=10`、BOM `[{M1, qty_per 2, unit_price 5.0, loss_rate 0.1}]`、工序 `[{OP10, standard_minutes 60}]`、库存 `[{M1, available_qty 100, stock_class raw}]`、`hour_rate=50`、`overhead_rate=20` |
| 预期 | `unit_cost=81.0`、`total_cost=810.0`、`cost_incomplete=false`、`missing_inputs=[]`、`basis=stock` |
| 备注 | **有库存的行必须由 BOM 行自带 `unit_price` 定价**；库存行给 `unit_cost` 无效，会得 `missing_stock_price` |

### FX-COST-002｜缺 quantity（口径 1 回归）

输入同 FX-COST-001 但**不给** `quantity`。预期：`cost_incomplete=true`、`missing_inputs` 含 `{"reason": "missing_quantity"}`、`total_cost` **不得为 0**（应为 `None`/无值）、`unit_cost=81.0` 照算。

### FX-PAY-001｜计件工资

| 项 | 值 |
|---|---|
| 报工 | `W001/ST-01/P1 qty100 scrap5 2026-09-10`、`W002/ST-01/P1 qty80 scrap0`、`W003/ST-02/P1 qty10`（无对应单价） |
| 单价 | `[{ST-01, P1, unit_rate 2.5, effective_from 2026-09-01}]` |
| 预期 | `totals=[{W001, 237.5}, {W002, 200.0}]`；`missing` 含 `W003 / missing_piece_rate`；`cost_incomplete=true` |

### FX-PAY-002｜月薪工资

| 项 | 值 |
|---|---|
| 输入 | 月薪 `{W001:6000, W002:5000}`；出勤 `{W001:{overtime_hours 10, absence_hours 0}, W002:{overtime_hours 0, absence_hours 8}}`；计件 `{W001:237.5}` |
| 预期 | W001：`overtime_pay=517.2414`、`gross_pay=6754.7414`；W002：`absence_deduct=229.8851`、`gross_pay=4770.1149`；`cost_incomplete=false` |

### FX-EXP-001｜费用分摊｜FX-ASSET-001｜资产分摊

- FX-EXP-001：费用 `[{electricity, 3000}]`、基准 `[{P1, quantity 60}, {P2, quantity 40}]`、口径 `quantity` → `P1=1800.0`、`P2=1200.0`、`total_allocated=3000.0`、`cost_incomplete=false`
- FX-ASSET-001：用量 `[{MOLD-1, P1, quantity 60}, {MOLD-1, P2, quantity 40}]`、成本 `{MOLD-1: 1000.0}`、口径 `quantity` → `P1=600.0`、`P2=400.0`、`cost_incomplete=false`

### FX-STMT-001｜对账

期初 `1000`、`in 500 / out 200` → `inflow=500.0`、`outflow=200.0`、`closing_balance=1300.0`。

### FX-INV-001｜库存财务视图（四态）

库存 `[{M1, 100, raw}, {M2, 50, wip}, {M3, 10, mystery}]`、单价 `{M1:5.0, M2:8.0}` → `raw: 100/500.0`、`wip: 50/400.0`、`unknown: 10/0.0` 且 `amount_incomplete=true`；`missing` 含 `M3/unknown_stock_class` 与 `M3/missing_unit_cost`；`cost_incomplete=true`。

## 三、测试项目

### G1 假数据包自身合规（人工核查，逐条签核）

| ID | 项目 | 预期 | 判据 |
|---|---|---|---|
| T-01 | 来源标识在位 | 每条假数据带 `source_ref: fixture:<ID>` + `evidence.fixture_id`/`observed_at`，且经 `save_costing_snapshot` 透传到输出的 `caller_evidence` | 抽查 5 条，缺一即失败 |
| T-02 | 不伪装成真实事实 | 假数据不含真实客户/供应商/员工/物料名称；`source_ref` 前缀均为 `fixture:` | 全量 grep，出现非 fixture 前缀即失败 |
| T-03 | 假数据与预期数值已登记 | 本文件 §二 的 fixture ID ↔ 用例 ↔ 预期数值三者一一对应 | 任一新用例未登记即失败 |

### G2 L1 公式层（不过 Gate、不落库）

| ID | 项目 | 输入 | 预期 / 判据 |
|---|---|---|---|
| T-10 | 材料成本 | `[{M1, qty_per 2, unit_price 5.0, loss_rate 0.1}]` | 材料成本 = `5.0×2×1.1 = 11.0` |
| T-11 | 工序成本 | `[{OP10, standard_minutes 60}]` + `hour_rate 50` / `overhead_rate 20` | 人工 `50.0`、制费 `20.0`（1 小时） |
| T-12 | 报价 | 材料 11 / 人工 50 / 制费 20、加价率 0.2 | `base_cost=81.0`、`quote_price=97.2` |
| T-13 | 订单审计 status | 见 `audit_order_cost` 输入形状 | `status ∈ {passed, low_profit, loss, cost_incomplete}`，**算不全必须降级 `cost_incomplete`，不得判盈利** |
| T-14 | 对账 | FX-STMT-001 | `closing_balance=1300.0` |
| T-15 | 费用/资产分摊 | FX-EXP-001 / FX-ASSET-001 | 见 §二；分摊合计 == 费用/成本总额 |
| T-16 | 工资 | FX-PAY-001 / FX-PAY-002 | 见 §二 |
| T-17 | 库存四态 | FX-INV-001 | 见 §二；**态未知不得归入任何一态** |

### G3 L2 Tool 层（落库与错误码）

| ID | 项目 | 操作 | 预期 / 判据 |
|---|---|---|---|
| T-20 | 试算只落 trial | 调 `save_costing_snapshot`（FX-COST-001） | 库中 `status=trial`；`month_summary` **不纳入**该快照 |
| T-21 | 缺账期拒绝 | 不给 `period` | `success=false`、`code=INVALID_INPUT` |
| T-22 | 快照号不覆盖 | 同 `snapshot_id` 落两次 | 第二次 `code=SNAPSHOT_EXISTS`，原快照不变 |
| T-23 | 月结后禁写 | 先 `close_month`，再 `save_costing_snapshot` | `code=MONTH_CLOSED`；试算不被落库 |
| T-24 | 确认前置校验 | ①不存在的 `snapshot_id`；②已 `confirmed` 的快照再确认 | ①`code=NOT_FOUND`；②幂等：`changed=false` + `pending_confirmation=false`（不开第二次门） |
| T-25 | 纯算数工具不落库 | `get_product_cost` / `audit_order_cost` / `allocate_expenses` / 工资两件 | 调用前后 M6 库快照数与单据数不变 |
| T-26 | 失败不得伪装成功 | 所有失败分支 | `success=false` 时必有 `code`，且 `business_status=failed`；不得出现 `success=true` 携带错误码 |

### G4 L3 Graph 门禁层（三段式）

| ID | 项目 | 操作 | 预期 / 判据 |
|---|---|---|---|
| T-30 | 试算无门 | tools=`[save_costing_snapshot]` | 运行直接完成，**不产生 interrupt**（D-008 刻意设计） |
| T-31 | 确认开门 | tools=`[save_costing_snapshot, confirm_costing_snapshot]` | 挂起在 `__interrupt__`，门型 `finance`，`allowed_roles` 含 `finance-officer` |
| T-32 | approve 生效 | resume `approve` | 库中 `status=confirmed` + `confirmed_by`/`confirmed_at` 有值 + 输出 `committed_by=finance_gate` |
| T-33 | reject 不生效 | resume `reject` | 库中仍 `status=trial`，**无 confirmed 行**；`approvals` 保留审批记录；Run 状态非 completed 的正常终态 |
| T-34 | 角色校验 | resume `approve` 但 `roles=["operator"]` | 抛 `GateError`（拒绝）；换 `admin` 或含 `finance-officer` 则通过 |
| T-35 | 月结冻结 | `close_month_costing` → approve；再次结账 | 冻结的是**被审阅的合计**（`trial_count` 不计入）；二次结账 `code=MONTH_ALREADY_CLOSED` |
| T-36 | 冲突不抛异常 | 批准前把快照删掉 / 把该期间先冻结，再 approve | 返回 `m6_conflict`（不抛异常）、**不写生效行**、步骤退回 pending（人工可恢复） |
| T-37 | 其余写工具同款 | `save_quotation` / `save_statement` / `upsert_asset_ledger` | approve → `confirmed`；reject → 草稿保留但不生效；`upsert_asset_ledger` 的 propose **不得改写已确认原值** |

### G5 本轮修复项回归（对应 PLAN T1–T3）

| ID | 项目 | 操作 | 预期 / 判据 |
|---|---|---|---|
| T-40 | 缺数量不编造（口径 1） | FX-COST-002 | `cost_incomplete=true`；`missing_inputs` 含 `missing_quantity`；`total_cost` 非 0（为 `None`）；`get_product_cost`（单台预览）**不受影响**：不给 quantity 仍正常算出 `unit_cost=81.0` 且 `cost_incomplete=false` |
| T-41 | 空工资 = 事实缺失（口径 2） | `calculate_piece_pay({}, {})`；`calculate_monthly_pay({}, {}, {})` | `facts_present=false`；`missing` 含 `missing_salary_facts`；**`cost_incomplete=false`**（不得误报） |
| T-42 | 有工资不误报（口径 2 反向） | FX-PAY-002 | `facts_present=true` 且 `cost_incomplete=false` |
| T-43 | finance 角色可用（口径 3） | 新租户绑定 `finance-officer` 并 resume approve | 通过；`operator` 仍被拒；`finance.approve` 的 catalog `gate` 列与 `GATE_PERMISSION["finance"]` 一致 |

### G6 红线与隔离

| ID | 项目 | 操作 | 预期 / 判据 |
|---|---|---|---|
| T-50 | 测试不碰真库 | 检查用例库路径 | 全部落在 `tmp_path`；`runtime/yunpai-m6.sqlite` 无写入 |
| T-51 | 租户隔离 | 租户 A 落快照，租户 B 读 | B 读不到 A 的数据 |
| T-52 | Skill 越界拒绝 | 经 M6 skill 调 M0/M5 工具 | 抛 `ValueError`（operation map 白名单） |
| T-53 | 事实值永不默认 | 取不到单价的行 | 计入 `missing` 且标 `cost_incomplete`；**不得用 0 或默认价冒充** |

### G7 API 层（真实 `create_app`）

| ID | 项目 | 操作 | 预期 / 判据 |
|---|---|---|---|
| T-60 | API 创建运行 | `POST /runs` 注入 FX-COST-001、`tools=[save_costing_snapshot, confirm_costing_snapshot]`、临时 `m6_db_path` | HTTP 200；`pending_gate.type=finance`，`allowed_roles` 含 `finance-officer`；`status=waiting_human` |
| T-61 | API approve | `POST /runs/{id}/resume` 携 `decision=approve`、finance 角色 | HTTP 200；GET 状态中 confirm 输出为 `confirmed`，`committed_by=finance_gate`，并有审批留痕 |
| T-62 | API reject | 同 T-60，resume `decision=reject` | HTTP 200；M6 快照仍为 `trial`，无 confirmed 生效行，审批记录为 reject |
| T-63 | API 越权 | resume 携 `roles=[operator]` | HTTP 200 但仍 `waiting_human`；finance 门保持不变，试算快照仍为 `trial` |
| T-64 | API 角色请求头 | 使用 `X-Actor-User` + `X-Actor-Roles: finance-officer` | 与 T-61 同等通过 |
| T-65 | API 公共状态 | `GET /runs/{id}` | 固定白名单含 `pending_gate`/`outputs`/`approvals`/`status`；不含 `__interrupt__`；附件 `content_b64`（如存在）脱敏 |
| T-66 | API 审阅可见性 | 同 T-60 | `pending_gate.review.total_cost=810.0` 且包含 `cost_incomplete`；该字段只展示，不参与授权 |

> API 对外 `pending_gate` 直接承载门对象；Graph 的 `__interrupt__` envelope 只存在内部事件流。该形状与 `state.public_state` 及既有仓储镜像保持一致。

## 四、验收标准

### 通过条件（全部满足才算通过）

1. **自动化项全绿**：`python -m pytest tests/ -q -k m6`，并单独执行 `python -m pytest tests/test_api_runs_m6.py -q`；计数以本轮实际输出为准。
2. **全量无回归**：`python -m pytest -q` 退出码 `0`。
3. **契约不变**：`python scripts/check_contracts.py` 退出码 `0`，工具计数 `147 / handler 128 / m6 24 / bound_local 128 / unbound 6` 与基线一致（两条 W2 为 D-008 刻意接受项，维持不变）。
4. **G1 人工项逐条签核**：T-01～T-03 有签核结论与证据路径，不得只写"已检查"。
5. **数值判据**：G2/G3/G5 的预期数值偏差 `≤ 0.0001` 视为通过；任一超差即失败。
6. **证据留档**：pytest 输出（含 passed/skipped 计数）、`check_contracts` 输出、G1 签核表；缺证据的项按未通过计。

### 不通过判定

- 任一 **P0 用例**（T-20、T-24、T-32、T-33、T-36、T-40、T-41、T-53）失败 → 整轮不通过。
- 出现 `success=true` 携带错误、或以 `0`/默认值冒充缺失事实 → 整轮不通过（红线，不按单条计）。
- 用例库路径指向真库 → 整轮不通过（污染生产数据的风险）。

### 环境级偶发失败：报缺陷前先复跑

**现象**：Windows 上偶发一条失败（或仅作为收尾噪音出现），报错形如

```
PermissionError: [WinError 5] 拒绝访问。:
'C:\Users\<用户名>\AppData\Local\Temp\pytest-of-<用户名>\pytest-current'
```

**性质**：这是 pytest 清理临时目录（`tmp_path` 的编号目录）时与 Windows 文件句柄/杀毒/索引服务竞争导致的环境问题，**与本轮代码无关**。实测：同一目录复跑即恢复 `179 passed / 1 skipped`。

**处置**：报缺陷前**先原样复跑一次**；仍失败再换一个干净的临时目录：

```bash
python -m pytest tests/ -q -k m6 --basetemp=%TEMP%\m6-run1     # Windows cmd
python -m pytest tests/ -q -k m6 --basetemp=/tmp/m6-run1       # Git Bash / macOS / Linux
```

只有**在干净 basetemp 下仍稳定复现**的失败才计为缺陷。请把两次输出都留下作为证据（说明是环境偶发而非缺陷）。


### 通过之后的下一步

本轮通过仅证明 **M6 财务链在假数据下自洽**，**不等于**生产验收。接真实数据库是**独立轮次**，切换点在装配层 `_assemble_costing_facts`（`orchestration_bridge.py:1180`），届时须重新验收数据齐备性，并处理两个已记账口径缺口：

- `standard_time` 单位未定（`_standard_minutes_of` 按秒 /60，M6 未接线；猜单位 = 静默 60 倍误差）
- `loss_rate` 在 `m6_cost.py:65` 走 `_num` 隐式 0.0，与 D-006 不一致

## 五、本轮明确不覆盖（不得据本轮结论推断）

| 项 | 原因 |
|---|---|
| M1→M5→M6 全链路 | 无 M6 Workflow；M6 走 `free` 路由 |
| 真实 canonical 数据齐备性 | 假数据不证明真库字段齐全 |
| 工序/人工/制费在真库下的正确性 | 依赖 `standard_time` 单位口径 |
| 工资的 canonical 事实面 | v2 无 canonical 工资实体，只能显式注入 |
| 供应商报价两件工具 | ⛔ 阻塞 R1b |
| 前端 UI 可见状态 | 本轮仅覆盖 API `public_state`，不做浏览器验收 |

---

## 附录 A：执行清单（照抄即可）

```bash
# 1) M6 聚焦面（主判据）
python -m pytest tests/ -q -k m6
#    本轮：179 passed, 1 skipped, 0 failed

# 2) 全量回归
python -m pytest -q
#    本轮：841 passed, 5 skipped, 0 failed

# 3) 契约校验（必须用默认模式）
python scripts/check_contracts.py
#    预期 exit 0；工具 147 / handler 128 / m6 24 / bound_local 128 / unbound 6

# 4) 身份面（T-43 判据，不含 m6 关键字，不会被 -k m6 选中）
python -m pytest tests/test_identity_placeholder.py -q

# 5) 夹具单独运行
python -m pytest tests/test_m6_fixture_pack.py -v
```

> ⚠️ **不要用 `--strict` 判验收**：`python scripts/check_contracts.py --strict` 当前 **exit 1**，原因是 D-008 刻意接受的 2 条 W2（`ingest_recognized`、`save_costing_snapshot` 属「写库但无门」），为已裁定项而非缺陷。验收只认默认模式。

## 附录 B：T-xx → 可执行用例映射

| T-xx | 覆盖用例（`tests/…`） |
|---|---|
| T-01 | `test_m6_fixture_pack.py::test_fixture_pack_has_provenance_on_every_record`；回读见 `..._cost_is_deterministic`（`snapshot.evidence.caller_evidence.fixture_id`） |
| T-02 | 人工：`grep source_ref tests/fixtures/m6/fixture_pack.json`（全为 `fixture:` 前缀）；辅助 `test_fixture_pack_has_provenance_on_every_record` |
| T-03 | 人工：`tests/fixtures/m6/fixture_pack.json` 的 `expected` 字段 ↔ 本文档 §二 逐条核对 |
| T-10 / T-11 | `test_m6_cost.py`（材料、工序、损耗用例）；`test_m6_costing_tools.py::test_resolve_prefers_stock_price_when_stock_known` |
| T-12 | `test_m6_cost.py`（报价加价率用例） |
| T-13 | `test_m6_cost_tools.py::test_audit_incomplete_never_looks_profitable`、`..._audit_flags_loss_and_low_profit` |
| T-14 | `test_m6_cost.py::test_statement_balances_opening_plus_inflow_minus_outflow`；`test_m6_fixture_pack.py::test_fixture_expense_asset_statement_and_inventory_values` |
| T-15 | `test_m6_fixture_pack.py::test_fixture_expense_asset_statement_and_inventory_values`；`test_m6_assets.py`（零基准缺失） |
| T-16 | `test_m6_fixture_pack.py::test_fixture_monthly_pay_values`、`..._distinguishes_partial_missing_rate`、`..._empty_payroll_is_explicitly_absent` |
| T-17 | `test_m6_fixture_pack.py::test_fixture_expense_asset_statement_and_inventory_values`；`test_m6_inventory_view.py`（四态 + 态未知） |
| T-20 | `test_m6_store.py::test_trial_snapshot_not_counted_in_month_summary`；`test_m6_costing_tools.py::test_save_writes_trial_snapshot_with_priced_lines` |
| T-21 | `test_m6_costing_tools.py::test_save_missing_required_inputs_is_reported_not_fabricated`；Registry 层必填校验路径见 `test_m6_fixture_pack.py` 的 `registry.call()` 调用 |
| T-22 | `test_m6_costing_tools.py::test_save_derives_snapshot_id_and_reports_duplicate` |
| T-23 | `test_m6_costing_tools.py::test_save_rejected_after_month_close`；`test_m6_store.py::test_closed_month_blocks_new_writes_and_confirms` |
| T-24 | `test_m6_costing_tools.py::test_confirm_is_idempotent_and_explicit_about_missing`、`..._confirm_rejected_when_month_closed`；`test_m6_store.py::test_confirm_is_idempotent_and_not_found_is_explicit` |
| T-25 | `test_m6_cost_tools.py::test_cost_read_tools_are_ungated_and_declared_readonly`、`..._read_tool_completes_in_graph_without_gate` |
| T-26 | `test_m6_costing_tools.py::test_m6_contracts_and_rules_agree`、`..._finance_gate_rules_never_auto_approve` |
| T-30 | `test_m6_costing_tools.py::test_trial_snapshot_is_intentionally_ungated`；`test_m6_skills.py::test_ledger_without_operation_only_reads` |
| T-31 | `test_m6_finance_gate.py::test_save_trial_then_confirm_through_finance_gate`（含 `allowed_roles == ["finance-officer","admin"]`） |
| T-32 | 同 T-31（`status=confirmed` + `committed_by=finance_gate` + `result == data`） |
| T-33 | `test_m6_finance_gate.py::test_reject_path_leaves_no_confirmed_row` |
| T-34 | `test_m6_finance_gate.py::test_finance_gate_rejects_non_finance_roles`；`test_m6_store.py::test_finance_gate_authorizes_only_finance_roles` |
| T-35 | `test_m6_finance_gate.py::test_close_month_freezes_reviewed_totals`；`test_m6_costing_tools.py::test_close_month_reports_reviewed_totals_without_freezing`；`test_m6_store.py::test_close_month_twice_is_rejected` |
| T-36 | `test_m6_finance_gate.py::test_confirm_conflict_does_not_raise_and_writes_nothing` |
| T-37 | `test_m6_finance_gate.py::test_document_commit_hook_confirms_saved_document`；`test_m6_documents.py`、`test_m6_assets.py`（propose 不改写已确认原值） |
| T-40 | `test_m6_fixture_pack.py::test_fixture_missing_quantity_keeps_unit_cost_and_null_total` |
| T-41 | `test_m6_fixture_pack.py::test_fixture_empty_payroll_is_explicitly_absent`；`test_m6_payroll.py::test_piece_pay_without_rates_returns_nothing_but_missing` |
| T-42 | `test_m6_fixture_pack.py::test_fixture_monthly_pay_values`、`..._distinguishes_partial_missing_rate` |
| T-43 | `test_identity_placeholder.py::test_finance_officer_seed_resolves_finance_permissions`、`..._permission_catalog_matches_gate_semantics`、`..._default_role_seeds_ten_roles`；`test_m6_store.py::test_finance_gate_type_registered_with_roles_and_decisions` |
| T-44（本轮新增项） | `test_m6_finance_gate.py` 的 `result == data` 两条断言（costing / close_month） |
| T-50 | `test_m6_store.py::test_store_path_resolution_order`；各用例的 `m6_db` fixture（`tmp_path` + `YUNPAI_M6_DB`） |
| T-51 | ⚠️ **无覆盖用例**（见附录 C） |
| T-52 | `test_m6_skills.py::test_skill_refuses_any_tool_outside_its_map`、`..._unknown_operation_is_refused` |
| T-53 | `test_m6_costing_tools.py::test_resolve_reports_missing_instead_of_inventing_price`；`test_m6_cost_tools.py::test_product_cost_reports_missing_without_inventing` |
| T-60 | `tests/test_api_runs_m6.py::test_api_create_run_exposes_finance_gate` |
| T-61 | `tests/test_api_runs_m6.py::test_api_resume_approve_persists_confirmed_snapshot` |
| T-62 | `tests/test_api_runs_m6.py::test_api_resume_reject_keeps_trial_snapshot` |
| T-63 | `tests/test_api_runs_m6.py::test_api_resume_operator_is_rejected_without_bypassing_gate` |
| T-64 | `tests/test_api_runs_m6.py::test_api_resume_accepts_trusted_role_headers` |
| T-65 / T-66 | `tests/test_api_runs_m6.py::test_api_public_state_contains_gate_outputs_and_redacts_content` |

## 附录 C：已知覆盖缺口（执行时须补测或明确挂起）

| 项 | 缺口 | 处置建议 |
|---|---|---|
| T-51 租户隔离 | **M6 无用例**：`tests/test_m6_store.py` 全文不含 `tenant_id`；M6 测试中唯一非 default 租户是 `test_m6_fixture_pack.py:42` 的 `fixture-tenant`，仅用于回读自身快照，**没有跨租户不可见断言** | 补一条：租户 A 落 trial 快照后，以租户 B 读 `list_snapshots`/`get_snapshot` 应取不到；或本轮明确挂起并记入遗留 |
| T-43 运行时授权 | 身份层已能解析 `finance.approve`/`cost.view`，但 Graph 的门判定取 `resume` 载荷中的 `roles`（`reviewer/gates.py` 的 `authorize()`），**不等于 API 已强制校验身份** | 按 D-015 已记为「登记不等于运行时授权」；验收时不得据此声明 API 已强制 |
| `-k m6` 覆盖面 | `-k` 按文件名/用例名匹配，`test_identity_placeholder.py` 等不含 `m6` 关键字的相关用例**不会被选中** | T-43 须单独跑附录 A 的第 4 条命令 |
| 工资“集在位但角色集空” | `compute_piece_pay` 在 `report_events` 有值而 `piece_rates` 为空时走**事实缺失**分支（`facts_present=false`），而非「逐行缺口」；该用例原断言 `cost_incomplete is True`，本轮改为 `False` | 属超出「输入为空」字面的语义扩展；无 fail-open（都不产数字），但执行时须确认此定性符合业务预期并签核 |
