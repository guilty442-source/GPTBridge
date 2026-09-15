from __future__ import annotations

from typing import Any, Mapping


class TransformerRuntimePipelineMixin:
    """Multi-model governed pipeline execution for generate()."""

    def _generate_pipeline(
        self, request: Mapping[str, Any], *,
        route: Mapping[str, Any], model_catalog: Mapping[str, Any],
    ) -> dict[str, Any]:
        pipeline_kind = (
            "complex"
            if request["complex_pipeline"]
            else "reasoning" if request["reasoning_pipeline"] else "division"
        )
        pipeline = self._pipeline_for_task(
            intent=str(request["intent"]),
            reasoning_effort=route["normalized_reasoning_effort"],
            complex_pipeline=bool(request["complex_pipeline"]),
            reasoning_pipeline=bool(request["reasoning_pipeline"]),
            division_pipeline=bool(request["division_pipeline"]),
            available_models=set(model_catalog),
        )
        missing_models = [
            model for _, model in pipeline if model not in model_catalog
        ]
        if missing_models:
            return {
                "ok": False,
                "error_code": "TRANSFORMER_PIPELINE_NOT_READY",
                "message": (
                    f"{pipeline_kind} pipeline has an unavailable fixed role owner"
                ),
                "missing_models": missing_models,
                "fallback_required": False,
            }
        (
            checkpoint_repository,
            checkpoint_run,
            checkpointed_stages,
        ) = self._pipeline_checkpoint_start(
            request=request,
            route=route,
            pipeline_kind=pipeline_kind,
            pipeline=pipeline,
        )
        return self._pipeline_run_stages(
            request, route=route, pipeline_kind=pipeline_kind,
            pipeline=pipeline, missing_models=missing_models,
            model_catalog=model_catalog,
            checkpoint_repository=checkpoint_repository,
            checkpoint_run=checkpoint_run,
            checkpointed_stages=checkpointed_stages,
        )

    def _pipeline_checkpoint_start(
        self, *, request: Mapping[str, Any], route: Mapping[str, Any],
        pipeline_kind: str, pipeline: list[tuple[str, str]],
    ) -> tuple[Any, dict[str, Any] | None, dict[int, dict[str, Any]]]:
        checkpoint_repository = self._checkpoint_repository
        checkpoint_run: dict[str, Any] | None = None
        checkpointed_stages: dict[int, dict[str, Any]] = {}
        if checkpoint_repository is not None:
            checkpoint_run = checkpoint_repository.start_or_resume(
                pipeline_kind=pipeline_kind,
                request={
                    "prompt": request["prompt"],
                    "intent": request["intent"],
                    "model_role": request["model_role"],
                    "output": dict(request["output"]),
                    "max_tokens": request["max_tokens"],
                    "temperature": request["temperature"],
                    "top_k": request["top_k"],
                    "reasoning_effort": route["normalized_reasoning_effort"],
                    "task_intensity": route["normalized_task_intensity"],
                    "pipeline": pipeline,
                },
            )
            checkpointed_stages = {
                int(item["stage_index"]): item
                for item in checkpoint_run.get("stages", [])
                if isinstance(item, Mapping)
            }
        return checkpoint_repository, checkpoint_run, checkpointed_stages

    def _pipeline_run_stages(
        self, request: Mapping[str, Any], *, route: Mapping[str, Any],
        pipeline_kind: str, pipeline: list[tuple[str, str]],
        missing_models: list[str], model_catalog: Mapping[str, Any],
        checkpoint_repository: Any, checkpoint_run: dict[str, Any] | None,
        checkpointed_stages: Mapping[int, Any],
    ) -> dict[str, Any]:
        run = {
            "pipeline_effort": route["normalized_reasoning_effort"],
            "model_catalog": model_catalog,
            "checkpointed_stages": checkpointed_stages,
            "checkpoint_repository": checkpoint_repository,
            "checkpoint_run": checkpoint_run,
            "cancel_event": request["cancel_event"],
            "progress_callback": request["progress_callback"],
        }
        stage_request = {
            "prompt": request["prompt"], "intent": request["intent"],
            "model_role": request["model_role"], "output": request["output"],
            "max_tokens": request["max_tokens"],
            "temperature": request["temperature"], "top_k": request["top_k"],
            "normalized_task_intensity": route["normalized_task_intensity"],
            "visual_inputs": route["visual_inputs"],
            "complex_pipeline": request["complex_pipeline"],
            "division_pipeline": request["division_pipeline"],
        }
        stage_texts: list[str] = []
        stage_audits: list[dict[str, Any]] = []
        for index, (stage, model) in enumerate(pipeline, start=1):
            outcome = self._pipeline_stage(
                index=index, stage=stage, model=model,
                stage_count=len(pipeline), request=stage_request,
                run=run, stage_texts=stage_texts,
            )
            stage_result = outcome["stage_result"]
            stage_audits.append(outcome["audit"])
            if stage_result.get("ok") is not True:
                return self._pipeline_failure_result(
                    stage_result=stage_result, stage=stage,
                    failure_adjudication=outcome["failure_adjudication"],
                    pipeline_kind=pipeline_kind, stage_audits=stage_audits,
                    checkpoint_run=checkpoint_run,
                )
            stage_texts.append(str(stage_result.get("text") or ""))
        return self._pipeline_final_result(
            stage_result=stage_result, pipeline_kind=pipeline_kind,
            stage_audits=stage_audits, missing_models=missing_models,
            checkpoint_repository=checkpoint_repository,
            checkpoint_run=checkpoint_run,
        )

    def _pipeline_stage(
        self, *, index: int, stage: str, model: str, stage_count: int,
        request: Mapping[str, Any], run: Mapping[str, Any],
        stage_texts: list[str],
    ) -> dict[str, Any]:
        is_final = index == stage_count
        effort = (
            "low"
            if (request["complex_pipeline"] or request["division_pipeline"])
            and index == 1
            else run["pipeline_effort"]
        )
        stage_prompt = self._pipeline_stage_prompt(
            stage=stage, prompt=str(request["prompt"]),
            prior="\n\n".join(stage_texts), is_final=is_final,
        )
        (
            stage_result,
            used_model,
            attempts,
            checkpoint_restored,
        ) = self._pipeline_stage_attempt(
            index=index, stage=stage, model=model, is_final=is_final,
            effort=effort, stage_prompt=stage_prompt, request=request, run=run,
        )
        failure_adjudication: dict[str, Any] = {}
        if not checkpoint_restored and stage_result.get("ok") is not True:
            stage_result, used_model, failure_adjudication = (
                self._pipeline_stage_adjudicate(
                    stage=stage, model=model, is_final=is_final, effort=effort,
                    stage_prompt=stage_prompt, request=request, run=run,
                    stage_result=stage_result, attempts=attempts,
                )
            )
        audit = self._pipeline_stage_record(
            index=index, stage=stage, model=model, used_model=used_model,
            is_final=is_final, stage_prompt=stage_prompt,
            stage_result=stage_result, attempts=attempts,
            failure_adjudication=failure_adjudication,
            checkpoint_restored=checkpoint_restored, run=run,
        )
        return {
            "stage_result": stage_result,
            "audit": audit,
            "failure_adjudication": failure_adjudication,
        }

    def _pipeline_stage_attempt(
        self, *, index: int, stage: str, model: str, is_final: bool,
        effort: str, stage_prompt: str, request: Mapping[str, Any],
        run: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str, list[dict[str, Any]], bool]:
        restored = run["checkpointed_stages"].get(index)
        checkpoint_restored = bool(
            restored
            and restored.get("completed") is True
            and restored.get("stage_name") == stage
        )
        if checkpoint_restored:
            stage_result = dict(restored.get("result") or {})
            used_model = str(restored.get("model") or model)
            attempts = [
                {
                    "model": used_model,
                    "ok": True,
                    "error_code": "",
                    "checkpoint_restored": True,
                }
            ]
            return stage_result, used_model, attempts, True
        stage_result = self._pipeline_stage_generate(
            stage=stage, model=model, is_final=is_final, effort=effort,
            stage_prompt=stage_prompt, request=request, run=run,
        )
        attempts = [
            {
                "model": model,
                "ok": stage_result.get("ok") is True,
                "error_code": stage_result.get("error_code"),
            }
        ]
        return stage_result, model, attempts, False

    def _pipeline_stage_generate(
        self, *, stage: str, model: str, is_final: bool, effort: str,
        stage_prompt: str, request: Mapping[str, Any], run: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.generate(
            prompt=stage_prompt,
            intent=self._pipeline_stage_intent(
                str(request["intent"]), stage, is_final
            ),
            model_role=str(request["model_role"]),
            output=request["output"],
            max_tokens=request["max_tokens"],
            temperature=request["temperature"],
            top_k=request["top_k"],
            reasoning_effort=effort,
            task_intensity=request["normalized_task_intensity"],
            requested_model=model,
            images=self._pipeline_stage_images(
                stage, request["visual_inputs"]
            ),
            complex_pipeline=False,
            reasoning_pipeline=False,
            division_pipeline=False,
            cancel_event=run["cancel_event"],
            progress_callback=(run["progress_callback"] if is_final else None),
            _release_after_generate=not (
                is_final and model in self.RESIDENT_MODELS
            ),
            _automatic_model_override=True,
        )

    def _pipeline_stage_adjudicate(
        self, *, stage: str, model: str, is_final: bool, effort: str,
        stage_prompt: str, request: Mapping[str, Any], run: Mapping[str, Any],
        stage_result: dict[str, Any], attempts: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        failure_adjudication = self._commander_adjudicate_model_failure(
            failed_model=model,
            failure=stage_result,
            intent=str(request["intent"]),
            task_intensity=str(request["normalized_task_intensity"]),
            model_catalog=run["model_catalog"],
        )
        assigned_model = str(
            failure_adjudication.get("assigned_model") or ""
        )
        used_model = model
        if assigned_model:
            stage_result = self._pipeline_reassigned_generate(
                stage=stage, model=assigned_model, is_final=is_final,
                effort=effort, stage_prompt=stage_prompt, request=request,
                run=run,
            )
            used_model = assigned_model
            attempts.append(
                {
                    "model": assigned_model,
                    "ok": stage_result.get("ok") is True,
                    "error_code": stage_result.get("error_code"),
                    "assigned_by": self.FAILURE_ADJUDICATOR_MODEL,
                }
            )
        return stage_result, used_model, failure_adjudication

    def _pipeline_reassigned_generate(
        self, *, stage: str, model: str, is_final: bool, effort: str,
        stage_prompt: str, request: Mapping[str, Any], run: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.generate(
            prompt=stage_prompt,
            intent=self._pipeline_stage_intent(
                str(request["intent"]), stage, is_final
            ),
            model_role=f"commander-reassigned-stage:{stage}",
            output=request["output"],
            max_tokens=request["max_tokens"],
            temperature=request["temperature"],
            top_k=request["top_k"],
            reasoning_effort=effort,
            task_intensity=request["normalized_task_intensity"],
            requested_model=model,
            images=self._pipeline_stage_images(
                stage, request["visual_inputs"]
            ),
            complex_pipeline=False,
            reasoning_pipeline=False,
            division_pipeline=False,
            cancel_event=run["cancel_event"],
            progress_callback=(run["progress_callback"] if is_final else None),
            _release_after_generate=True,
            _automatic_model_override=True,
        )

    def _pipeline_stage_record(
        self, *, index: int, stage: str, model: str, used_model: str,
        is_final: bool, stage_prompt: str, stage_result: dict[str, Any],
        attempts: list[dict[str, Any]], checkpoint_restored: bool,
        failure_adjudication: dict[str, Any], run: Mapping[str, Any],
    ) -> dict[str, Any]:
        if (
            not checkpoint_restored
            and run["checkpoint_repository"] is not None
            and run["checkpoint_run"] is not None
        ):
            run["checkpoint_repository"].record_stage(
                run_id=str(run["checkpoint_run"]["run_id"]),
                stage_index=index,
                stage_name=stage,
                model=used_model,
                prompt=stage_prompt,
                result=stage_result,
            )
        return {
            "sequence": index,
            "stage": stage,
            "primary_model": model,
            "model": used_model,
            "attempts": attempts,
            "failure_adjudication": failure_adjudication,
            "ok": stage_result.get("ok") is True,
            "checkpoint_restored": checkpoint_restored,
            "released_after_stage": not (
                is_final and used_model in self.RESIDENT_MODELS
            ),
        }

    @staticmethod
    def _pipeline_stage_prompt(
        *, stage: str, prompt: str, prior: str, is_final: bool
    ) -> str:
        stage_prompt = (
            f"Original task:\n{prompt}\n\n"
            f"Pipeline stage: {stage}.\n"
            "Use only facts supplied by the original task or governed context. "
            "Do not claim that tools were executed."
        )
        if stage == "understand-traditional-chinese-context-intent-and-parameters":
            stage_prompt += (
                " Preserve the original Traditional Chinese and prioritize Taiwan "
                "Chinese vocabulary, colloquial expressions, ellipsis, likely typos, "
                "and mixed Chinese-English identifiers. Do not translate the command "
                "to English before identifying intent, actions, objects, parameters, "
                "constraints, context references, and ambiguity. If a destructive "
                "operation remains ambiguous after context completion, require safe "
                "confirmation and do not prepare execution."
            )
        if prior:
            stage_prompt += f"\n\nPrior governed stage output:\n{prior}"
        if not is_final:
            stage_prompt += "\nReturn a concise internal handoff for the next model."
        else:
            stage_prompt += (
                "\nReturn only the verified final answer for the user; do not expose "
                "the internal pipeline or hidden reasoning."
            )
        return stage_prompt

    @staticmethod
    def _pipeline_stage_intent(intent: str, stage: str, is_final: bool) -> str:
        if (
            is_final
            or stage == "recognize-classify-tag-and-summarize-visual-files"
        ):
            return intent
        return "conversation"

    @staticmethod
    def _pipeline_stage_images(stage: str, visual_inputs: list[str]) -> list[str]:
        if stage == "recognize-classify-tag-and-summarize-visual-files":
            return visual_inputs
        return []

    @staticmethod
    def _pipeline_failure_result(
        *, stage_result: dict[str, Any], stage: str,
        failure_adjudication: dict[str, Any], pipeline_kind: str,
        stage_audits: list[dict[str, Any]],
        checkpoint_run: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            **stage_result,
            "error_code": "TRANSFORMER_PIPELINE_STAGE_FAILED",
            "failed_stage": stage,
            "failure_adjudication": failure_adjudication,
            f"{pipeline_kind}_pipeline": {
                "executed": False,
                "stages": stage_audits,
                "checkpoint_persisted": checkpoint_run is not None,
                "checkpoint_run_id": (
                    str(checkpoint_run["run_id"])
                    if checkpoint_run is not None
                    else ""
                ),
            },
        }

    def _pipeline_final_result(
        self, *, stage_result: dict[str, Any], pipeline_kind: str,
        stage_audits: list[dict[str, Any]], missing_models: list[str],
        checkpoint_repository: Any, checkpoint_run: dict[str, Any] | None,
    ) -> dict[str, Any]:
        final_result = dict(stage_result)
        final_result["model_selected_by_user"] = False
        if checkpoint_repository is not None and checkpoint_run is not None:
            checkpoint_repository.complete(str(checkpoint_run["run_id"]))
        final_result[f"{pipeline_kind}_pipeline"] = {
            "executed": True,
            "stages": stage_audits,
            "skipped_missing_models": missing_models,
            "optimization": "minimum-specialist-stages-for-requested-effort",
            "resource_policy": "bounded-parallel-independent-branches-and-sequential-dependent-handoffs",
            "maximum_concurrent_transformers": self.MAX_CONCURRENT_TRANSFORMERS,
            "parallel_policy": "independent-subtasks-only",
            "checkpoint_resumed": bool(
                checkpoint_run and checkpoint_run.get("resumed")
            ),
            "temporary_checkpoint_cleared": checkpoint_run is not None,
        }
        return final_result
