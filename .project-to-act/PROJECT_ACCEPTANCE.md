# 项目验收

> 当前验收与有效证据的紧凑视图。原始输出和完整报告保存在外部制品位置并以路径和哈希引用。

## 当前验收结论

- 结论：V2 工具迁移 6 分片（M0–M5）已全合入集成分支并全绿（E-004）；F-004 完成、F-006 完成（E-006）；Gate 覆盖缺口 0（E-005，R21 收口）；**M6 财务（F-008）B0a/B0b/R0/R1a + B1（三批）+ B2 + B3 + B4 通过（E-008：实体收口 + 内核搬运 + 成本账 6 工具与三段式 commit 钩子 + 3 件内核读工具 + 单据台账 6 件 + 凭据 2 件 + 资产台账 3 件）**
- 验收范围：V2-M3 注册批次（119 工具）+ 集成收口 + W913 双路径联调 + **M6 财务（B0a/B0b/R0/R1a + B1 三批 + B2 + B3 + B4，工具面 143）**
- 最后检查：2026-09-14
- 遗留问题：**B-001 已裁定（D-005，三段式先批准后落库；存量写工具转 B-014）**；B-003..B-007 已修复（第四轮 K1–K6）；B-013（R0）已决策并执行首批；M6 余批 B5–B7 待做（B1–B4 已完成）

## 验收标准

| 标准 ID | 标准 | 状态 | 验证方法摘要 | 证据 ID |
|---|---|---|---|---|
| A-001 | 方案书阶段：书一/书二/书三经用户逐卡/逐章审核通过并 commit 入库 | 通过 | 用户会话确认 + git 提交记录 | E-001 |
| A-002 | 骨架阶段：LangGraph 唯一执行路径可用，API 契约兼容冒烟通过 | 基本通过 | pytest 全绿+API 冒烟（E-002）；逐字段流式契约快照留 M5 录制比对 | E-002 |
| A-003 | 注册阶段：119 工具+8 技能台账登记完整，五项 checklist 全过 | 通过 | 全量 pytest 643 passed/4 skipped + check_contracts exit 0 + Gate 覆盖核对（44 需补→43 覆盖/0 缺口/1 有意不加） | E-004/E-005 |
| A-004 | 联调阶段：W913 workflow 路径 m5.released 且 free 路径全链跑通 | 通过 | workflow `run-b84071dc…`（route.source=explicit）与 free `run-3f6b9383…`（route.source=llm）均 completed + `released` + `is_current_head=true`（判定器 PASS 5/5）；free 旧死点 `ingest_m5_planning_snapshot` completed | E-006 |
| A-005 | 自进化阶段：四接缝（观察/注入消费/使用反馈/红线周期）测试全绿 | 待检查 | pytest 分项 + 演示证据 | 无 |

## 证据索引

