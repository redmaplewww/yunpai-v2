# 项目功能

> 功能范围与当前状态的唯一清单。API、数据模型和函数级设计放在项目设计文档中并从此处引用。

## 状态定义

- 候选：尚未批准进入范围
- 已规划：已确认但未开始
- 进行中：正在实现
- 已阻塞：等待外部条件
- 已完成：完成条件满足且有证据
- 已取消：退出范围并保留原因

## 功能清单

| 功能 ID | 功能 | 来源引用 | 优先级 | 状态 | 验收状态 | 依赖 | 完成条件 | 设计引用 | 证据 ID |
|---|---|---|---|---|---|---|---|---|---|
| F-001 | 编排重构方案书（三本） | 用户会话 2026-09-09 | P0 | 已完成 | 已验收（用户确认） | - | 书一/书二/书三完成，用户审核通过并 commit 入库 | docs/01、docs/02、docs/03 | E-001 |
| F-002 | 编排骨架搭建 | docs/01 §13 | P0 | 已完成 | 代码验收（E-002） | F-001 | LangGraph 图跑通 chat/free/Gate interrupt-resume；API 冒烟；checkpointer 可用 | docs/02 §1-§3、§8 | E-002 |
| F-003 | 可复用资产迁移 | docs/01 §11 | P0 | 已完成 | 代码验收（E-002） | F-002 | 合同/注册中心/存储/识别/LLM/进化包+运行库迁入，随迁测试全绿 | docs/02 §9-§11 | E-002 |
| F-004 | 工具与技能逐个审查注册 | docs/01 §12 | P0 | 已完成 | 代码验收（E-004） | F-003 | 119 工具（115 manifest+4 本地）+8 技能台账全登记，五项 checklist 逐个过审 | docs/02 §7 | E-004 |
| F-005 | 自进化接线与三缺口补全 | docs/01 §10 | P1 | 已规划 | - | F-004 | 观察/注入消费/使用反馈/红线周期四接缝测试全绿 | docs/02 §11 | - |
| F-006 | 联调验收（W913 双路径） | docs/01 §13 | P0 | 已完成 | 验收通过（E-006） | F-005 | workflow `run-b84071dc…`（explicit）与 free `run-3f6b9383…`（route.source=llm）均 completed + `m5.released` + `is_current_head=true`，判定器 PASS 5/5（K1–K6 修复后打通） | docs/02 §13 | E-006 |
| F-008 | M6 财务：成本核算链 + 成本明细账 | M6-开发计划-v2口径-20260913.md §0 | P0 | 进行中 | 增量验收（E-008） | F-004 | 成本链可按 D5/D6 口径算出并落快照；三段式（先批准后落库）生效；试算/正式分离；28 件工具注册（已 19/28：B0b 4 + B1 9 + B2 6） | M6-开发计划 §3–§7 | E-008 |

## 功能变更历史

