"""Xingcheng Sovereign — Owned Domain Operations (A20).

Real operations confined to the owned domain root. Every path resolves inside
the domain; escape attempts fail closed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .._base import SovereignBase, SovereignRequest, SovereignOutcome
from core_system.codex_decision import accepted_outcome, refusal_outcome

_logger = logging.getLogger("gptbridge.sovereign.xingcheng.domain")


class XingchengDomainMixin:
    """Owned-domain powers — confined operations per A20."""

    _owned_domain_root: str
    _project_root: Path
    _isolated: bool
    _auto_metrics: dict[str, Any]
    _last_snapshot: dict[str, Any]
    _pending_anomalies: list[dict[str, Any]]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._auto_metrics = {
            "observe_cycles": 0, "analyze_cycles": 0, "reason_cycles": 0,
            "manage_cycles": 0, "health_checks": 0, "anomalies_detected": 0,
            "channel_notifications": 0, "db_maintenance_runs": 0,
            "model_loads": 0, "config_updates": 0,
            "last_auto_cycle": "", "last_anomaly": "",
        }
        self._last_snapshot = {}
        self._pending_anomalies = []

    def _resolve_in_domain(self, raw_path: str | None) -> Path | None:
        """Resolve path under owned domain root. Returns None if escapes domain."""
        if not raw_path:
            return None
        root = Path(self._owned_domain_root).resolve()
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        if resolved != root and root not in resolved.parents:
            return None
        return resolved

    def _is_in_owned_domain(self, request: SovereignRequest) -> bool:
        """A20: verify request target is inside owned domain."""
        target = str(request.payload.get("target", "")).replace("\\", "/")
        if target.startswith(self._owned_domain_root):
            return True
        system_roots = (
            (self._project_root / "main-system").as_posix(),
            (self._project_root / "governance_rule").as_posix(),
        )
        if any(target.startswith(root) for root in system_roots):
            return False
        return request.payload.get("domain_confirmed") is True

    def _domain_governance_file(self, filename: str) -> Path:
        return Path(self._owned_domain_root) / filename

    def _read_domain_json(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def _read_domain_json_list(self, path: Path) -> list[Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []

    def _write_domain_json(self, path: Path, data: Any) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except OSError:
            return False

    async def _adjudicate_observe(self, request: SovereignRequest) -> SovereignOutcome:
        snapshot = self._observe_domain()
        self._last_snapshot = snapshot
        return accepted_outcome(
            {"action": "observe", "domain": "owned", "snapshot": snapshot},
            self.verified_basis("A20"),
        )

    async def _adjudicate_analyze(self, request: SovereignRequest) -> SovereignOutcome:
        snapshot = self._last_snapshot or self._observe_domain()
        anomalies = self._analyze_domain(snapshot)
        return accepted_outcome(
            {"action": "analyze", "domain": "owned", "anomalies": anomalies},
            self.verified_basis("A20"),
        )

    async def _adjudicate_reason(self, request: SovereignRequest) -> SovereignOutcome:
        """A20 reason power: advisory conclusion from query + domain evidence."""
        query = str(request.payload.get("query") or "")
        snapshot = self._last_snapshot or self._observe_domain()
        anomalies = self._analyze_domain(snapshot)
        conclusion = {
            "query": query,
            "evidence_basis": {
                "snapshot_at": snapshot.get("observed_at"),
                "anomaly_count": len(anomalies),
            },
            "conclusion": "domain-anomalies-present" if anomalies else "domain-nominal",
            "related_anomalies": anomalies[:5],
            "advisory": True,
            "reasoned_at": self._iso_now(),
        }
        return accepted_outcome(
            {"action": "reason", "domain": "owned", "reasoning": conclusion},
            self.verified_basis("A20"),
        )

    async def _adjudicate_decide(self, request: SovereignRequest) -> SovereignOutcome:
        decision = request.payload.get("decision")
        decision_id = f"decision-{len(self._pending_anomalies) + 1}"
        return accepted_outcome(
            {"action": "decide", "domain": "owned", "decision_id": decision_id, "decision": decision},
            self.verified_basis("A20", "A12"),
        )

    async def _adjudicate_manage(self, request: SovereignRequest) -> SovereignOutcome:
        actions = self._manage_domain()
        return accepted_outcome(
            {"action": "manage", "domain": "owned", "resource": request.payload.get("resource"), "actions": actions},
            self.verified_basis("A20"),
        )

    async def _adjudicate_authorize(self, request: SovereignRequest) -> SovereignOutcome:
        """A20 authorize: record domain-internal authorization grant."""
        permission = request.payload.get("permission")
        if not permission:
            return refusal_outcome("MISSING_PERMISSION", self.verified_basis("A20"))
        grant = {
            "permission": permission,
            "grantee": request.payload.get("grantee") or request.requester,
            "scope": request.payload.get("scope") or "owned-domain",
            "granted_at": self._iso_now(),
            "expires_at": request.payload.get("expires_at"),
        }
        registry = self._domain_governance_file("domain-authorizations.json")
        grants = self._read_domain_json_list(registry)
        grants.append(grant)
        grant_id = f"grant-{len(grants)}"
        grant["grant_id"] = grant_id
        if not self._write_domain_json(registry, grants[-500:]):
            return refusal_outcome("EXECUTION_FAILED", self.verified_basis("A20"))
        return accepted_outcome(
            {"action": "authorize", "domain": "owned", "grant_id": grant_id, "grant": grant},
            self.verified_basis("A20"),
        )

    async def _adjudicate_execute(self, request: SovereignRequest) -> SovereignOutcome:
        """A20 execute: bounded domain operations."""
        operation = request.payload.get("operation") or {}
        if not isinstance(operation, dict):
            return refusal_outcome("INVALID_OPERATION", self.verified_basis("A20"))
        op = str(operation.get("op") or "")
        target = self._resolve_in_domain(operation.get("path"))
        if target is None:
            return refusal_outcome("OUTSIDE_OWNED_DOMAIN", self.verified_basis("A20"))

        result: dict[str, Any] = {"op": op, "path": str(target)}
        try:
            if op == "mkdir":
                target.mkdir(parents=True, exist_ok=True)
                result["created"] = True
            elif op == "db-vacuum":
                if target.suffix != ".sqlite3" or not target.is_file():
                    return refusal_outcome("INVALID_OPERATION", self.verified_basis("A20"))
                conn = sqlite3.connect(str(target))
                try:
                    conn.execute("VACUUM")
                finally:
                    conn.close()
                result["vacuumed"] = True
            elif op == "checkpoint-cleanup":
                if not target.is_file() or "checkpoint" not in target.name:
                    return refusal_outcome("INVALID_OPERATION", self.verified_basis("A20"))
                target.unlink()
                result["deleted"] = True
            else:
                return refusal_outcome("UNKNOWN_OPERATION", self.verified_basis("A20"))
        except OSError:
            return refusal_outcome("EXECUTION_FAILED", self.verified_basis("A20"))
        return accepted_outcome(
            {"action": "execute", "domain": "owned", "result": result},
            self.verified_basis("A20"),
        )

    async def _adjudicate_write(self, request: SovereignRequest) -> SovereignOutcome:
        """A20 write: write file content inside owned domain."""
        target = self._resolve_in_domain(request.payload.get("path"))
        content = request.payload.get("content")
        if target is None:
            return refusal_outcome("OUTSIDE_OWNED_DOMAIN", self.verified_basis("A20"))
        if content is None:
            return refusal_outcome("MISSING_CONTENT", self.verified_basis("A20"))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
        except OSError:
            return refusal_outcome("EXECUTION_FAILED", self.verified_basis("A20"))
        return accepted_outcome(
            {"action": "write", "domain": "owned", "path": str(target)},
            self.verified_basis("A20"),
        )

    async def _adjudicate_delete(self, request: SovereignRequest) -> SovereignOutcome:
        """A20 delete: remove file inside owned domain."""
        target = self._resolve_in_domain(request.payload.get("path"))
        if target is None:
            return refusal_outcome("OUTSIDE_OWNED_DOMAIN", self.verified_basis("A20"))
        try:
            target.unlink(missing_ok=True)
        except OSError:
            return refusal_outcome("EXECUTION_FAILED", self.verified_basis("A20"))
        return accepted_outcome(
            {"action": "delete", "domain": "owned", "path": str(target)},
            self.verified_basis("A20"),
        )

    async def _adjudicate_configure(self, request: SovereignRequest) -> SovereignOutcome:
        """A20 configure: domain-internal configuration."""
        key = request.payload.get("key")
        value = request.payload.get("value")
        if not key:
            return refusal_outcome("MISSING_KEY", self.verified_basis("A20"))
        config = self._read_domain_json(self._domain_governance_file("domain-config.json"))
        config[key] = value
        if not self._write_domain_json(self._domain_governance_file("domain-config.json"), config):
            return refusal_outcome("EXECUTION_FAILED", self.verified_basis("A20"))
        return accepted_outcome(
            {"action": "configure", "domain": "owned", "key": key, "value": value},
            self.verified_basis("A20"),
        )

    async def _adjudicate_channel_coordinate(self, request: SovereignRequest) -> SovereignOutcome:
        """A20 channel coordinate: notify system through information layer."""
        message = request.payload.get("message") or {}
        anomaly = request.payload.get("anomaly")
        if anomaly:
            self._pending_anomalies.append(anomaly)
            await self._notify_anomalies()
        self._auto_metrics["channel_notifications"] += 1
        return accepted_outcome(
            {"action": "channel.coordinate", "domain": "owned", "notified": True},
            self.verified_basis("A20", "A66"),
        )

    # --- Internal helpers ---

    def _observe_domain(self) -> dict[str, Any]:
        """Observe owned domain state."""
        self._auto_metrics["observe_cycles"] += 1
        self._auto_metrics["last_auto_cycle"] = self._iso_now()
        root = Path(self._owned_domain_root)
        return {
            "observed_at": self._iso_now(),
            "domain_root": str(root),
            "exists": root.exists(),
            "db_size_bytes": sum(
                f.stat().st_size for f in root.rglob("*.sqlite3") if f.is_file()
            ),
            "model_dir_size_bytes": sum(
                f.stat().st_size for f in root.rglob("*") if f.is_file()
            ),
        }

    def _analyze_domain(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """Analyze domain snapshot for anomalies."""
        self._auto_metrics["analyze_cycles"] += 1
        anomalies: list[dict[str, Any]] = []
        db_size = snapshot.get("db_size_bytes", 0)
        if db_size > 500 * 1024 * 1024:
            anomalies.append({"type": "db-size", "severity": "warning", "detail": f"{db_size} bytes"})
        return anomalies

    def _manage_domain(self) -> list[dict[str, Any]]:
        """Perform domain maintenance actions."""
        self._auto_metrics["manage_cycles"] += 1
        actions = []
        # Could add db vacuum, checkpoint cleanup, etc.
        return actions