"""BootCore state management mixin — state, paths, governance bootstrap.

Provides state file I/O, runtime path setup, and governance bootstrap
token generation for the BootCore supervisor.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from typing import Any


class BootCoreStateMixin:
    """State management methods for BootCore."""

    # --- runtime paths (shared by governance bootstrap + phase imports) ---

    def _ensure_runtime_paths(self) -> None:
        workspace = str(self.workspace_root)
        src_core = str(self.workspace_root / "main-system" / "src-core")
        shared = str(self.workspace_root / "shared-layer" / "src")
        for p in (workspace, src_core, shared):
            if p not in sys.path:
                sys.path.insert(0, p)
        os.environ["GPTBRIDGE_PROJECT_ROOT"] = workspace

    # --- governance bootstrap ---

    def _generate_governance_bootstrap(self) -> str:
        """Generate a fresh governance bootstrap token for the backend."""
        self._ensure_runtime_paths()
        from governance_rule.execution.authentication import sign_launcher_attestation
        from governance_rule.execution.integrity import build_integrity_manifest

        workspace = str(self.workspace_root)
        launcher_key = secrets.token_bytes(32)
        issued_at = int(time.time())
        key_id = secrets.token_hex(16)
        integrity = build_integrity_manifest(workspace, launcher_key, issued_at=issued_at, key_id=key_id)
        attestation = sign_launcher_attestation(
            launcher_key,
            actor="governance/main-system",
            bound_tool_id="main-system",
            caller_path="main-system/src-core/main.py",
            process_id=os.getpid(),
            issued_at=issued_at,
            key_id=key_id,
        )
        payload = {
            "format_version": 1,
            "launcher_key": base64.b64encode(launcher_key).decode("ascii"),
            "integrity_manifest": {k: v for k, v in integrity.__dict__.items()},
            "identity_attestation": {k: v for k, v in attestation.__dict__.items()},
        }
        return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")

    # --- state ---

    def _write_state(self, **extra: object) -> None:
        payload = {
            "role": "boot-core",
            "status": self._status,
            "pid": os.getpid(),
            "backend_pid": self._child.pid if self._child is not None else None,
            "restarts": self._restarts,
            "max_restarts": self._max_restarts,
            "last_exit": self._last_exit,
            "updated_at": _iso_now(),
            **extra,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        except OSError:
            pass

    def _write_orchestrator_report(self, report: dict[str, Any]) -> None:
        """Write orchestrator report for decision_sovereign consumption."""
        path = (
            self.workspace_root
            / "main-system"
            / "launcher"
            / "state"
            / "orchestrator-report.json"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            pass


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()
