#!/usr/bin/env python
"""GPTBridge backend launcher — prepares environment and delegates to boot_core.

This script bridges the gap between the launcher (start.ps1) and the Python
backend by generating the GPTBRIDGE_GOVERNANCE_BOOTSTRAP token in-process and
then starting boot_core.py, which in turn spawns and supervises main.py --serve.

NOTE: This is a DEVELOPMENT-ONLY shortcut.  It bypasses the startup_orchestrator
dependency probes (assumes READY).  For production launches use
launcher/scripts/start.ps1 instead.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

# Windows: suppress console window for background subprocess calls
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]


def generate_bootstrap_token(project_root: Path) -> str:
    """Generate a governance bootstrap token for the main-system."""
    sys.path.insert(0, str(project_root))
    sys.path.insert(0, str(project_root / "main-system" / "src-core"))
    sys.path.insert(0, str(project_root / "shared-layer" / "src"))

    from dataclasses import asdict
    from governance_rule.execution.authentication import sign_launcher_attestation
    from governance_rule.execution.integrity import build_integrity_manifest

    launcher_key = secrets.token_bytes(32)
    issued_at = int(time.time())
    key_id = secrets.token_hex(16)
    integrity = build_integrity_manifest(
        project_root, launcher_key, issued_at=issued_at, key_id=key_id
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


def main() -> int:
    project_root = WORKSPACE_ROOT.resolve()
    token = generate_bootstrap_token(project_root)
    os.environ["GPTBRIDGE_GOVERNANCE_BOOTSTRAP"] = token
    os.environ["GPTBRIDGE_PROJECT_ROOT"] = str(project_root)
    os.environ.setdefault("GPTBRIDGE_STARTUP_STATE", "READY")

    # Load PostgreSQL DSNs from User environment if not already set in Process
    for name in (
        "GPTBRIDGE_POSTGRES_DSN",
        "GPTBRIDGE_POSTGRES_ADMIN_DSN",
        "GPTBRIDGE_MODULE_DSNS",
        "GPTBRIDGE_XINGCHENG_IDENTITY_DSN",
        "GPTBRIDGE_XINGCHENG_COGNITION_DSN",
    ):
        if not os.environ.get(name):
            # On Windows, check User-scoped env via registry-like approach
            import ctypes
            try:
                hkey = ctypes.windll.advapi32.RegOpenKeyExW(
                    ctypes.c_void_p(0x80000001),  # HKEY_CURRENT_USER
                    "Environment",
                    0,
                    0x20019,  # KEY_READ
                )
                buf = ctypes.create_unicode_buffer(4096)
                size = ctypes.c_uint32(4096)
                rc = ctypes.windll.advapi32.RegQueryValueExW(
                    hkey, name, None, None, buf, ctypes.byref(size),
                )
                ctypes.windll.advapi32.RegCloseKey(hkey)
                if rc == 0:
                    os.environ[name] = buf.value
            except Exception:
                pass

    boot_core = project_root / "main-system" / "src-core" / "boot_core.py"
    cmd = [sys.executable, str(boot_core), "--serve", "--auto-kill-backend-port"]
    print(f"[start_backend] Launching boot_core...", file=sys.stderr)
    print(f"[start_backend] Project root: {project_root}", file=sys.stderr)
    result = subprocess.run(cmd, cwd=str(project_root / "main-system"), creationflags=_CREATE_NO_WINDOW)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
