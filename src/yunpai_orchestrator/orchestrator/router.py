"""路由决策器（书二 §4.1）——单一决策链：显式 > LLM 提案 > 最小确定性回退。

与旧实现的本质区别（治痛点 2/3/5）：
1. LLM 的候选目录经绑定状态过滤（CatalogView），不可能提案未绑定/废弃工具；
2. 确定性回退只剩最小规则，旧 INTENT_TO_TOOL 中文关键词大表不迁移；
3. 一次决策一处留痕（route_decision: source/confidence/reason/raw）。

INT2 第五轮（G1）补充：附件**全部** ``kind=master_data``（无订单附件）时，
LLM 的「文档主链」提案不让生效（``_contradicts_master_data``），由确定性规则 0
落到 ``route=free`` + ``business-data-identification``；含订单附件的行为不变。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..binding import CatalogView, visible_tool_names
from ..llm import QwenRouter
from ..workflow_registry import KNOWN_WORKFLOWS
from .workflow_engine import WorkflowEngine

#: 高阶 Skill 使用顺序偏序（legacy agents.py:29-38 原样迁移）。
SKILL_USAGE_ORDER: tuple[str, ...] = (
    "business-data-identification",
    "yunpai-m1-document-parser",
    "yunpai-m0-data-foundation",
    "yunpai-m2-bom-sop",
    "yunpai-m3-material-planning",
    "yunpai-m4-procurement",
    "yunpai-m5-pmc",
    "yunpai-m5-pmc-lifecycle",
)

#: 触发"原始文件→主链"回退的附件特征（最小规则，替代旧关键词大表）。
_BUSINESS_DOC_SNIFFS = ("xlsx", "xls", "pdf", "docx", "zip", "rar", "7z", "csv", "tsv")


@dataclass
class RouteDecision:
    route: str                       # workflow | free | chat
    source: str                      # explicit | llm | deterministic_fallback
    intent: str = ""
    workflow_id: str | None = None
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    answer: str = ""                 # route=chat 时的回答
    raw: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": self.route, "source": self.source, "intent": self.intent,
            "workflow_id": self.workflow_id, "tools": self.tools, "skills": self.skills,
            "confidence": self.confidence, "reason": self.reason, "raw": self.raw,
        }


class Router:
    def __init__(self, llm: QwenRouter, registry, skills, engine: WorkflowEngine | None = None):
        self.llm = llm
        self.registry = registry
        self.skills = skills
        self.engine = engine or WorkflowEngine()

    # ── 决策链入口 ──────────────────────────────────────────────
    async def decide(self, state: dict[str, Any]) -> RouteDecision:
        request = state.get("request", {})
        if d := self._explicit(request):
            return d
        knowledge = state.get("knowledge_context") or []
        if d := await self._llm(request, state, knowledge):
            # G1 深根因（父会话 2026-09-09 补充）：LLM 在「全 master_data 附件」时可能
            # 仍提案订单主链（prompt 曾自相矛盾）。此时不让矛盾提案生效，交给确定性
            # 规则 0 接管（route=free + business-data-identification），而不是把基础
            # 资料当订单送进 M1 解析。
            if not self._contradicts_master_data(d, state):
                return d
        return self._fallback(state)

    def _contradicts_master_data(self, decision: RouteDecision, state: dict[str, Any]) -> bool:
        """LLM 提案文档主链，但附件全是基础资料（无 order）→ 由规则 0 接管。"""
        if decision.route != "workflow" or decision.workflow_id != "m1_m5_document_to_plan":
            return False
        return _all_master_data(state, state.get("attachments") or [],
                                state.get("request") or {})

    # ── 1) 显式参数（一票否决） ─────────────────────────────────
    def _explicit(self, request: dict[str, Any]) -> RouteDecision | None:
        workflow = str(request.get("workflow") or "").strip()
        tools = [str(t) for t in (request.get("tools") or []) if str(t).strip()]
        skills = [str(s) for s in (request.get("skills") or []) if str(s).strip()]
        if workflow:
            if workflow not in KNOWN_WORKFLOWS:
                raise ValueError(f"unknown workflow: {workflow}（合法值：{list(KNOWN_WORKFLOWS)}）")
            return RouteDecision(route="workflow", source="explicit", workflow_id=workflow,
                                 reason="请求显式指定 workflow", confidence=1.0)
        if tools or skills:
            visible = set(visible_tool_names(self.registry))
            bad_tools = [t for t in tools if t not in visible]
            if bad_tools:
                raise ValueError(f"显式工具未绑定或不可见: {bad_tools}")
            skill_names = set(self.skills.specs)
            bad_skills = [s for s in skills if s not in skill_names]
            if bad_skills:
                raise ValueError(f"未知 skill: {bad_skills}")
            return RouteDecision(route="free", source="explicit", tools=tools, skills=skills,
                                 reason="请求显式指定工具/技能", confidence=1.0)
        return None

    # ── 2) LLM 提案（目录经绑定过滤） ───────────────────────────
    async def _llm(self, request: dict[str, Any], state: dict[str, Any],
                   knowledge: list[dict[str, Any]]) -> RouteDecision | None:
        request_for_llm = dict(request)
        knowledge_consumed = False
        if knowledge:
            request_for_llm["knowledge_hints"] = _format_hints(knowledge)
            knowledge_consumed = True
        try:
            proposal = await self.llm.classify(request_for_llm, CatalogView(self.registry), self.skills)
        except Exception:
            return None
        if not proposal.get("ok"):
            return None
        decision = proposal.get("decision") or {}
        parsed = self._validate(decision)
        if parsed is None:
            return None
        parsed.raw = decision
        if knowledge_consumed:
            parsed.reason = (parsed.reason + "；已附注入知识(仅参考)").strip("；")
        return parsed

    def _validate(self, decision: dict[str, Any]) -> RouteDecision | None:
        route = str(decision.get("route") or "").strip()
        if route not in ("workflow", "free", "chat"):
            return None
        tools = [str(t) for t in (decision.get("tools") or []) if str(t).strip()]
        skills = [str(s) for s in (decision.get("skills") or []) if str(s).strip()]
        if route == "workflow":
            workflow_id = str(decision.get("workflow_id") or "").strip()
            if workflow_id not in KNOWN_WORKFLOWS:
                return None
            # G1 深根因（父会话补充）：route=workflow 曾把 LLM 提案的 skills 静默丢掉。
            # 工作流步骤仍只按 workflow 展开（技能不派发），但**不静默**——保留已注册
            # 技能并在 reason 里写清「仅留痕、不派发」，避免「提案了技能却查无痕迹」。
            skill_names = set(self.skills.specs)
            skills = _ordered_skills([s for s in skills if s in skill_names])
            reason = str(decision.get("reason") or "")
            if skills:
                reason = (f"{reason}；LLM 同时提案 skills={skills}，"
                          "route=workflow 只执行工作流步骤（技能不派发，仅留痕）").strip("；")
            return RouteDecision(route="workflow", source="llm", workflow_id=workflow_id,
                                 intent=str(decision.get("intent") or ""),
                                 skills=skills,
                                 confidence=float(decision.get("confidence") or 0),
                                 reason=reason)
        if route == "free":
            visible = set(visible_tool_names(self.registry))
            skill_names = set(self.skills.specs)
            if not tools and not skills:
                return None  # free 必须至少一个工具或技能，否则会空转出「0 步完成」
            if any(t not in visible for t in tools):
                return None
            if any(s not in skill_names for s in skills):
                return None
            skills = _ordered_skills(skills)
            return RouteDecision(route="free", source="llm", tools=tools, skills=skills,
                                 intent=str(decision.get("intent") or ""),
                                 confidence=float(decision.get("confidence") or 0),
                                 reason=str(decision.get("reason") or ""))
        return RouteDecision(route="chat", source="llm", intent=str(decision.get("intent") or ""),
                             confidence=float(decision.get("confidence") or 0),
                             reason=str(decision.get("reason") or ""),
                             answer=str(decision.get("answer") or ""))

    # ── 3) 最小确定性回退（治痛点 3：只剩三条规则 + 主数据附件规则） ──
    def _fallback(self, state: dict[str, Any]) -> RouteDecision:
        attachments = state.get("attachments") or []
        request = state.get("request", {})
        # 规则 0（G1，INT2 第五轮）：附件**全部**是 ``kind=master_data``（基础资料
        # BOM/SOP）且没有订单附件时，走业务资料识别技能——否则会被下面的规则 1
        # 吃成 `m1_m5_document_to_plan`（M1 解析订单），基础资料永远落不了 canonical。
        # 判定只看显式 ``kind``（不猜文件名/文案）；只要出现 ``order`` 或任何非
        # master_data 附件就完全不改变既有行为（继续走规则 1）。
        if _all_master_data(state, attachments, request):
            return RouteDecision(route="free", source="deterministic_fallback",
                                 intent="identify_business_data",
                                 skills=["business-data-identification"],
                                 reason="附件全部为基础资料（kind=master_data）且无订单附件"
                                        "（确定性回退规则 0）",
                                 confidence=0.5)
        has_doc = any(
            (str(a.get("kind") or "") == "order")
            or (str(a.get("sniffed_format") or a.get("content_type") or "").lower()
                .split("/")[-1] in _BUSINESS_DOC_SNIFFS)
            for a in attachments if isinstance(a, dict)
        )
        if has_doc:
            return RouteDecision(route="workflow", source="deterministic_fallback",
                                 workflow_id="m1_m5_document_to_plan", intent="document_to_plan",
                                 reason="附件含原始业务文件（确定性回退规则 1）", confidence=0.5)
        if request.get("canonical_ready"):
            return RouteDecision(route="workflow", source="deterministic_fallback",
                                 workflow_id="canonical_to_m5", intent="canonical_to_schedule",
                                 reason="请求声明 canonical 已就绪（确定性回退规则 2）", confidence=0.5)
        suggestions = "、".join(sorted(visible_tool_names(self.registry))[:12])
        return RouteDecision(
            route="chat", source="deterministic_fallback", intent="chat",
            reason="无显式参数且无法判定业务链（确定性回退规则 3）",
            confidence=0.3,
            answer="我没能确定这次任务要走哪条业务链。可以直接说明目标（例如上传订单文件做全链排程），"
                   f"或显式指定工具。当前可用能力示例：{suggestions}…",
        )


def _attachment_items(state: dict[str, Any], attachments: list[Any],
                      request: dict[str, Any]) -> list[dict[str, Any]]:
    """取本次请求的附件列表（第一个非空的来源，顺序：state → documents → attachments）。

    ``state["attachments"]`` 由 ``state.new_state_v2`` 从 ``request.attachments``
    **或** ``request.documents`` 归一（``state.py:100``）；直接调 ``_fallback`` 的
    单测/内嵌调用可能只给 ``request``，故三个来源都看一眼（不合并，避免同一文件
    被重复计一次 kind 判定）。
    """
    for source in (attachments, request.get("documents"), request.get("attachments")):
        if isinstance(source, list) and source:
            return [item for item in source if isinstance(item, dict)]
    return []


def _all_master_data(state: dict[str, Any], attachments: list[Any],
                     request: dict[str, Any]) -> bool:
    """附件非空且**全部**显式 ``kind == "master_data"``（G1 规则 0 的判定）。

    含任何 ``order`` 附件或未声明 ``kind`` 的附件一律返回 False（不改既有行为）。
    """
    items = _attachment_items(state, attachments, request)
    return bool(items) and all(str(item.get("kind") or "") == "master_data" for item in items)


def _ordered_skills(skills: list[str]) -> list[str]:
    """按 SKILL_USAGE_ORDER 偏序排列（提案顺序不满足偏序时自动纠正）。"""
    order = {name: i for i, name in enumerate(SKILL_USAGE_ORDER)}
    return sorted(skills, key=lambda s: order.get(s, len(order)))


def _format_hints(knowledge: list[dict[str, Any]]) -> list[str]:
    hints = []
    for item in knowledge:
        title = str(item.get("title") or item.get("kind") or "knowledge")
        body = str(item.get("content") or item.get("payload") or "")[:160]
        hints.append(f"[{title}] {body}")
    return hints


def strip_code_fence(text: str) -> str:  # 供测试复用的纯函数
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()
