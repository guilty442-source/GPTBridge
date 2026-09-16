"""Worker/file claim registry (task §18, A375 coordination).

Worktrees isolate directories, not intent — two AI workers can still edit
the same file on different branches.  Claims are a *coordination* signal
only: a conflicting claim never blocks work outright; it surfaces as
``CLAIM_CONFLICT`` in health/reporting so humans and the coordinator can
sequence the work.  Claims never replace Git merge-conflict detection.

Storage: ``<git-common>/gptbridge-automation/claims/<claim_id>.json`` —
one atomically-written file per claim so concurrent workers never share a
write target.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .git_repository import GitRepository

DEFAULT_LEASE_SECONDS: float = 3600.0


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").strip("/").casefold()


def _paths_overlap(left: str, right: str) -> bool:
    """True when one path covers the other (file or directory prefix)."""
    if left == right:
        return True
    return left.startswith(right + "/") or right.startswith(left + "/")


class ClaimRegistry:
    """File-backed claim registry scoped to one repository."""

    def __init__(self, root: str | Path) -> None:
        repo = GitRepository(root)
        result = repo.run(["rev-parse", "--git-common-dir"])
        raw = (result.stdout or "").strip()
        common = Path(raw)
        if not common.is_absolute():
            common = repo.path / common
        self._dir = common.resolve() / "gptbridge-automation" / "claims"
        self._dir.mkdir(parents=True, exist_ok=True)

    # -- storage --------------------------------------------------------

    def _path(self, claim_id: str) -> Path:
        return self._dir / f"{claim_id}.json"

    def _write(self, payload: dict[str, Any]) -> None:
        target = self._path(payload["claim_id"])
        tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, target)

    def _iter(self) -> Iterable[dict[str, Any]]:
        for path in sorted(self._dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                yield payload

    # -- API ------------------------------------------------------------

    def claim(
        self,
        worker_id: str,
        task_id: str,
        branch: str,
        paths: Iterable[str],
        *,
        base_revision: str = "",
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any]:
        """Register a path claim; reports conflicts but never blocks."""
        now = time.time()
        record = {
            "claim_id": uuid.uuid4().hex[:16],
            "worker_id": str(worker_id),
            "task_id": str(task_id),
            "branch": str(branch),
            "paths": sorted({_norm(p) for p in paths if str(p).strip()}),
            "created_at": now,
            "expires_at": now + max(60.0, float(lease_seconds)),
            "base_revision": str(base_revision),
            "released": False,
        }
        self._write(record)
        conflicts = self.find_conflicts(
            record["paths"],
            exclude=record["claim_id"],
            exclude_worker=record["worker_id"],
        )
        return {"claim": record, "conflicts": conflicts,
                "status": "CLAIM_CONFLICT" if conflicts else "CLAIMED"}

    def renew_claim(self, claim_id: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> bool:
        record = self._read(claim_id)
        if record is None or record.get("released"):
            return False
        record["expires_at"] = time.time() + max(60.0, float(lease_seconds))
        self._write(record)
        return True

    def release_claim(self, claim_id: str) -> bool:
        record = self._read(claim_id)
        if record is None:
            return False
        record["released"] = True
        record["released_at"] = time.time()
        self._write(record)
        return True

    def _read(self, claim_id: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._path(claim_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def active(self) -> list[dict[str, Any]]:
        now = time.time()
        return [
            record
            for record in self._iter()
            if not record.get("released") and float(record.get("expires_at", 0)) > now
        ]

    def find_conflicts(
        self,
        paths: Iterable[str],
        *,
        exclude: str = "",
        exclude_worker: str = "",
    ) -> list[dict[str, Any]]:
        """Return active claims by *other* workers whose paths overlap."""
        wanted = {_norm(p) for p in paths if str(p).strip()}
        conflicts: list[dict[str, Any]] = []
        for record in self.active():
            if record.get("claim_id") == exclude:
                continue
            if exclude_worker and record.get("worker_id") == exclude_worker:
                continue
            overlap = sorted(
                {a for a in wanted for b in record.get("paths", []) if _paths_overlap(a, b)}
            )
            if overlap:
                conflicts.append(
                    {
                        "claim_id": record.get("claim_id"),
                        "worker_id": record.get("worker_id"),
                        "task_id": record.get("task_id"),
                        "branch": record.get("branch"),
                        "overlap": overlap,
                        "expires_at": record.get("expires_at"),
                    }
                )
        return conflicts

    def sweep_expired(self) -> int:
        """Mark expired claims released (keeps files as evidence)."""
        now = time.time()
        swept = 0
        for record in self._iter():
            if not record.get("released") and float(record.get("expires_at", 0)) <= now:
                record["released"] = True
                record["released_at"] = now
                record["release_reason"] = "expired"
                self._write(record)
                swept += 1
        return swept


__all__ = ["ClaimRegistry", "DEFAULT_LEASE_SECONDS"]
