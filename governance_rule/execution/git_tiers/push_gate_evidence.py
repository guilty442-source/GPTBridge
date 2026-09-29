"""Push/convergence audit-evidence records (A185 split).

Extracted from ``push_gate.py`` (source-size contract): §10.69-F①/D④
periodic ``ahead==0`` convergence evidence (ledger + small state file
under the git common dir — "local main == origin/main" continuously
provable) and §10.69-C④/F④ consolidated push decision records (gate
outcomes, revisions, result) into the governed audit ledger.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

from . import branch_policy
from .git_repository import GitRepository

_STATE_FILENAME = "gptbridge-push-gate.json"
_STATE_SCHEMA = "gptbridge-push-gate/v1"


def _state_path(root: str | Path) -> Path:
    """State file lives next to the workspace-sync lock (git common dir)."""
    repo = GitRepository(root)
    result = repo.run(["rev-parse", "--git-common-dir"])
    raw = (result.stdout or "").strip()
    common = Path(raw)
    if not common.is_absolute():
        common = repo.path / common
    return common.resolve() / _STATE_FILENAME


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _ahead_behind(repo: GitRepository) -> tuple[int | None, int | None]:
    """(ahead, behind) of local main vs origin/main; None when unknown."""
    ahead = repo.run(["rev-list", "--count", "origin/main..main"])
    behind = repo.run(["rev-list", "--count", "main..origin/main"])
    try:
        ahead_n = int((ahead.stdout or "").strip()) if ahead.returncode == 0 else None
        behind_n = int((behind.stdout or "").strip()) if behind.returncode == 0 else None
    except (TypeError, ValueError):
        ahead_n = behind_n = None
    return ahead_n, behind_n


def record_convergence_evidence(
    root: str | Path, *, actor: str, pushed: bool = False
) -> dict[str, Any]:
    """Periodic ``ahead==0`` proof: ledger entry + convergence state (F①/D④).

    Records every synchronization outcome: the local↔origin relation,
    ahead/behind counts and a running ``consecutive_in_sync`` streak.
    Ledger entries make the proof continuous; the state file is the
    latest snapshot for status surfaces.
    """
    from . import audit_log
    from .repo_sync import sync_state

    repo = GitRepository(root)
    state = sync_state(root)
    ahead_n, behind_n = _ahead_behind(repo)
    in_sync = state["state"] == "IN_SYNC" and ahead_n == 0

    path = _state_path(root)
    prior = _read_state(path)
    streak = int(prior.get("convergence", {}).get("consecutive_in_sync") or 0)
    streak = streak + 1 if in_sync else 0

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    convergence = {
        "state": state["state"],
        "ahead": ahead_n,
        "behind": behind_n,
        "ahead_zero": in_sync,
        "consecutive_in_sync": streak,
        "last_checked": now,
        "local_main_sha": state["local_main_sha"],
        "origin_main_sha": state["origin_main_sha"],
    }
    record = dict(prior)
    record.update(
        {
            "schema": _STATE_SCHEMA,
            "updated_at": now,
            "convergence": convergence,
        }
    )
    _write_state(path, record)

    audit_log(
        1,
        "sync-state origin/main",
        actor,
        True,
        f"sync_state={state['state']} ahead={ahead_n} behind={behind_n}"
        f" streak={streak} pushed={pushed}",
        operation="convergence-check",
        phase="result",
        result="in-sync" if in_sync else state["state"].lower(),
        returncode=0 if in_sync else 1,
    )
    return convergence


def record_push_evidence(
    root: str | Path,
    *,
    actor: str,
    test_gate: Mapping[str, Any] | None,
    pushed: bool,
    detail: str = "",
) -> dict[str, Any]:
    """Consolidated push decision record (C④/F④): ledger + state file.

    One record carries the mandatory native-test gate outcome plus the
    revisions involved, so a push is provably preceded by audit PASS +
    native suite PASS.
    """
    from . import audit_log

    repo = GitRepository(root)
    local_sha = (
        repo.run(["rev-parse", branch_policy.MAIN_BRANCH]).stdout or ""
    ).strip()
    origin_sha = (
        repo.run(["rev-parse", f"origin/{branch_policy.MAIN_BRANCH}"]).stdout
        or ""
    ).strip()
    gate_summary = "none"
    if test_gate is not None:
        totals = (
            test_gate.get("totals")
            if isinstance(test_gate.get("totals"), Mapping)
            else {}
        )
        gate_summary = (
            f"tests={'pass' if test_gate.get('passed') else 'fail'}"
            f" skipped={bool(test_gate.get('skipped'))}"
            f" suites={len(test_gate.get('suites') or [])}"
            f" pass={totals.get('pass', 0)}"
            f" fail={totals.get('fail', 0)}"
            f" blocked={totals.get('blocked', 0)}"
            f" rebuilt={bool(test_gate.get('rebuilt'))}"
            f" ms={test_gate.get('duration_ms')}"
        )
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    path = _state_path(root)
    record = _read_state(path)
    record.update(
        {
            "schema": _STATE_SCHEMA,
            "updated_at": now,
            "last_test_gate": dict(test_gate) if test_gate else None,
            "last_push": {
                "at": now,
                "result": "pushed" if pushed else "denied",
                "local_main_sha": local_sha,
                "origin_main_sha": origin_sha,
                "test_gate": gate_summary,
                "detail": detail[:300],
            },
        }
    )
    _write_state(path, record)

    audit_log(
        2,
        "push origin main",
        actor,
        pushed,
        f"mandatory-test-gate[{gate_summary}] {detail}".strip()[:500],
        operation="push",
        phase="result",
        result="pushed" if pushed else "denied",
        returncode=0 if pushed else 1,
    )
    return record


__all__ = [
    "record_convergence_evidence", "record_push_evidence",
    "_STATE_FILENAME", "_STATE_SCHEMA", "_ahead_behind", "_read_state",
    "_state_path", "_write_state",
]
