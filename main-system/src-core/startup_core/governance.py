from __future__ import annotations

import base64
import json
import os
import secrets
import time
from dataclasses import asdict

class GovernanceMixin:
    def _generate_governance_bootstrap(self) -> str:
        """Generate a fresh governance bootstrap token for main.py.

        Called before each spawn (and re-spawn) so the 30-second expiry
        window in the identity attestation is always fresh.
        """
        self._ensure_runtime_paths()

        from dataclasses import asdict
        from governance_rule.execution.authentication import (
            sign_launcher_attestation,
        )
        from governance_rule.execution.integrity import build_integrity_manifest

        workspace = self.workspace_root
        launcher_key = secrets.token_bytes(32)
        issued_at = int(time.time())
        key_id = secrets.token_hex(16)
        integrity = build_integrity_manifest(
            workspace, launcher_key, issued_at=issued_at, key_id=key_id
        )
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
            "integrity_manifest": asdict(integrity),
            "identity_attestation": asdict(attestation),
        }
        return base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