| 证据 ID | 时间 | 方法摘要 | 退出状态 | 版本或文件哈希 | 结果摘要 | 证据位置 | 有效期 |
|---|---|---|---|---|---|---|---|
| E-001 | 2026-09-09 | 用户会话确认三本方案书并批准按文档完整开发（/goal） | 通过 | commit 841447a（三书齐） | 三本书冻结 v1.0，成为代码阶段唯一设计依据 | 本仓库 git 历史 | 长期 |
| E-002 | 2026-09-09 | pytest -q → 376 passed/4 skipped；API 冒烟：POST /runs=200(chat completed)、GET /runs/{id}=completed、resume 无 Gate=409、NDJSON 流式 run_start→assistant_delta→state_snapshot→run_done、/tools /skills /health=200 | 通过 | 分支 feat/orchestrator-skeleton-20260909 | V2-M1/M2 代码验收；流式逐字段契约快照与 W913 全链留 M5 | 本仓库 git 历史 | 长期 |
| E-003 | 2026-09-09 | V2-M3 开工门基线复验：.venv(Py3.12.10) pytest -q → 376 passed/4 skipped(120.7s) 对齐 E-002；注册目录对账 119 specs=115 manifest+4 local、四态 30/53/1/35、CatalogView=84 无 UNBOUND/DEPRECATED 泄漏、production 降级 OK、8 技能一致；路由专项单测 18/18；活体冒烟 9 用例×2 配置（确定性回退 + DeepSeek 真实 LLM 注入 QwenRouter 同码路径，8027 临时实例）8/9：chat/free/显式一票否决/确定性回退 1-2/Gate 挂起/NDJSON 流全通，LLM 提案层经真实模型走通（DeepSeek 1-3s、本地 Qwen qwen3.8-27b 49-76s），_validate 护栏对不可见工具/未知技能有效 | 通过（开工门开启；4 项发现转 R1 处理清单） | 分支 feat/orchestrator-skeleton-20260909（账本行未提交，随 R1 首 PR 入库） | 发现：① /tools 端点暴露全量 119 含 5 个红线名且 run_mrp_procurement_plan 标 bound=true 与绑定表 UNBOUND 矛盾；② 显式非法参数（未知 workflow/不可见工具）返回 HTTP 500 而非 4xx 结构化错误；③ LLM workflow 提案闭环缺失——system prompt 未注入 KNOWN_WORKFLOWS/未要求 workflow_id，_validate 对 route=workflow 必拒，实际仅靠附件确定性规则兜底；④ 本地 Qwen 实测延迟 49-76s 超默认 QWEN_TIMEOUT_S=45s，且 classify 空 key 即 not_configured（本地模型需占位 key，guide_chat 有占位而 classify 无）。另：默认 python=3.10 与 .venv 并存，误用致 6 failed（缺 pypdf+langgraph 版本差异），基线解释器=.venv | 会话记录+本表（临时脚本在 %TEMP%\yunpai-verify） | 至 R1 收口 |
| E-004 | 2026-09-09 | pytest 642 passed/4 skipped(exit0)；check_contracts exit0；handlers114/bound_local100/unbound6/visible109/rules51；Gate 44→42/缺口1；W913 free 通过、workflow 未达 released | 通过（F-004 完成/F-006 部分） | @a75fab3 | REPORT-MIG-INTEGRATION.md | 长期 |
| E-005 | 2026-09-09 | R21 收口 Gate 缺口：m0.json:150 review_gate candidate→data（gate_type_for→blocked_input）+ 断言 tests/test_migration_m0.py:466-507；pytest 643 passed/4 skipped(exit0)；check_contracts exit0（--strict 无新增 W） | 通过（44 需补→43 覆盖/0 缺口/1 有意不加） | @f44fbf7 | GATE-COVERAGE-INT2.md §3.3 | 长期 |
| E-006 | 2026-09-09 | W913 双路径实跑（v2_w913.py）：workflow run-b84071dc…/free run-3f6b9383…（source=llm）均 completed+released+is_current_head true；pytest 659 passed/4 skipped(exit0)；check_contracts exit0 | 通过（F-006 完成） | @1175bfe | REPORT-MIG-INTEGRATION.md §11 | 长期 |
| E-008 | 2026-09-13 |**M6 增量（B0a + B0b + R0 + R1a + B1-1 + B1-2 + B1-3 + B2 + B3 + B4）**：CANONICAL_SCHEMA 收口 `expense`/`delivery_note`/`stock_class`(四态)+`workshop`+`amount`；`m6_defaults.py`(83)/`m6_cost.py`(601)/`m6_price_source.py`(含 D5 `resolve_material_prices`)/`m6_store.py`/`m6_tools.py` 落位；`registry-manifests/m6.json` 20 契约（成本账 9 + 单据 6 + 凭据 2 + 资产 3）+ `finance` 门型入 gates.py + `_REVIEW_GATE_MAP` 补 `finance`；B1 第三批抽 `_cost_breakdown` 共用预算（save 与 get_product_cost 同口径，测试断言单位成本相等）+ `_assemble_costing_facts` 共用装配；`graph.py` 三个 `_apply_m6_*` commit 钩子（approve 分支分派、冲突不抛异常）+ `state.review_applied`；`orchestration_bridge` 装配接线（含 R1a `read_m4_tracking_price_facts` 读 M4B 追踪价源）；`ENTITY_TYPES`+2、`FACADE_KINDS`+2、`list_expenses`/`list_delivery_notes`（并修掉信封未归一导致恒空的 bug）、m0.json+4 契约、RULES 门+2；新增 `test_m6_cost`(15)/`test_m6_expense`(9)/`test_m6_entities`(6)/`test_m6_price_source`(6)/`test_m6_store`(14)/`test_m6_costing_tools`(21)/`test_m6_finance_gate`(7)/`test_m6_entities_roundtrip`(3)，`test_m6_cost_tools`(14)/`test_m6_documents`(13)/`test_m6_delivery`(7)/`test_m6_assets`(7)，`test_binding` 计数锁 119→123→129→132→138→140→143。pytest **784 passed/2 skipped**（基线 661/2）；check_contracts exit 0（工具 119→143、handler 100→124）—— 三段式 5 条验收断言（说明一 §1.7）全过：trial 不进汇总 / finance 门 interrupt→approve 才翻 confirmed / reject 不产生 confirmed 行 / 越权角色被拦且不抛异常 / 无自动放行 |通过（增量；F-008 进行中） | 分支 `feat/m6-finance-20260913`（提交 1d796ce / ed4d32c / 5531c08 + 本批）；范围与口径见 `M6-三项决策说明-20260913.md`、D-008 见 `PROJECT_OVERVIEW.md` | B-013 已决策并执行首批；B-014 待排期；B5–B7 待做（B1–B4 已完成）；`stock_class` 枚举值来源为空（数据侧）；币种/含税未换算；老仓 7 个工具级 registry 用例随其工具批次移植 | 至 M6 全批次完成 |

