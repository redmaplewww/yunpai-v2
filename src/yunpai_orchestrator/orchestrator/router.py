"""路由决策器（书二 §4.1）——单一决策链：显式 > LLM 提案 > 最小确定性回退。

与旧实现的本质区别（治痛点 2/3/5）：
1. LLM 的候选目录经绑定状态过滤（CatalogView），不可能提案未绑定/废弃工具；
2. 确定性回退只剩最小规则，旧 INTENT_TO_TOOL 中文关键词大表不迁移；
3. 一次决策一处留痕（route_decision: source/confidence/reason/raw）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..binding import CatalogView, visible_tool_names
from ..llm import QwenRouter
from ..workflow_registry import KNOWN_WORKFLOWS
from .workflow_engine import WorkflowEngine

#: 高阶 Skill 使用顺序偏序（legacy agents.py:29-38 原样迁移；M6 按 F-008 追加）。
#: M6 排在末位：它消费的 M0 canonical 事实与 M5 报工事实都由前面的 Skill 产出，
#: 成本核算/台账是链尾（见《M6-开发计划-v2口径》§0「M6 = 成本核算链 + 成本明细账」）。
SKILL_USAGE_ORDER: tuple[str, ...] = (
    "business-data-identification",
    "yunpai-m1-document-parser",
    "yunpai-m0-data-foundation",
    "yunpai-m2-bom-sop",
    "yunpai-m3-material-planning",
    "yunpai-m4-procurement",
    "yunpai-m5-pmc",
    "yunpai-m5-pmc-lifecycle",
    "yunpai-m6-finance",
    "yunpai-m6-ledger",
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
            return d
        return self._fallback(state)

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
            return RouteDecision(route="workflow", source="llm", workflow_id=workflow_id,
                                 intent=str(decision.get("intent") or ""),
                                 confidence=float(decision.get("confidence") or 0),
                                 reason=str(decision.get("reason") or ""))
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

    # ── 3) 最小确定性回退（治痛点 3：只剩三条规则） ─────────────
    def _fallback(self, state: dict[str, Any]) -> RouteDecision:
        attachments = state.get("attachments") or []
        request = state.get("request", {})
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
