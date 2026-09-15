"""global-cleaner consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "vaultly" / "src" / "backend" / "services"),
    # Own tool path last: insert(0) makes it win over other tools' `backend`
    # packages (several tools ship a top-level `backend` package).
    str(_ROOT / "Standalone tools" / "global-cleaner" / "src"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p

########################################################################
# source: global-cleaner/tests/test_main_system_governance_health.py
########################################################################
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MAIN_CORE = ROOT / "main-system" / "src-core"
SHARED_LAYER_SRC = ROOT / "shared-layer" / "src"
for source_root in (MAIN_CORE, SHARED_LAYER_SRC):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))


from core_system.governance_runtime import MainSystemGovernance  # noqa: E402
from tasks.toolbox_service import ToolboxService  # noqa: E402


class _IntegrityAuthentication:
    def __init__(self, *, denied: bool = False) -> None:
        self.denied = denied
        self.calls = 0

    def verify_runtime_integrity(self) -> None:
        self.calls += 1
        if self.denied:
            raise PermissionError("PERMISSION_DENIED")


def _runtime(authentication: _IntegrityAuthentication) -> MainSystemGovernance:
    runtime = MainSystemGovernance.__new__(MainSystemGovernance)
    runtime._authentication = authentication
    runtime._project_root = Path.cwd()
    runtime._integrity_ready = True
    runtime._integrity_checked_at = 0.0
    runtime._integrity_cache = {}
    return runtime


def test_runtime_integrity_health_reports_current_authority() -> None:
    authentication = _IntegrityAuthentication()
    runtime = _runtime(authentication)

    assert runtime.runtime_integrity_ready(max_age_seconds=0) is True
    assert authentication.calls == 1


def test_runtime_integrity_health_rejects_stale_launch_credential() -> None:
    authentication = _IntegrityAuthentication(denied=True)
    runtime = _runtime(authentication)

    assert runtime.runtime_integrity_ready(max_age_seconds=0) is False
    assert authentication.calls == 1


def test_daily_cleaner_uses_governed_source_without_opening_ui() -> None:
    manifest = {
        "launch": {
            "primary": "executable",
            "background": "governed-source-channel",
        },
        "request_channel": {
            "model": "governance-authenticated-shared-layer",
            "runtime_entry": "src/channel_runtime.py",
            "direct_instruction": "PERMISSION_DENIED",
        },
    }

    assert ToolboxService._source_launch_requested(
        manifest,
        background=True,
        requested_mode="source",
        executable_exists=True,
    ) is True
    assert ToolboxService._source_launch_requested(
        manifest,
        background=False,
        requested_mode="",
        executable_exists=True,
    ) is False
    assert ToolboxService._source_launch_requested(
        manifest,
        background=True,
        requested_mode="executable",
        executable_exists=True,
    ) is False


def test_global_cleaner_dual_runtime_selects_an_available_mode() -> None:
    manifest = {
        "launch": {
            "mode": "dual-runtime",
            "selection": "automatic",
            "runtimes": ["governed-source-ui", "executable"],
            "background": "governed-source-channel",
        },
        "request_channel": {
            "model": "governance-authenticated-shared-layer",
            "runtime_entry": "src/channel_runtime.py",
            "direct_instruction": "PERMISSION_DENIED",
        },
    }

    assert ToolboxService._source_launch_requested(
        manifest,
        background=False,
        requested_mode="",
        executable_exists=False,
    ) is True
    assert ToolboxService._source_fallback_allowed(manifest, "executable") is True
    assert ToolboxService._executable_fallback_allowed(
        manifest,
        "source",
        executable_exists=True,
    ) is True
    assert ToolboxService._executable_fallback_allowed(
        manifest,
        "source",
        executable_exists=False,
    ) is False
    assert ToolboxService._source_launch_requested(
        manifest,
        background=False,
        requested_mode="executable",
        executable_exists=False,
    ) is False
