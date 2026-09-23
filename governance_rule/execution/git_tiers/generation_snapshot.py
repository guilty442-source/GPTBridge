"""Per-generation shared Git snapshot (G101 / blueprint §3.2–§3.3).

One capture per ``(worktree, generation)`` is shared by every in-process
gate — status, changed paths, ahead/behind and affected scope are
computed once instead of every consumer spawning its own git
subprocesses.

Freshness model:

- ``_probe`` (zero subprocess): ``.git`` HEAD content, resolved branch
  tip, index stat and packed-refs stat.  Any commit, stage, checkout or
  branch move changes the probe, so metadata is always fresh.
- Worktree edits that never touch ``.git`` are bounded by ``max_age_s``:
  callers needing hard dirty-state freshness pass ``0`` (one porcelain
  subprocess per call — same cost as before); automation ticks pass a
  positive TTL so a whole cycle shares one capture per worktree.
- ``notify_changed`` is the event-driven half (§3.3): in-process writers
  (self-commit, sync, hooks) invalidate eagerly; the periodic sweep
  remains as low-frequency insurance.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

_CACHE: dict[str, "GenerationSnapshot"] = {}
_GIT_DIRS: dict[str, tuple[Path, Path]] = {}


@dataclass(frozen=True)
class GenerationSnapshot:
    """One immutable view of a worktree at a single generation."""

    worktree: str
    generation: str
    head_oid: str = ""
    branch: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    detached: bool = False
    dirty: bool = False
    fingerprint: str = ""
    changed_paths: tuple[str, ...] = ()
    staged_paths: tuple[str, ...] = ()
    unstaged_paths: tuple[str, ...] = ()
    affected_scope: tuple[str, ...] = ()
    status_map: dict[str, str] = field(default_factory=dict)
    captured_at: float = 0.0


def scope_of(path: str) -> str:
    """Affected-scope label for a changed path (§3.5).

    ``Standalone tools/<tool>/`` and ``.worktrees/<name>/`` map to their
    second component (each independent tool / worktree is its own scope);
    other paths map to their top-level directory; root files map to
    ``"(root)"``.  Porcelain collapses untracked directories into
    ``<dir>/`` entries — those map to the directory itself.
    """
    is_dir_entry = path.endswith("/")
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts:
        return "(root)"
    if len(parts) > 1 and parts[0] in ("Standalone tools", ".worktrees"):
        return f"{parts[0]}/{parts[1]}"
    if is_dir_entry or len(parts) > 1:
        return parts[0]
    return "(root)"


def _resolve_git_dirs(worktree: Path) -> tuple[Path, Path] | None:
    """(git_dir, common_dir) — cached per worktree; pure filesystem after
    the first resolution (the layout never changes within a generation)."""
    key = os.path.normcase(str(worktree))
    cached = _GIT_DIRS.get(key)
    if cached is not None:
        return cached
    dotgit = worktree / ".git"
    try:
        if dotgit.is_dir():
            git_dir = dotgit
        elif dotgit.is_file():
            # Linked worktree: ``.git`` is a file with ``gitdir: <path>``.
            text = dotgit.read_text(encoding="utf-8", errors="replace")
            marker = "gitdir:"
            line = next(
                (ln for ln in text.splitlines() if ln.startswith(marker)),
                "",
            )
            if not line:
                return None
            git_dir = Path(line[len(marker):].strip())
            if not git_dir.is_absolute():
                git_dir = (worktree / git_dir).resolve()
        else:
            return None
        commondir_file = git_dir / "commondir"
        if commondir_file.is_file():
            common = git_dir / commondir_file.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
            common_dir = common.resolve()
        else:
            common_dir = git_dir
    except OSError:
        return None
    result = (git_dir, common_dir)
    _GIT_DIRS[key] = result
    return result


def _stat_key(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return "-"
    return f"{st.st_mtime_ns}:{st.st_size}"


def _probe(worktree: Path) -> str | None:
    """Zero-subprocess generation key from ``.git`` metadata.

    Covers commits (branch tip), staging (index), ref updates
    (packed-refs) and checkouts (HEAD).  ``None`` when the path is not a
    git worktree.
    """
    dirs = _resolve_git_dirs(worktree)
    if dirs is None:
        return None
    git_dir, common_dir = dirs
    try:
        head = (git_dir / "HEAD").read_text(
            encoding="utf-8", errors="replace"
        ).strip()
    except OSError:
        head = ""
    tip = ""
    if head.startswith("ref:"):
        ref = head[4:].strip()
        ref_file = common_dir / ref
        try:
            tip = ref_file.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            tip = ""  # packed — the packed-refs stat below still covers it
    else:
        tip = head
    # Index CONTENT hash, not mtime: ``git status`` refreshes the index's
    # cached stat data on every read, so mtime alone would invalidate the
    # probe on every capture.  Content hashing survives the no-op rewrite.
    try:
        index_key = hashlib.sha256(
            (git_dir / "index").read_bytes()
        ).hexdigest()[:16]
    except OSError:
        index_key = "-"
    return "|".join(
        [
            tip or head,
            index_key,
            _stat_key(common_dir / "packed-refs"),
            _stat_key(git_dir / "MERGE_HEAD"),
        ]
    )


def _capture(worktree: Path, generation: str) -> GenerationSnapshot:
    """``status -z --branch`` + ``diff --numstat`` → full snapshot (§3.2).

    Two subprocesses per capture shared by all consumers; the numstat
    covers content churn of already-dirty paths (porcelain xy does not),
    which keeps the debounce fingerprint semantics identical to the old
    ``_state_fingerprint``.
    """
    from .git_repository import GitRepository
    from .porcelain import status_v2

    repo = GitRepository(worktree)
    try:
        status = status_v2(repo)
        numstat = (repo.run(["diff", "--numstat", "HEAD"]).stdout or "")
    except Exception:  # noqa: BLE001 — snapshot is evidence, never raises
        return GenerationSnapshot(
            worktree=str(worktree),
            generation=generation,
            captured_at=time.monotonic(),
        )
    status_map = status.legacy_map()
    changed = status.changed_paths()
    fingerprint = hashlib.sha256(
        (
            "\n".join(
                f"{entry.xy} {entry.path}" for entry in status.entries
            )
            + numstat
        ).encode("utf-8")
    ).hexdigest()
    return GenerationSnapshot(
        worktree=str(worktree),
        generation=generation,
        head_oid=status.head_oid,
        branch=status.branch,
        upstream=status.upstream,
        ahead=status.ahead,
        behind=status.behind,
        detached=status.detached,
        dirty=not status.clean,
        fingerprint=fingerprint,
        changed_paths=changed,
        staged_paths=tuple(
            e.path for e in status.entries if e.staged
        ),
        unstaged_paths=tuple(
            e.path for e in status.entries if e.unstaged
        ),
        affected_scope=tuple(sorted({scope_of(p) for p in changed})),
        status_map=status_map,
        captured_at=time.monotonic(),
    )


def generation_snapshot(
    worktree: str | Path, *, max_age_s: float = 0.0
) -> GenerationSnapshot:
    """Shared snapshot for ``worktree``.

    The cached snapshot is reused only while the ``.git`` probe is
    unchanged AND the capture is younger than ``max_age_s``; anything
    else forces a fresh capture.  ``max_age_s=0`` (default) always
    recaptures — safe for gates that need hard freshness; automation
    ticks should pass a TTL around one sweep interval.
    """
    path = Path(worktree)
    key = os.path.normcase(str(path))
    probe = _probe(path)
    cached = _CACHE.get(key)
    if (
        cached is not None
        and probe is not None
        and probe == cached.generation
        and max_age_s > 0.0  # TTL 0 = always recapture (hard freshness)
        and (time.monotonic() - cached.captured_at) <= max_age_s
    ):
        return cached
    snap = _capture(path, probe or "unavailable")
    if probe is not None:
        # Re-probe after capture: ``git status`` may have refreshed index
        # stat-cache content during the capture itself; storing the
        # post-capture probe prevents that self-mutation from
        # invalidating the snapshot on the next read.
        import dataclasses

        snap = dataclasses.replace(
            snap, generation=_probe(path) or snap.generation
        )
        _CACHE[key] = snap
    return snap


def notify_changed(worktree: str | Path) -> None:
    """Event-driven invalidation (§3.3): an in-process writer reports the
    worktree changed — the next reader recaptures regardless of TTL."""
    _CACHE.pop(os.path.normcase(str(Path(worktree))), None)


def invalidate_all() -> None:
    _CACHE.clear()


__all__ = [
    "GenerationSnapshot",
    "generation_snapshot",
    "invalidate_all",
    "notify_changed",
    "scope_of",
]
