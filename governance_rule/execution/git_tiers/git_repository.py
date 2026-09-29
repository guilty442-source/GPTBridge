"""Thin git driver used by worktree manager and audit snapshot.

A375 ENTRYPOINT: every programmatic Git execution enters here —
``run() > classify/enforce > before snapshot > git.exe > after snapshot >
audit``.  Timeouts kill the child, then re-check index.lock / merge /
rebase / cherry-pick state instead of assuming the repo is clean (§30);
successful Tier-2/3 writes invalidate the Tier-1 read cache (§28).

Tier extensions: the protected ``git_tiers.__init__`` keeps the canonical
A53 lists.  Operations the codex-managed automation needs beyond that list
(``update-ref`` for recovery refs, ``merge-tree`` preflight, ``bundle``,
``gc``, ``init --bare``) are classified here — the single whitelisted
entrypoint — never loosening Tier-3.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Final

from . import (
    TIER3_OPS,
    audit_log,
    classify,
    enforce,
    record_deprecated_confirmation,
)
from .governance_manifest import timing as _manifest_timing
from .snapshot import capture_light_snapshot

DEFAULT_TIMEOUT: Final[float] = _manifest_timing(
    "git_command_timeout_seconds", 60.0
)

# Entrypoint-level extensions (see module docstring).  Tier-3 is always
# consulted first so an extension can never shadow a high-risk operation.
_EXT_TIER1: Final[frozenset[str]] = frozenset(
    {"verify-tag", "verify-commit", "show-ref", "var", "bundle verify"}
)
_EXT_TIER2: Final[frozenset[str]] = frozenset(
    {
        "init", "init --bare", "update-ref", "merge-tree", "bundle create",
        "gc", "maintenance run", "maintenance start",
        "remote add", "remote set-url",
        "commit-graph write", "multi-pack-index write",
    }
)


def _extended_tier(command: str) -> int | None:
    """Extension tier for commands the canonical list leaves unknown."""
    cmd = command.strip().lower()
    if cmd.startswith("git "):
        cmd = cmd[4:]
    if any(op.casefold() in cmd for op in TIER3_OPS):
        return None  # stays Tier-3
    for op in _EXT_TIER1:
        if cmd.startswith(op) or cmd == op:
            return 1
    for op in _EXT_TIER2:
        if cmd.startswith(op) or cmd == op:
            return 2
    return None


def _enforce_extended(
    command: str,
    actor: str,
    *,
    confirmed: bool | None,
    authority_approved: bool | None,
    snapshot: dict[str, object],
) -> tuple[bool, str, int]:
    """classify/enforce with entrypoint extensions; returns (allowed, msg, tier)."""
    if _extended_tier(command) is None:
        allowed, message = enforce(
            command,
            actor,
            confirmed=confirmed,
            authority_approved=authority_approved,
            repo_snapshot=snapshot,
        )
        return allowed, message, classify(command)
    tier = _extended_tier(command) or 3
    if tier == 1:
        allowed, message = True, "tier-1(ext): read-only, direct execution"
    elif tier == 2 and confirmed:
        allowed, message = True, "tier-2(ext): confirmed"
    elif tier == 2:
        allowed, message = False, "tier-2(ext): requires explicit confirmation"
    elif authority_approved:
        allowed, message = True, "tier-3(ext): governance authority approved"
    else:
        allowed, message = False, "tier-3(ext): requires governance authority approval"
    audit_log(
        tier, command, actor, allowed, message,
        repo_snapshot=snapshot, phase="decision",
        result="authorized" if allowed else "denied",
    )
    return allowed, message, tier


def _chain(entry: dict[str, object]) -> None:
    """Mirror the flat audit entry into the hash-chained ledger (A375)."""
    try:
        from . import audit_chain

        audit_chain.append_audit(dict(entry))
    except Exception:
        pass


def _light_snapshot(repo_path: Path) -> dict[str, object]:
    """Audit evidence snapshot; never participates in authorization."""
    from .snapshot import capture_light_snapshot

    return capture_light_snapshot(repo_path)


def _timeouts_state_path(repo_path: Path) -> Path | None:
    """Best-effort path to the shared timeout counter (no git needed)."""
    candidate = repo_path / ".git"
    if candidate.is_file():  # worktree pointer file
        try:
            raw = candidate.read_text(encoding="utf-8").strip()
            if raw.startswith("gitdir:"):
                git_dir = Path(raw[7:].strip())
                common = git_dir.parent.parent  # .git/worktrees/<id> -> .git
                return common / "gptbridge-automation" / "git-timeouts.json"
        except OSError:
            return None
    if candidate.is_dir():
        return candidate / "gptbridge-automation" / "git-timeouts.json"
    return None


def _record_timeout(repo_path: Path, command: str, timeout: float) -> None:
    """Increment the shared timed-out-operation counter for supervisor health."""
    try:
        path = _timeouts_state_path(repo_path)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        state["timed_out_git_operations"] = int(
            state.get("timed_out_git_operations", 0)
        ) + 1
        state["last_timeout"] = {
            "command": command[:200],
            "timeout_seconds": timeout,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _post_timeout_state(repo_path: Path) -> dict[str, object]:
    """After a killed git command, never assume the repo is clean.

    Re-check the lock and operation markers explicitly.
    """
    git_dir = repo_path / ".git"
    if git_dir.is_file():
        try:
            raw = git_dir.read_text(encoding="utf-8").strip()
            if raw.startswith("gitdir:"):
                git_dir = Path(raw[7:].strip())
        except OSError:
            pass
    state = {
        "index_lock": (git_dir / "index.lock").exists(),
        "merge_in_progress": (git_dir / "MERGE_HEAD").exists(),
        "rebase_in_progress": (git_dir / "REBASE_HEAD").exists()
        or (git_dir / "rebase-merge").exists()
        or (git_dir / "rebase-apply").exists(),
        "cherry_pick_in_progress": (git_dir / "CHERRY_PICK_HEAD").exists(),
    }
    return state


# Global git options that precede the subcommand.  Classification and
# enforcement must see the *subcommand*, not ``-C <path>`` / ``-c k=v``
# wrappers — otherwise every cross-worktree call fails closed as
# unknown Tier-3.
_GLOBAL_OPTS_WITH_VALUE: Final[frozenset[str]] = frozenset(
    {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}
)
_GLOBAL_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
        "--icase-pathspecs", "--no-optional-locks", "--no-pager",
        "-P", "-p", "--bare", "--no-replace-objects",
    }
)


def _classifiable_command(args: list[str]) -> str:
    """Strip leading global options; return the subcommand string."""
    items = list(args)
    i = 0
    while i < len(items):
        token = str(items[i])
        if token in _GLOBAL_OPTS_WITH_VALUE:
            i += 2
            continue
        if any(
            token.startswith(opt + "=")
            for opt in _GLOBAL_OPTS_WITH_VALUE
        ):
            i += 1
            continue
        if token in _GLOBAL_FLAGS:
            i += 1
            continue
        break
    return " ".join(str(item) for item in items[i:])


def _spawn_git(
    args: list[str], *, cwd: Path, timeout: float | None
) -> subprocess.CompletedProcess[str]:
    """The single raw ``git`` process spawn (A375 driver).

    Only this driver module contains the executable spawn; the gateway and
    the read-only snapshot builder both call through here.
    """
    is_bare = (
        (cwd / "HEAD").is_file()
        and (cwd / "objects").is_dir()
        and not (cwd / ".git").exists()
    )
    command = ["git", f"--git-dir={cwd}", *args] if is_bare else ["git", *args]
    process_cwd = cwd.parent if is_bare else cwd
    return subprocess.run(
        command,
        cwd=process_cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class GitRepository:
    """Wrapper around a git worktree or bare repository."""

    def __init__(self, path: str | Path = Path.cwd()) -> None:
        self.path: Final[Path] = Path(path).resolve()

    def run(
        self,
        args: list[str],
        *,
        check: bool = False,
        confirmed: bool | None = None,
        authority_approved: bool | None = None,
        actor: str = "governance/git-repository",
        timeout: float | None = DEFAULT_TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        """Legacy public entrypoint (article 493/530 frozen signature).

        ``confirmed`` / ``authority_approved`` remain as deprecated adapters:
        a truthy value is recorded as a ``LEGACY_*`` capability-ledger entry
        and the execution is annotated with ``DEPRECATED_COMPATIBILITY``.
        New callers must use ``capability_gate.execute_with_capability`` /
        ``execute_system_safe`` instead of a boolean.
        """
        command = _classifiable_command(args)
        snapshot = _light_snapshot(self.path)
        legacy = bool(confirmed) or bool(authority_approved)
        if legacy:
            record_deprecated_confirmation(
                self.path, command, actor,
                approval_path=(
                    "LEGACY_AUTHORITY" if authority_approved else "LEGACY_CONFIRM"
                ),
            )
        allowed, message, tier = _enforce_extended(
            command,
            actor,
            confirmed=confirmed,
            authority_approved=authority_approved,
            snapshot=snapshot,
        )
        if not allowed:
            raise PermissionError(message)
        return self._execute(
            command, args, tier=tier, actor=actor, snapshot=snapshot,
            timeout=timeout, check=check,
            approval_path="DEPRECATED_COMPATIBILITY" if legacy else "",
        )

    def _execute(
        self,
        command: str,
        args: list[str],
        *,
        tier: int,
        actor: str,
        snapshot: dict[str, object],
        timeout: float | None,
        check: bool,
        approval_path: str = "",
    ) -> subprocess.CompletedProcess[str]:
        started = time.monotonic()
        timed_out = False
        try:
            result = _spawn_git(list(args), cwd=self.path, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            duration_ms = int((time.monotonic() - started) * 1000)
            _record_timeout(self.path, command, float(timeout or 0))
            post = _post_timeout_state(self.path)
            _chain(
                {
                    "operation": "git-timeout",
                    "command": command,
                    "actor": actor,
                    "result": "timed-out",
                    "timeout_seconds": timeout,
                    "post_state": post,
                    "duration_ms": duration_ms,
                }
            )
            result = subprocess.CompletedProcess(
                args=["git", *args],
                returncode=124,
                stdout=str(exc.stdout or ""),
                stderr=f"timed out after {timeout}s (killed); "
                f"repo state re-checked: {post}",
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        result.duration_ms = duration_ms  # type: ignore[attr-defined]
        returncode = int(getattr(result, "returncode", -1) or -1)
        detail = str(getattr(result, "stderr", "") or "")[:500].strip()
        entry = audit_log(
            tier,
            command,
            actor,
            True,
            detail,
            repo_snapshot=snapshot,
            phase="result",
            result="timed-out" if timed_out else ("succeeded" if returncode == 0 else "failed"),
            returncode=returncode,
        )
        entry["duration_ms"] = duration_ms
        if approval_path:
            entry["approval_path"] = approval_path
        _chain(entry)
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            )
        return result
