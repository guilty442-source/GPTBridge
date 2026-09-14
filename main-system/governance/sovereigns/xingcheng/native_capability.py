"""Xingcheng Sovereign — Native Capability Module (A336).

Programming analysis/design/authoring/refactoring/debugging/verification/
migration/generated-code review are exclusive native-model capabilities.

Automation executor: whole-system automation coordination — decomposition,
scheduling, dispatch, tool orchestration, convergence, verification,
failure containment.
"""

from __future__ import annotations

import logging
from typing import Any

from .._base import SovereignBase, SovereignRequest, SovereignOutcome
from core_system.codex_decision import accepted_outcome, refusal_outcome

_logger = logging.getLogger("gptbridge.sovereign.xingcheng.native")

# A336: authorization modes for automation executor
_AUTOMATION_AUTHORIZATION_MODES = frozenset({"user-command", "codex-mandate"})


class XingchengNativeMixin:
    """Native-model capabilities + whole-system automation executor."""

    _program_tasks: dict[str, dict[str, Any]]
    _automation_tasks: dict[str, dict[str, Any]]
    _owned_domain_root: str

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._program_tasks = {}
        self._automation_tasks = {}

    def _automation_authorized(self, request: SovereignRequest) -> bool:
        """A336: automation requires explicit user command or codex mandate."""
        authorization = request.payload.get("authorization")
        if not isinstance(authorization, dict):
            return False
        mode = authorization.get("mode")
        return mode in _AUTOMATION_AUTHORIZATION_MODES and bool(authorization.get("reference"))

    async def adjudicate_native_capability(self, request: SovereignRequest) -> SovereignOutcome:
        """Route A336 native-model programming + automation intents."""
        intent = request.intent
        if intent.startswith("program."):
            return await self._adjudicate_program(request)
        return await self._adjudicate_automation(request)

    async def _adjudicate_program(self, request: SovereignRequest) -> SovereignOutcome:
        """A336 NATIVE-CAPABILITY: programming analysis/design/authoring/refactoring/
        debugging/verification/migration/generated-code review.
        """
        op = request.intent.split(".", 1)[1]
        output_path = request.payload.get("output_path")
        resolved_output = None
        if output_path:
            resolved_output = self._resolve_in_domain(str(output_path))
            if resolved_output is None:
                return refusal_outcome("CROSS_ROOT_MUTATION", self.verified_basis("A336"))
        task_id = f"program-{len(self._program_tasks) + 1}"
        task = {
            "task_id": task_id,
            "op": op,
            "subject": request.payload.get("subject") or request.subject,
            "spec": request.payload.get("spec"),
            "output_path": str(resolved_output) if resolved_output else None,
            "executor": "xingcheng-native-model",
            "status": "accepted",
            "created_at": self._iso_now(),
        }
        self._program_tasks[task_id] = task
        return accepted_outcome(
            {
                "action": f"program.{op}",
                "task": task,
                "executor": "xingcheng-native-model",
                "boundary": "no-permission-grant+no-routing-evidence-alteration+no-direct-channel-operation",
            },
            self.verified_basis("A336"),
        )

    async def _adjudicate_automation(self, request: SovereignRequest) -> SovereignOutcome:
        """A336 AUTOMATION-EXECUTOR: whole-system automation coordination."""
        if not self._automation_authorized(request):
            return refusal_outcome("AUTOMATION_AUTHORIZATION_REQUIRED", self.verified_basis("A336"))
        intent = request.intent
        if intent == "automation.decompose":
            return self._automation_decompose(request)
        if intent == "automation.schedule":
            return self._automation_schedule(request)
        if intent == "automation.dispatch":
            return self._automation_dispatch(request)
        if intent == "automation.converge":
            return self._automation_converge(request)
        if intent == "automation.verify-result":
            return self._automation_verify(request)
        return self._automation_contain(request)

    def _automation_decompose(self, request: SovereignRequest) -> SovereignOutcome:
        task_id = f"auto-{len(self._automation_tasks) + 1}"
        steps = request.payload.get("steps") or []
        self._automation_tasks[task_id] = {
            "objective": request.payload.get("objective") or request.subject,
            "steps": [{"step": s, "state": "pending"} for s in steps],
            "state": "decomposed",
            "created_at": self._iso_now(),
        }
        return accepted_outcome(
            {"task_id": task_id, "state": "decomposed", "steps": len(steps)},
            self.verified_basis("A336"),
        )

    def _automation_task_or_refusal(self, request: SovereignRequest):
        task_id = str(request.payload.get("task_id") or "")
        task = self._automation_tasks.get(task_id)
        if task is None:
            return refusal_outcome("UNKNOWN_AUTOMATION_TASK", self.verified_basis("A336"))
        return task

    def _automation_schedule(self, request: SovereignRequest) -> SovereignOutcome:
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task
        order = request.payload.get("order")
        if isinstance(order, list) and order:
            indexed = {i: step for i, step in enumerate(task["steps"])}
            task["schedule"] = [indexed[i] for i in order if i in indexed]
        else:
            task["schedule"] = list(task["steps"])
        task["state"] = "scheduled"
        return accepted_outcome(
            {
                "task_id": request.payload.get("task_id"),
                "state": "scheduled",
                "scheduled_steps": len(task["schedule"]),
            },
            self.verified_basis("A336"),
        )

    def _automation_dispatch(self, request: SovereignRequest) -> SovereignOutcome:
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task
        step_index = request.payload.get("step_index")
        steps = task.get("schedule") or task["steps"]
        if not isinstance(step_index, int) or step_index < 0 or step_index >= len(steps):
            return refusal_outcome("INVALID_STEP_INDEX", self.verified_basis("A336"))
        steps[step_index]["state"] = "dispatched"
        steps[step_index]["dispatched_at"] = self._iso_now()
        task["state"] = "in-progress"
        return accepted_outcome(
            {
                "task_id": request.payload.get("task_id"),
                "step_index": step_index,
                "handoff": "typed",
                "state": "dispatched",
            },
            self.verified_basis("A336"),
        )

    def _automation_converge(self, request: SovereignRequest) -> SovereignOutcome:
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task
        states = [s["state"] for s in task["steps"]]
        converged = bool(states) and all(s in ("converged", "success", "done") for s in states)
        task["state"] = "converged" if converged else task["state"]
        return accepted_outcome(
            {"task_id": request.payload.get("task_id"), "converged": converged, "step_states": states},
            self.verified_basis("A336"),
        )

    def _automation_verify(self, request: SovereignRequest) -> SovereignOutcome:
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task
        evidence = request.payload.get("evidence")
        if not evidence:
            return refusal_outcome("MISSING_RESULT_EVIDENCE", self.verified_basis("A336"))
        task["result_evidence"] = evidence
        task["state"] = "verified"
        task["verified_at"] = self._iso_now()
        return accepted_outcome(
            {"task_id": request.payload.get("task_id"), "state": "verified"},
            self.verified_basis("A336"),
        )

    def _automation_contain(self, request: SovereignRequest) -> SovereignOutcome:
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task
        step_index = request.payload.get("step_index")
        task["state"] = "contained"
        task["containment"] = {
            "step_index": step_index,
            "reason": request.payload.get("reason"),
            "contained_at": self._iso_now(),
        }
        return accepted_outcome(
            {"task_id": request.payload.get("task_id"), "state": "contained", "failure_propagation": "halted"},
            self.verified_basis("A336"),
        )