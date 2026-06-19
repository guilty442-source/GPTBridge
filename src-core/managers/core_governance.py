from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from settings.config import load_config


ALLOWED_CORE_FAILURES = {
    "startup_failure",
    "provider_failure",
    "ipc_failure",
    "websocket_failure",
    "browser_session_failure",
    "orchestrator_failure",
    "developer_authorized",
}

CORE_PATH_PREFIXES = (
    "src-core",
    "src-ui/main",
    "src-ui/renderer",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "tsconfig.main.json",
    "vite.config.ts",
    "start-hidden.vbs",
)

CORE_WRITE_DENIED_ACTORS = {
    "design",
    "self_check",
    "settings",
}

GEMINI_CODE_ASSIST_DENIED_ALIASES = {
    "gemini_code_assist",
    "gemini-code-assist",
    "gemini code assist",
    "google_gemini_code_assist",
    "google-gemini-code-assist",
    "google gemini code assist",
}


def _normalize_actor(actor: str) -> str:
    lowered = actor.strip().lower()
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


def is_write_denied_actor(actor: str) -> bool:
    raw = actor.strip().lower()
    normalized = _normalize_actor(actor)
    tokens = set(normalized.split())

    if raw in CORE_WRITE_DENIED_ACTORS or normalized in CORE_WRITE_DENIED_ACTORS:
        return True

    if raw in GEMINI_CODE_ASSIST_DENIED_ALIASES or normalized in GEMINI_CODE_ASSIST_DENIED_ALIASES:
        return True

    return (
        "gemini" in tokens
        and "assist" in tokens
        and ("code" in tokens or "coder" in tokens)
    )


@dataclass(frozen=True)
class CoreGovernanceDecision:
    ok: bool
    reason: str
    blocked_paths: tuple[str, ...] = ()


class CoreGovernance:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    def is_locked(self) -> bool:
        return bool(load_config().get("core_lock_enabled", True))

    def is_core_path(self, path: Path | str) -> bool:
        rel_path = self._relative_path(path)
        rel_posix = rel_path.as_posix()
        return any(rel_posix == prefix or rel_posix.startswith(f"{prefix}/") for prefix in CORE_PATH_PREFIXES)

    def block_direct_core_writes(self, actor: str, paths: Iterable[Path | str]) -> CoreGovernanceDecision:
        blocked = tuple(sorted(self._relative_path(path).as_posix() for path in paths if self.is_core_path(path)))
        if is_write_denied_actor(actor) and blocked:
            return CoreGovernanceDecision(
                ok=False,
                reason=f"{actor} cannot modify core-system files while Core Lock is enabled",
                blocked_paths=blocked,
            )
        return CoreGovernanceDecision(ok=True, reason="no blocked core paths")

    def authorize_core_change(
        self,
        actor: str,
        paths: Iterable[Path | str],
        failure_reason: str = "",
        override: bool = False,
    ) -> CoreGovernanceDecision:
        blocked = tuple(sorted(self._relative_path(path).as_posix() for path in paths if self.is_core_path(path)))
        if not blocked:
            return CoreGovernanceDecision(ok=True, reason="no core paths requested")

        if is_write_denied_actor(actor):
            return CoreGovernanceDecision(
                ok=False,
                reason=f"{actor} is not allowed to modify core-system files",
                blocked_paths=blocked,
            )

        if not self.is_locked():
            return CoreGovernanceDecision(ok=True, reason="Core Lock disabled by configuration", blocked_paths=blocked)

        if override:
            return CoreGovernanceDecision(ok=True, reason="explicit user override", blocked_paths=blocked)

        normalized_reason = failure_reason.strip().lower()
        if normalized_reason in ALLOWED_CORE_FAILURES:
            return CoreGovernanceDecision(ok=True, reason=normalized_reason, blocked_paths=blocked)

        return CoreGovernanceDecision(
            ok=False,
            reason="Core Lock is enabled and no approved failure or override was provided",
            blocked_paths=blocked,
        )

    def require_no_core_paths(self, actor: str, paths: Iterable[Path | str]) -> None:
        decision = self.block_direct_core_writes(actor, paths)
        if not decision.ok:
            blocked = ", ".join(decision.blocked_paths)
            raise PermissionError(f"{decision.reason}: {blocked}")

    def _relative_path(self, path: Path | str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            return Path(candidate.as_posix())
        try:
            return candidate.resolve().relative_to(self.project_root)
        except ValueError:
            return Path(candidate.name)
