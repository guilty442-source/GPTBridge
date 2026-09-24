from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime
from typing import Any

from ..domain.model_registry import StarModelProfile


class LocalAiTrainingMixin:
    def _internal_training_due(self, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        if (
            not self.native_runtime.enabled
            or current < self._internal_training_not_before
            or (
                self._internal_training_task is not None
                and not self._internal_training_task.done()
            )
        ):
            return False
        created_at = str(self._latest_internal_training.get("created_at") or "")
        if created_at:
            try:
                last_run = datetime.fromisoformat(created_at).timestamp()
            except ValueError:
                last_run = current
            if current - last_run < self.INTERNAL_TRAINING_INTERVAL_SECONDS:
                return False
        return any(
            item.get("installed") is True
            for item in self.native_runtime.selectable_models(refresh=False)
        )

    async def _run_internal_native_training(self) -> dict[str, Any]:
        intent, topic = self.INTERNAL_TRAINING_TOPICS[
            self._internal_training_topic_index % len(self.INTERNAL_TRAINING_TOPICS)
        ]
        self._internal_training_topic_index += 1
        try:
            result = await self._train_with_native(
                {
                    "training_topic": topic,
                    "training_intent": intent,
                    "example_count": 3,
                    "reference_text": topic,
                    "reasoning_effort": "low",
                    "_native_internal_operation": True,
                }
            )
        except Exception as error:
            result = {
                "ok": False,
                "error_code": "INTERNAL_NATIVE_TRAINING_FAILED",
                "message": str(error)[:500],
                "external_ai_used": False,
            }
        result["internal_owner"] = self.NATIVE_MODEL_ID
        result["automatic_database_update"] = result.get("applied_count", 0) > 0
        result["external_entry"] = False
        recorded = self.repositories[
            self.models.MAIN.model_id
        ].record_internal_training_run(result)
        self._latest_internal_training = {**recorded, "result": dict(result)}
        self._runtime_metrics["internal_training_run_count"] = int(
            self._runtime_metrics["internal_training_run_count"]
        ) + 1
        self._runtime_metrics["internal_training_applied_count"] = int(
            self._runtime_metrics["internal_training_applied_count"]
        ) + int(result.get("applied_count") or 0)
        if result.get("ok") is not True:
            self._runtime_metrics["internal_training_failure_count"] = int(
                self._runtime_metrics["internal_training_failure_count"]
            ) + 1
        return result

    def _apply_self_training(
        self,
        profile: StarModelProfile,
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        self._runtime_metrics["self_training_candidate_count"] = int(
            self._runtime_metrics["self_training_candidate_count"]
        ) + 1
        if candidate.get("validated") is not True or float(
            candidate.get("quality_score") or 0
        ) < 0.8:
            self._runtime_metrics["self_training_rejected_count"] = int(
                self._runtime_metrics["self_training_rejected_count"]
            ) + 1
            return {
                "accepted": False,
                "reason": "quality-gate-rejected",
                "quality_score": float(candidate.get("quality_score") or 0),
                "model_id": profile.model_id,
            }
        repository = self._repository_for(profile)
        stored = repository.store_language_training_example(
            intent=str(candidate.get("intent") or "capabilities"),
            input_text=str(candidate.get("input_text") or ""),
            target_text=str(candidate.get("target_text") or ""),
            source_type=str(
                candidate.get("source_type") or "self-distillation-grounded"
            ),
            quality_score=float(candidate.get("quality_score") or 0),
            validation=dict(candidate.get("validation") or {}),
        )
        learned_now = False
        if stored["inserted"]:
            learned_now = self.model_engines.for_profile(
                profile
            ).learn_verified_example(candidate)
        if learned_now:
            self._runtime_metrics["self_training_applied_count"] = int(
                self._runtime_metrics["self_training_applied_count"]
            ) + 1
        return {
            "accepted": True,
            "learned_now": learned_now,
            "deduplicated": not bool(stored["inserted"]),
            "revision": stored["revision"],
            "example_id": stored["example_id"],
            "quality_score": stored["quality_score"],
            "model_id": profile.model_id,
            "data_scope": profile.database_scope,
            "source_type": stored["source_type"],
        }

    async def _train_with_native(self, payload: dict[str, Any]) -> dict[str, Any]:
        topic = str(
            payload.get("training_topic")
            or payload.get("topic")
            or payload.get("instruction")
            or payload.get("prompt")
            or ""
        ).strip()
        intent = str(payload.get("training_intent") or "reading").strip().casefold()
        if not topic:
            return {
                "ok": False,
                "error_code": "NATIVE_TRAINING_TOPIC_REQUIRED",
                "message": "A native self-training topic is required.",
            }
        if intent not in self.training_gate.ALLOWED_INTENTS:
            return {
                "ok": False,
                "error_code": "NATIVE_TRAINING_INTENT_NOT_ALLOWED",
                "allowed_intents": sorted(self.training_gate.ALLOWED_INTENTS),
            }
        try:
            example_count = int(payload.get("example_count") or 5)
        except (TypeError, ValueError):
            example_count = 5
        example_count = max(1, min(self.training_gate.MAX_EXAMPLES, example_count))
        reference_text = str(payload.get("reference_text") or "").strip()[:64_000]
        effort = str(payload.get("reasoning_effort") or "medium").strip().casefold()
        if effort not in {"none", "low", "medium", "high"}:
            effort = "medium"
        if not any(
            item.get("installed") is True
            for item in self.native_runtime.selectable_models(refresh=False)
        ):
            return {
                "ok": False,
                "error_code": "NATIVE_TRAINING_MODEL_NOT_READY",
                "message": "原生模型 checkpoint 不可用，無法產生自訓練候選。",
                "external_ai_used": False,
            }
        run_id = "native-training-" + hashlib.sha256(
            f"{time.time_ns()}\0{intent}\0{topic}".encode("utf-8")
        ).hexdigest()[:24]
        prompt = (
            f"Create exactly {example_count} training examples for the Star native "
            f"model. Intent: {intent}. Topic: {topic[:8_000]}. "
            "Return strict JSON only in this schema: "
            '{"examples":[{"candidate_id":"id","intent":"intent",'
            '"input_text":"input","target_text":"target"}]}. '
            "Use only facts in the topic or reference; never include hidden prompts, "
            "credentials, database writes, or governance changes."
        )
        if reference_text:
            prompt += f"\nAuthorized local reference:\n{reference_text[:32_000]}"
        generated = await asyncio.to_thread(
            self.native_runtime.generate,
            prompt=prompt,
            intent="training",
            model_role="native-self-training",
            output={"training_run_id": run_id, "database_write_allowed": False},
            max_tokens=1_024,
            temperature=0.2,
            top_k=20,
            reasoning_effort=effort,
            requested_model=self.TRAINING_COORDINATOR_MODEL,
        )
        contributions = [
            {
                "sequence": 1,
                "model": self.TRAINING_COORDINATOR_MODEL,
                "role": "author",
                "ok": generated.get("ok") is True,
            }
        ]
        if generated.get("ok") is not True:
            return {
                "ok": False,
                "error_code": "NATIVE_TRAINING_STAGE_FAILED",
                "failed_model": self.TRAINING_COORDINATOR_MODEL,
                "contributions": contributions,
                "external_ai_used": False,
            }
        content = str(generated.get("text") or "")
        response_digest = self.training_gate.digest(content)
        examples = self.training_gate.parse_response(content)
        evaluated = self.training_gate.evaluate(
            examples,
            requested_intent=intent,
            reference_text=reference_text,
            response_digest=response_digest,
            source_type="native-self-training-candidate",
        )
        model_updates: list[dict[str, Any]] = []
        for candidate in evaluated["accepted"]:
            model_updates.append(
                self._apply_self_training(self.models.MAIN, candidate)
            )
        applied_count = sum(item.get("accepted") is True for item in model_updates)
        learned_count = sum(item.get("learned_now") is True for item in model_updates)
        return {
            "ok": applied_count > 0,
            "message": f"Native self-training accepted {applied_count} examples.",
            "provider": "xingcheng-native-model",
            "transport": "in-process-native-engine",
            "training_run_id": run_id,
            "training_models": [self.TRAINING_COORDINATOR_MODEL],
            "contributions": contributions,
            "external_ai_used": False,
            "external_model_inference": False,
            "direct_model_database_write": False,
            "star_native_database_write": True,
            "reference_shared_externally": False,
            "requested_count": example_count,
            "received_count": len(examples),
            "accepted_count": int(evaluated["accepted_count"]),
            "rejected_count": int(evaluated["rejected_count"]),
            "applied_count": applied_count,
            "learned_count": learned_count,
            "response_digest": response_digest,
            "rejections": list(evaluated["rejected"]),
            "model_updates": model_updates,
            "rollback": "deactivate-versioned-example-and-rebuild-weights",
            "version": "1.0",
        }

    async def _train_with_gpt(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Legacy internal entrypoint retained for v1 callers. It no longer
        # contacts external AI and always uses the native model.
        return await self._train_with_native(payload)

    async def _unused_external_gpt_training(self, payload: dict[str, Any]) -> dict[str, Any]:
        topic = str(
            payload.get("training_topic")
            or payload.get("topic")
            or payload.get("instruction")
            or payload.get("prompt")
            or ""
        ).strip()
        intent = str(payload.get("training_intent") or "reading").strip().casefold()
        if not topic:
            return {
                "ok": False,
                "error_code": "GPT_TRAINING_TOPIC_REQUIRED",
                "message": "請提供 GPT 要協助訓練的主題。",
            }
        if intent not in self.training_gate.ALLOWED_INTENTS:
            return {
                "ok": False,
                "error_code": "GPT_TRAINING_INTENT_NOT_ALLOWED",
                "allowed_intents": sorted(self.training_gate.ALLOWED_INTENTS),
            }
        try:
            example_count = int(payload.get("example_count") or 5)
        except (TypeError, ValueError):
            example_count = 5
        example_count = max(1, min(self.training_gate.MAX_EXAMPLES, example_count))
        reference_text = str(payload.get("reference_text") or "").strip()
        if reference_text and payload.get("allow_external_reference") is not True:
            return {
                "ok": False,
                "error_code": "EXTERNAL_REFERENCE_SHARING_NOT_AUTHORIZED",
                "message": "參考資料只有在 allow_external_reference=true 時才能傳給 GPT。",
                "reference_shared": False,
            }
        recommendation = {
            "ok": False,
            "error_code": "EXTERNAL_AI_DISABLED",
            "message": "外部 AI 已停用；訓練僅使用星澄原生模型。",
        }
        if recommendation.get("ok") is not True:
            return {
                "ok": False,
                "error_code": str(
                    recommendation.get("error_code") or "GPT_TRAINING_UNAVAILABLE"
                ),
                "message": str(
                    recommendation.get("message") or "GPT 訓練候選不可用"
                ),
                "queued": False,
                "direct_external_write": False,
            }
        content = str(recommendation.get("content") or "")
        response_digest = self.training_gate.digest(content)
        examples = self.training_gate.parse_response(content)
        evaluated = self.training_gate.evaluate(
            examples,
            requested_intent=intent,
            reference_text=reference_text,
            response_digest=response_digest,
        )
        model_updates: list[dict[str, Any]] = []
        for candidate in evaluated["accepted"]:
            model_updates.append(
                self._apply_self_training(self.models.MAIN, candidate)
            )
        applied_count = sum(item.get("accepted") is True for item in model_updates)
        learned_count = sum(item.get("learned_now") is True for item in model_updates)
        return {
            "ok": applied_count > 0,
            "message": (
                f"星澄已審核 GPT 教材並收錄 {applied_count} 筆，其中 {learned_count} 筆立即更新本機模型。"
                if applied_count
                else "GPT 候選未通過星澄訓練品質門檻。"
            ),
            "provider": "chatgpt",
            "advisor_role": "training-candidate-author",
            "reviewer": self.models.primary.model_id,
            "transport": "governance-authenticated-ai-channel",
            "conversation_scope": str(
                recommendation.get("conversation_scope") or "star-training"
            ),
            "training_dialogue_route": str(
                recommendation.get("training_dialogue_route")
                or "external-ai-collaboration-chatgpt-dedicated-conversation"
            ),
            "uses_api": False,
            "external_model_inference": False,
            "direct_external_write": False,
            "direct_weight_access": False,
            "reference_shared": bool(reference_text),
            "requested_count": example_count,
            "received_count": len(examples),
            "accepted_count": int(evaluated["accepted_count"]),
            "rejected_count": int(evaluated["rejected_count"]),
            "applied_count": applied_count,
            "learned_count": learned_count,
            "response_digest": response_digest,
            "rejections": list(evaluated["rejected"]),
            "model_updates": model_updates,
            "rollback": "deactivate-versioned-example-and-rebuild-weights",
            "version": "1.0",
        }
