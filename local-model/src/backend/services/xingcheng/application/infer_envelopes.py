from __future__ import annotations

from typing import Any


class InferEnvelopesMixin:

    def _infer_scheduling_envelopes(
        self,
        output: dict[str, Any],
        inference_payload: dict[str, Any],
        assigned_model: str,
        planned_intents: list[str],
    ) -> None:
        output["task_arrangement"] = {
            "mode": "traditional-chinese-first-governed-workflow",
            "task_allocation_model": self.GENERALIST_COORDINATOR_MODEL,
            "integration_model": self.FINAL_COORDINATOR_MODEL,
            "manual_assignment_allowed": False,
            "star_native_model_included": False,
            "external_ai_used": False,
            "project_scope": "all-project-source-excluding-governance-rule",
            "tasks": self._arrange_ollama_tasks(planned_intents),
        }
        scheduled_intensity = str(
            inference_payload.get("task_intensity") or "normal"
        ).strip().casefold()
        output["model_scheduling"] = {
            "automatic": True,
            "selected_model": assigned_model,
            "selection_dimensions": [
                "intent",
                "task_intensity",
                "reasoning_effort",
                "generation_speed",
                "installed_models",
                "residency",
            ],
            "rules_file": "config/繁體中文自動模式規則.json",
            "task_intensity": scheduled_intensity,
            "task_intensity_source": str(
                inference_payload.get("task_intensity_source")
                or "automatic-traditional-chinese-command-assessment"
            ),
            "policy": dict(
                self.transformer_runtime.TASK_INTENSITY_SCHEDULING.get(
                    scheduled_intensity,
                    self.transformer_runtime.TASK_INTENSITY_SCHEDULING["normal"],
                )
            ),
            "parallel_independent_subtasks": True,
            "sequential_dependent_stages": True,
            "model_escalation": "failure-only",
            "manual_model_selection_required": False,
        }

    def _infer_autonomous_agent_envelope(
        self,
        inference_payload: dict[str, Any],
        command_plan: dict[str, Any],
        planned_intents: list[str],
        command_understanding_model: str,
        command_understanding: dict[str, Any],
        autonomous_agent: bool,
    ) -> dict[str, Any]:
        return {
            "enabled": autonomous_agent,
            "star_native_model_included": False,
            "mode": "bounded-plan-execute-verify-recover",
            "project_scope": "all-project-source-excluding-governance-rule",
            "command_understood": bool(command_understanding.get("recognized")),
            "understanding_layer": "traditional-chinese-taiwan-first",
            "english_translation_before_intent": False,
            "semantic_plan_schema": "star-semantic-plan/v1",
            "task_classification": dict(
                command_plan.get("task_classification") or {}
            ),
            "task_intensity": dict(command_plan.get("task_intensity") or {}),
            "safety_gate": dict(command_plan.get("safety") or {}),
            "execution_requested": bool(
                command_understanding.get("execution_requested")
            ),
            "planned_intents": list(planned_intents),
            "planned_steps": self._arrange_ollama_tasks(planned_intents),
            "workflow_sequence": [
                *self.AUTOMATIC_WORKFLOW_SEQUENCE,
            ],
            "command_understanding_model": command_understanding_model,
            "planner_model": self.GENERALIST_COORDINATOR_MODEL,
            "integration_model": self.FINAL_COORDINATOR_MODEL,
            "executor_model": self.CODING_EXPERT_MODEL,
            "inspection_model": self.RELEASE_REVIEW_MODEL,
            "result_model": self.FINAL_COORDINATOR_MODEL,
            "backup_policy": "none",
            "failure_adjudicator": self.COMMAND_UNDERSTANDING_MODEL,
            "commander_dynamic_reassignment": True,
            "maximum_dynamic_reassignments": 1,
            "governance_checked": True,
            "governance_rule_mutable": False,
            "programming_project_root": str(
                inference_payload.get("programming_folder") or ""
            ),
            "programming_folder": str(
                inference_payload.get("programming_folder") or ""
            ),
            "outside_programming_scope_allowed": False,
            "database_write_performed": False,
            "status": "executing" if autonomous_agent else "disabled",
        }

    def _infer_user_command_envelope(
        self,
        inference_payload: dict[str, Any],
        planned_intent: str,
        prompt: str,
        command_understanding: dict[str, Any],
        command_plan: dict[str, Any],
        raw_command: str,
        assigned_model: str,
        command_understanding_model: str,
        autonomous_agent: bool,
    ) -> dict[str, Any]:
        return {
                "entry": "model-dialogue",
                "accepted": True,
                "command": str(inference_payload.get("user_command") or prompt)[:32_000],
                "intent": planned_intent,
                "understood": bool(command_understanding.get("recognized")),
                "understanding_layer": "traditional-chinese-taiwan-first",
                "normalized_command": str(
                    command_plan.get("normalized_input") or raw_command
                )[:32_000],
                "actions": list(command_plan.get("actions") or []),
                "operation_objects": list(
                    command_plan.get("operation_objects") or []
                ),
                "parameters": dict(command_plan.get("parameters") or {}),
                "constraints": list(
                    command_plan.get("specific_constraints") or []
                ),
                "context_completion": dict(
                    command_plan.get("context_completion") or {}
                ),
                "task_classification": dict(
                    command_plan.get("task_classification") or {}
                ),
                "task_intensity": dict(
                    command_plan.get("task_intensity") or {}
                ),
                "safety_gate": dict(command_plan.get("safety") or {}),
                "execution_requested": bool(
                    command_understanding.get("execution_requested")
                ),
                "assigned_model": assigned_model,
                "command_understanding_model": command_understanding_model,
                "planner_model": self.GENERALIST_COORDINATOR_MODEL,
                "integration_model": self.FINAL_COORDINATOR_MODEL,
                "executor_model": self.CODING_EXPERT_MODEL,
                "inspection_model": self.RELEASE_REVIEW_MODEL,
                "result_model": self.DATA_COORDINATOR_MODEL,
                "project_scope": "all-project-source-excluding-governance-rule",
                "governance_checked": True,
                "status": "executing" if autonomous_agent else "planned",
        }

    def _infer_propagate_execution_state(
        self,
        output: dict[str, Any],
    ) -> None:
        agent_state = output.get("autonomous_agent")
        if isinstance(agent_state, dict) and agent_state.get("enabled") is True:
            execution_state = output.get("instruction_execution")
            execution_status = (
                str(execution_state.get("status") or "completed")
                if isinstance(execution_state, dict)
                else "completed"
            )
            agent_state["status"] = execution_status
            agent_state["command_executed"] = bool(
                isinstance(execution_state, dict)
                and execution_state.get("executed") is True
            )
            agent_state["missing_inputs"] = (
                list(execution_state.get("missing_inputs") or [])
                if isinstance(execution_state, dict)
                else []
            )
        command_state = output.get("user_command")
        if isinstance(command_state, dict):
            execution_state = output.get("instruction_execution")
            command_state["status"] = (
                str(execution_state.get("status") or "completed")
                if isinstance(execution_state, dict)
                else "completed"
            )
            command_state["executed"] = bool(
                isinstance(execution_state, dict)
                and execution_state.get("executed") is True
            )
            command_state["missing_inputs"] = (
                list(execution_state.get("missing_inputs") or [])
                if isinstance(execution_state, dict)
                else []
            )
