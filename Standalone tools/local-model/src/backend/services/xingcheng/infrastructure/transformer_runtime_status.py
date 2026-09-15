from __future__ import annotations

import time
import urllib.error
from typing import Any, Mapping


class TransformerRuntimeStatusMixin:
    """Probe, catalog records and governed status reporting."""

    def probe(self, *, refresh: bool = True) -> dict[str, Any]:
        if not self.enabled:
            return dict(self._status)
        now = time.monotonic()
        if not refresh:
            last = self._status.get("last_probed_at")
            if last and (now - float(last)) < self._probe_cache_ttl:
                return dict(self._status)
        started = time.perf_counter()
        try:
            tags = self._transport("GET", f"{self.endpoint}/api/tags", None, 3.0)
            version = self._transport("GET", f"{self.endpoint}/api/version", None, 3.0)
            models = tags.get("models") if isinstance(tags.get("models"), list) else []
            selectable_models = self._selectable_model_records(models)
            installed_names = {
                str(item.get("name") or item.get("model") or "").strip()
                for item in models
                if isinstance(item, Mapping)
            }
            default_model_installed = any(
                item["name"] == self.model for item in selectable_models
            )
            installed = bool(selectable_models)
            status = {
                **self._status,
                "available": True,
                "model_installed": installed,
                "default_model_installed": default_model_installed,
                "ollama_version": str(version.get("version") or ""),
                "last_error": "" if installed else "no-supported-model-installed",
                "selectable_models": selectable_models,
                "embedding_model": self.EMBEDDING_MODEL,
                "embedding_model_installed": self.EMBEDDING_MODEL in installed_names,
            }
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as error:
            status = {
                **self._status,
                "available": False,
                "model_installed": False,
                "last_error": str(error)[:500],
            }
        status["last_probe_latency_ms"] = round(
            (time.perf_counter() - started) * 1_000, 3
        )
        status["last_probed_at"] = time.monotonic()
        with self._lock:
            self._status = status
        return dict(status)

    @classmethod
    def _selectable_model_records(
        cls, models: list[Any]
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for item in models:
            record = cls._selectable_model_record(item)
            if record is not None:
                records.append(record)
        return sorted(records, key=lambda record: (not record["default"], record["name"]))

    @classmethod
    def _selectable_model_record(cls, item: Any) -> dict[str, Any] | None:
        if not isinstance(item, Mapping):
            return None
        name = str(item.get("name") or item.get("model") or "").strip()
        if (
            cls.MODEL_NAME_PATTERN.fullmatch(name) is None
            or name in cls.NON_GENERATIVE_MODELS
            or name.startswith("qwen3-embedding:")
            or "reranker" in name.casefold()
        ):
            return None
        details = item.get("details")
        details = details if isinstance(details, Mapping) else {}
        known = cls.KNOWN_MODEL_METADATA.get(name, {})
        record = cls._model_record_base_fields(name, item, details, known)
        record.update(cls._model_record_role_fields(name))
        record.update(
            {
                "residency": str(known.get("residency") or "non-resident"),
                "daily_group": str(known.get("daily_group") or ""),
                "evaluation": dict(known.get("evaluation") or {}),
                "allowed_intents": list(known.get("allowed_intents") or []),
                "exclusive_scope": bool(known.get("allowed_intents")),
                "size_bytes": int(item.get("size") or 0),
                "default": name == cls.MODEL,
                "installed": True,
            }
        )
        return record

    @classmethod
    def _model_record_base_fields(
        cls,
        name: str,
        item: Mapping[str, Any],
        details: Mapping[str, Any],
        known: Mapping[str, Any],
    ) -> dict[str, Any]:
        parameter_count = str(
            known.get("parameter_count") or details.get("parameter_size") or "unknown"
        )
        return {
            "name": name,
            "label": str(known.get("label") or name),
            "family": str(
                known.get("family") or details.get("family") or "unknown"
            ),
            "parameter_class": str(
                known.get("parameter_class") or parameter_count
            ),
            "parameter_count": parameter_count,
            "quantization": str(
                known.get("quantization")
                or details.get("quantization_level")
                or "unknown"
            ),
            "architecture": str(
                known.get("architecture") or "decoder-only-transformer"
            ),
            "context_window": int(
                known.get("context_window") or cls.CONTEXT_WINDOW
            ),
            "license": str(known.get("license") or "model-specific-license"),
            "usage_class": str(known.get("usage_class") or "other"),
        }

    @classmethod
    def _model_record_role_fields(cls, name: str) -> dict[str, Any]:
        assignment = cls.MODEL_ROLE_ASSIGNMENTS.get(name, {})
        positioning = cls.MODEL_RUNTIME_POSITIONING.get(name, {})
        return {
            "primary_responsibility": str(
                assignment.get("primary_responsibility") or ""
            ),
            "secondary_responsibilities": list(
                assignment.get("secondary_responsibilities") or []
            ),
            "task_levels": list(assignment.get("task_levels") or []),
            "task_level_labels": [
                cls.TASK_LEVEL_LABELS.get(str(level), str(level))
                for level in assignment.get("task_levels") or []
            ],
            "execution_speed": str(positioning.get("execution_speed") or ""),
            "execution_speed_label": cls.EXECUTION_SPEED_LABELS.get(
                str(positioning.get("execution_speed") or ""), ""
            ),
            "reasoning_intensity": str(
                positioning.get("reasoning_intensity") or ""
            ),
            "reasoning_intensity_label": cls.REASONING_INTENSITY_LABELS.get(
                str(positioning.get("reasoning_intensity") or ""), ""
            ),
        }

    def selectable_models(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        status = self.probe(refresh=refresh)
        models = status.get("selectable_models")
        if not isinstance(models, list):
            return []
        return [dict(item) for item in models if isinstance(item, Mapping)]

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self._status,
                "model_family": self.MODEL_FAMILY,
                "parameter_class": self.PARAMETER_CLASS,
                "parameter_count": self.PARAMETER_COUNT,
                "quantization": self.QUANTIZATION,
                "architecture": self.ARCHITECTURE,
                "context_window": self.CONTEXT_WINDOW,
                "foundation_model_license": "Apache-2.0",
                "third_party_foundation_weights": True,
                "model_selection": "installed-local-models",
                "model_role_assignments": self._status_model_role_assignments(),
                "task_levels": dict(self.TASK_LEVEL_LABELS),
                "execution_speed_positions": dict(self.EXECUTION_SPEED_LABELS),
                "reasoning_intensity_positions": dict(
                    self.REASONING_INTENSITY_LABELS
                ),
                "primary_language": "zh-TW",
                "language_policy": "traditional-chinese-by-default-user-override-allowed",
                "routing_priority": self._status_routing_priority(),
                "residency_policy": self._status_residency_policy(),
                "adaptive_context": self._status_adaptive_context(),
                "automatic_model_routing": self._status_automatic_model_routing(),
                "complex_task_pipeline": self._status_complex_task_pipeline(),
                "reasoning_pipeline": self._status_reasoning_pipeline(),
                "division_of_labor_pipeline": self._status_division_pipeline(),
            }

    def _status_model_role_assignments(self) -> dict[str, Any]:
        return {
            model: {
                **dict(assignment),
                "task_level_labels": [
                    self.TASK_LEVEL_LABELS.get(str(level), str(level))
                    for level in assignment.get("task_levels") or []
                ],
                **dict(self.MODEL_RUNTIME_POSITIONING.get(model, {})),
                "execution_speed_label": self.EXECUTION_SPEED_LABELS.get(
                    str(
                        self.MODEL_RUNTIME_POSITIONING.get(model, {}).get(
                            "execution_speed"
                        )
                        or ""
                    ),
                    "",
                ),
                "reasoning_intensity_label": self.REASONING_INTENSITY_LABELS.get(
                    str(
                        self.MODEL_RUNTIME_POSITIONING.get(model, {}).get(
                            "reasoning_intensity"
                        )
                        or ""
                    ),
                    "",
                ),
            }
            for model, assignment in self.MODEL_ROLE_ASSIGNMENTS.items()
        }

    def _status_routing_priority(self) -> dict[str, Any]:
        return {
            "policy": "speed-then-reasoning-depth-then-capability-strength",
            "project_scope": "all-project-source-excluding-governance-rule",
            "fast_daily": [
                "glm4:9b",
                "nemotron-3-nano:4b-q8_0",
            ],
            "command_understanding": [self.COMMAND_UNDERSTANDING_MODEL],
            "workflow_frontend": [self.FRONTEND_WORKER_MODEL],
            "visual_file_recognition": [
                self.VISUAL_FILE_MANAGEMENT_MODEL
            ],
            "fast_coding_and_command_execution": [
                "granite-code:3b",
            ],
            "capability_composition": [
                "gemma4:26b-a4b-it-qat",
            ],
            "calculation": [
                "deepseek-r1:14b",
            ],
            "autonomous_agent_and_command_execution": [
                "nemotron-3.5-lightning:30b-a3b-q4_K_M",
            ],
            "embedding_search": [self.EMBEDDING_MODEL],
            "star_native_model_included": False,
            "external_ai_used": False,
        }

    def _status_residency_policy(self) -> dict[str, Any]:
        return {
            "resident": sorted(self.RESIDENT_MODELS),
            "non_resident": sorted(
                set(self.KNOWN_MODEL_METADATA) - set(self.RESIDENT_MODELS)
            ),
            "unknown_installed_models": "non-resident",
            "resident_evicted_before_non_resident": False,
            "pipeline_release_after_each_stage": True,
            "maximum_concurrent_transformers": self.MAX_CONCURRENT_TRANSFORMERS,
            "commander_maximum_parallel": self.COMMANDER_MAX_PARALLEL,
            "commander_scaling_policy": "low-load-resident-dynamic-upgrade",
            "parallel_policy": "independent-subtasks-only",
            "resident_active_context": self.RESIDENT_CONTEXT_WINDOW,
            "non_resident_safe_start_context": self.SAFE_CONTEXT_WINDOW,
            "non_resident_maximum_context": self.NON_RESIDENT_CONTEXT_WINDOW,
            "full_project_context": "embedding-retrieval-plus-bounded-model-context",
        }

    def _status_adaptive_context(self) -> dict[str, Any]:
        return {
            "automatic": True,
            "safe_start": self.SAFE_CONTEXT_WINDOW,
            "minimum": self.MIN_CONTEXT_WINDOW,
            "maximum": self.NON_RESIDENT_CONTEXT_WINDOW,
            "steps": list(self.CONTEXT_WINDOW_STEPS),
            "growth_success_threshold": self.CONTEXT_GROWTH_SUCCESS_THRESHOLD,
            "memory_pressure_retry": True,
            "content_truncation_allowed": False,
            "model_states": {
                model: dict(state)
                for model, state in self._context_states.items()
            },
            "checkpoint_store_enabled": self._checkpoint_repository is not None,
        }

    def _status_automatic_model_routing(self) -> dict[str, Any]:
        return {
            "policy": "fixed-primary-owner-no-preconfigured-backup-commander-dynamic-reassignment",
            "default_effort": "medium",
            "command_understanding": self.COMMAND_UNDERSTANDING_MODEL,
            "workflow_frontend_after_command_understanding": self.FRONTEND_WORKER_MODEL,
            "task_commander": self.TASK_ALLOCATION_MODEL,
            "integration_acceptance": self.INTEGRATION_MODEL,
            "failure_adjudicator": self.FAILURE_ADJUDICATOR_MODEL,
            "automatic_fallback": [],
            "commander_dynamic_reassignment": True,
            "maximum_dynamic_reassignments": 1,
            "conversation": "glm4:9b",
            "search": "mistral-small:24b",
            "visual": self.VISUAL_FILE_MANAGEMENT_MODEL,
            "fast_visual": "gemma4:e2b-it-qat",
            "multimodal": "gemma4:12b-it-qat",
            "advanced_multimodal": "gemma4:26b-a4b-it-qat",
            "visual_reasoning": "qwen3-vl:8b-thinking",
            "visual_model_scope": "visual-file-recognition-only",
            "command_understanding_language_priority": "traditional-chinese-taiwan-first",
            "translate_to_english_before_intent": True,
            "reading": "gemma4:12b-it-qat",
            "capability_composition": "gemma4:26b-a4b-it-qat",
            "calculation": "deepseek-r1:14b",
            "statistics": "deepseek-r1:14b",
            "coding": "qwen3.6:35b-a3b-coding",
            "command_execution": "qwen3.6:35b-a3b-coding",
            "autonomous_agent": "nemotron-3.5-lightning:30b-a3b-q4_K_M",
            "self_upgrade": "qwen3.6:35b-a3b-coding",
            "reasoning": "deepseek-r1:14b",
            "analysis": "deepseek-r1:14b",
            "risk": "deepseek-r1:14b",
            "data_organization": "ibm/granite4.2:30b-q4_K_M",
            "training": "local-ollama-ensemble",
            "embedding": self.EMBEDDING_MODEL,
            "default": "glm4:9b",
        }

    def _status_complex_task_pipeline(self) -> dict[str, Any]:
        return {
            "automatic": True,
            "sequence": [
                "qwen3.8-understand-command-for-all-tasks",
                "rnj-1-workflow-frontend",
                "classify-intensity-decompose-and-route-subtasks",
                "perform-ordered-role-work",
                "integrate-results-and-plan-governed-operation",
                "prepare-and-execute-governed-operation",
                "cross-validate-repair-or-escalate",
                "verify-and-produce-traditional-chinese-result",
            ],
            "authorities": {
                "understand": self.COMMAND_UNDERSTANDING_MODEL,
                "frontend_worker": self.FRONTEND_WORKER_MODEL,
                "allocate": self.TASK_ALLOCATION_MODEL,
                "integrate": self.INTEGRATION_MODEL,
                "execute": self.EXECUTION_MODEL,
                "inspect": self.INSPECTION_MODEL,
                "result": self.RESULT_MODEL,
                "model_failure_adjudication": self.FAILURE_ADJUDICATOR_MODEL,
            },
            "default_effort": "medium",
            "backup_policy": "no-preconfigured-backup",
            "failure_policy": "qwen3.8-may-dynamically-reassign-once",
            "star_native_model_included": False,
        }

    def _status_reasoning_pipeline(self) -> dict[str, Any]:
        return {
            "low": ["deepseek-r1:14b"],
            "medium": [
                "deepseek-r1:14b",
                "qwen3.8:27b-q4_K_M",
            ],
            "high": [
                "deepseek-r1:14b",
                "qwen3.8:27b-q4_K_M",
            ],
            "default_effort": "medium",
        }

    def _status_division_pipeline(self) -> dict[str, Any]:
        return {
            "primary_workflow": "traditional-chinese-first-governed-pipeline",
            "first_stage": "traditional-chinese-understanding-fixed-owner",
            "command_understanding_authority": self.COMMAND_UNDERSTANDING_MODEL,
            "search_data_command": [
                "ibm/granite4.2:30b-q4_K_M",
                "mistral-small:24b",
            ],
            "coding_and_execution_medium": [
                "qwen3-coder:30b-a3b-q4_K_M",
                "qwen3.6:35b-a3b-coding",
            ],
            "coding_and_execution_high": [
                "qwen3-coder:30b-a3b-q4_K_M",
                "qwen3.6:35b-a3b-coding",
            ],
            "handoff": "bounded-prior-stage-output",
            "manual_model_selection_bypasses_division": True,
        }
