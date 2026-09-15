# M6 文件规范收敛任务记录

- 任务：M6 Tool 契约与财务边界收敛
- 基线：`feat/m6-finance-20260913` / `7790e4a`
- 依据：FactoryBrain Tool / Workflow / Skill 命名与结构规范 V1.1；并行开发接入规范 V1.2；用户 2026-09-15 授权
- 目标：在不假设 main 未冻结架构的前提下，修复 M6 已确认的 fail-open，并逐步补齐统一 Tool 返回契约与上下文传递。
- 非目标：本任务不迁移全局 Data Manager、Domain Service 或 Repository 架构；不改现有 Tool ID、HTTP 路径和数据库表。

## 不变量

1. 显式对账交易缺少方向或金额时不得生成成功结果。
2. 资产分摊基准缺失或为零时必须显式标记 `cost_incomplete`。
3. M6 自定义数据库路径必须随运行上下文透传；未提供时不改变通用上下文契约。
4. v2 旧 `success/data/errors` 兼容字段保留，同时提供规范要求的 `result/error/business_status`。

## 验收

- M6 聚焦测试全绿。
- 契约加载、去重和绑定检查通过；标准检查器常规模式 exit 0。`--strict` 会把 D-008 明确接受的 `save_costing_snapshot` 无门 W2 作为失败，不能作为本轮通过条件。
- `git diff --check` 通过，工作树差异仅限本任务文件。
- 现有 M6 Tool ID、API 路径和三段式状态流转保持兼容。

## 当前范围

已实施：对账金额 fail-closed、资产零基准 fail-closed、M6 路径上下文透传、统一返回字段、对应回归测试。

后续独立任务：按冻结架构拆分 Tool 文件、Data Manager / Domain Service / Repository、M6 Workflow 和 Skill 文件命名迁移。
