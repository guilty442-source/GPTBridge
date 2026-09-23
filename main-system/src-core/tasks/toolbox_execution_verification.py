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
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)


class ExecutionVerificationMixin:
    """A334 execution identity and module-assignment verification."""

    def _module_code_for_identity(self, identity: str) -> str:
            """Resolve the codex module architecture code for a governed identity.

            The naive ``tool_id.upper().replace("-", "_")`` derivation is only
            valid for the *hosting* runtime identity.  Companions, nested sealed
            identities (e.g. ``xingcheng`` inside ``local-model``) and resident
            authority modules first resolve through the sealed manifest binding
            in the identity registry to their hosting tool; unresolvable
            identities keep the derived code so the registry lookup fails closed.
            """
            code = str(identity).strip().upper().replace("-", "_")
            try:
                if module_assignment(code) is not None:
                    return code
                identities = identity_group_snapshot().identities
            except (OSError, KeyError, ValueError, RuntimeError):
                return code
            for bound in identities:
                if str(getattr(bound, "bound_tool_id", "") or "").strip() != identity:
                    continue
                binding = getattr(bound, "manifest_binding", None)
                template = str(getattr(binding, "path_template", "") or "")
                if not getattr(binding, "required", False) or not template:
                    continue
                parts = Path(template.format(tool_id=identity)).parts
                host = ""
                if "Standalone tools" in parts:
                    index = parts.index("Standalone tools")
                    if len(parts) > index + 1:
                        host = parts[index + 1]
                elif len(parts) >= 2:
                    host = parts[0]
                if host:
                    host_code = host.upper().replace("-", "_")
                    try:
                        if module_assignment(host_code) is not None:
                            return host_code
                    except (OSError, KeyError, ValueError, RuntimeError):
                        pass
            return code

    def _bound_manifest_identity(self, channel_id: str, tool_dir: Path) -> str:
            """Read the sealed-registry bound manifest's declared identity.

            For a nested governed identity (e.g. ``xingcheng`` inside
            ``local-model``) the manifest pinned by the identity registry must
            declare that identity — proving the channel claimant is the bound
            identity rather than a self-asserted name.
            """
            try:
                identities = identity_group_snapshot().identities
            except (OSError, KeyError, ValueError, RuntimeError):
                return ""
            for bound in identities:
                if str(getattr(bound, "bound_tool_id", "") or "").strip() != channel_id:
                    continue
                binding = getattr(bound, "manifest_binding", None)
                template = str(getattr(binding, "path_template", "") or "")
                id_field = str(getattr(binding, "tool_id_field", "") or "id")
                if not getattr(binding, "required", False) or not template:
                    continue
                manifest_path = (
                    self.project_root / template.format(tool_id=channel_id)
                ).resolve()
                try:
                    manifest_path.relative_to(Path(tool_dir).resolve())
                except ValueError:
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                declared = str(manifest.get(id_field) or "").strip()
                if declared:
                    return declared
            return ""

    def _attested_execution_identity(self, tool_id: str) -> str:
            """A334: the executing individual's attested identity — never an echo.

            The identity is resolved through the authenticated chain, never
            copied from the request: the identity that actually claims the
            shared-layer channel (sealed-registry bound, the same identity the
            governance bootstrap token is minted for) is resolved to its
            hosting module's registered execution identity.  A standalone
            runtime scoped to a single tool attests that tool's chain.
            """
            source = str(tool_id).strip()
            allowed = getattr(self, "allowed_tool_ids", None)
            if allowed is not None and len(allowed) == 1:
                source = str(next(iter(allowed))).strip() or source
            if not source:
                return ""
            channel_id = self._channel_target_tool_id(source)
            if not channel_id:
                return ""
            return self._module_code_for_identity(channel_id)

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
        # A604: the sub-sovereign hierarchy is retired; single-purpose
        # module dispatch declares the ``none-single-purpose-module-
        # dispatch`` sentinel instead of a managing sub-sovereign.  Any
        # other value still must resolve as an active hierarchy child.
        if managing != "none-single-purpose-module-dispatch" and (
            not managing or parent_of(managing) is None
        ):
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
