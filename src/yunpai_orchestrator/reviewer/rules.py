"""合同驱动审查规则（书二 §6.1）——旧 agents.py:388-449 if-elif 的规则化迁移。

迁移对照（旧分支 → 规则条目）：
- ingest_document: needs_review / overall_confidence<0.8 → gate review（M1 低置信）
- solve_scheduling: lifecycle_status=="draft" → gate apply（M5 发布门）
- replan_m5_schedule: lifecycle_status=="draft" → gate apply（M5 重排候选门）
- dispatch_m5_schedule: status=="pending" → gate authorization（M5 派工外部副作用面）
- prepare_m5_department_message / record_m5_knowledge: success → gate authorization
  （M5 本地写操作；旧实现是执行前授权门，V2 无 preflight → 等价的后置授权门）
- data_import_commit: success==False → fail
- run_bom_sop_workflow: 产出工程草稿 → gate engineering（禁当 retry 用）
- ingest_canonical: 候选落库 → gate candidate
- business-data-identification（技能，字段在顶层）: status=="needs_product_code"
  → gate blocked_input（补 product_code 重试）；m0_candidate_records 非空
  → gate candidate（批准后发布 M0 canonical，G3/G4）
- M3 旧审批四件: 执行成功 → gate authorization（R8 后置等价门；合同 review_gate 同步声明）
- BLOCKED_INPUT 结果 → gate blocked_input（data 补数门）
- M4 写工具（16 条，rows-S5.md §「需补审查」）：缺供应商映射 → gate procurement
  （补数可重跑）；其余写操作 → gate authorization（执行后人工授权，approve 不重跑副作用）
默认规则：manifest spec.review_gate ∈ {candidate,review,engineering,procurement,schedule}
且未授权时 → 对应 Gate（schedule 归一化为 apply）。

> M5 覆盖边界（rows-S6 ⑦）：`ingest_m5_planning_snapshot` 是两条 M5 workflow 的
> 装配步骤，旧实现的前置授权门对 workflow 步骤不生效 → V2 不加新门，改由
> 合同声明 `review_gate="data"` + `BLOCKED_INPUT` 规则兜底（六类快照不齐即开
> data 门）；`get_m5_pmc_progress`/`record_m5_knowledge` 的失败是合同强制的
> 硬失败（见 m5_tools.py 头注），不走门。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

GATE_REVIEW = "review"
GATE_APPLY = "apply"


@dataclass(frozen=True)
class Check:
    field: str                    # 结果内点路径，如 "data.needs_review"；支持 "*" 逐项展开
    op: str                       # eq | ne | lt | gt | truthy | any_falsy
    value: Any = None
    action: str = "pass"          # pass | fail | gate:<type>
    reason: str = ""


def _authorization_gate(reason: str) -> list[Check]:
    """R8 口径：**后置等价门**——写已发生，人工 authorization 授权后流程才继续。

    V2 的门模型是 reviewer 后置驱动（``graph.py:205-209`` 唯一开门点），本轮迁移
    **不恢复执行前门**（需改 planner/executor/checkpointer，属 V2 核心重构）；
    因此写/审批类工具用「执行后 authorization 门 + 合同 ``review_gate`` 声明」双写，
    语义差异登记在 ``_migration/REPORT-MIG-<分片>.md`` 的「已知缺口」一节。
    """
    return [Check("success", "eq", True, action="gate:authorization", reason=reason)]


#: 显式规则表（旧 reviewer 行为全集主干；R2-R5 注册批次逐工具对照补全）。
RULES: dict[str, list[Check]] = {
    "ingest_document": [
        Check("data.needs_review", "truthy", action=f"gate:{GATE_REVIEW}",
              reason="M1 解析低置信/需人工复核（needs_review）"),
        Check("data.overall_confidence", "lt", 0.8, action=f"gate:{GATE_REVIEW}",
              reason="M1 解析整体置信度低于 0.8"),
    ],
    "solve_scheduling": [
        Check("data.lifecycle_status", "eq", "draft", action=f"gate:{GATE_APPLY}",
              reason="M5 排程为 draft，发布须人工 apply Gate（pressure_only 亦禁自动发布）"),
    ],
    "replan_m5_schedule": [
        Check("data.lifecycle_status", "eq", "draft", action=f"gate:{GATE_APPLY}",
              reason="M5 重排产出 draft 候选版本，设置 current/发布须人工 apply Gate"),
    ],
    "dispatch_m5_schedule": [
        Check("data.status", "eq", "pending", action="gate:authorization",
              reason="M5 派工写入 durable pending 记录（外部副作用面），执行前须人工授权"),
    ],
    "prepare_m5_department_message": [
        Check("success", "eq", True, action="gate:authorization",
              reason="M5 部门消息草稿落库须人工授权；审批/发送仍在外部系统，Agent 无审批工具"),
    ],
    "record_m5_knowledge": [
        Check("success", "eq", True, action="gate:authorization",
              reason="M5 知识案例沉淀是本地写操作，须人工授权（旧实现为前置授权门）"),
    ],
    "data_import_commit": [
        Check("success", "eq", False, action="fail", reason="M0 发布失败（fail-closed：回读计数为 0）"),
    ],
    # M0 分片裁决（rows-S1 data_import_commit「审查需决策」二选一）：
    # **保留 fail 终态**（不可恢复的发布失败不得靠重试或补数蒙过），
    # **可恢复的输入缺口**走 evaluate 顶部的 BLOCKED_INPUT 短路 → blocked_input（data Gate：
    # retry+supplement）。两条路径互斥，既不降级也不删除任何门。
    "data_import_run": [
        Check("status", "eq", "failed", action="fail",
              reason="M0 未生成可审核候选（fail-closed：候选为空不得继续下游）"),
        Check("candidates", "truthy", action="gate:candidate",
              reason="M0 候选已登记，必须人工裁决（resolve）后才能 commit 发布 canonical"),
    ],
    "data_import_resolve": [
        Check("status", "eq", "approved", action="gate:candidate",
              reason="候选批准裁决须 M0 candidate Gate 复核（防 LLM 伪造裁决）"),
    ],
    "data_import_rollback": [
        Check("status", "eq", "rolled_back", action="gate:candidate",
              reason="canonical 回滚是破坏性写，须 M0 candidate Gate 复核"),
    ],
    "run_bom_sop_workflow": [
        Check("success", "eq", True, action="gate:engineering",
              reason="M2 产出工程草稿需工程确认（engineering Gate 不得当 retry 使用）"),
    ],
    # ── M2 写工具（rows-S3.md「需补审查」3 条，M2 分片补）──
    # 旧架构对这三个工具开**执行前** authorization 门：它们既不在
    # contracts.POST_REVIEWED_DRAFT_TOOLS，也不在 SAFE_LOCAL_TOOLS，自由模式下必须先授权
    # （legacy/agents.py:382-384 → pre_execution authorization）。V2 新图唯一的开门点是
    # reviewer_check_node（graph.py:205-209）在执行**之后**读本表的 findings，因此这里补
    # 等价的后置 authorization 门：写入 m2_templates/m2_history_lines/m2_runs/制品目录的
    # 工具必须人工授权（operator/admin）才能继续，不得被 LLM 置信度绕过。
    # 前置门本身的缺口是系统性设计问题，已登记 REQUESTS-MIG-M2.md。
    "generate_m2_bom_controlled": [
        Check("standard_bom", "ne", None, action="gate:authorization",
              reason="M2 受控 BOM 写入 m2_history_lines/m2_runs，须人工授权（旧架构为执行前 authorization 门）"),
    ],
    "onboard_m2_bom_template": [
        Check("proposals", "ne", None, action="gate:authorization",
              reason="M2 模板 onboard 写入 m2_templates/m2_history_lines，须人工授权（旧架构为执行前 authorization 门）"),
    ],
    "generate_m2_sop": [
        Check("status", "eq", "generated", action="gate:authorization",
              reason="M2 SOP 制品落盘（docx）+ run/artifact 记录，须人工授权（旧架构为执行前 authorization 门）"),
    ],
    "ingest_canonical": [
        Check("success", "eq", True, action="gate:candidate",
              reason="canonical 候选落库须 M0 candidate Gate 审批后发布"),
    ],
    # ── 基础资料识别技能（G3/G4，INT2 第五轮）────────────────────────────
    # 技能结果不是工具信封（无 ``data`` 层）：``status`` / ``m0_candidate_records``
    # 都在顶层，故 check 字段路径不加 ``data.`` 前缀。
    #   1) 缺产品编码（G4）→ blocked_input（data Gate：retry+supplement 补
    #      product_code 后重试）。fail-closed：不静默 completed、不发布空批次；
    #   2) 有候选记录（G3）→ candidate Gate，批准后由 graph 发布 canonical
    #      （``_publish_business_canonical``），否则 71 条候选静默丢弃。
    # 与 R18 口径一致：主数据/事实审批用 candidate；输入缺口用 blocked_input。
    # 顺序：candidate 在前 = 本技能的**主门**（``gate_type_for`` 取第一个 gate 动作），
    # 两条 check 互斥（``needs_product_code`` 时候选必为空），不影响 evaluate 命中。
    "business-data-identification": [
        Check("m0_candidate_records", "truthy", action="gate:candidate",
              reason="业务资料候选须人工审核后发布 canonical（M0 candidate Gate）"),
        Check("status", "eq", "needs_product_code", action="gate:blocked_input",
              reason="基础资料已识别但缺少产品编码：补 product_code 后重试（不得静默发布空批次）"),
    ],
    # ── M3 旧审批族（R6/R8）：外部写入 → 后置 authorization 门（合同 review_gate 同步声明）──
    "approve_m3_task": _authorization_gate(
        "旧 M3 审批（approve）为外部写入：执行后置 authorization 门，写发生在授权之前（见报告「已知缺口」）"),
    "reject_m3_task": _authorization_gate(
        "旧 M3 审批（reject）为外部写入：执行后置 authorization 门，写发生在授权之前（见报告「已知缺口」）"),
    "request_change_m3_task": _authorization_gate(
        "旧 M3 审批（request_change）为外部写入：执行后置 authorization 门，写发生在授权之前（见报告「已知缺口」）"),
    "approve_to_send_m3_task": _authorization_gate(
        "旧 M3 审批（approve_to_send）为外部写入：执行后置 authorization 门，写发生在授权之前（见报告「已知缺口」）"),
    # ── M4 采购 / 供应商 / 跟踪（rows-S5.md §「需补审查（19，系统性 + 2 个 P0）」）──
    # 系统性缺口：V2 对 M4 无任何 Gate 路径（rows-S5.md:41/89）——写工具必须逐条补门。
    # 门型选择（与 M2/M5 分片同一范式）：
    #   * ``authorization``：写操作执行后需人工授权。``approve`` 决策把步骤直接标
    #     completed（``graph.py:243-250``），**不重跑副作用**，因此不会重复写库；
    #     ``apply_decision`` 把工具并入 ``authorized_steps`` 后不再开门。
    #   * ``procurement``：仅用于「补供应商数据后可重跑」的场景。决策
    #     ``retry``/``supplier_by_material`` 会带 supplement 重装配重试
    #     （``graph.py:256-259``）→ 条件必须在补数后自动消失，否则会反复开门。
    "import_m4_purchase_suggestions_json": [
        Check("data.items.*.supplier_name", "any_falsy", action="gate:procurement",
              reason="采购建议缺少权威供应商映射（缺供应商 → 补供应商后重试；"
                     "与 INT agents.py:478-482 同口径）"),
    ],
    "generate_m4_purchase_orders": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="生成采购单草稿是持久化写操作，必须人工授权"),
    ],
    "submit_m4_purchase_order_review": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="提交采购单人工审核是状态写操作，必须人工授权"),
    ],
    "approve_m4_purchase_order": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="采购单批准（→approved_for_message）是写操作，必须人工授权（P0：V2 原无任何 Gate 路径）"),
    ],
    "request_changes_m4_purchase_order": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="退回修改是状态写操作，必须人工授权"),
    ],
    "generate_m4_purchase_inquiry_message": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="生成供应商询价草稿写 supplier_message 表，必须人工授权后才可对外使用"),
    ],
    "send_m4_purchase_order": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="legacy 出站记录是写操作，必须人工授权（P0-5：远程调用已在 registry 拦截）"),
    ],
    "create_m4_supplier": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="新建供应商主数据是写操作，必须人工授权"),
    ],
    "update_m4_supplier": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="更新供应商主数据是写操作，必须人工授权"),
    ],
    "create_m4_supplier_reply": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="保存供应商回复原文是写操作，必须人工授权"),
    ],
    "parse_m4_supplier_reply": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="解析结果回写 parse_status/parse_provider 是写操作，必须人工授权（不自动写追踪）"),
    ],
    "confirm_m4_supplier_reply": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="人工确认解析结果会写入追踪（正式承诺），必须人工授权"),
    ],
    "confirm_m4_supplier_fact": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="行级供应事实落库是写操作，必须人工授权"),
    ],
    "scan_m4_purchase_alerts": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="预警扫描写预警表并回标超期，必须人工授权"),
    ],
    "generate_m4_urge_message": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="催单草稿覆盖 alert.urge_message 是写操作，必须人工授权"),
    ],
    "query_m4_material_supply_snapshot": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="查询即惰性持久化供应快照（写操作，非只读）；必须人工授权（P0-1："
                     "已从 M4_READ_ONLY_SKILL_OPERATIONS 摘除）"),
    ],
    # ── M1 写类工具（rows-S2「审查需补」4 条）───────────────────────────────
    # 旧系统用 m1_tooling.M1_WRITE_SKILL_OPERATIONS + agents.py preflight 做
    # 「执行前授权门」；V2 只有执行后审查（graph.py:205-209 是唯一开门点），且这
    # 4 个工具在 registry-manifests/m1.json 里没有 review_gate 声明（名字不含
    # approve/commit/dispatch/write/send/publish → registry.py:55-62 判
    # side_effect=none → review_gate=none），实测在 V2 **完全无门**。按父会话 R8
    # 裁决：本轮不恢复执行前门，补后置等价门（authorization）+ manifest 契约声明，
    # 「写发生在批准之前」的语义差异登记在 REPORT-MIG-M1.md「已知缺口」。
    "ingest_m1_archive": [
        Check("status", "ne", "failed", action="gate:authorization",
              reason="M1 归档解包与子任务解析落库（写）须人工授权确认"),
    ],
    "submit_m1_review": [
        Check("status", "eq", "done", action="gate:authorization",
              reason="M1 人工审核通过落库（approve→done，写）须授权留痕，禁止 LLM 置信度放行"),
    ],
    "generate_m1_report": [
        Check("report_status", "truthy", action="gate:authorization",
              reason="M1 识别报告生成与缓存写入（写）须人工授权确认"),
    ],
    "export_m1_order": [
        Check("data.generated", "eq", True, action="gate:authorization",
              reason="M1 订单 Excel 导出落盘（写：文件 + download_url）须人工授权确认"),
    ],
}

#: M0 canonical 写工具（rows-S1「审查需补」）：成功即开 candidate 门。
#: 为什么必须写进 RULES 而不是只改 manifest：V2 的 `reviewer_check_node` 只消费本表，
#: manifest 的 `review_gate` 目前仅被 `scripts/check_contracts.py`（W1/W2）读取
#: ——只声明不登记会变成「有门不生效」。manifest 侧同步声明 side_effect/review_gate
#: 保持契约自检干净（`check_contracts.py --strict`）。
M0_CANDIDATE_GATE_TOOLS: tuple[str, ...] = (
    "data_catalog_ingest_publish",
    "data_catalog_document_candidate_publish",
    "data_catalog_file_publish",
    "m0_products_import", "m0_orders_import", "m0_boms_import", "m0_materials_import",
    "m0_suppliers_import", "m0_equipment_import", "m0_routes_import",
    "m0_operations_import", "m0_tooling_import",
)
for _m0_write_tool in M0_CANDIDATE_GATE_TOOLS:
    RULES.setdefault(_m0_write_tool, [
        Check("success", "eq", True, action="gate:candidate",
              reason="M0 canonical 写入须 candidate Gate 人工审批（data-steward/m0-reviewer/admin）"),
    ])

#: manifest review_gate 值 → Gate 类型归一化（registry._contract_defaults 产生）。
_REVIEW_GATE_MAP = {
    "candidate": "candidate",
    "review": GATE_REVIEW,
    "engineering": "engineering",
    "procurement": "procurement",
    "schedule": GATE_APPLY,       # 求解类合同语义归一到发布门
    "apply": GATE_APPLY,
    "sensitive_data": "sensitive_data",
    "authorization": "authorization",
    "data": "blocked_input",
    "blocked_input": "blocked_input",
    "none": "",
}


def gate_type_for(tool: str, spec: Any | None) -> str:
    if tool in RULES:
        for check in RULES[tool]:
            if check.action.startswith("gate:"):
                return check.action.split(":", 1)[1]
    if spec is None:
        return ""
    return _REVIEW_GATE_MAP.get(str(getattr(spec, "review_gate", "") or ""), "")


def _dig(result: dict[str, Any], path: str) -> Any:
    """点路径取值；``*`` 表示「在列表元素上逐项展开后续字段」。

    例：``data.items.*.supplier_name`` → 每个建议行的 supplier_name 列表。
    未加 ``*`` 的路径行为与迁移前完全一致（逐层 dict 取值）。
    """
    node: Any = result
    for part in path.split("."):
        if part == "*":
            if not isinstance(node, list):
                return None
            continue  # 后续字段逐项映射到列表元素
        if isinstance(node, list):
            node = [item.get(part) if isinstance(item, dict) else None for item in node]
            continue
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _blank(value: Any) -> bool:
    return value in (None, "", [], {})


def _hit(check: Check, result: dict[str, Any]) -> bool:
    actual = _dig(result, check.field)
    if check.op == "eq":
        return actual == check.value
    if check.op == "ne":
        return actual != check.value
    if check.op == "lt":
        try:
            return float(actual) < float(check.value)
        except (TypeError, ValueError):
            return False
    if check.op == "gt":
        try:
            return float(actual) > float(check.value)
        except (TypeError, ValueError):
            return False
    if check.op == "truthy":
        return bool(actual)
    if check.op == "any_falsy":
        # 列表形状结果（如采购建议行）专用：展开后存在空值即命中。
        # 路径取不到值（None）时保守不命中——避免对非目标形状误开门。
        if actual is None:
            return False
        values = actual if isinstance(actual, list) else [actual]
        return any(_blank(value) for value in values)
    return False


def _diagnostics(result: dict[str, Any]) -> dict[str, Any]:
    """从工具结果抽取可诊断信息，随 finding 一起进 Gate（「缺什么、怎么补」）。

    口径（按优先级取第一个非空）：
    - ``code``：errors[0].code → result.code；
    - ``message``：errors[0].message → result.message → ``data.recovery`` →
      ``data.open_customer_questions[].question``（M2 skill 的补数问句）；
    - ``missing_fields``：``data.missing`` → ``data.missing_fields`` →
      ``result.missing_fields`` → ``data.open_customer_questions[].field``。
    """
    errors = result.get("errors") if isinstance(result.get("errors"), list) else []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    questions = data.get("open_customer_questions")
    questions = [q for q in questions if isinstance(q, dict)] if isinstance(questions, list) else []
    missing = data.get("missing") or data.get("missing_fields") or result.get("missing_fields") or []
    if not isinstance(missing, list):
        missing = []
    missing = [item for item in missing if isinstance(item, (str, dict))]
    if not missing:
        missing = [str(q.get("field")) for q in questions if str(q.get("field") or "")]
    message = str(first.get("message") or result.get("message") or data.get("recovery") or "")
    if not message:
        message = "；".join(str(q.get("question") or "") for q in questions if str(q.get("question") or ""))
    return {
        "code": str(first.get("code") or result.get("code") or ""),
        "message": message,
        "missing_fields": missing,
    }


def evaluate(tool: str, result: dict[str, Any], spec: Any | None = None) -> list[dict[str, Any]]:
    """返回按序命中的 findings：[{check, action, gate, reason}]；空列表=通过。

    每条 finding 额外携带 ``code`` / ``message`` / ``missing_fields``（工具结果里的
    可诊断信息），供 graph 在建门时写入 Gate——否则 blocked_input 门只有静态
    ``reason``，前端/驱动无法回答「缺什么、怎么补」（W913 基线实测）。
    """
    findings: list[dict[str, Any]] = []
    diagnostics = _diagnostics(result)
    if str(result.get("code") or "").upper() == "BLOCKED_INPUT":
        findings.append({"check": "blocked_input", "action": "gate:blocked_input",
                         "gate": "blocked_input", "reason": "装配缺口需补数据（data Gate：retry+supplement）",
                         **diagnostics})
        return findings
    for check in RULES.get(tool, []):
        if _hit(check, result):
            action = check.action
            findings.append({
                "check": f"{check.field} {check.op} {check.value}",
                "action": action,
                "gate": action.split(":", 1)[1] if action.startswith("gate:") else "",
                "reason": check.reason,
                **diagnostics,
            })
    return findings


def default_gate_for_authorized(tool: str, spec: Any | None, authorized_steps: list[str]) -> bool:
    """合同默认门：有 review_gate 且该工具步骤已人工授权 → 放行。"""
    gate = gate_type_for(tool, spec)
    return (not gate) or (tool in authorized_steps)


#: 审查永不自动放行人工门（旧审计 P0 教训；test_no_auto_approve 锁定）。
AUTO_APPROVE_ALLOWED = False
