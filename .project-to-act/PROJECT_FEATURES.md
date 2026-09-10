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
| F-007 | 基础资料上传接线（BOM/SOP 先行落 canonical） | docs/01 §12、docs/02 §4.1/§6.1 | P0 | 已完成 | 验收通过（E-007） | F-004 | 面板形状（message+documents，全 `kind=master_data`）→ `route=free`+`business-data-identification` → candidate 门批准 `published 72`（`bom W-H913`/`document W-H913-sop`）→ 再传订单 M2 **不再开 data 门**（bom 248 行/SOP 15 工序） | docs/02 §4.1 路由、§6.1 审查规则 | E-007 |

## 功能变更历史

| 日期 | 功能 ID | 变化摘要 | 原因与影响 | 证据 ID | 确认来源 |
|---|---|---|---|---|---|
| 2026-09-09 | F-001 | 立项：两本方案书撰写 | 编排重构的书面依据，审核后成为代码阶段唯一事实源 | 无 | 用户会话 |
| 2026-09-09 | F-001 | 扩充为三本：新增书三《功能需求对齐手册》（docs/03），任务表 xlsx 改号 04；书二保留不动 | 用户反馈：书二偏架构视角，需一本"每个功能是什么/为了什么/与需求对齐"的手册，须多参照需求文档 | 无 | 用户会话 |
| 2026-09-09 | F-004 | 6 分片（M0–M5）全合入集成分支：119 工具全部登记，`handlers 85→114`、`bound_local 30→100`、`unbound 35→6`、`visible 84→109`、`rules 5→51`；Gate 覆盖核对 44 项需补 → 已覆盖 42 / 缺口 1（`data_import_commit` 合同门漂移） | 迁移主体完成；缺口与 R8 已知缺口（批准前已写）按「先改书再改码」登记 | E-004 | 集成收口（INT2） |
| 2026-09-09 | F-006 | free 路径实测跑通旧死点 `ingest_m5_planning_snapshot`（completed / `snapshots_stored`）；workflow 路径实测至 M5 solve，未达 `m5.released`，4 类卡点逐条留证 | 联调验收部分完成，卡点见 `_migration/REPORT-MIG-INTEGRATION.md` §4 | E-004 | 集成收口（INT2） |
| 2026-09-09 | F-006 | **验收通过**：K1–K6 修复后 workflow `run-b84071dc…` 与 free `run-3f6b9383…`（LLM 路由，confidence 0.95）均 completed + `m5.released` + `is_current_head=true`，5 工序 / makespan 3220 / processing 1420 分钟，判定器 PASS 5/5 | 联调验收完成（B-003..B-007 已修复）；B-001 仍待「先改书再改码」 | E-006 | W913 打通（INT2 第四轮） |
| 2026-09-09 | F-007 | 立项并完成：基础资料上传接线（G1 路由规则 0 + 矛盾 LLM 提案否决 + prompt 判据互斥 + `_validate` 保留 skills；G2 `skill_payload` 顶层键并入；G3 技能 candidate 门 + 批准发布 canonical；G4 `needs_product_code` fail-closed；R2 读口补业务体 attributes 归一） | 前端「基础资料识别落库」面板原始形状此前走不通：路由被判订单链、技能拿不到产品编码、72 条候选静默丢弃、缺编码静默空批次 | E-007 | 基础资料先行打通（INT2 第五轮） |
