"""Central bare-repo server hooks — install + health verification (§16).

The central receiver ``E:\\GPTBridge.git`` must carry a *real*
``hooks/pre-receive`` — not just the source template.  The deployed hook
is a thin stub that delegates to the governed implementation
``governance_rule/git-hooks/pre-receive`` inside the main worktree.

``server_hook_health`` verifies:
  * hook exists and is executable/readable with a valid shebang
  * hook content hash matches the deployment manifest
  * the delegate target exists and matches its recorded hash
  * a Python runtime is available
  * the governance module is importable

Result: HEALTHY | DEGRADED | ERROR.  When not HEALTHY the receiver is
fail-closed: ``central_write_enabled = False``.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .central import central_path

GOVERNED_HOOK = Path("governance_rule/git-hooks/pre-receive")
HOOK_MANIFEST = "hook-manifest.json"

_STUB_TEMPLATE = '''#!/usr/bin/env python
"""GPTBridge governed pre-receive delegate stub (central bare repo).

Deployed and verified by git_tiers.server_hooks — do not edit here.
"""
import os
import runpy
import sys

os.environ.setdefault("GPTBRIDGE_PROJECT_ROOT", {root!r})
os.environ["GPTBRIDGE_CENTRAL_REPO"] = "1"
runpy.run_path({delegate!r}, run_name="__main__")
'''


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _delegate(root: str | Path) -> Path:
    return Path(root).resolve() / GOVERNED_HOOK


def hook_stub_text(root: str | Path) -> str:
    return _STUB_TEMPLATE.format(
        root=str(Path(root).resolve()),
        delegate=str(_delegate(root)),
    )


def install_central_hook(root: str | Path, central_dir: str | Path | None = None) -> dict[str, Any]:
    """Deploy the pre-receive stub + manifest into the central bare repo."""
    central = Path(central_dir) if central_dir else central_path(root)
    hooks_dir = central / "hooks"
    if not central.is_dir():
        return {"installed": False, "error": "central repo missing"}
    hooks_dir.mkdir(parents=True, exist_ok=True)

    delegate = _delegate(root)
    if not delegate.is_file():
        return {"installed": False, "error": f"delegate missing: {delegate}"}

    hook_path = hooks_dir / "pre-receive"
    text = hook_stub_text(root)
    hook_path.write_text(text, encoding="utf-8")
    try:
        hook_path.chmod(0o755)
    except OSError:
        pass

    manifest = {
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hook": "pre-receive",
        "hook_sha256": _sha256(hook_path),
        "delegate": str(delegate),
        "delegate_sha256": _sha256(delegate),
        "python": sys.executable,
    }
    (hooks_dir / HOOK_MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"installed": True, "hook": str(hook_path), "manifest": manifest}


def server_hook_health(root: str | Path, central_dir: str | Path | None = None) -> dict[str, Any]:
    """Verify the central pre-receive hook; fail-closed when unhealthy."""
    central = Path(central_dir) if central_dir else central_path(root)
    checks: dict[str, Any] = {}
    status = "HEALTHY"

    def fail(name: str, detail: str, level: str = "ERROR") -> None:
        nonlocal status
        checks[name] = {"ok": False, "detail": detail}
        if level == "ERROR":
            status = "ERROR"
        elif status == "HEALTHY":
            status = "DEGRADED"

    hook_path = central / "hooks" / "pre-receive"
    manifest_path = central / "hooks" / HOOK_MANIFEST

    checks["central_exists"] = {"ok": central.is_dir(), "detail": str(central)}
    if not central.is_dir():
        fail("central_exists", "central bare repo missing")

    if hook_path.is_file():
        text = hook_path.read_text(encoding="utf-8", errors="replace")
        executable = text.startswith("#!") and os.access(
            hook_path, os.R_OK
        )
        checks["hook_exists"] = {"ok": True, "detail": str(hook_path)}
        checks["hook_executable"] = {
            "ok": executable,
            "detail": "shebang+readable" if executable else "not executable",
        }
        if not executable:
            fail("hook_executable", "pre-receive is not executable")
    else:
        fail("hook_exists", "pre-receive hook missing")
        text = ""

    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fail("manifest", "manifest unreadable", level="DEGRADED")
    else:
        fail("manifest", "hook manifest missing", level="DEGRADED")

    if manifest.get("hook_sha256"):
        actual = _sha256(hook_path)
        ok = actual == manifest["hook_sha256"]
        checks["hook_hash"] = {"ok": ok, "detail": "sha256 match" if ok else "hash mismatch"}
        if not ok:
            fail("hook_hash", "deployed hook differs from manifest")
    elif hook_path.is_file():
        expected = hashlib.sha256(hook_stub_text(root).encode("utf-8")).hexdigest()
        ok = _sha256(hook_path) == expected
        checks["hook_hash"] = {"ok": ok, "detail": "template match" if ok else "unexpected content"}
        if not ok:
            fail("hook_hash", "hook content unexpected", level="DEGRADED")

    delegate = _delegate(root)
    if delegate.is_file():
        ok = True
        if manifest.get("delegate_sha256"):
            ok = _sha256(delegate) == manifest["delegate_sha256"]
        checks["delegate"] = {"ok": ok, "detail": str(delegate)}
        if not ok:
            fail("delegate", "delegate hash mismatch")
    else:
        fail("delegate", f"delegate missing: {delegate}")

    python_ok = bool(sys.executable) and Path(sys.executable).is_file()
    checks["python_runtime"] = {"ok": python_ok, "detail": sys.executable}
    if not python_ok:
        fail("python_runtime", "no python runtime")

    try:
        import importlib.util

        spec = importlib.util.find_spec("governance_rule.execution.git_tiers")
        module_ok = spec is not None
    except Exception:
        module_ok = False
    checks["governance_module"] = {"ok": module_ok, "detail": "git_tiers importable"}
    if not module_ok:
        fail("governance_module", "governance module not loadable")

    return {
        "server_hook_health": status,
        "central_write_enabled": status == "HEALTHY",
        "checks": checks,
        "hook": str(hook_path),
    }


__all__ = [
    "GOVERNED_HOOK",
    "HOOK_MANIFEST",
    "hook_stub_text",
    "install_central_hook",
    "server_hook_health",
]