## Gate 记录

| Gate ID | 日期 | Gate | 对象 | 结果 | 证据 ID | 豁免与确认人 |
|---|---|---|---|---|---|---|

## 验收记录

| 日期 | 检查范围 | 证据 ID | 结果 | 遗留问题 | 结论 |
|---|---|---|---|---|---|
| 2026-09-09 | V2 工具迁移集成收口（6 分片 + fact_gateway + W913 双路径 + Gate 覆盖 + 账本） | E-004 | 通过（642 passed/4 skipped；check_contracts exit 0） | workflow 路径未达 m5.released（报告 §4）；B-001/B-002 待「先改书再改码」 | F-004 完成、F-006 部分完成 |
| 2026-09-09 | Gate 覆盖缺口微收口（INT2 第三轮 · R21：`data_import_commit` 声明对齐 + 断言） | E-005 | 通过（643 passed/4 skipped；check_contracts exit 0；--strict 无新增 W） | workflow 路径未达 m5.released（K1–K5）；B-001 批准前已写 | Gate 缺口 0（43 覆盖 + 1 有意不加 = 44）；B-002 已修复 |
| 2026-09-09 | W913 workflow 路径打通（INT2 第四轮 · K1–K6：M0 前向装配 / 门补数可见 / 工序形状 / 降级日历 / 降级资源） | E-006 | 通过（659 passed/4 skipped；check_contracts exit 0；双路径 completed+released，判定器 PASS 5/5） | B-001 批准前已写（先改书再改码）；K6 实跑路径工位来自 supplement，由严格校验单测锁定 | F-006 完成；B-003..B-007 已修复 |
| 2026-09-13 | M6 财务第一批增量（B0a 实体收口 + R0 内核搬运；F-008） | E-008 | 通过（685 passed/2 skipped，基线 661/2 零回归） | B-013 已决策并执行首批；B-014 待排期；`stock_class` 四态待 B0b 裁定 | F-008 进行中；D-005/D-006/D-007 已裁定，书二 §6.2.1 已补 |
| 2026-09-13 | M6 财务 B1 第二批（成本账 6 工具 + 三段式 commit 钩子 + R1a 装配接线；F-008） | E-008 | 通过（743 passed/2 skipped，基线 712/2 零回归；三段式 5 条断言全过；check_contracts exit 0） | B1 其余内核件与 B2 待做；`stock_class` 值来源为空 | F-008 进行中；D-008 新增（试算快照刻意无门，接受一条 W2） |
| 2026-09-14 | M6 财务 B1 第三批（3 件内核读工具 = B1 全批收口；F-008） | E-008 | 通过（757 passed/2 skipped，基线 743/2 零回归；同口径断言「预览单位成本 == 快照单位成本」；读工具图内不开门、库内容逐条不变；check_contracts exit 0） | B2–B7 待做；`stock_class` 值来源为空；币种/含税未换算 | F-008 进行中；B1 全批收口 |
| 2026-09-14 | M6 财务 B2（单据台账 6 件：报价单 + 对账单；F-008） | E-008 | 通过（770 passed/2 skipped，基线 757/2 零回归；propose→finance 门→commit e2e 与 reject 不生效双双断言；缺成本不报价；check_contracts exit 0） | B3–B7 待做；对账明细暂须显式给 transactions/lines（B3 由送货单/采购单生成） | F-008 进行中；`_apply_m6_document_commit` 已接入真实工具 |
| 2026-09-14 | M6 财务 B3（凭据 2 件：`get_delivery_note` + `generate_statement`；F-008） | E-008 | 通过（777 passed/2 skipped，基线 770/2 零回归；真 canonical 往返锁「拆信封」；依据缺失拒出单、不误用单价；`save_statement` 可自动取依据且仍走三段式；check_contracts exit 0） | B4–B7 待做；供应商侧对账按 supplier 名/编码匹配 | F-008 进行中；对账明细已可从送货单/入库事实生成 |
| 2026-09-14 | M6 财务 B4（资产台账 3 件：`get_asset_ledger` / `upsert_asset_ledger` / `compute_asset_benefit`；F-008） | E-008 | 通过（784 passed/2 skipped，基线 777/2 零回归；修订模型「propose 不改写已确认原值」+ reject 不生效 + 分摊只用 effective 原值均断言；check_contracts exit 0） | B5–B7 待做；`stock_class` 数据侧仍空；币种/含税未换算 | F-008 进行中；第四个 commit 钩子 `_apply_m6_asset_commit` 已接入 |
