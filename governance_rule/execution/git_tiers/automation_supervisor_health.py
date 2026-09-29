"""Supervisor health-evidence surfaces (A185 split).

Extracted from ``automation_supervisor_loop.py`` (source-size contract):
the low-frequency health snapshots the supervisor writes into its
registry — sync/queue/audit/claims plus DEEP_HEALTH (spec 88: fsck,
commit-graph, multi-pack-index, ref pressure, cache stats).  These run
on the supervisor's slow cadence only; the 20 s health cycle never runs
fsck.
"""
from __future__ import annotations

from pathlib import Path

from .automation_supervisor_state import _state_dir


def _health_surfaces(root: str | Path) -> dict[str, object]:
    """Extended supervisor health: sync, queue, audit, claims."""
    health: dict[str, object] = {}
    try:
        from .repo_sync import sync_state
        from .merge_queue import MergeQueue
        from . import audit_chain

        state = sync_state(root)
        health["sync"] = {
            "state": state["state"],
            "local_main_sha": state["local_main_sha"],
            "origin_main_sha": state["origin_main_sha"],
        }
        health["merge_queue"] = MergeQueue(root).stats()
        health["audit"] = audit_chain.chain_health()
        try:
            from .claims import ClaimRegistry

            registry = ClaimRegistry(root)
            health["active_claims"] = len(registry.active())
        except Exception:
            health["active_claims"] = "unavailable"
        timeouts_path = _state_dir(root) / "git-timeouts.json"
        if timeouts_path.is_file():
            import json as _json

            try:
                health["timed_out_git_operations"] = _json.loads(
                    timeouts_path.read_text(encoding="utf-8")
                ).get("timed_out_git_operations", 0)
            except (OSError, _json.JSONDecodeError):
                health["timed_out_git_operations"] = "unreadable"
        else:
            health["timed_out_git_operations"] = 0
    except Exception as exc:
        health["health_error"] = type(exc).__name__
    return health


def _deep_health_surfaces(root: str | Path) -> dict[str, object]:
    """DEEP_HEALTH (spec 88): low-frequency, heavier integrity checks.

    Deliberately NOT run in the 20s health cycle: fsck, commit-graph verify,
    multi-pack-index verify, object database domain checks and ref pressure.
    """
    deep: dict[str, object] = {}
    try:
        from .git_repository import GitRepository
        from .git_perf import snapshot as perf_snapshot
        from . import git_cache

        repo = GitRepository(root)
        git_dir = repo.run(["rev-parse", "--git-dir"]).stdout.strip()
        resolved = Path(git_dir)
        if not resolved.is_absolute():
            resolved = Path(root) / resolved
        resolved = resolved.resolve()
        common = repo.run(["rev-parse", "--git-common-dir"]).stdout.strip()
        common_dir = Path(common)
        if not common_dir.is_absolute():
            common_dir = Path(root) / common_dir
        common_dir = common_dir.resolve()

        graph_dir = common_dir / "objects" / "info" / "commit-graphs"
        midx = common_dir / "objects" / "pack" / "multi-pack-index"
        deep["commit_graph"] = {
            "exists": graph_dir.is_dir()
            or (common_dir / "objects" / "info" / "commit-graph").exists(),
            "chain_files": len(
                list(graph_dir.glob("graph-*.graph"))
            ) if graph_dir.is_dir() else 0,
        }
        deep["midx_exists"] = midx.is_file()
        fsck = repo.run(["fsck", "--no-dangling"], timeout=120)
        deep["fsck"] = {
            "clean": fsck.returncode == 0,
            "detail": (fsck.stderr or fsck.stdout or "")[:300],
        }
        deep["git_perf"] = perf_snapshot(root)
        deep["ref_pressure"] = _ref_pressure(root, common_dir)
        deep["cache_entries"] = git_cache.stats()
    except Exception as exc:
        deep["deep_error"] = type(exc).__name__
    return deep


def _ref_pressure(root: str | Path, common_dir: Path) -> dict[str, int]:
    """Ref counts per category (spec 96): local / remote / recovery / codex."""
    try:
        from .git_repository import GitRepository

        repo = GitRepository(root)
        refs = repo.run(["for-each-ref", "--format=%(refname)"]).stdout.splitlines()
        counts = {
            "local_branches": sum(r.startswith("refs/heads/") for r in refs),
            "remote_tracking": sum(r.startswith("refs/remotes/") for r in refs),
            "recovery": sum(r.startswith("refs/gptbridge/recovery/") for r in refs),
            "temporary": sum(
                r.startswith(("refs/gptbridge/tmp/", "refs/workers/")) for r in refs
            ),
            "codex": sum(r.startswith("refs/codex/") for r in refs),
            "tags": sum(r.startswith("refs/tags/") for r in refs),
            "total": len(refs),
        }
        return counts
    except Exception:
        return {}


__all__ = ["_deep_health_surfaces", "_health_surfaces", "_ref_pressure"]
