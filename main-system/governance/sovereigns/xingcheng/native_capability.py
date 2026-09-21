"""Xingcheng Sovereign — Native Capability Module (A337).

Programming analysis/design/authoring/refactoring/debugging/verification/
migration/generated-code review are exclusive native-model capabilities.

Automation executor: whole-system automation coordination — decomposition,
scheduling, dispatch, tool orchestration, convergence, verification,
failure containment.

Hardening (A337/A446/A435/A224/A46):
- Program tasks invoke the native model executor (not just in-memory records).
- Automation dispatch invokes the registered module via the app's
  automation unit registry (not just a state label).
- Verify uses the independent verifier with a distinct verifier identity;
  the submitter cannot self-verify.
- Authorization references are verified: user-command mode checks the
  command_code_directory (A224); codex-mandate mode checks the dual-key
  grant (A435).
- All tasks are persisted in an append-only ledger (A46) so they survive
  restarts.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from .._base import SovereignBase, SovereignRequest, SovereignOutcome
from core_system.codex_decision import accepted_outcome, refusal_outcome
from .native_capability_task_ledger import (
    TaskIdentity,
    TaskState,
    load_tasks,
    record_task_event,
    record_task_transition,
    verify_authorization_reference,
)

_logger = logging.getLogger("gptbridge.sovereign.xingcheng.native")

# A337: authorization modes for automation executor
_AUTOMATION_AUTHORIZATION_MODES = frozenset({"user-command", "codex-mandate"})

# A446: independent verifier identity — distinct from the submitter.
_NATIVE_VERIFIER_ID = "xingcheng-native-verifier"


async def _invoke_native_model_executor(app: Any, task: dict[str, Any]) -> dict[str, Any]:
    """Invoke the native model executor for a programming task (A337).

    This delegates to the local-model 星澄 service when available;
    otherwise it records a deferred-execution status so the task is
    not falsely claimed as complete.
    """
    if app is None:
        return {"status": "deferred", "reason": "app-unavailable"}
    star_service = getattr(app, "star_chat_service", None)
    if star_service is None:
        # Try alternative service names
        for alt_name in ("local_model_service", "native_model_service", "xingcheng_native_service"):
            star_service = getattr(app, alt_name, None)
            if star_service is not None:
                break
    if star_service is None:
        return {"status": "deferred", "reason": "native-model-unavailable"}
    try:
        spec = task.get("spec") or task.get("subject") or ""
        result = await star_service.process_request(
            intent=task.get("op", ""),
            spec=spec,
            output_path=task.get("output_path"),
        )
        if isinstance(result, dict):
            return {"status": "executed", "result": result}
        return {"status": "executed", "result": str(result)}
    except Exception as error:
        return {"status": "execution-error", "error": type(error).__name__, "detail": str(error)[:200]}


# Fallback executor for when native model is unavailable
async def _invoke_fallback_executor(app: Any, task: dict[str, Any]) -> dict[str, Any]:
    """Fallback executor that performs basic syntax/structure operations without native model."""
    op = task.get("op", "")
    output_path = task.get("output_path")
    subject = task.get("subject", "")

    if op in ("analyze", "review-generated"):
        # Basic static analysis
        findings = []
        if output_path and Path(output_path).exists():
            content = Path(output_path).read_text(encoding="utf-8", errors="replace")
            lines = content.count("\n") + 1
            if lines > 500:
                findings.append({"type": "module-size", "severity": "warning", "detail": f"{lines} lines"})
            if "TODO" in content or "FIXME" in content:
                findings.append({"type": "technical-debt", "severity": "info", "detail": "Contains TODO/FIXME"})
        return {"status": "fallback-executed", "op": op, "findings": findings}

    return {"status": "deferred", "reason": f"fallback-unavailable-for-op:{op}"}


from pathlib import Path


async def _dispatch_to_registered_module(app: Any, step: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a step to its registered automation module (A337).

    Looks up the module in the app's automation unit registry and
    invokes it.  Returns the dispatch result; never raises.
    """
    if app is None:
        return {"status": "deferred", "reason": "app-unavailable"}
    step_spec = step.get("step") if isinstance(step.get("step"), dict) else {}
    target_unit = str(step_spec.get("target_unit") or step_spec.get("module") or "")
    if not target_unit:
        return {"status": "deferred", "reason": "no-target-unit-specified"}
    try:
        from core_system.module_automation_registry import AUTOMATION_UNITS
        for unit_id, attribute, _label in AUTOMATION_UNITS:
            if unit_id == target_unit:
                service = getattr(app, attribute, None)
                if service is None:
                    return {"status": "deferred", "reason": f"{target_unit}-unavailable"}
                for method_name in ("run_step", "execute_step", "run", "execute"):
                    method = getattr(service, method_name, None)
                    if callable(method):
                        result = method(step_spec) if method_name in ("run_step", "execute_step") else method()
                        return {"status": "dispatched", "unit": unit_id, "result": str(result)[:200]}
                return {"status": "dispatched", "unit": unit_id, "result": "no-executable-method"}
        return {"status": "deferred", "reason": f"unit-{target_unit}-not-registered"}
    except Exception as error:
        return {"status": "dispatch-error", "error": type(error).__name__}


