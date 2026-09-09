from __future__ import annotations

import asyncio
from typing import Any


class InferSpecialistsMixin:

    async def _infer_apply_specialists(
        self,
        output: dict[str, Any],
        inference_payload: dict[str, Any],
        profile: Any,
        planned_intent: str,
        native_model_requested: bool,
        prompt: str,
    ) -> None:
        if profile == self.models.MATHEMATICAL:
            output["mathematical_result"] = self.mathematical_expert.process(
                inference_payload, str(output.get("intent") or planned_intent)
            )
        if str(output.get("intent") or planned_intent) == "reading":
            reading_result = self.reading_expert.process(inference_payload)
            output["reading_result"] = reading_result
            if reading_result.get("ok") is True:
                reading_response = str(reading_result.get("response") or "")
                reading_token_count = len(
                    self.native_model.runtime.language_model.tokenize(reading_response)
                )
                output["response"] = reading_response
                generation = output.get("generation")
                if not isinstance(generation, dict):
                    generation = {}
                generation.update(
                    {
                        "text": reading_response,
                        "token_count": reading_token_count,
                        "facts_preserved": True,
                        "grounding": "supplied-document-citations",
                        "reading_extractive_grounding": True,
                    }
                )
                output["generation"] = generation
                output["evidence"] = list(reading_result.get("citations") or [])
                semantic_understanding = output.get("semantic_understanding")
                if isinstance(semantic_understanding, dict):
                    semantic_understanding["document_understanding"] = dict(
                        reading_result.get("metrics") or {}
                    )
                context_retrieval = output.get("context_retrieval")
                if isinstance(context_retrieval, dict):
                    context_retrieval["document_chunk_count"] = int(
                        (reading_result.get("metrics") or {}).get("chunk_count") or 0
                    )
                    context_retrieval["supplied_documents_only"] = True
                evidence_policy = output.get("evidence_policy")
                if isinstance(evidence_policy, dict):
                    evidence_policy["verbatim_citation_offsets_required"] = True
                    evidence_policy["unsupported_reading_answers_rejected"] = True
                evidence_sufficient = reading_result.get("evidence_sufficient") is True
                output["_training_candidate"] = {
                    "intent": "reading",
                    "input_text": prompt,
                    "target_text": reading_response,
                    "source_type": "source-attributed-reading",
                    "quality_score": 0.95,
                    "validated": bool(
                        evidence_sufficient and 8 <= reading_token_count <= 360
                    ),
                    "validation": {
                        **dict(reading_result.get("quality") or {}),
                        "evidence_sufficient": evidence_sufficient,
                        "bounded_output": 8 <= reading_token_count <= 360,
                    },
                }
            else:
                reading_message = str(
                    reading_result.get("message")
                    or "請提供需要閱讀的文件內容。"
                )
                output["response"] = reading_message
                generation = output.get("generation")
                if not isinstance(generation, dict):
                    generation = {}
                generation.update(
                    {
                        "text": reading_message,
                        "token_count": len(
                            self.native_model.runtime.language_model.tokenize(
                                reading_message
                            )
                        ),
                        "facts_preserved": True,
                        "reading_extractive_grounding": False,
                        "grounding_fallback_reason": "reading-content-required",
                    }
                )
                output["generation"] = generation
                output["_training_candidate"] = {
                    "intent": "reading",
                    "input_text": prompt,
                    "target_text": reading_message,
                    "source_type": "reading-input-required",
                    "quality_score": 0.0,
                    "validated": False,
                    "validation": {"reading_content_supplied": False},
                }
        if profile == self.models.CODING:
            active_intent = str(output.get("intent") or planned_intent)
            if active_intent == "self_upgrade" and not native_model_requested:
                output["self_repair"] = await asyncio.to_thread(
                    self._execute_self_repair_command
                )
            coding_result = self.coding_expert.process(
                inference_payload, active_intent
            )
            proposal = coding_result.get("upgrade_proposal")
            if (
                not native_model_requested
                and isinstance(proposal, dict)
                and proposal.get("proposal_ready") is True
            ):
                target = proposal.get("target")
                stored_proposal = self._repository_for(
                    self.models.CODING
                ).store_code_upgrade_proposal(
                    target_path=str(
                        target.get("path") if isinstance(target, dict) else ""
                    ),
                    language=str(coding_result.get("language") or ""),
                    source_text=str(coding_result.get("source") or ""),
                    validation=dict(coding_result.get("validation") or {}),
                )
                proposal["persistence"] = stored_proposal
            output["coding_result"] = coding_result

    def _infer_instruction_execution(
        self,
        output: dict[str, Any],
        inference_payload: dict[str, Any],
        native_model_requested: bool,
        planned_intent: str,
    ) -> None:
        instruction_execution = output.get("instruction_execution")
        if isinstance(instruction_execution, dict):
            instruction_execution["coordinator_model"] = (
                self.NATIVE_MODEL_ID
                if native_model_requested
                else self.FINAL_COORDINATOR_MODEL
            )
            instruction_execution["assigned_model"] = (
                self.NATIVE_MODEL_ID
                if native_model_requested
                else self._ollama_model_for_intent(
                    str(output.get("intent") or planned_intent)
                )
            )
            instruction_execution["delegated"] = not native_model_requested
            intent = str(output.get("intent") or "")
            executed = True
            missing_inputs: list[str] = []
            if intent == "search" and not isinstance(output.get("market_research"), dict):
                executed = False
                missing_inputs.append("holdings")
            elif intent in {"analysis", "risk"} and output.get("analysis") is None:
                executed = False
                missing_inputs.append("holdings")
            elif intent == "statistics" and not (
                isinstance(output.get("mathematical_result"), dict)
                and "statistics" in output["mathematical_result"]
            ):
                executed = False
                missing_inputs.append("numbers")
            elif intent == "data_organization" and not (
                isinstance(output.get("mathematical_result"), dict)
                and "data_organization" in output["mathematical_result"]
            ):
                executed = False
                missing_inputs.append("records")
            elif intent == "calculation" and not (
                isinstance(output.get("mathematical_result"), dict)
                and "calculation" in output["mathematical_result"]
            ):
                executed = False
                missing_inputs.append("expression")
            elif intent == "self_upgrade":
                self_repair = output.get("self_repair")
                executed = bool(
                    isinstance(self_repair, dict)
                    and self_repair.get("executed") is True
                )
                if not executed:
                    missing_inputs.append("self-repair-execution")
            elif intent == "coding" and not (
                isinstance(output.get("coding_result"), dict)
                and output["coding_result"].get("ok") is True
            ):
                executed = False
                missing_inputs.append("valid-code-spec")
            elif intent == "reading" and not (
                isinstance(output.get("reading_result"), dict)
                and output["reading_result"].get("ok") is True
            ):
                executed = False
                missing_inputs.append("document-text-or-documents")
            elif intent == "visual" and not any(
                isinstance(inference_payload.get(key), list)
                and bool(inference_payload.get(key))
                for key in ("images", "video_frames", "document_images")
            ):
                executed = False
                missing_inputs.append(
                    "images-or-video-frames-or-document-images"
                )
            instruction_execution["executed"] = executed
            instruction_execution["status"] = "completed" if executed else "input-required"
            instruction_execution["missing_inputs"] = missing_inputs
