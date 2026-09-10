from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("yunpai.agent")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class QwenConfig:
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8088/v1"
    model: str = "qwen3.8-27b"
    api_key: str = ""
    timeout_s: float = 45.0

    @classmethod
    def from_env(cls) -> "QwenConfig":
        return cls(
            enabled=_env_bool("QWEN_ROUTER_ENABLED", True),
            base_url=os.getenv("QWEN_BASE_URL", "http://127.0.0.1:8088/v1").rstrip("/"),  # v2 单一默认（config.LLMConfig 同源）
            model=os.getenv("QWEN_MODEL", "qwen3.8-27b"),
            api_key=os.getenv("QWEN_API_KEY", ""),
            timeout_s=float(os.getenv("QWEN_TIMEOUT_S", "45")),
        )

    def public(self) -> dict[str, Any]:
        return {
            "provider": "qwen",
            "model": self.model,
            "base_url": self.base_url,
            "enabled": self.enabled,
            "configured": bool(self.api_key),
        }


class QwenRouter:
    """OpenAI-compatible Qwen client used only for intent and route proposals."""

    def __init__(self, config: QwenConfig | None = None) -> None:
        self.config = config or QwenConfig.from_env()

    async def classify(self, request: dict[str, Any], registry: Any, skills: Any = None) -> dict[str, Any]:
        started = time.perf_counter()
        metadata = self.config.public()
        if not self.config.enabled:
            return {"ok": False, "status": "disabled", "model": {**metadata, "status": "disabled"}}
        if not self.config.api_key:
            return {"ok": False, "status": "not_configured", "model": {**metadata, "status": "not_configured"}}
        try:
            import httpx

            catalog = [
                {"name": spec.name, "module": spec.module, "method": spec.method, "version": getattr(spec, "version", "")}
                for spec in registry.specs.values()
            ]
            if skills is None:
                skills = getattr(self, "skills", None)
            skill_catalog = skills.catalog() if skills is not None and hasattr(skills, "catalog") else []
            prompt = self._prompt(request, catalog, skill_catalog)
            headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
            body = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 512,
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_object"},
            }
            # trust_env=False prevents an HTTP proxy from intercepting the private model address.
            async with httpx.AsyncClient(timeout=self.config.timeout_s, trust_env=False) as client:
                response = await client.post(f"{self.config.base_url}/chat/completions", headers=headers, json=body)
                response.raise_for_status()
                payload = response.json()
            content = self._content(payload)
            decision = self._parse_decision(content)
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            logger.info("qwen.intent_route status=ok model=%s latency_ms=%s route=%s tools=%s", self.config.model, elapsed, decision.get("route"), decision.get("tools", []))
            return {"ok": True, "status": "ok", "decision": decision, "model": {**metadata, "status": "ok", "latency_ms": elapsed}}
        except Exception as exc:
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            logger.warning("qwen.intent_route status=error model=%s latency_ms=%s error=%s", self.config.model, elapsed, exc)
            return {"ok": False, "status": "error", "error": str(exc), "model": {**metadata, "status": "error", "latency_ms": elapsed}}

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是云湃制造系统的 Planner 路由器。只负责识别用户意图和选择执行路径，不执行工具。"
            "必须只输出一个 JSON 对象，不要 Markdown 或思维过程。route 必须是字面值 workflow、free、chat，绝对不能使用 production_planning、erp 或其他自定义路由名。"
            "workflow 仅用于完整 M0 到 M5 订单/采购/排程主链；free 用于一个或多个已注册工具或已注册高阶 Skill；chat 用于解释性对话。"
            "JSON 字段必须为 intent、route、workflow_id、tools、confidence、reason；"
            "route=workflow 时 workflow_id 必须从给定 workflows 中按 id 精确选择，不能为空也不能自造；"
            "route=chat 时必须额外返回 answer，用中文直接回答用户问题。可选 skills 字段用于选择高阶 Skill，只能从给定 skill catalog 中按 name 精确选择。"
            "tools 只能从给定 catalog 选择。skill 名称必须与 catalog 中的 name 完全一致，不能自造或拼接版本号。"
            "选择高阶 Skill 时不要同时列出该 Skill 内部会调用的工具（tools 只填 Skill 之外的独立工具，无法确定时留空）；"
            "上传文件时按附件 kind 判定，两条判据互斥：附件中含 kind=\"order\" 的条目 → 首选 workflow=m1_m5_document_to_plan（订单主链）；"
            "附件非空且全部为 kind=\"master_data\"（BOM、SOP、设备、工位、人员、库存、供应商、财务、目录批量等基础资料）且不含 order 附件 → route=free 且 skills 必须给出 business-data-identification。"
            "两类同时出现时以订单主链为准（workflow=m1_m5_document_to_plan），不要同时给 business-data-identification。"
        )

    @staticmethod
    def _prompt(request: dict[str, Any], catalog: list[dict[str, Any]], skill_catalog: list[dict[str, Any]] | None = None) -> str:
        from .workflow_registry import KNOWN_WORKFLOWS, load_workflow

        message = str(request.get("message") or request.get("task") or "")
        file_items = list(request.get("documents", [])) + list(request.get("attachments", []))
        file_names = [str(item.get("filename", "")) for item in file_items if isinstance(item, dict)]
        skill_names = [str(skill) for skill in (request.get("skills") or [])] if isinstance(request.get("skills"), list) else []
        workflows = [
            {"id": wid, "description": str(load_workflow(wid).get("description") or "")[:120]}
            for wid in KNOWN_WORKFLOWS
        ]
        return json.dumps({
            "message": message,
            "uploaded_files": file_names,
            "workflows": workflows,
            "catalog": catalog,
            "skills": skill_catalog or [{"name": "business-data-identification", "description": "识别业务资料并写入可审核候选库；不直接发布 M0 canonical 事实"}],
            "requested_skills": skill_names,
        }, ensure_ascii=False)

    @staticmethod
    def _content(payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ValueError("Qwen response has no choices")
        message = choices[0].get("message") or {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Qwen response has empty content")
        return content

    @staticmethod
    def _parse_decision(content: str) -> dict[str, Any]:
        cleaned = content.strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if not match:
                raise ValueError("Qwen response is not valid JSON")
            value = json.loads(match.group(0))
        if not isinstance(value, dict):
            raise ValueError("Qwen route decision must be an object")
        route = value.get("route")
        if route not in {"workflow", "free", "chat"}:
            raise ValueError(f"invalid Qwen route: {route}")
        tools = value.get("tools", [])
        if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
            raise ValueError("Qwen tools must be a string array")
        workflow_id = value.get("workflow_id")
        workflow_id = workflow_id.strip() if isinstance(workflow_id, str) else ""
        try:
            confidence = max(0.0, min(1.0, float(value.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        return {
            "intent": str(value.get("intent") or "unknown"),
            "route": route,
            "workflow_id": workflow_id or None,
            "tools": tools,
            "skills": [str(skill) for skill in value.get("skills", [])] if isinstance(value.get("skills", []), list) else [],
            "confidence": confidence,
            "reason": str(value.get("reason") or ""),
            "answer": str(value.get("answer") or ""),
        }

    async def map_to_canonical(self, sample: dict[str, Any]) -> dict[str, Any]:
        """把文件样本映射成 canonical 记录（多模态：表格看表头/行，图片/PDF 看图）。

        只做「理解 + 映射 + 分类」，数值从文件照抄；结构由调用方经
        ``canonical_schema.validate_canonical`` 确定性校验。
        """
        started = time.perf_counter()
        metadata = self.config.public()
        if not self.config.enabled:
            return {"ok": False, "status": "disabled", "model": {**metadata, "status": "disabled"}}
        if not self.config.api_key:
            return {"ok": False, "status": "not_configured", "model": {**metadata, "status": "not_configured"}}
        try:
            import httpx

            headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
            body = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": self._canonical_system_prompt()},
                    {"role": "user", "content": self._canonical_prompt(sample)},
                ],
                "temperature": 0,
                "max_tokens": 8192,
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_object"},
            }
            async with httpx.AsyncClient(timeout=self.config.timeout_s, trust_env=False) as client:
                response = await client.post(f"{self.config.base_url}/chat/completions", headers=headers, json=body)
                response.raise_for_status()
                payload = response.json()
            decision = self._parse_canonical(self._content(payload))
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            logger.info("qwen.map_to_canonical status=ok model=%s latency_ms=%s entity_type=%s records=%s", self.config.model, elapsed, decision.get("entity_type"), len(decision.get("records", [])))
            return {"ok": True, "status": "ok", "decision": decision, "model": {**metadata, "status": "ok", "latency_ms": elapsed}}
        except Exception as exc:
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            logger.warning("qwen.map_to_canonical status=error model=%s latency_ms=%s error=%s", self.config.model, elapsed, exc)
            return {"ok": False, "status": "error", "error": str(exc), "model": {**metadata, "status": "error", "latency_ms": elapsed}}

    async def guide_chat(self, *, scale: str | None, departments: list[str],
                         assignments: list[dict[str, Any]], roster: list[dict[str, Any]],
                         message: str, roles: list[dict[str, Any]],
                         permissions: list[dict[str, Any]]) -> dict[str, Any]:
        """引导AI 对话判断（F-015：结构与分配判断交给模型，代码只做模板）。

        模型只产出**扁平数据**（部门名单 + 人员分配，人员带所属部门），组织树
        的绘制由前端模板完成；首轮（needs_scale）返回三档规模的建议部门名单。
        返回 ``{ok, reply, scale, needs_scale, confirm, departments, assignments,
        scale_departments}``。确认落地由调用方在 confirm=true 时执行。
        """
        started = time.perf_counter()
        metadata = self.config.public()
        if not self.config.enabled:
            return {"ok": False, "status": "disabled", "model": {**metadata, "status": "disabled"}}
        try:
            import httpx

            prompt = json.dumps({
                "current_scale": scale,
                "current_departments": list(departments or []),
                "current_assignments": list(assignments or []),
                "roster": roster,
                "user_message": message,
                "roles": roles,
                "permissions": permissions,
            }, ensure_ascii=False)
            body = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": (
                        "你是云湃制造系统的组织架构引导助手。你会收到：当前部门名单、当前人员分配（每人含姓名/"
                        "角色/所属部门）、花名册（姓名/岗位/部门）、可分配角色及含义、用户最新一句话。"
                        "判断用户意图，只输出一个 JSON 对象，字段：\n"
                        "reply（给用户的中文回复，简短自然）\n"
                        "scale（用户选定/变更规模时 small|medium|large，否则 null）\n"
                        "needs_scale（还不知道规模、需要先让用户选时 true，否则 false）\n"
                        "confirm（用户明确要落地当前架构，如“就这样/确认/好/可以/落地”时为 true，否则 false）\n"
                        "departments（更新后的部门/组织单元名数组，如 [\"生产部\",\"品质部\"]）\n"
                        "assignments（更新后的人员分配数组，每项 {name:\"张三\", roles:[\"factory-director\"], "
                        "dept:\"生产部\", manager:\"上级姓名或空\"}；manager 是其直接上级（汇报对象）的姓名，"
                        "最高负责人（如厂长）的 manager 为空字符串；尚未分配人员则空数组）\n"
                        "scale_departments（仅当 needs_scale=true 时返回对象 {small:[...], medium:[...], large:[...]}，"
                        "分别是小/中/大规模的建议部门名单）\n\n"
                        "规则：\n"
                        "1) 规模复杂度——small 2~3 个部门、medium 4~6 个、large 6~9 个；"
                        "scale_departments 三档要体现部门数量的复杂度差异。\n"
                        "2) 账号分配 + 汇报关系：按花名册岗位(skill)与部门匹配角色（角色含义见 roles）；"
                        "给出管理层级——最高负责人（厂长）manager 为空；部门负责人/经理 report 给厂长；"
                        "组长 report 给部门负责人；普通员工 report 给组长；拿不准给 worker 并在 reply 提示。\n"
                        "3) 角色 code 只能从 roles 里选，不能自造。\n"
                        "4) 用户的所有增删改（加/删部门、设/撤角色、调动人员/调整上级）都要直接反映到 departments/assignments 里，"
                        "返回更新后的完整名单，并在 reply 说清变化。\n"
                        "5) 只依据花名册与当前方案判断，不编造花名册之外的人员；用户明确提到的新名字可以加入。"
                    )},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 4096,
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": False},
                "response_format": {"type": "json_object"},
            }
            # 本地模型常无鉴权：api_key 为空时用占位 token（不因此拒绝）。
            token = self.config.api_key or "local"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            # 引导要一次生成名单，本地 27b 较慢：给更长超时（至少 180s，与 Planner 路由解耦）。
            async with httpx.AsyncClient(timeout=max(self.config.timeout_s, 180.0), trust_env=False) as client:
                response = await client.post(f"{self.config.base_url}/chat/completions", headers=headers, json=body)
                response.raise_for_status()
                payload = response.json()
            content = self._content(payload)
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE | re.DOTALL).strip()
            decision = json.loads(cleaned)
            if not isinstance(decision, dict):
                raise ValueError("guide_chat response is not an object")
            departments = decision.get("departments") if isinstance(decision.get("departments"), list) else []
            assignments = decision.get("assignments") if isinstance(decision.get("assignments"), list) else []
            scale_departments = decision.get("scale_departments") if isinstance(decision.get("scale_departments"), dict) else {}
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            logger.info("qwen.guide_chat status=ok model=%s latency_ms=%s depts=%s assigns=%s confirm=%s",
                        self.config.model, elapsed, len(departments), len(assignments), bool(decision.get("confirm")))
            return {"ok": True, "status": "ok",
                    "reply": str(decision.get("reply") or ""),
                    "scale": decision.get("scale"),
                    "needs_scale": bool(decision.get("needs_scale")),
                    "confirm": bool(decision.get("confirm")),
                    "departments": departments,
                    "assignments": assignments,
                    "scale_departments": scale_departments,
                    "model": {**metadata, "status": "ok", "latency_ms": elapsed}}
        except Exception as exc:
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            logger.warning("qwen.guide_chat status=error model=%s latency_ms=%s error=%s",
                           self.config.model, elapsed, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "status": "error", "error": f"{type(exc).__name__}: {exc}",
                    "model": {**metadata, "status": "error", "latency_ms": elapsed}}

    @staticmethod
    def _canonical_system_prompt() -> str:
        from .canonical_schema import CANONICAL_SCHEMA

        lines = []
        for entity_type, spec in CANONICAL_SCHEMA.items():
            lines.append(f"- {entity_type}: 必填[{','.join(spec['required'])}] 允许[{','.join(spec['fields'])}]")
        schema_text = "\n".join(lines)
        return (
            "你是云湃制造系统的数据映射器。根据给定的文件样本（表格看 headers/sample_rows/sheets 的 raw_rows，图片/PDF 直接看图）"
            "判断业务实体类型，并把内容抽取成我们 canonical 格式的记录。"
            "只输出一个 JSON 对象，字段为 entity_type、records、confidence、needs_review、reason。"
            "entity_type 只能是下列之一；records 每条是对象，字段名只能用该类型「允许」集合内的字段；"
            "数值必须从文件照抄，绝不编造；每条记录可带 _source（sheet/row/col/raw 或 page/image）定位证据。"
            "records 最多输出前 50 条，超出部分省略（不要为了穷举所有行而把 JSON 写超长导致截断）。"
            "对于表格/多 sheet 文件，额外输出 column_mapping（表头名→canonical 字段名），"
            "例如 {\"物料编码\":\"material_code\",\"材料名称\":\"material_name\",\"用量\":\"quantity\",\"单位\":\"unit\"}；"
            "后续会用确定性代码按该映射抽取全量行，所以 column_mapping 的表头名要照抄文件里的实际表头。"
            "confidence 是 0 到 1 浮点；不确定（<0.7）或关键字段缺失时 needs_review=true。reason 一句话说明依据。"
            "特殊结构指引：① 作业指导书(SOP)：每个 sheet 是一道工序，从「制作工站/文件编号/IE工时/作业步骤」抽取"
            "document 的 route_steps 数组，每项为 {operation_code, operation_name, station, standard_minutes}；"
            "operation_code 用 文件编号+序号（如 TX-001-01），standard_minutes 从 IE工时 的秒数除以 60 得到，station 取制作工站。"
            "② 成品成本分析表/BOM 表：从「物料编码/材料名称/用量/单位/单价/供应商」抽取 bom 的 lines，"
            "每行 {material_code, material_name, quantity, unit}，用量照抄数值列。"
            "\ncanonical schema：\n" + schema_text
        )

    @staticmethod
    def _canonical_prompt(sample: dict[str, Any]) -> list[dict[str, Any]]:
        text = json.dumps({
            "filename": str(sample.get("filename") or ""),
            "detected_format": str((sample.get("sniff") or {}).get("detected_format") or ""),
            "headers": list(sample.get("headers") or []),
            "sample_rows": list(sample.get("sample_rows") or []),
            "sheet_names": list(sample.get("sheet_names") or []),
            "sheets": list(sample.get("sheets") or []),
            "row_count": sample.get("row_count"),
        }, ensure_ascii=False)
        parts: list[dict[str, Any]] = [
            {"type": "text", "text": "文件样本：\n" + text + "\n若含多 sheet（sheet_names 是全部 sheet，sheets 是前几个 sheet 的采样），请综合判断整体业务类型并抽取 canonical 记录。"},
        ]
        for image_b64 in (sample.get("images") or []):
            parts.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + str(image_b64)}})
        return parts

    @staticmethod
    def _parse_canonical(content: str) -> dict[str, Any]:
        from .canonical_schema import CANONICAL_SCHEMA

        cleaned = content.strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if not match:
                raise ValueError("Qwen canonical response is not valid JSON")
            value = json.loads(match.group(0))
        if not isinstance(value, dict):
            raise ValueError("Qwen canonical decision must be an object")
        entity_type = str(value.get("entity_type") or "")
        if entity_type not in CANONICAL_SCHEMA:
            raise ValueError(f"invalid entity_type: {entity_type}")
        records = value.get("records", [])
        if not isinstance(records, list) or not records or not all(isinstance(record, dict) for record in records):
            raise ValueError("Qwen records must be a non-empty object array")
        try:
            confidence = max(0.0, min(1.0, float(value.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        column_mapping = value.get("column_mapping")
        if not isinstance(column_mapping, dict):
            column_mapping = {}
        return {
            "entity_type": entity_type,
            "records": records,
            "column_mapping": {str(k): str(v) for k, v in column_mapping.items()},
            "confidence": confidence,
            "needs_review": bool(value.get("needs_review")) or confidence < 0.7,
            "reason": str(value.get("reason") or ""),
        }
