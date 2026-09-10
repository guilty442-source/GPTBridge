"""Root containment constants and dataclasses — A201/E175, A202/E176.

Types and constants extracted from root_containment for source-size
compliance (A185/E160).  This module holds no verification or signal logic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Final

# ---------------------------------------------------------------------------
# Canonical roots (A201: CANONICAL-CODE-ROOT, A202: CANONICAL-ENVIRONMENT-ROOT)
# ---------------------------------------------------------------------------

CANONICAL_CODE_ROOT: Final[str] = r"E:\GPTBridge"
CANONICAL_ENVIRONMENT_ROOT: Final[str] = r"E:\AI"

# A202: REQUIRED-LAYOUT under E:\AI
ENVIRONMENT_REQUIRED_LAYOUT: Final[tuple[str, ...]] = (
    "runtimes",
    "environments",
    "packages",
    "toolchains",
    "models",
    "services",
    "caches",
)

# A202: EXCEPTION-1 — Windows 11 native tools at OS-owned paths
WINDOWS11_NATIVE_EXCEPTION: Final[str] = "windows-11-native-tools"

# A202: EXCEPTION-2 — Ollama at exact registered C-drive paths
OLLAMA_C_DRIVE_EXCEPTION: Final[str] = "ollama-c-drive"

# A201: FORBIDDEN — outside-root locations
FORBIDDEN_CODE_LOCATIONS: Final[tuple[str, ...]] = (
    "user-profile",
    "Desktop",
    "Documents",
    "Downloads",
    "AppData",
    "ProgramData",
    "Windows",
    "System",
    "other-drive",
    "UNC",
    "network",
    "share",
    "cloud-sync",
    "temp",
)

# A201: CHECKPOINTS — when to verify code root
CODE_ROOT_CHECKPOINTS: Final[tuple[str, ...]] = (
    "startup",
    "code-change",
    "tool-call",
    "Git-operation",
    "dependency-install",
    "generation",
    "build",
    "test",
    "package",
    "hot-update",
    "repair",
    "runtime-load",
)


# ---------------------------------------------------------------------------
# Code root containment verification result (A201/E175)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CodeRootCheck:
    """Result of verifying a path is within the canonical code root (A201)."""

    ok: bool
    requested_path: str
    resolved_path: str
    root: str
    is_descendant: bool
    violation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Environment root containment verification result (A202/E176)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnvironmentRootCheck:
    """Result of verifying a path is within the canonical environment root (A202)."""

    ok: bool
    requested_path: str
    resolved_path: str
    root: str
    is_descendant: bool
    is_windows11_native_exception: bool
    is_ollama_c_drive_exception: bool
    violation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Combined root containment report (A201 + A202)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RootContainmentReport:
    """Combined code-root and environment-root containment report."""

    code_root_ok: bool
    environment_root_ok: bool
    code_check: CodeRootCheck | None
    environment_check: EnvironmentRootCheck | None

    @property
    def ok(self) -> bool:
        return self.code_root_ok and self.environment_root_ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code_root_ok": self.code_root_ok,
            "environment_root_ok": self.environment_root_ok,
            "code_check": self.code_check.as_dict() if self.code_check else None,
            "environment_check": self.environment_check.as_dict() if self.environment_check else None,
            "basis": "A201/E175+A202/E176",
        }
