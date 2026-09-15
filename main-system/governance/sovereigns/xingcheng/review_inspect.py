"""Permission review and inspect mixin for XingchengReviewMixin (A185 split).

Contains the A319 permission review and A330 inspect adjudication
methods extracted from XingchengReviewMixin.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .._base import SovereignRequest, SovereignOutcome
from core_system.codex_decision import accepted_outcome


class XingchengInspectMixin:
    """Permission review and inspect adjudication methods."""

    _reviews: dict[str, Any]
    app: Any



    async def _adjudicate_permission_review(self, request: SovereignRequest) -> SovereignOutcome:
        """A319: independent privileged read-only examination of permission request."""
        payload = request.payload
        aspects: dict[str, str] = {}
        aspects["codex"] = "present" if payload.get("basis") or payload.get("codex_ref") else "missing"
        aspects["identity"] = "present" if payload.get("actor") else "missing"
        aspects["scope"] = "present" if payload.get("scope") else "missing"
        aspects["purpose"] = "present" if payload.get("purpose") else "missing"
        aspects["least-privilege"] = "present" if payload.get("least_privilege") else "missing"
        aspects["separation"] = "present" if payload.get("separation") else "missing"
        aspects["expiry"] = "present" if payload.get("expiry") else "missing"
        aspects["risk"] = "present" if payload.get("risk") else "missing"
        aspects["current-evidence"] = "present" if payload.get("evidence") else "missing"
        missing = [k for k, v in aspects.items() if v == "missing"]
        if not payload.get("actor") or not payload.get("capability") or not payload.get("target"):
            finding = "require-change"
        elif aspects["current-evidence"] == "missing" or aspects["risk"] == "missing":
            finding = "deny-objection"
        elif missing:
            finding = "require-change"
        else:
            finding = "pass"
        review_id = f"permission-review-{len(self._reviews) + 1}"
        record = {
            "kind": "permission-review",
            "finding": finding,
            "aspects": aspects,
            "missing": missing,
            "subject": {
                "actor": payload.get("actor"),
                "capability": payload.get("capability"),
                "target": payload.get("target"),
            },
            "advisory": True,
            "confidential": True,
            "reviewed_at": self._iso_now(),
            "expires_at": payload.get("expiry"),
        }
        self._reviews[review_id] = record
        return accepted_outcome(
            {
                "action": "permission-review",
                "review_id": review_id,
                "finding": finding,
                "aspects": aspects,
                "evidence": record["subject"],
                "expiry": payload.get("expiry"),
                "note": "decision-sovereign may decide only after current 星澄 review",
            },
            self.verified_basis("A319"),
        )

    async def _adjudicate_inspect(self, request: SovereignRequest) -> SovereignOutcome:
        """A330: free-entry confidential read-only inspection."""
        layer = str(request.payload.get("layer") or "unspecified")
        inspection_id = f"inspect-{len(self._reviews) + 1}"
        view: dict[str, Any] = {}
        app_root = getattr(self.app, "project_root", None)
        if app_root:
            safe_name = "".join(c for c in layer if c.isalnum() or c in ("-", "_"))
            state_path = Path(app_root) / "main-system" / "runtime" / "state" / f"{safe_name}.json"
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    view = payload
            except (OSError, UnicodeError, json.JSONDecodeError):
                view = {}
        self._reviews[inspection_id] = {
            "kind": "layer-inspection",
            "layer": layer,
            "access": "read-only",
            "confidential": True,
            "view_keys": sorted(view.keys()),
            "inspected_at": self._iso_now(),
        }
        return accepted_outcome(
            {
                "action": "inspect",
                "inspection_id": inspection_id,
                "layer": layer,
                "access": "confidential-read-only",
                "mutating": False,
                "executing": False,
                "preapproval": "none-required",
                "continuity": "inspection-cannot-interrupt-or-alter-state",
                "view": view,
            },
            self.verified_basis("A330"),
        )


__all__ = ["XingchengInspectMixin"]
