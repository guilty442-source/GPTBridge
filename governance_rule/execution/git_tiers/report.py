"""Phase acceptance report (task §36) — sections A through N.

Aggregates the governed state surfaces into one verifiable report.
All reads are Tier-1 / file reads; nothing here mutates the repository.

CLI:
    python -m governance_rule.execution.git_tiers.report [--root PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import audit_chain
from .automation_supervisor_state import status as supervisor_status
from .branch_policy import classify_branch, policy_digest
from .branch_reconcile import reconcile_branches
from .central import evaluate_push_gates, tri_state
from .claims import ClaimRegistry
from .git_maintenance import maintenance_safe, object_stats
from .git_perf import batch_ref_snapshot, snapshot as perf_snapshot
from .git_repository import GitRepository
from .merge_queue import MergeQueue
from .recovery import list_recovery_refs
from .server_hooks import server_hook_health
from .worktree_manager import WorktreeManager

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_report(root: str | Path) -> dict[str, Any]:
    repo = GitRepository(root)
    manager = WorktreeManager(repo)
    worktrees = manager.list_worktrees()
    state = tri_state(root)
    hooks = server_hook_health(root)
    queue = MergeQueue(root)
    reconcile = reconcile_branches(root)
    try:
        claims = ClaimRegistry(root)
        claim_info = {
            "active": len(claims.active()),
        }
    except Exception:
        claim_info = {"active": "unavailable"}
    try:
        obj = object_stats(repo)
    except Exception:
        obj = {}
    sup = supervisor_status(root)
    try:
        perf = perf_snapshot(Path(root))
    except Exception:
        perf = {}
    try:
        refs = batch_ref_snapshot(repo)
        refs_meta = {
            "total": len(refs),
            "local_branches": sum(1 for r in refs if r["name"].startswith("refs/heads/")),
            "remote_refs": sum(1 for r in refs if r["name"].startswith("refs/remotes/")),
            "recovery_refs": sum(1 for r in refs if r["name"].startswith("refs/recovery/")),
            "tags": sum(1 for r in refs if r["name"].startswith("refs/tags/")),
            "refs": refs,
        }
    except Exception:
        refs_meta = {"total": "unavailable", "refs": []}

    report = {
        "A_repository_topology": {
            "root": str(Path(root).resolve()),
            "central_path": state["central_path"],
            "central_exists": state["central_exists"],
            "origin": "origin remote (GitHub mirror)",
            "local_main_sha": state["local_main_sha"],
            "central_main_sha": state["central_main_sha"],
            "origin_main_sha": state["origin_main_sha"],
            "sync_state": state["state"],
        },
        "B_worktree_inventory": worktrees,
        "C_worker_watcher_mapping": sup.get("children", []),
        "D_branch_policy": {
            "policy_hash": policy_digest(),
            "states": reconcile["states"],
            "retirement_candidates": reconcile["retirement_candidates"],
        },
        "E_merge_queue": queue.stats(),
        "F_lock_state": {
            "maintenance_safe": maintenance_safe(root),
        },
        "G_supervisor_health": {
            "running": sup.get("running"),
            "state": sup.get("state"),
            "sync_cycles": sup.get("sync_cycles"),
            "last_sync_result": sup.get("last_sync_result"),
            "push": sup.get("push", False),
        },
        "H_central_health": {
            "state": state["state"],
            "relations": state["relations"],
            "write_enabled": hooks["central_write_enabled"],
        },
        "I_audit_health": audit_chain.chain_health(),
        "J_hook_health": hooks,
        "K_object_database_health": obj,
        "L_recovery_capability": {
            "recovery_refs": list_recovery_refs(repo),
            "claims": claim_info,
        },
        "M_test_results": "see pytest run",
        "N_remaining_risks": _risks(state, hooks, queue),
        "O_performance_telemetry": perf,
        "P_refs_snapshot": refs_meta,
    }
    return report


def _risks(state: dict[str, Any], hooks: dict[str, Any], queue: MergeQueue) -> list[str]:
    risks: list[str] = []
    if state["state"] in {"DIVERGED", "MISSING_REF"}:
        risks.append(f"central sync state: {state['state']}")
    if hooks["server_hook_health"] != "HEALTHY":
        risks.append(f"server hooks: {hooks['server_hook_health']}")
    stats = queue.stats()
    if stats["blocked"] or stats["conflicted"]:
        risks.append(
            f"merge queue: {stats['blocked']} blocked, {stats['conflicted']} conflicted"
        )
    return risks


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GPTBridge governance report (A–N).")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = build_report(args.root)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
