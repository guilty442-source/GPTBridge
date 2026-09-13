"""Repair coordinator — request I/O mixin.

Extracted from RepairCoordinator: the information-layer request file
read/write helpers and the pending/awaiting/acknowledge query methods
that operate on the repair-requests.json state file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .repair_coordinator_types import _iso_now


class RepairCoordinatorRequestsMixin:
    """Request file I/O and query methods for RepairCoordinator."""

    def _requests_file(self) -> Path:
        """Information-layer state file for governed repair requests."""
        return (
            self.project_root  # type: ignore[attr-defined]
            / "main-system"
            / "runtime"
            / "state"
            / "repair-requests.json"
        )

    def _read_requests(self) -> list[dict[str, Any]]:
        path = self._requests_file()
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _write_requests(self, requests: list[dict[str, Any]]) -> None:
        path = self._requests_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(requests, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            pass

    def pending_requests(self) -> list[dict[str, Any]]:
        """Return pending repair requests for the decision-sovereign to process."""
        return [
            req for req in self._read_requests()
            if req.get("status") == "pending"
        ]

    def acknowledge_request(
        self,
        request_id: str,
        *,
        decision: str,
        ok: bool,
    ) -> None:
        """Record the decision-sovereign's decision on a repair request."""
        requests = self._read_requests()
        for req in requests:
            if req.get("request_id") == request_id:
                req["status"] = decision
                req["sovereign_decided_at"] = _iso_now()
                req["sovereign_decision_ok"] = ok
                break
        self._write_requests(requests)

    def await_user_confirmation(
        self,
        request_id: str,
        *,
        classified: dict[str, Any] | None = None,
    ) -> None:
        """Mark a repair request as awaiting explicit user confirmation."""
        requests = self._read_requests()
        for req in requests:
            if req.get("request_id") == request_id:
                req["status"] = "awaiting-confirmation"
                req["awaiting_confirmation_at"] = _iso_now()
                if classified is not None:
                    req["classified"] = classified
                break
        self._write_requests(requests)

    def awaiting_confirmation_requests(self) -> list[dict[str, Any]]:
        """Return repair requests waiting for explicit user confirmation."""
        return [
            req for req in self._read_requests()
            if req.get("status") == "awaiting-confirmation"
        ]

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        """Return one repair request by id."""
        for req in self._read_requests():
            if req.get("request_id") == request_id:
                return req
        return None

    def mark_request_status(
        self,
        request_id: str,
        status: str,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Update a request's status and optional fields; returns the record."""
        requests = self._read_requests()
        updated: dict[str, Any] | None = None
        for req in requests:
            if req.get("request_id") == request_id:
                req["status"] = status
                req.update(fields)
                updated = req
                break
        if updated is not None:
            self._write_requests(requests)
        return updated
