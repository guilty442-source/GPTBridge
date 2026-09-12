from __future__ import annotations

import asyncio
import json
import re
from typing import Any


class LocalAiTeachingMixin:
    def _submit_teaching_example(self, payload: dict[str, Any]) -> dict[str, Any]:
        intent = str(payload.get("training_intent") or "reasoning").strip().casefold()
        input_text = str(payload.get("input_text") or payload.get("instruction") or "").strip()
        target_text = str(payload.get("target_text") or payload.get("ideal_response") or "").strip()
        reference_text = str(payload.get("reference_text") or "").strip()
        if intent not in self.ollama_training_gate.ALLOWED_INTENTS:
            return {
                "ok": False,
                "error_code": "TEACHING_INTENT_NOT_ALLOWED",
                "message": "這個教學分類不在允許範圍內。",
                "allowed_intents": sorted(self.ollama_training_gate.ALLOWED_INTENTS),
            }
        if not input_text or not target_text:
            return {
                "ok": False,
                "error_code": "TEACHING_EXAMPLE_REQUIRED",
                "message": "請同時提供指令與理想回答。",
            }
        candidate_digest = self.ollama_training_gate.digest(
            f"{intent}\0{input_text}\0{target_text}\0{reference_text}"
        )
        evaluated = self.ollama_training_gate.evaluate(
            [
                {
                    "candidate_id": f"owner-example-{candidate_digest[:20]}",
                    "intent": intent,
                    "input_text": input_text,
                    "target_text": target_text,
                }
            ],
            requested_intent=intent,
            reference_text=reference_text,
            response_digest=candidate_digest,
            source_type="owner-governed-teaching-candidate",
            received_via="star-chat-governance-authenticated-ai-channel",
        )
        updates: list[dict[str, Any]] = []
        for candidate in evaluated["accepted"]:
            updates.append(self._apply_self_training(self.models.MAIN, candidate))
        accepted = bool(updates and updates[0].get("accepted") is True)
        learned_now = bool(updates and updates[0].get("learned_now") is True)
        return {
            "ok": accepted,
            "message": (
                "教學樣本已通過星澄驗證並立即加入本機學習層。"
                if learned_now
                else "教學樣本已存在，保留原有版本。"
                if accepted
                else "教學樣本未通過品質與事實一致性檢查。"
            ),
            "accepted_count": int(evaluated["accepted_count"]),
            "rejected_count": int(evaluated["rejected_count"]),
            "rejections": list(evaluated["rejected"]),
            "model_updates": updates,
            "direct_weight_access": False,
            "automatic_foundation_weight_replacement": False,
            "rollback": "deactivate-versioned-example-and-rebuild-learning-layer",
            "version": "1.0",
        }

    async def _tune_investment_parameters(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        current = self.investment_repository.investment_parameter_values()
        context = str(
            payload.get("context")
            or payload.get("instruction")
            or payload.get("prompt")
            or ""
        ).strip()
        recommendation = await asyncio.to_thread(
            self.transformer_runtime.generate,
            prompt=(
                "Review the governed investment parameters and return only a JSON array of "
                "objects with parameter_key, proposed_value, reason, confidence, and evidence. "
                f"Current parameters: {json.dumps(current, ensure_ascii=False)}. "
                f"User context: {context[:8_000]}"
            ),
            intent="analysis",
            model_role="local-parameter-advisor",
            output={"response": "", "analysis": {"current_parameters": current}},
            reasoning_effort="high",
            reasoning_pipeline=True,
        )
        if recommendation.get("ok") is not True:
            return {
                "ok": False,
                "queued": False,
                "message": str(recommendation.get("message") or "本機參數建議不可用"),
                "error_code": str(
                    recommendation.get("error_code")
                    or "PARAMETER_RECOMMENDATION_UNAVAILABLE"
                ),
                "current_parameters": current,
                "applied": [],
                "external_ai_used": False,
            }
        recommendations = self._parameter_recommendations_from_text(
            str(recommendation.get("text") or "")
        )
        applied = await asyncio.to_thread(
            self.investment_repository.apply_chatgpt_parameter_recommendations,
            recommendations,
        )
        return {
            "ok": True,
            "queued": False,
            "message": f"本機模型已審核並套用 {len(applied)} 項參數。",
            "advisor": self.MATHEMATICAL_REVIEW_MODEL,
            "reviewer": self.FINAL_COORDINATOR_MODEL,
            "database_owner": "star-investment-native-model",
            "applied": applied,
            "current_parameters": self.investment_repository.investment_parameter_values(),
            "rejected_count": max(0, len(recommendations) - len(applied)),
            "accepted_memory": [],
            "external_ai_used": False,
            "transport": "ollama-loopback-only",
            "governance_checked": True,
        }

    @staticmethod
    def _memory_candidates(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, dict) or not isinstance(value.get("candidates"), list):
            return []
        return [dict(item) for item in value["candidates"] if isinstance(item, dict)]

    @classmethod
    def _nested_memory_candidates(cls, value: Any) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        if isinstance(value, dict):
            output.extend(cls._memory_candidates(value.get("memory_interchange")))
            for item in value.values():
                if isinstance(item, (dict, list)):
                    output.extend(cls._nested_memory_candidates(item))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    output.extend(cls._nested_memory_candidates(item))
        return list(
            {
                str(item.get("candidate_id") or json.dumps(item, sort_keys=True)): item
                for item in output
            }.values()
        )

    @staticmethod
    def _plan_external_collaboration(
        prompt: str, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        normalized = str(prompt or "").strip()
        if payload.get("use_external_collaboration") is False:
            return []
        external_label = r"(?:外部\s*AI|AI\s*協作|多\s*AI|external\s+AI|AI\s+collaboration)"
        if re.search(
            rf"(?:不要|不得|不可|禁止|無需|不用|不必|do\s+not|don't|without)"
            rf"[^，,。；;！？!?\n]{{0,16}}{external_label}",
            normalized,
            flags=re.IGNORECASE,
        ):
            return []
        explicitly_requested = payload.get("use_external_collaboration") is True or any(
            token in normalized
            for token in (
                "外部AI",
                "外部 AI",
                "AI協作",
                "AI 協作",
                "多AI",
                "多 AI",
                "external AI",
                "AI collaboration",
            )
        )
        if not explicitly_requested:
            return []
        rules = (
            ("search", ("搜尋", "查詢", "找資料")),
            ("advanced_search", ("高階搜尋", "深入搜尋", "深度搜尋")),
            ("calculation", ("高階計算", "外部驗算")),
            ("longform", ("長文", "文件", "報告")),
            ("reasoning", ("深度推理", "外部推理", "邏輯推理")),
            ("social_media", ("社群", "社交媒體")),
            ("trends", ("潮流", "時事")),
            ("breaking_news", ("突發新聞", "突發事件")),
        )
        task_types = [
            task_type
            for task_type, tokens in rules
            if any(token in normalized for token in tokens)
        ]
        if not task_types:
            task_types = ["general"]
        return [
            {
                "task_type": task_type,
                "content": normalized[:64_000],
                "business_scope": str(
                    payload.get("business_scope") or "general"
                ),
            }
            for task_type in list(dict.fromkeys(task_types))[:6]
        ]

    @staticmethod
    def _parameter_recommendations_from_text(text: str) -> list[dict[str, Any]]:
        candidate = str(text or "").strip()
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            match = re.search(r"\[[\s\S]*\]", candidate)
            if match is None:
                return []
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                return []
        return [dict(item) for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
