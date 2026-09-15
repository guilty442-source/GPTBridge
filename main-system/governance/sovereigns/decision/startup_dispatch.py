"""Decision Sovereign — Startup Stack Dispatch (A63/A64/A128/A130).

Adjudicates the sovereign-stack startup sequence and delegates execution
to the governed SovereignStackExecutor. Decision-only; no materialization.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome
from core_system.sovereign_utils import _iso_now


class DecisionStartupDispatchMixin:
    """Startup stack dispatch adjudication (A64)."""

    app: Any
    workspace_root: Any
    launcher_report_path: Any
    module_id: str
    platform_id: str
    _sub_sovereigns: dict[str, Any]
    _autonomy_task: Any
    _autonomy_stop: Any

    def _dependency_state(self) -> str:
        env_state = str(os.environ.get("GPTBRIDGE_STARTUP_STATE", "")).strip()
        if env_state:
            return env_state
        report = self._load_report()
        return str(report.get("state") or "UNKNOWN")

    def _load_report(self) -> dict[str, Any]:
        import json
        try:
            payload = json.loads(
                self.launcher_report_path.read_text(encoding="utf-8")
            )
            return payload if isinstance(payload, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    async def _adjudicate_startup_dispatch(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A64: mother process delegates startup stack dispatch to decision-sovereign."""
        dependency_state = request.payload.get("dependency_state", "UNKNOWN")
        if dependency_state not in ("READY", "DEGRADED", "RECOVERY"):
            return refusal_outcome("INVALID_DEPENDENCY_STATE", self.verified_basis("A128", "A130"))

        return accepted_outcome(
            {
                "authorized": True,
                "sequence": [
                    "sync-sub-sovereigns-and-cleaner",
                    "permission-sovereign",
                    "maintenance-and-self-maintenance",
                    "decision-sovereign-and-sub-sovereigns",
                ],
                "dependency_state": dependency_state,
                "parallelism": "bounded-independent-per-A155",
            },
            self.verified_basis("A128", "A130", "A155"),
        )

    async def start_sovereign_stack(self) -> bool:
        """Adjudicate startup dispatch, then delegate to governed executor (A63/A64)."""
        outcome = await self._adjudicate_startup_dispatch(
            SovereignRequest(
                intent="startup.stack.dispatch",
                subject="sovereign-stack",
                requester="startup-executor",
                payload={"dependency_state": self._dependency_state()},
            )
        )
        if not outcome.accepted:
            reason = outcome.refusal.reason_code if outcome.refusal else "UNKNOWN"
            raise RuntimeError(f"startup-dispatch-refused:{reason}")

        executor = getattr(self.app, "sovereign_stack_executor", None)
        if executor is None:
            from core_system.sovereign_stack_executor import (
                SovereignStackExecutor,
            )

            executor = SovereignStackExecutor(self.app)
            self.app.sovereign_stack_executor = executor
        return await executor.activate(self)

    async def stop(self) -> None:
        """Dispatch deactivation to governed executor, then stop."""
        await self._stop_autonomy_loop()
        executor = getattr(self.app, "sovereign_stack_executor", None)
        if executor is not None:
            await executor.deactivate(self)
        self._save_state({"stopped_at": _iso_now()})
        await super().stop()

    def _save_state(self, payload: dict[str, Any]) -> None:
        import json
        import os
        from pathlib import Path
        self.runtime_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.runtime_state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.runtime_state_path)


__all__ = ["DecisionStartupDispatchMixin"]