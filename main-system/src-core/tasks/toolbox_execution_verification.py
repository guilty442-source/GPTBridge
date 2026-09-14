"""A334 execution verification mixin (A185 split).

Contains the module-assignment verification gate and the identity
resolution helpers used by the execution mixin.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from governance.registries import (
    module_assignment,
    parent_of,
    validate_execution_identity,
)


class ExecutionVerificationMixin:
    """A334 execution identity and module-assignment verification."""

    def _module_code_for_identity(self, identity: str) -> str:
        """Resolve the codex module architecture code for a governed identity.

        The module code is the canonical architecture identifier used by the
        module-assignment registry.  It is derived from the identity group
        snapshot so the same identity always resolves to the same module
        code regardless of the caller's context.
        """
        from governance_rule.permission_directory.registries.permissions.identity_groups import (
            identity_group_snapshot,
        )

        registry = identity_group_snapshot()
        for group in registry.groups:
            for member in group.identities:
                if member.identity_code == identity or member.language_name == identity:
                    return f"module-{member.identity_code.lower()}"
        # Fallback: derive from the identity string directly.
        cleaned = identity.strip().lower()
        if "/" in cleaned:
            cleaned = cleaned.split("/")[-1]
        return f"module-{cleaned}"

    def _bound_manifest_identity(self, channel_id: str, tool_dir: Path) -> str:
        """Read the bound manifest's declared identity for the channel.

        The bound manifest is the manifest.json inside the tool directory.
        It must declare the same channel identity that the sealed registry
        assigned to the tool, otherwise the A334 gate denies execution.
        """
        manifest_path = tool_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return ""
        if not isinstance(manifest, dict):
            return ""
        identities = manifest.get("governance_identities") or []
        if not isinstance(identities, list):
            return ""
        for entry in identities:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("identity_code") or "") == channel_id:
                return channel_id
        return ""

    def _attested_execution_identity(self, tool_id: str) -> str:
        """Resolve the attested execution identity for the given tool id.

        The execution identity is the individual identity bound to the
        tool's governed runtime at launch.  It is attested by the
        launcher and sealed in the governance bootstrap token.
        """
        try:
            owner = self._runtime_owner_tool_id(tool_id)
            runtime_id = self._governed_runtime_tool_id(owner)
            return str(runtime_id or "")
        except (PermissionError, OSError, KeyError, ValueError, RuntimeError):
            return ""

    def _verify_module_assignment(self, tool_id: str) -> Dict[str, Any] | None:
        """A334 execution gate: the module registry is the machine authority.

        Four-way identity consistency, fail-closed: the manifest declares
        the requested tool id; the channel claimant is the sealed-registry
        governed identity (the identity the governance bootstrap token is
        bound to at launch); a nested channel identity declares itself in
        its registry-pinned bound manifest; and the module code derived
        from the requested tool must match the registered execution
        identity derived independently from the bound channel identity.

        Returns an error response when the gate denies, otherwise None.
        """
        try:
            manifest, tool_dir = self._load_manifest_cached(tool_id)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "TOOL_MANIFEST_UNAVAILABLE",
                "message": f"A334: tool manifest is unavailable: {type(error).__name__}",
            }
        if not isinstance(manifest, dict) or str(
            manifest.get("id") or ""
        ).strip() != tool_id:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "MANIFEST_IDENTITY_MISMATCH",
                "message": "A334: manifest identity does not match the tool id",
            }
        try:
            owner = self._runtime_owner_tool_id(tool_id, manifest)
            channel_id = str(
                self._governed_runtime_tool_id(owner) or ""
            ).strip()
        except PermissionError:
            channel_id = ""
        except (OSError, KeyError, ValueError, RuntimeError):
            channel_id = ""
        if not channel_id:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "RUNTIME_IDENTITY_UNRESOLVED",
                "message": "A334: no sealed-registry identity claims the channel",
            }
        if channel_id != owner and self._bound_manifest_identity(
            channel_id, tool_dir
        ) != channel_id:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "CHANNEL_MANIFEST_IDENTITY_MISMATCH",
                "message": "A334: bound manifest does not declare the channel identity",
            }
        module_code = self._module_code_for_identity(tool_id)
        try:
            row = module_assignment(module_code)
        except (OSError, KeyError, ValueError, RuntimeError) as error:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "MODULE_REGISTRY_UNAVAILABLE",
                "message": f"A334 module-assignment registry is unavailable: {type(error).__name__}",
            }
        if row is None:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "MODULE_NOT_IN_REGISTRY",
                "message": "A334: executable module is not registered",
            }
        execution_identity = self._attested_execution_identity(tool_id)
        if not execution_identity:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "EXECUTION_IDENTITY_NOT_ATTESTED",
                "message": "A334: executing individual identity is not attested",
            }
        if not validate_execution_identity(module_code, execution_identity):
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "EXECUTION_IDENTITY_MISMATCH",
                "message": "A334: execution identity does not match registry",
            }
        managing = str(row.get("managing_sub_sovereign") or "")
        if not managing or parent_of(managing) is None:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "MANAGING_SUB_SOVEREIGN_UNREGISTERED",
                "message": "A334: managing sub-sovereign is not in the hierarchy registry",
            }
        if not row.get("decision_authority") or not row.get("review_authority"):
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "MODULE_AUTHORITY_INCOMPLETE",
                "message": "A334: module decision/review authority is incomplete",
            }
        return None


__all__ = ["ExecutionVerificationMixin"]
