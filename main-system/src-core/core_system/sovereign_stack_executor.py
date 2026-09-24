"""Sovereign Stack Executor — facade.

Governed executor: materializes and activates the five peer sovereign
cores (A63/A128/A130).

A592/A604: the sub-sovereign layer is eliminated — there are no child
identities to materialize, authorize, start or route to
(FORBID:sub-sovereign-routing).  The five cores coordinate through
registered modules instead.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .sovereign_stack_executor_activation import SovereignStackActivationMixin


class SovereignStackExecutor(SovereignStackActivationMixin):
    """Governed executor: materializes and activates the sovereign stack."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._startup_failures: list[dict[str, str]] = []

    # ------------------------------------------------------------------
    # Top-level core materialization
    # ------------------------------------------------------------------

    def _materialize_top_sovereigns(self, sovereign: Any) -> None:
        """Ensure the other top-level sovereigns exist."""
        app = self.app
        top_specs = (
            (
                "permission_sovereign",
                "governance.sovereigns.permission_sovereign",
                "PermissionSovereign",
            ),
            (
                "automation_sovereign",
                "governance.sovereigns.automation_sovereign",
                "AutomationSovereign",
            ),
            (
                "system_runtime_sovereign",
                "governance.sovereigns.system_runtime_sovereign",
                "SystemRuntimeSovereign",
            ),
        )
        for attr_name, module_name, class_name in top_specs:
            if getattr(app, attr_name, None) is not None:
                continue
            try:
                cls = getattr(import_module(module_name), class_name)
                if attr_name == "permission_sovereign":
                    setattr(
                        app,
                        attr_name,
                        cls(app, governance=getattr(app, "governance", None)),
                    )
                else:
                    setattr(app, attr_name, cls(app))
            except Exception as error:
                self._startup_failures.append(
                    {
                        "sub_sovereign": attr_name,
                        "error": f"top-sovereign-materialize:{type(error).__name__}: {error}",
                    }
                )

    async def _start_top_sovereigns(self, sovereign: Any) -> None:
        """Activate the top-level coordination sovereigns."""
        app = self.app
        for top in (
            getattr(app, "system_runtime_sovereign", None),
            getattr(app, "permission_sovereign", None),
            getattr(app, "automation_sovereign", None),
        ):
            if top is not None and not getattr(top, "_started", False):
                try:
                    await top.start()
                except Exception as error:
                    self._startup_failures.append(
                        {
                            "sub_sovereign": getattr(top, "sovereign_id", "?"),
                            "error": f"top-sovereign:{type(error).__name__}: {error}",
                        }
                    )


__all__ = ["SovereignStackExecutor"]
