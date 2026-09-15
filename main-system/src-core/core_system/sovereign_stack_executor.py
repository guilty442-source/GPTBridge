"""Sovereign Stack Executor — facade.

This module provides the SovereignStackExecutor class.  Constants
and helpers live in :mod:`core_system.sovereign_stack_executor_constants`.

Governed executor: materializes and activates the sovereign stack
(A63/A128/A130/A334).
"""

from __future__ import annotations

import asyncio
import time
from importlib import import_module
from typing import Any

from governance.registries import children_of, validate_child_parent

from .sovereign_stack_executor_constants import (
    _CHILD_CLASSES,
    _resolve_child_class,
    _child_start_kwargs,
)
from .sovereign_stack_executor_children import SovereignStackChildrenMixin
from .sovereign_stack_executor_activation import SovereignStackActivationMixin


class SovereignStackExecutor(SovereignStackChildrenMixin, SovereignStackActivationMixin):
    """Governed executor: materializes and activates the sovereign stack."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._startup_failures: list[dict[str, str]] = []

    # ------------------------------------------------------------------
    # A334 materialization: every child under its codex-registered parent
    # ------------------------------------------------------------------

    def _materialize_top_sovereigns(self, sovereign: Any) -> None:
        """Ensure the other top-level sovereigns exist before child materialization."""
        app = self.app
        top_specs = (
            (
                "permission_sovereign",
                "governance.sovereigns.permission_sovereign",
                "PermissionSovereign",
            ),
            (
                "synchronization_sovereign",
                "governance.sovereigns.synchronization_sovereign",
                "SynchronizationSovereign",
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
            getattr(app, "synchronization_sovereign", None),
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

    def _parent_object(self, sovereign: Any, parent_id: str) -> Any:
        """Resolve a registered parent identity to the live sovereign."""
        app = self.app
        synchronization = getattr(app, "synchronization_sovereign", None)
        return {
            "decision-sovereign": sovereign,
            "permission-sovereign": getattr(app, "permission_sovereign", None),
            "synchronization-sovereign": synchronization,
            # Codex id renamed to automation-sovereign (97e8a34); the code
            # object keeps the synchronization class/attr until the full
            # rename lands.
            "automation-sovereign": synchronization,
            "system-runtime-sovereign": getattr(
                app, "system_runtime_sovereign", None
            ),
            # A485: the learning sub-sovereign is a child of 星澄.
            "星澄": getattr(app, "xingcheng_sovereign", None),
        }.get(parent_id)

    def _materialize_children(self, sovereign: Any) -> None:
        """Instantiate every active registry child under its codex parent."""
        app = self.app
        for child_id, class_ref in _CHILD_CLASSES.items():
            parent_id = self._codex_parent(child_id)
            parent = self._parent_object(sovereign, parent_id) if parent_id else None
            if parent is None:
                self._startup_failures.append(
                    {
                        "sub_sovereign": child_id,
                        "error": f"codex-parent-unavailable:{parent_id}",
                    }
                )
                continue
            registry = getattr(parent, "_sub_sovereigns", None)
            if registry is None:
                continue
            if child_id not in registry:
                try:
                    child_cls = _resolve_child_class(class_ref)
                    registry[child_id] = child_cls(app, parent=parent)
                except Exception as error:
                    self._startup_failures.append(
                        {
                            "sub_sovereign": child_id,
                            "error": f"materialize:{type(error).__name__}: {error}",
                        }
                    )

        app_registry = getattr(app, "_sub_sovereigns", None)
        if isinstance(app_registry, dict):
            for parent in {
                sovereign,
                getattr(app, "permission_sovereign", None),
                getattr(app, "synchronization_sovereign", None),
                getattr(app, "system_runtime_sovereign", None),
                getattr(app, "xingcheng_sovereign", None),
            }:
                if parent is not None:
                    app_registry.update(getattr(parent, "_sub_sovereigns", {}))

    @staticmethod
    def _codex_parent(child_id: str) -> str | None:
        from governance.registries import parent_of

        return parent_of(child_id)

    async def _start_child(
        self, sovereign: Any, tag: str, child_id: str
    ) -> dict[str, Any]:
        """Parent-authorized child activation (A334 + governed execution)."""
        parent_id = self._codex_parent(child_id)
        parent = self._parent_object(sovereign, parent_id) if parent_id else None
        child = None
        if parent is not None:
            child = getattr(parent, "_sub_sovereigns", {}).get(child_id)
        if child is None:
            self._startup_failures.append(
                {"sub_sovereign": tag, "error": f"not-materialized:{child_id}"}
            )
            return {}
        outcome = parent.authorize_child_activation(child_id)
        if not outcome.accepted:
            reason = outcome.refusal.reason_code if outcome.refusal else "REFUSED"
            self._startup_failures.append(
                {"sub_sovereign": tag, "error": f"parent-authorization:{reason}"}
            )
            return {}
        try:
            result = await child.start(
                **_child_start_kwargs(self.app, child_id)
            )
            try:
                parent.record_child_success(child_id)
            except Exception:
                pass
            return result
        except Exception as error:
            try:
                parent.record_child_failure(child_id)
            except Exception:
                pass
            self._startup_failures.append(
                {"sub_sovereign": tag, "error": f"{type(error).__name__}: {error}"}
            )
            return {}

    # ------------------------------------------------------------------
    # Governed single-child restart (decision-layer autonomy entry point)
    # ------------------------------------------------------------------

    async def restart_child(
        self, sovereign: Any, child_id: str
    ) -> dict[str, Any]:
        """Restart one materialized child under codex-parent authorization."""
        parent_id = self._codex_parent(child_id)
        parent = (
            self._parent_object(sovereign, parent_id) if parent_id else None
        )
        if parent is None:
            return {
                "ok": False,
                "child": child_id,
                "error": f"codex-parent-unavailable:{parent_id}",
            }
        registry = getattr(parent, "_sub_sovereigns", None)
        if registry is None:
            return {
                "ok": False,
                "child": child_id,
                "error": "parent-registry-unavailable",
            }
        child = registry.get(child_id)
        if child is None:
            class_ref = _CHILD_CLASSES.get(child_id)
            if class_ref is None:
                return {
                    "ok": False,
                    "child": child_id,
                    "error": f"unknown-child:{child_id}",
                }
            try:
                child_cls = _resolve_child_class(class_ref)
                registry[child_id] = child_cls(self.app, parent=parent)
            except Exception as error:
                return {
                    "ok": False,
                    "child": child_id,
                    "error": f"rematerialize:{type(error).__name__}: {error}",
                }
        await self._start_child(sovereign, child_id, child_id)
        child = registry.get(child_id)
        return {
            "ok": bool(getattr(child, "_started", False)),
            "child": child_id,
            "parent": parent_id,
        }

    # ------------------------------------------------------------------
    # Activation sequence
    # ------------------------------------------------------------------


__all__ = ["SovereignStackExecutor"]