| 日期 | 功能 ID | 变化摘要 | 原因与影响 | 证据 ID | 确认来源 |
|---|---|---|---|---|---|
| 2026-09-09 | F-001 | 立项：两本方案书撰写 | 编排重构的书面依据，审核后成为代码阶段唯一事实源 | 无 | 用户会话 |
| 2026-09-09 | F-001 | 扩充为三本：新增书三《功能需求对齐手册》（docs/03），任务表 xlsx 改号 04；书二保留不动 | 用户反馈：书二偏架构视角，需一本"每个功能是什么/为了什么/与需求对齐"的手册，须多参照需求文档 | 无 | 用户会话 |
| 2026-09-09 | F-004 | 6 分片（M0–M5）全合入集成分支：119 工具全部登记，`handlers 85→114`、`bound_local 30→100`、`unbound 35→6`、`visible 84→109`、`rules 5→51`；Gate 覆盖核对 44 项需补 → 已覆盖 42 / 缺口 1（`data_import_commit` 合同门漂移） | 迁移主体完成；缺口与 R8 已知缺口（批准前已写）按「先改书再改码」登记 | E-004 | 集成收口（INT2） |
| 2026-09-09 | F-006 | free 路径实测跑通旧死点 `ingest_m5_planning_snapshot`（completed / `snapshots_stored`）；workflow 路径实测至 M5 solve，未达 `m5.released`，4 类卡点逐条留证 | 联调验收部分完成，卡点见 `_migration/REPORT-MIG-INTEGRATION.md` §4 | E-004 | 集成收口（INT2） |
| 2026-09-09 | F-006 | **验收通过**：K1–K6 修复后 workflow `run-b84071dc…` 与 free `run-3f6b9383…`（LLM 路由，confidence 0.95）均 completed + `m5.released` + `is_current_head=true`，5 工序 / makespan 3220 / processing 1420 分钟，判定器 PASS 5/5 | 联调验收完成（B-003..B-007 已修复）；B-001 仍待「先改书再改码」 | E-006 | W913 打通（INT2 第四轮） |
| 2026-09-13 | F-008 | 立项：M6 财务（成本核算链 + 成本明细账）进入 v2 范围（分支 `feat/m6-finance-20260913`）。**口径变更**：`PROJECT_OVERVIEW` 原"非目标"含"不做 M6 财务…（其工具合入 main 后走注册流程进 v2）"——现改为**在 v2 直接开发**，须记 D 行 | 用户会话说"直接基于 V2 main 基线进行开发 M6 模块"；原计划（`M6-开发计划-20260910.md`）是照老仓 39092 写的，v2 实测其"已有 8 件工具 + expense/delivery_note 实体"**一件都没有**，故重出 v2 口径计划 | 无 | 用户会话（2026-09-13） |
| 2026-09-13 | F-008 | 第一批增量：**B0a** `CANONICAL_SCHEMA` 收口 `expense`/`delivery_note`（老仓留在 facade 层 → 源分裂修复）+ `workshop` 接口预留（tooling/equipment）；**R0** 搬入 `m6_defaults.py`（83 行口径层）+ `m6_cost.py`（601 行算法内核） | 识别链原会拒收 expense/delivery_note；费率/损耗/库存成本价在 v2 无 canonical 来源（R0） | E-008 | 用户会话裁定 D-005/D-006/D-007 |
| 2026-09-13 | F-008 | 第二批增量：**B0b** `expense`/`delivery_note` 实体接入面（`ENTITY_TYPES`/`FACADE_KINDS`/`list_expenses`/`list_delivery_notes`/`stock_class` 四态）；**R1a** 缺料价源解析器（读口订正为 M4B 采购追踪行的 `unit_price`，采购单不含价）；**B1-1** `m6_store` 存储层 + `finance` 门型；**B1-2** 成本账 6 工具（`registry-manifests/m6.json`）+ `graph.py` 三个 `_apply_m6_*` commit 钩子 + R1a 装配接线；工具面 119→**129** | 三段式（D-005）从条文落成可执行实现：试算只写 `trial` 不进汇总、`finance` 门批准后由 commit 钩子翻 `confirmed`、`reject` 不产生生效行（5 条断言全过）；「试算快照无门」以显式 `review_gate="none"` + 一条 W2 表达（**D-008**）；顺带修掉 B0b 读口信封未归一导致 `list_expenses` 恒空的 bug | E-008 | 用户会话裁定 D-008（2026-09-13） |
| 2026-09-14 | F-008 | 第三批增量（**B1 全批收口**）：`get_product_cost` / `audit_order_cost` / `allocate_expenses` 三件纯算数读工具（工具面 129→**132**），抽 `_cost_breakdown` 共用预算 + `_assemble_costing_facts` 共用装配 | 当场算（D8 试算/报价预览）与试算快照必须同口径——抽共用函数并由测试断言单位成本相等，防"预览与落库漂移"；`audit_order_cost` 的"算不全"强制降级 `cost_incomplete`（绝不当盈利）；三件只读不落库、无门，缺数一律以 `cost_incomplete` 表达 | E-008 | Agent（按 D-005/D-006 口径实现） |
| 2026-09-14 | F-008 | 第四批增量（**B2 单据台账**）：`generate_quotation` / `save_quotation` / `list_quotations` / `get_quotation` + `save_statement` / `list_statements`（工具面 132→**138**） | 单据类写走三段式（propose 落 `trial` 草稿 → `finance` 门 → `_apply_m6_document_commit` 翻 `confirmed`），reject 保留草稿但不生效；**缺成本不报价**（行内三项成本未给且滚不出来时该行进 missing 且不计入合计，堵掉老仓"缺成本当 0 报出成本为零的价"）；同号单据显式拒绝、单号确定性派生 | E-008 | Agent（按 D-005 三段式实现） |