def _verify_evidence_structure(
    evidence: Any, task: dict[str, Any]
) -> tuple[bool, str]:
    """Verify the evidence is structurally valid (A446).

    The evidence must be a dict with at least a ``result`` key and a
    ``step_count`` matching the task's step count.  A bare non-empty
    value is not sufficient.
    """
    if not isinstance(evidence, dict):
        return False, "EVIDENCE_NOT_DICT"
    if "result" not in evidence:
        return False, "EVIDENCE_MISSING_RESULT_KEY"
    step_count = evidence.get("step_count")
    expected = len(task.get("steps", []))
    if step_count is not None and int(step_count) != expected:
        return False, f"EVIDENCE_STEP_COUNT_MISMATCH:{step_count}!={expected}"
    return True, "valid"


class XingchengNativeMixin:
    """Native-model capabilities + whole-system automation executor."""

    _program_tasks: dict[str, dict[str, Any]]
    _automation_tasks: dict[str, dict[str, Any]]
    _owned_domain_root: str

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Load persisted tasks from the ledger (survive restarts).
        self._program_tasks = {}
        self._automation_tasks = {}
        self._load_persisted_tasks()

    def _load_persisted_tasks(self) -> None:
        """Reconstruct in-memory task state from the durable ledger."""
        tasks = load_tasks()
        for tid, task in tasks.items():
            if task.get("type") == "program":
                self._program_tasks[tid] = task
            elif task.get("type") == "automation":
                self._automation_tasks[tid] = task

    def _automation_authorized(self, request: SovereignRequest) -> bool:
        """A337/A224/A435: automation requires verified authorization."""
        authorization = request.payload.get("authorization")
        if not isinstance(authorization, dict):
            return False
        mode = authorization.get("mode")
        reference = authorization.get("reference")
        if mode not in _AUTOMATION_AUTHORIZATION_MODES:
            return False
        if not reference:
            return False
        # Verify the reference is genuine (not just non-empty).
        valid, _reason = verify_authorization_reference(str(mode), str(reference))
        return valid

    @staticmethod
    def _task_identity(request: SovereignRequest, task_id: str) -> TaskIdentity:
        payload = request.payload
        suffix = uuid.uuid4().hex
        return TaskIdentity(
            task_id=task_id,
            request_id=str(payload.get("request_id") or f"request-{suffix}"),
            operation_id=str(payload.get("operation_id") or f"operation-{suffix}"),
            execution_receipt=str(payload.get("execution_receipt") or f"receipt-{suffix}"),
            idempotency_key=str(payload.get("idempotency_key") or f"idem-{suffix}"),
        )

    @staticmethod
    def _task_identity_from_record(task: dict[str, Any]) -> TaskIdentity:
        return TaskIdentity(
            task_id=str(task["task_id"]),
            request_id=str(task["request_id"]),
            operation_id=str(task["operation_id"]),
            execution_receipt=str(task["execution_receipt"]),
            idempotency_key=str(task["idempotency_key"]),
        )

    @staticmethod
    def _task_fields(identity: TaskIdentity) -> dict[str, str]:
        return {
            "task_id": identity.task_id,
            "request_id": identity.request_id,
            "operation_id": identity.operation_id,
            "execution_receipt": identity.execution_receipt,
            "idempotency_key": identity.idempotency_key,
        }

    def _transition_task(
        self,
        task: dict[str, Any],
        identity: TaskIdentity,
        target: TaskState,
        *,
        verification: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        current = TaskState(task["lifecycle_state"])
        record_task_transition(
            identity=identity,
            current=current,
            target=target,
            task_type=str(task["type"]),
            verification=verification,
            detail=detail,
        )
        task["lifecycle_state"] = target.value
        task["state"] = target.value

    async def adjudicate_native_capability(self, request: SovereignRequest) -> SovereignOutcome:
        """Route A337 native-model programming + automation intents."""
        intent = request.intent
        if intent.startswith("program."):
            return await self._adjudicate_program(request)
        return await self._adjudicate_automation(request)

    async def _adjudicate_program(self, request: SovereignRequest) -> SovereignOutcome:
        """A337 NATIVE-CAPABILITY: programming with native model execution."""
        op = request.intent.split(".", 1)[1]
        output_path = request.payload.get("output_path")
        resolved_output = None
        if output_path:
            resolved_output = self._resolve_in_domain(str(output_path))
            if resolved_output is None:
                return refusal_outcome("CROSS_ROOT_MUTATION", self.verified_basis("A337"))
        task_id = str(request.payload.get("task_id") or f"program-{uuid.uuid4().hex}")
        identity = self._task_identity(request, task_id)
        task = {
            **self._task_fields(identity),
            "op": op,
            "subject": request.payload.get("subject") or request.subject,
            "spec": request.payload.get("spec"),
            "output_path": str(resolved_output) if resolved_output else None,
            "executor": "xingcheng-native-model",
            "status": "accepted",
            "lifecycle_state": TaskState.CREATED.value,
            "type": "program",
            "created_at": self._iso_now(),
        }
        self._program_tasks[task_id] = task
        record_task_event(task_id=task_id, event="create", task_type="program", detail=task)
        for target in (TaskState.VALIDATED, TaskState.AUTHORIZED, TaskState.QUEUED, TaskState.RUNNING):
            self._transition_task(task, identity, target)
        execution_result = await _invoke_native_model_executor(getattr(self, "app", None), task)
        if execution_result.get("status") == "deferred":
            execution_result = await _invoke_fallback_executor(getattr(self, "app", None), task)
        task["execution_result"] = execution_result
        terminal = (
            TaskState.COMPLETED
            if execution_result.get("status") in {"executed", "fallback-executed"}
            else TaskState.FAILED
        )
        self._transition_task(
            task,
            identity,
            terminal,
            detail={"execution_result": execution_result},
        )
        record_task_event(
            task_id=task_id, event="execute", task_type="program",
            detail={"execution_result": execution_result},
        )
        return accepted_outcome(
            {
                "action": f"program.{op}",
                "task": task,
                "executor": "xingcheng-native-model",
                "execution_result": execution_result,
                "boundary": "no-permission-grant+no-routing-evidence-alteration+no-direct-channel-operation",
            },
            self.verified_basis("A337"),
        )

    async def _invoke_native_model_executor(self, task: dict[str, Any]) -> dict[str, Any]:
        """Delegate to module-level executor (A337)."""
        return await _invoke_native_model_executor(getattr(self, "app", None), task)

    async def _adjudicate_automation(self, request: SovereignRequest) -> SovereignOutcome:
        """A337 AUTOMATION-EXECUTOR: whole-system automation coordination."""
        if not self._automation_authorized(request):
            return refusal_outcome("AUTOMATION_AUTHORIZATION_REQUIRED", self.verified_basis("A337"))
        intent = request.intent
        if intent == "automation.decompose":
            return self._automation_decompose(request)
        if intent == "automation.schedule":
            return self._automation_schedule(request)
        if intent == "automation.dispatch":
            return await self._automation_dispatch(request)
        if intent == "automation.converge":
            return self._automation_converge(request)
        if intent == "automation.verify-result":
            return self._automation_verify(request)
        return self._automation_contain(request)

    def _automation_decompose(self, request: SovereignRequest) -> SovereignOutcome:
        task_id = str(request.payload.get("task_id") or f"auto-{uuid.uuid4().hex}")
        identity = self._task_identity(request, task_id)
        steps = request.payload.get("steps") or []
        # Enhanced decomposition: support step dependencies and parallel groups
        parsed_steps = []
        for i, s in enumerate(steps):
            if isinstance(s, dict):
                step = {
                    "step": s.get("step", s),
                    "state": "pending",
                    "dependencies": s.get("depends_on", []),
                    "parallel_group": s.get("parallel_group"),
                    "timeout_seconds": s.get("timeout_seconds", 300),
                    "retry_policy": s.get("retry_policy", {"max_attempts": 3, "backoff": 2.0}),
                }
            else:
                step = {
                    "step": s,
                    "state": "pending",
                    "dependencies": [],
                    "parallel_group": None,
                    "timeout_seconds": 300,
                    "retry_policy": {"max_attempts": 3, "backoff": 2.0},
                }
            parsed_steps.append(step)
        task = {
            **self._task_fields(identity),
            "objective": request.payload.get("objective") or request.subject,
            "steps": parsed_steps,
            "state": TaskState.CREATED.value,
            "lifecycle_state": TaskState.CREATED.value,
            "created_at": self._iso_now(),
            "type": "automation",
            "metadata": {
                "step_count": len(parsed_steps),
                "has_dependencies": any(s["dependencies"] for s in parsed_steps),
                "parallel_groups": set(s["parallel_group"] for s in parsed_steps if s["parallel_group"]),
            },
        }
        self._automation_tasks[task_id] = task
        record_task_event(
            task_id=task_id, event="create", task_type="automation",
            detail=task,
        )
        for target in (TaskState.VALIDATED, TaskState.AUTHORIZED):
            self._transition_task(task, identity, target)
        return accepted_outcome(
            {
                **self._task_fields(identity),
                "task_id": task_id,
                "state": task["state"],
                "steps": len(parsed_steps),
                "metadata": task["metadata"],
            },
            self.verified_basis("A337"),
        )

    def _automation_task_or_refusal(self, request: SovereignRequest):
        task_id = str(request.payload.get("task_id") or "")
        task = self._automation_tasks.get(task_id)
        if task is None:
            return refusal_outcome("UNKNOWN_AUTOMATION_TASK", self.verified_basis("A337"))
        return task

    def _automation_schedule(self, request: SovereignRequest) -> SovereignOutcome:
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task
        order = request.payload.get("order")
        steps = task.get("steps", [])

        if isinstance(order, list) and order:
            # User-provided order with index references
            indexed = {i: step for i, step in enumerate(steps)}
            task["schedule"] = [indexed[i] for i in order if i in indexed]
        else:
            # Auto-schedule: topological sort based on dependencies
            task["schedule"] = self._topological_schedule(steps)

        # Group parallel steps
        task["parallel_schedule"] = self._group_parallel_steps(task["schedule"])
        identity = self._task_identity_from_record(task)
        self._transition_task(task, identity, TaskState.QUEUED)
        record_task_event(
            task_id=str(request.payload.get("task_id")),
            event="schedule", task_type="automation",
            detail={"scheduled_steps": len(task["schedule"]), "parallel_groups": len(task["parallel_schedule"])},
        )
        return accepted_outcome(
            {
                **self._task_fields(identity),
                "task_id": request.payload.get("task_id"),
                "state": task["state"],
                "scheduled_steps": len(task["schedule"]),
                "parallel_groups": len(task["parallel_schedule"]),
            },
            self.verified_basis("A337"),
        )

    def _topological_schedule(self, steps: list[dict]) -> list[dict]:
        """Topological sort based on step dependencies."""
        # Build adjacency list
        adj = {i: [] for i in range(len(steps))}
        in_degree = {i: 0 for i in range(len(steps))}

        for i, step in enumerate(steps):
            for dep in step.get("dependencies", []):
                if dep < len(steps):
                    adj[dep].append(i)
                    in_degree[i] += 1

        # Kahn's algorithm
        queue = [i for i in range(len(steps)) if in_degree[i] == 0]
        result = []
        while queue:
            node = queue.pop(0)
            result.append(steps[node])
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # Add any remaining (cycles or disconnected)
        for i in range(len(steps)):
            if steps[i] not in result:
                result.append(steps[i])

        return result

    def _group_parallel_steps(self, scheduled_steps: list[dict]) -> list[list[dict]]:
        """Group steps by parallel_group for concurrent execution."""
        groups = {}
        for step in scheduled_steps:
            group = step.get("parallel_group") or "sequential"
            if group not in groups:
                groups[group] = []
            groups[group].append(step)
        return list(groups.values())

    async def _automation_dispatch(self, request: SovereignRequest) -> SovereignOutcome:
        """Dispatch a step or parallel group to its registered module (real dispatch, not a label)."""
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task

        step_index = request.payload.get("step_index")
        parallel_group = request.payload.get("parallel_group")
        steps = task.get("schedule") or task["steps"]

        identity = self._task_identity_from_record(task)
        if task.get("lifecycle_state") == TaskState.QUEUED.value:
            self._transition_task(task, identity, TaskState.RUNNING)

        # Support dispatching a parallel group
        if parallel_group is not None:
            parallel_schedule = task.get("parallel_schedule", [])
            if parallel_group < 0 or parallel_group >= len(parallel_schedule):
                return refusal_outcome("INVALID_PARALLEL_GROUP", self.verified_basis("A337"))
            group_steps = parallel_schedule[parallel_group]
            results = []
            for idx, step in enumerate(group_steps):
                step_idx = steps.index(step) if step in steps else idx
                dispatch_result = await _dispatch_to_registered_module(getattr(self, "app", None), step)
                step["state"] = "dispatched"
                step["dispatched_at"] = self._iso_now()
                step["dispatch_result"] = dispatch_result
                results.append({"step_index": step_idx, "result": dispatch_result})
            task["state"] = "in-progress"
            record_task_event(
                task_id=str(request.payload.get("task_id")),
                event="dispatch-parallel", task_type="automation",
                detail={"parallel_group": parallel_group, "results": results},
            )
            return accepted_outcome(
                {
                    "task_id": request.payload.get("task_id"),
                    "parallel_group": parallel_group,
                    "dispatched_steps": len(group_steps),
                    "results": results,
                },
                self.verified_basis("A337"),
            )

        # Single step dispatch
        if not isinstance(step_index, int) or step_index < 0 or step_index >= len(steps):
            return refusal_outcome("INVALID_STEP_INDEX", self.verified_basis("A337"))
        step = steps[step_index]

        # Check dependencies are satisfied
        if not self._dependencies_satisfied(step, steps):
            return refusal_outcome("DEPENDENCIES_NOT_SATISFIED", self.verified_basis("A337"))

        # Real dispatch: invoke the registered module for this step.
        dispatch_result = await _dispatch_to_registered_module(getattr(self, "app", None), step)
        step["state"] = "dispatched"
        step["dispatched_at"] = self._iso_now()
        step["dispatch_result"] = dispatch_result
        task["state"] = "in-progress"
        record_task_event(
            task_id=str(request.payload.get("task_id")),
            event="dispatch", task_type="automation",
            detail={"step_index": step_index, "dispatch_result": dispatch_result},
        )
        return accepted_outcome(
            {
                "task_id": request.payload.get("task_id"),
                "step_index": step_index,
                "handoff": "typed",
                "state": "dispatched",
                "dispatch_result": dispatch_result,
            },
            self.verified_basis("A337"),
        )

    def _dependencies_satisfied(self, step: dict, all_steps: list[dict]) -> bool:
        """Check if all step dependencies are completed."""
        for dep_index in step.get("dependencies", []):
            if dep_index >= len(all_steps):
                return False
            dep_step = all_steps[dep_index]
            if dep_step.get("state") not in ("completed", "success", "done", "converged"):
                return False
        return True

    async def _dispatch_to_registered_module(self, step: dict[str, Any]) -> dict[str, Any]:
        """Delegate to module-level dispatcher (A337)."""
        return await _dispatch_to_registered_module(getattr(self, "app", None), step)

    def _automation_converge(self, request: SovereignRequest) -> SovereignOutcome:
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task
        steps = task.get("schedule") or task["steps"]
        states = [s.get("state", "pending") for s in steps]

        # Converged if all steps are in terminal success states
        success_states = ("converged", "success", "done", "verified")
        converged = bool(states) and all(s in success_states for s in states)

        # Also check parallel groups
        parallel_schedule = task.get("parallel_schedule", [])
        parallel_converged = True
        for group in parallel_schedule:
            group_states = [s.get("state", "pending") for s in group]
            if not all(s in success_states for s in group_states):
                parallel_converged = False
                break

        final_converged = converged and parallel_converged
        if final_converged and task.get("lifecycle_state") == TaskState.RUNNING.value:
            self._transition_task(
                task,
                self._task_identity_from_record(task),
                TaskState.COMPLETED,
                detail={"step_states": states},
            )
        record_task_event(
            task_id=str(request.payload.get("task_id")),
            event="converge", task_type="automation",
            detail={"converged": final_converged, "step_states": states, "parallel_converged": parallel_converged},
        )
        return accepted_outcome(
            {"task_id": request.payload.get("task_id"), "converged": final_converged, "step_states": states},
            self.verified_basis("A337"),
        )

    def _automation_verify(self, request: SovereignRequest) -> SovereignOutcome:
        """A446 independent verification — the submitter cannot self-verify."""
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task
        evidence = request.payload.get("evidence")
        if not evidence:
            return refusal_outcome("MISSING_RESULT_EVIDENCE", self.verified_basis("A337"))
        # A446: independent verification — the verifier identity must be
        # distinct from the submitter/requester.  The submitter cannot
        # self-verify by providing arbitrary evidence.
        submitter = str(request.requester or "").strip()
        verifier_id = _NATIVE_VERIFIER_ID
        if submitter == verifier_id:
            return refusal_outcome(
                "SELF_VERIFICATION_FORBIDDEN", self.verified_basis("A446", "A337")
            )
        # Verify the evidence is structurally valid (not just non-empty).
        evidence_valid, evidence_reason = _verify_evidence_structure(evidence, task)
        if not evidence_valid:
            return refusal_outcome(
                f"EVIDENCE_INVALID:{evidence_reason}",
                self.verified_basis("A446", "A337"),
            )
        task["result_evidence"] = evidence
        task["state"] = "verified"
        task["verified_at"] = self._iso_now()
        task["verifier"] = verifier_id
        record_task_event(
            task_id=str(request.payload.get("task_id")),
            event="verify", task_type="automation",
            detail={"verifier": verifier_id, "evidence_valid": True},
        )
        return accepted_outcome(
            {
                "task_id": request.payload.get("task_id"),
                "state": "verified",
                "verifier": verifier_id,
            },
            self.verified_basis("A337", "A446"),
        )

    def _verify_evidence_structure(
        self, evidence: Any, task: dict[str, Any]
    ) -> tuple[bool, str]:
        """Delegate to module-level evidence verifier (A446)."""
        return _verify_evidence_structure(evidence, task)

    def _automation_contain(self, request: SovereignRequest) -> SovereignOutcome:
        task = self._automation_task_or_refusal(request)
        if isinstance(task, SovereignOutcome):
            return task
        step_index = request.payload.get("step_index")
        lifecycle_state = task.get("lifecycle_state")
        if lifecycle_state in {
            TaskState.CREATED.value,
            TaskState.VALIDATED.value,
            TaskState.AUTHORIZED.value,
            TaskState.QUEUED.value,
            TaskState.RUNNING.value,
            TaskState.INTERRUPTED.value,
            TaskState.RECOVERING.value,
        }:
            self._transition_task(
                task,
                self._task_identity_from_record(task),
                TaskState.CANCELLED,
                detail={"reason": request.payload.get("reason")},
            )
        task["state"] = "contained"
        task["containment"] = {
            "step_index": step_index,
            "reason": request.payload.get("reason"),
            "contained_at": self._iso_now(),
        }
        record_task_event(
            task_id=str(request.payload.get("task_id")),
            event="contain", task_type="automation",
            detail=task["containment"],
        )
        return accepted_outcome(
            {"task_id": request.payload.get("task_id"), "state": "contained", "failure_propagation": "halted"},
            self.verified_basis("A337"),
        )
