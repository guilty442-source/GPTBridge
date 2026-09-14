"""Codex amendment execution and consumption through the information layer.

The executor receives a full-replacement request from the information layer,
copies the new sealed codex into place, restores read-only, and broadcasts the
change.  Consumers reload the codex so the new rule set is applied globally.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Any

from governance_rule.execution.codex_dual_key import mint_dual_key_grant
from governance_rule.execution.codex_reconcile import bounded_lookup
from governance_rule.execution.codex_repository import CODEX_DATABASE_PATH
from governance_rule.execution.codex_session import (
    revoke_codex_read_contexts,
)
from shared_layer.runtime_gateway import InformationChannelGateway

from governance.sovereigns.permission_sovereign import re_certify_permission_sovereign


def _restore_read_only(path: Path) -> None:
    """Make a path read-only after replacement (Windows / POSIX)."""
    os.chmod(path, stat.S_IREAD)


def _make_writable(path: Path) -> None:
    """Lift read-only bit so the file can be replaced."""
    current = path.stat().st_mode if path.is_file() else 0
    os.chmod(path, current | stat.S_IWRITE)


def _amendment_grant() -> str:
    """Mint the dual-key grant for one amendment-verification read."""
    return mint_dual_key_grant(
        operation="codex-open:bounded-machine-lookup",
        primary_actor="codex-amendment-executor",
        secondary_actor="permission-sovereign",
        purpose="amendment-verification",
        scope=("codex:identity",),
        access_class="bounded-machine-lookup",
    )


async def _amendment_executor(command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Handle ``codex.amend`` from the information layer."""
    if command != "codex.amend":
        return "ignored", {"reason": "unhandled command"}

    project_root = Path(payload.get("project_root", Path.cwd())).resolve()
    source = project_root / payload["source_path"]
    if not source.is_file():
        return "failed", {"error_code": "SOURCE_NOT_FOUND"}

    sha256_expected = payload.get("sha256", "")
    import hashlib
    sha256_actual = hashlib.sha256(source.read_bytes()).hexdigest()
    if sha256_actual != sha256_expected:
        return "failed", {"error_code": "SHA256_MISMATCH"}

    target = Path(CODEX_DATABASE_PATH).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        _make_writable(target)
    shutil.copy2(source, target)
    _restore_read_only(target)

    new_version = payload.get("new_version", "unknown")
    # A435: an amendment invalidates every outstanding read context —
    # sessions bound to the old codex version are revoked, then the new
    # authority is verified through one bounded official-entry lookup.
    revoke_codex_read_contexts()
    # A435 two-key boundary: amendment verification requires a grant
    # countersigned by the entry owner (permission-sovereign).
    bounded_lookup(
        "codex-amendment-executor",
        purpose="amendment-verification",
        scope=("codex:identity",),
        reader=lambda ctx: ctx.codex_identity(),
        dual_key_grant=_amendment_grant(),
    )

    return "applied", {
        "amendment_id": payload.get("amendment_id"),
        "new_version": new_version,
        "sha256": sha256_actual,
    }


async def _amendment_consumer(command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Handle ``codex.applied`` so the latest codex is reloaded globally."""
    if command != "codex.applied":
        return "ignored", {"reason": "unhandled command"}

    # A435: codex.applied invalidates all read contexts (version binding)
    # before consumers re-certify against the new authority.
    revoke_codex_read_contexts()
    bounded_lookup(
        "codex-amendment-executor",
        purpose="amendment-verification",
        scope=("codex:identity",),
        reader=lambda ctx: ctx.codex_identity(),
        dual_key_grant=_amendment_grant(),
    )
    re_certify_permission_sovereign()
    return "reloaded", {"amendment_id": payload.get("amendment_id")}


class CodexAmendmentBus:
    """Information-layer bus for codex amendment commands and events."""

    def __init__(self, audit: Any | None = None) -> None:
        self.gateway = InformationChannelGateway(
            handler=self._route,
            audit=audit,
        )

    async def _route(
        self,
        command: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        if command == "codex.amend":
            return await _amendment_executor(command, payload)
        if command == "codex.applied":
            return await _amendment_consumer(command, payload)
        return "ignored", {"reason": "unknown command"}

    async def submit(
        self,
        source_path: Path | str,
        previous_version: str,
        new_version: str,
        signed_by: str,
        project_root: Path | str,
    ) -> dict[str, Any]:
        """Dispatch a full-replacement codex amendment."""
        source = Path(source_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"codex source not found: {source}")

        import hashlib
        payload_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        amendment_id = f"codex-amend-{new_version}-{payload_hash[:16]}"

        return await self.gateway.dispatch(
            sender="governance-rule",
            destination="main-system",
            command="codex.amend",
            payload={
                "project_root": str(Path(project_root).resolve()),
                "source_path": str(source.relative_to(Path(project_root).resolve())),
                "amendment_id": amendment_id,
                "previous_version": previous_version,
                "new_version": new_version,
                "signed_by": signed_by,
                "sha256": payload_hash,
            },
        )

    async def broadcast_applied(self, amendment_id: str, new_version: str) -> None:
        """Notify all authorized consumers that a new codex is in effect."""
        await self.gateway.dispatch(
            sender="main-system",
            destination="*",
            command="codex.applied",
            payload={
                "amendment_id": amendment_id,
                "new_version": new_version,
            },
        )


__all__ = ["CodexAmendmentBus", "_amendment_executor", "_amendment_consumer"]
