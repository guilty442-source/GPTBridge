from __future__ import annotations

import json
import re
from typing import Any, Mapping


class TransformerRuntimeRoutingMixin:
    """Intent routing, pipeline composition and commander adjudication."""

    def model_candidates_for_intent(
        self,
        intent: str,
        reasoning_effort: str = "medium",
        task_intensity: str = "",
        *,
        refresh: bool = False,
    ) -> list[str]:
        catalog = {
            str(item.get("name") or "")
            for item in self.selectable_models(refresh=refresh)
        }
        normalized_intent = str(intent or "conversation").strip().casefold()
        normalized_effort = str(reasoning_effort or "medium").strip().casefold()
        if normalized_effort not in self.REASONING_EFFORTS:
            normalized_effort = "medium"
        normalized_intensity = str(task_intensity or "").strip().casefold()
        routing_effort = (
            "low" if normalized_intensity == "simple" else normalized_effort
        )
        if routing_effort in {"none", "low"}:
            preferred = self.LOW_EFFORT_MODEL_PREFERENCES.get(
                normalized_intent,
                self.INTENT_MODEL_PREFERENCES.get(normalized_intent, ()),
            )
        else:
            preferred = self.INTENT_MODEL_PREFERENCES.get(normalized_intent, ())
        candidates = list(preferred or ("glm4:9b",))
        if normalized_intensity:
            level_candidates = [
                model
                for model in candidates
                if not self.MODEL_ROLE_ASSIGNMENTS.get(model, {}).get("task_levels")
                or normalized_intensity
                in self.MODEL_ROLE_ASSIGNMENTS[model]["task_levels"]
            ]
            if level_candidates:
                candidates = level_candidates
        ordered: list[str] = []
        for candidate in candidates:
            if candidate in catalog and candidate not in ordered:
                ordered.append(candidate)
        if ordered:
            return ordered
        return candidates

    @classmethod
    def _tier_capped_models(cls, installed: set[str], tier_cap: str) -> set[str]:
        """Restrict the candidate pool to small/medium tiers for latency-
        sensitive interactive paths.  Empty cap keeps the full pool; an
        unknown tier or an empty intersection returns an empty set so the
        caller falls back to the uncapped pool."""
        normalized = str(tier_cap or "").strip().casefold()
        if normalized not in cls.MODEL_SIZE_TIERS:
            return set()
        allowed: set[str] = set()
        for tier in cls.MODEL_SIZE_TIERS:
            allowed.update(cls.MODEL_SIZE_TIERS[tier])
            if tier == normalized:
                break
        return installed & allowed

    def preferred_model_for_intent(
        self, intent: str, reasoning_effort: str = "medium"
    ) -> str:
        return self.model_candidates_for_intent(intent, reasoning_effort)[0]

    def select_model_for_request(
        self,
        intent: str,
        *,
        reasoning_effort: str = "medium",
        task_intensity: str = "normal",
        generation_speed: str = "medium",
        tier_cap: str = "",
    ) -> str:
        """Choose one installed model using role, quality, speed and residency."""
        installed = {
            str(item.get("name") or "")
            for item in self.selectable_models(refresh=False)
        }
        if not installed:
            return ""
        capped = self._tier_capped_models(installed, tier_cap)
        if capped:
            installed = capped
        normalized_intent = str(intent or "conversation").strip().casefold()
        preferred = self.INTENT_MODEL_PREFERENCES.get(normalized_intent, ())
        low_preferred = self.LOW_EFFORT_MODEL_PREFERENCES.get(normalized_intent, ())
        effort = str(reasoning_effort or "medium").strip().casefold()
        intensity = str(task_intensity or "normal").strip().casefold()
        speed = str(generation_speed or "medium").strip().casefold()
        if (
            normalized_intent in {"coding", "command_execution"}
            and intensity in {"simple", "normal"}
            and "granite-code:3b" in installed
        ):
            return "granite-code:3b"
        ranked = self._rank_request_models(
            installed,
            preferred=preferred,
            low_preferred=low_preferred,
            effort=effort,
            intensity=intensity,
            speed=speed,
        )
        return max(ranked, key=lambda item: (item[0], item[1]))[1]

    def _rank_request_models(
        self,
        installed: set[str],
        *,
        preferred: Any,
        low_preferred: Any,
        effort: str,
        intensity: str,
        speed: str,
    ) -> list[tuple[float, str]]:
        speed_weight = {"slow": 0.5, "low": 0.8, "medium": 1.2, "high": 2.0, "ultra": 2.8}.get(speed, 1.2)
        reasoning_weight = {"none": 0.2, "low": 0.6, "medium": 1.3, "high": 2.2}.get(effort, 1.3)
        if intensity == "simple":
            speed_weight += 1.0
            reasoning_weight *= 0.6
        elif intensity == "difficult":
            reasoning_weight += 1.0
        return [
            (
                self._route_model_score(
                    model,
                    preferred=preferred,
                    low_preferred=low_preferred,
                    effort=effort,
                    intensity=intensity,
                    speed=speed,
                    speed_weight=speed_weight,
                    reasoning_weight=reasoning_weight,
                ),
                model,
            )
            for model in installed
        ]

    def _route_model_score(
        self,
        model: str,
        *,
        preferred: Any,
        low_preferred: Any,
        effort: str,
        intensity: str,
        speed: str,
        speed_weight: float,
        reasoning_weight: float,
    ) -> float:
        metadata = self.KNOWN_MODEL_METADATA.get(model, {})
        evaluation = metadata.get("evaluation") or {}
        score = (
            float(evaluation.get("speed") or 0) * speed_weight
            + float(evaluation.get("strength") or 0) * 1.5
            + float(evaluation.get("reasoning_depth") or 0) * reasoning_weight
        )
        if model in preferred:
            score += 8.0 - preferred.index(model)
        if effort in {"none", "low"} and model in low_preferred:
            score += 6.0 - low_preferred.index(model)
        if model in self.RESIDENT_MODELS:
            score += 4.0 if speed in {"high", "ultra"} or intensity == "simple" else 2.0
        levels = self.MODEL_ROLE_ASSIGNMENTS.get(model, {}).get("task_levels") or ()
        if levels and intensity not in levels:
            score -= 5.0
        return score

    def _commander_adjudicate_model_failure(
        self,
        *,
        failed_model: str,
        failure: Mapping[str, Any],
        intent: str,
        task_intensity: str,
        model_catalog: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        if failed_model == self.FAILURE_ADJUDICATOR_MODEL:
            return {
                "adjudicator_model": self.FAILURE_ADJUDICATOR_MODEL,
                "decision": "stop-and-report-commander-failure",
                "assigned_model": "",
                "dynamic_reassignment": False,
            }
        candidates = {
            name: {
                "primary_responsibility": record.get("primary_responsibility"),
                "secondary_responsibilities": record.get(
                    "secondary_responsibilities"
                ),
                "task_levels": record.get("task_levels"),
                "execution_speed": record.get("execution_speed"),
                "reasoning_intensity": record.get("reasoning_intensity"),
            }
            for name, record in model_catalog.items()
            if name != failed_model and name not in self.NON_GENERATIVE_MODELS
        }
        prompt = self._commander_adjudication_prompt(
            intent=intent,
            task_intensity=task_intensity,
            failed_model=failed_model,
            failure=failure,
            candidates=candidates,
        )
        result = self._run_commander_adjudication(prompt)
        if result.get("ok") is not True:
            return {
                "adjudicator_model": self.FAILURE_ADJUDICATOR_MODEL,
                "decision": "stop-and-report-adjudication-failure",
                "assigned_model": "",
                "dynamic_reassignment": False,
                "adjudication_inference": result,
            }
        return self._parse_commander_adjudication(result, candidates)

    def _run_commander_adjudication(self, prompt: str) -> dict[str, Any]:
        return self.generate(
            prompt=prompt,
            intent="conversation",
            model_role="model-failure-commander-adjudication",
            output={"response": ""},
            max_tokens=256,
            reasoning_effort="high",
            task_intensity="difficult",
            requested_model=self.FAILURE_ADJUDICATOR_MODEL,
            complex_pipeline=False,
            reasoning_pipeline=False,
            division_pipeline=False,
            _automatic_model_override=True,
            _base_default_retry=True,
        )

    @staticmethod
    def _commander_adjudication_prompt(
        *,
        intent: str,
        task_intensity: str,
        failed_model: str,
        failure: Mapping[str, Any],
        candidates: Mapping[str, Any],
    ) -> str:
        return (
            "你是 Qwen3.8 作業總指揮。某個固定主責模型失敗，請裁決是否動態改派。"
            "這不是預設備用；最多只能指定一個模型頂上。只輸出單一 JSON 物件："
            '{"decision":"reassign"或"stop","assigned_model":"模型名稱或空字串",'
            '"reason":"簡短理由"}。不得指定失敗模型、未列出的模型或星澄。\n'
            f"任務意圖：{intent}\n任務層級：{task_intensity}\n"
            f"失敗模型：{failed_model}\n"
            f"失敗代碼：{failure.get('error_code')}\n"
            f"候選模型與職責：{json.dumps(candidates, ensure_ascii=False)}"
        )

    def _parse_commander_adjudication(
        self,
        result: Mapping[str, Any],
        candidates: Mapping[str, Any],
    ) -> dict[str, Any]:
        match = re.search(r"\{.*\}", str(result.get("text") or ""), flags=re.DOTALL)
        try:
            decoded = json.loads(match.group(0)) if match else {}
        except json.JSONDecodeError:
            decoded = {}
        assigned_model = str(decoded.get("assigned_model") or "").strip()
        decision = str(decoded.get("decision") or "stop").strip().casefold()
        if decision != "reassign" or assigned_model not in candidates:
            assigned_model = ""
            decision = "stop"
        return {
            "adjudicator_model": self.FAILURE_ADJUDICATOR_MODEL,
            "decision": decision,
            "assigned_model": assigned_model,
            "reason": str(decoded.get("reason") or ""),
            "dynamic_reassignment": bool(assigned_model),
            "preconfigured_backup_used": False,
        }

    @classmethod
    def _pipeline_for_task(
        cls,
        *,
        intent: str,
        reasoning_effort: str,
        complex_pipeline: bool,
        reasoning_pipeline: bool,
        division_pipeline: bool,
        available_models: set[str] | None = None,
    ) -> list[tuple[str, str]]:
        normalized_intent = str(intent or "conversation").strip().casefold()
        if complex_pipeline:
            return cls._complex_pipeline_stages(normalized_intent)
        if reasoning_pipeline:
            return cls._reasoning_pipeline_stages(reasoning_effort)
        if division_pipeline:
            return cls._division_pipeline_stages(
                normalized_intent, reasoning_effort
            )
        return []

    @classmethod
    def _complex_pipeline_stages(
        cls, normalized_intent: str
    ) -> list[tuple[str, str]]:
        specialist_stage, specialist_model = cls._complex_specialist(
            normalized_intent
        )
        return [
            (
                "classify-intensity-decompose-and-route-subtasks",
                cls.TASK_ALLOCATION_MODEL,
            ),
            (specialist_stage, specialist_model),
            (
                "integrate-results-and-plan-governed-operation",
                cls.INTEGRATION_MODEL,
            ),
            (
                "prepare-and-execute-governed-operation",
                cls.EXECUTION_MODEL,
            ),
            (
                "cross-validate-repair-or-escalate",
                cls.INSPECTION_MODEL,
            ),
            (
                "verify-and-produce-traditional-chinese-result",
                cls.RESULT_MODEL,
            ),
        ]

    @classmethod
    def _complex_specialist(cls, normalized_intent: str) -> tuple[str, str]:
        if normalized_intent in cls.VISUAL_FILE_MANAGEMENT_INTENTS:
            return (
                "recognize-classify-tag-and-summarize-visual-files",
                cls.VISUAL_FILE_MANAGEMENT_MODEL,
            )
        if normalized_intent in cls.DOMAIN_REASONING_INTENTS:
            return "perform-domain-reasoning", "deepseek-r1:14b"
        if normalized_intent in {
            "coding",
            "command_execution",
            "autonomous_agent",
            "self_upgrade",
            "repair",
        }:
            return (
                "prepare-code-and-execution-handoff",
                "qwen3-coder:30b-a3b-q4_K_M",
            )
        if normalized_intent in {"search", "data", "data_organization"}:
            return (
                "retrieve-and-organize-by-authority",
                "ibm/granite4.2:30b-q4_K_M",
            )
        if normalized_intent == "capabilities":
            return (
                "compose-capabilities-by-authority",
                "gemma4:26b-a4b-it-qat",
            )
        return "perform-assigned-role-work", "gemma4:12b-it-qat"

    @classmethod
    def _reasoning_pipeline_stages(
        cls, reasoning_effort: str
    ) -> list[tuple[str, str]]:
        pipeline = [
            (
                "independent-domain-reasoning",
                "deepseek-r1:14b",
            )
        ]
        if reasoning_effort in {"medium", "high"}:
            pipeline.append(
                ("final-coordinate-and-verify", cls.INTEGRATION_MODEL)
            )
        return pipeline

    @classmethod
    def _division_pipeline_stages(
        cls, normalized_intent: str, reasoning_effort: str
    ) -> list[tuple[str, str]]:
        if normalized_intent in {"search", "data", "command_understanding"}:
            return [
                ("fast-grounded-synthesis", "mistral-small:24b"),
            ]
        if normalized_intent in {
            "coding",
            "command_execution",
            "autonomous_agent",
            "self_upgrade",
            "repair",
        }:
            pipeline = [
                ("prepare-agent-and-code", "qwen3-coder:30b-a3b-q4_K_M"),
                ("execute-agent-and-code", cls.EXECUTION_MODEL),
            ]
            if reasoning_effort == "high":
                pipeline.append(
                    ("final-coordinate-and-verify", cls.INTEGRATION_MODEL)
                )
            return pipeline
        return []
