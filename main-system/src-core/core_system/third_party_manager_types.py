"""Third-Party Manager — constants, data structures, and helpers.

Provides the constants, dataclasses, and helper functions used by
the ThirdPartyManager.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .versioning import component_version

THIRD_PARTY_MANAGER_VERSION = component_version("third-party-manager")

# Tools that can be auto-updated by the manager (safe, self-contained).
AUTO_UPDATABLE_TOOLS: frozenset[str] = frozenset({"uv", "npm", "ollama", "electron"})

# Capabilities that prove a governed approval token before any third-party
# update may execute.
THIRD_PARTY_UPDATE_APPROVAL_CAPABILITIES: frozenset[str] = frozenset(
    {"hot-update", "hot-reload"}
)

# Probe commands: tool_id → (command, args, version_regex)
_PROBE_COMMANDS: dict[str, tuple[str, list[str], str]] = {
    "git": ("git", ["--version"], r"git version (\S+)"),
    "python": ("python", ["--version"], r"Python (\S+)"),
    "node": ("node", ["--version"], r"v(\S+)"),
    "npm": ("npm.cmd", ["--version"], r"(\S+)"),
    "uv": ("uv", ["--version"], r"uv (\S+)"),
    "ollama": ("ollama", ["--version"], r"version\s+(?:is\s+)?(\S+)"),
    "qdrant": ("qdrant", ["--version"], r"qdrant (?:version )?(\S+)"),
    "postgresql": ("psql", ["--version"], r"psql \(PostgreSQL\) (\S+)"),
}

# Update commands: tool_id → (command, args)
_UPDATE_COMMANDS: dict[str, tuple[str, list[str]]] = {
    "uv": ("uv", ["self", "update"]),
    "npm": ("npm.cmd", ["install", "-g", "npm@latest"]),
    "ollama": ("ollama", ["update"]),
}


def _background_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_version(version: str) -> str:
    """Normalize a version string for comparison (strip v prefix, build suffixes)."""
    v = version.strip()
    if len(v) > 1 and v[0] in ("v", "V") and v[1].isdigit():
        v = v[1:]
    v = re.split(r"[+\s]", v, maxsplit=1)[0]
    return v.casefold()


@dataclass
class ToolVersionInfo:
    """Detected version information for a single tool."""

    tool_id: str
    recorded_version: str = ""
    detected_version: str = ""
    detected: bool = False
    path: str = ""
    last_probed_at: str = ""
    error: str = ""

    @property
    def version_matches(self) -> bool:
        if not self.detected or not self.recorded_version:
            return False
        return _normalize_version(self.detected_version) == _normalize_version(
            self.recorded_version
        )

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        if not self.detected:
            return "missing"
        if self.recorded_version and not self.version_matches:
            return "version-drift"
        return "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "recorded_version": self.recorded_version,
            "detected_version": self.detected_version,
            "detected": self.detected,
            "path": self.path,
            "last_probed_at": self.last_probed_at,
            "error": self.error,
            "status": self.status,
            "version_matches": self.version_matches,
        }


@dataclass
class UpdateCheckResult:
    """Result of checking for available updates."""

    tool_id: str
    auto_updatable: bool = False
    current_version: str = ""
    latest_version: str = ""
    update_available: bool = False
    last_checked_at: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "auto_updatable": self.auto_updatable,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "update_available": self.update_available,
            "last_checked_at": self.last_checked_at,
            "error": self.error,
        }


@dataclass
class UpdateExecutionResult:
    """Result of executing an update."""

    tool_id: str
    ok: bool = False
    before_version: str = ""
    after_version: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    executed_at: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "ok": self.ok,
            "before_version": self.before_version,
            "after_version": self.after_version,
            "exit_code": self.exit_code,
            "executed_at": self.executed_at,
            "error": self.error,
            "stdout_bytes": len(self.stdout.encode("utf-8", errors="replace")),
            "stderr_bytes": len(self.stderr.encode("utf-8", errors="replace")),
        }


__all__ = [
    "THIRD_PARTY_MANAGER_VERSION",
    "AUTO_UPDATABLE_TOOLS",
    "THIRD_PARTY_UPDATE_APPROVAL_CAPABILITIES",
    "_PROBE_COMMANDS",
    "_UPDATE_COMMANDS",
    "_background_subprocess_kwargs",
    "_iso_now",
    "_normalize_version",
    "ToolVersionInfo",
    "UpdateCheckResult",
    "UpdateExecutionResult",
]
