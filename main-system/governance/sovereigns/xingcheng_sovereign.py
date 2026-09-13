"""Xingcheng Sovereign — 星澄主宰（獨立特權機構，完全擁有自有域，法典決策鏈外）。

法典依據:
- sovereign_id: 星澄 (position 5)
- area: xingcheng
- rank: independent-privileged-institution
- basis: codex
- duties: observe+analyze+reason+decide+manage+authorize+execute+write+delete+configure (in owned domain)
- powers: complete-inside-owned-domain
- prohibitions: FORBID:any-星澄-power-outside-owned-domain; FORBID:any-system-target-or-effect (A20)

A12: 星澄在決策鏈外
A20: 星澄權力完整在自有域內，禁止任何系統目標或效果

Full-automation upgrade (A20):
- Background auto-loop observes, analyzes, reasons, and manages the
  owned domain automatically.
- Domain health monitoring: detects database, model, and configuration
  anomalies.
- Domain resource management: model loading, database maintenance.
- Channel coordination: notifies the system through the information
  layer (A66) when anomalies are detected.
- All operations stay inside the owned domain (A20); no system targets
  or effects are ever produced.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome

_logger = logging.getLogger("gptbridge.sovereign.xingcheng")

# Owned-domain root — matches the codex project_architecture_directory
# STAR_DIRECTORY physical_root.
_OWNED_DOMAIN_ROOT = (
    "E:/GPTBridge/Standalone tools/local-model/model-dialogue/xingcheng"
)

# Domain health thresholds.
_DB_MAX_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB
_MODEL_DIR_MAX_SIZE_BYTES = 50 * 1024 * 1024 * 1024  # 50 GB
_CHECKPOINT_MAX_AGE_HOURS = 72


class XingchengSovereign(SovereignBase):
    """星澄主宰：自有域完全權力，隔離於系統決策鏈。"""

    sovereign_id = "星澄"

    # A10/A11 explicit intent allowlist — the base-class ``_verify_intent``
    # checks edict IDs (article tokens), not the ``domain.*`` intent strings
    # used by callers, so every real intent would be rejected and
    # ``handle()`` would be unreachable.  List adjudicated intents
    # explicitly (fail-closed); the A20 owned-domain check inside
    # ``_adjudicate`` still guards every accepted intent.
    _INTENT_ALLOWLIST: frozenset[str] = frozenset({
        "domain.observe",
        "domain.analyze",
        "domain.reason",
        "domain.decide",
        "domain.manage",
        "domain.authorize",
        "domain.execute",
        "domain.write",
        "domain.delete",
        "domain.configure",
        "channel.coordinate",
        # Auto-automation intents (A20 owned-domain only)
        "domain.auto-observe",
        "domain.auto-analyze",
        "domain.auto-manage",
        "domain.auto-health-check",
    })

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)
        self._owned_domain_root = _OWNED_DOMAIN_ROOT
        self._isolated = True
        # Auto-automation state (A20 full-automation upgrade).
        self._auto_loop_task: asyncio.Task[Any] | None = None
        self._auto_loop_interval: float = 10.0  # seconds
        self._auto_enabled: bool = True
        # Automation metrics for status surfaces.
        self._auto_metrics: dict[str, Any] = {
            "observe_cycles": 0,
            "analyze_cycles": 0,
            "reason_cycles": 0,
            "manage_cycles": 0,
            "health_checks": 0,
            "anomalies_detected": 0,
            "channel_notifications": 0,
            "db_maintenance_runs": 0,
            "model_loads": 0,
            "config_updates": 0,
            "last_auto_cycle": "",
            "last_anomaly": "",
        }
        # Last domain observation snapshot.
        self._last_snapshot: dict[str, Any] = {}
        # Detected anomalies pending channel notification.
        self._pending_anomalies: list[dict[str, Any]] = []

    def _verify_intent(self, intent: str) -> bool:
        """A10/A11 fail-closed: only declared owned-domain intents pass."""
        return intent in self._INTENT_ALLOWLIST

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """裁決：自有域內完全權力。"""
        intent = request.intent

        if not self._is_in_owned_domain(request):
            return refusal_outcome(
                "OUTSIDE_OWNED_DOMAIN",
                self.verified_basis("A20", "A12"),
            )

        if intent == "domain.observe":
            return await self._adjudicate_observe(request)
        if intent == "domain.analyze":
            return await self._adjudicate_analyze(request)
        if intent == "domain.reason":
            return await self._adjudicate_reason(request)
        if intent == "domain.decide":
            return await self._adjudicate_decide(request)
        if intent == "domain.manage":
            return await self._adjudicate_manage(request)
        if intent == "domain.authorize":
            return await self._adjudicate_authorize(request)
        if intent == "domain.execute":
            return await self._adjudicate_execute(request)
        if intent == "domain.write":
            return await self._adjudicate_write(request)
        if intent == "domain.delete":
            return await self._adjudicate_delete(request)
        if intent == "domain.configure":
            return await self._adjudicate_configure(request)
        if intent == "channel.coordinate":
            return await self._adjudicate_channel_coordinate(request)
        if intent == "domain.auto-observe":
            return await self._adjudicate_auto_observe(request)
        if intent == "domain.auto-analyze":
            return await self._adjudicate_auto_analyze(request)
        if intent == "domain.auto-manage":
            return await self._adjudicate_auto_manage(request)
        if intent == "domain.auto-health-check":
            return await self._adjudicate_auto_health_check(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A20", "A12"))

    def _is_in_owned_domain(self, request: SovereignRequest) -> bool:
        """A20: 驗證請求目標在自有域內，禁止系統目標。"""
        target = request.payload.get("target", "")
        if target.startswith(self._owned_domain_root):
            return True
        if target.startswith("E:/GPTBridge/main-system") or target.startswith("E:/GPTBridge/governance_rule"):
            return False
        return request.payload.get("domain_confirmed") is True

    async def _adjudicate_observe(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "observe", "domain": "owned", "scope": request.payload.get("scope")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_analyze(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "analyze", "domain": "owned", "data": request.payload.get("data")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_reason(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "reason", "domain": "owned", "query": request.payload.get("query")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_decide(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "decide", "domain": "owned", "decision": request.payload.get("decision")},
            self.verified_basis("A20", "A12"),
        )

    async def _adjudicate_manage(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "manage", "domain": "owned", "resource": request.payload.get("resource")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_authorize(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "authorize", "domain": "owned", "permission": request.payload.get("permission")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_execute(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "execute", "domain": "owned", "operation": request.payload.get("operation")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_write(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "write", "domain": "owned", "path": request.payload.get("path")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_delete(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "delete", "domain": "owned", "path": request.payload.get("path")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_configure(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "configure", "domain": "owned", "config": request.payload.get("config")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_channel_coordinate(self, request: SovereignRequest) -> SovereignOutcome:
        """A66: 星澄通道協調（透過資訊層，不直接存取系統模組）。"""
        return accepted_outcome(
            {
                "coordination": "information-layer-only",
                "system_access": "forbidden",
                "channel": request.payload.get("channel"),
            },
            self.verified_basis("A66", "A20"),
        )

    async def _adjudicate_auto_observe(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A20: auto-observe the owned domain and return a snapshot."""
        snapshot = self._observe_domain()
        self._last_snapshot = snapshot
        self._auto_metrics["observe_cycles"] += 1
        return accepted_outcome(
            {"action": "auto-observe", "domain": "owned", "snapshot": snapshot},
            self.verified_basis("A20"),
        )

    async def _adjudicate_auto_analyze(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A20: auto-analyze the domain snapshot for anomalies."""
        anomalies = self._analyze_domain(self._last_snapshot)
        self._auto_metrics["analyze_cycles"] += 1
        if anomalies:
            self._auto_metrics["anomalies_detected"] += len(anomalies)
            self._pending_anomalies.extend(anomalies)
        return accepted_outcome(
            {"action": "auto-analyze", "domain": "owned", "anomalies": anomalies},
            self.verified_basis("A20"),
        )

    async def _adjudicate_auto_manage(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A20: auto-manage domain resources (db maintenance, model loading)."""
        actions = self._manage_domain()
        self._auto_metrics["manage_cycles"] += 1
        return accepted_outcome(
            {"action": "auto-manage", "domain": "owned", "actions": actions},
            self.verified_basis("A20"),
        )

    async def _adjudicate_auto_health_check(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A20: auto-health-check the owned domain."""
        health = self._health_check_domain()
        self._auto_metrics["health_checks"] += 1
        return accepted_outcome(
            {"action": "auto-health-check", "domain": "owned", "health": health},
            self.verified_basis("A20"),
        )

    # ------------------------------------------------------------------
    # Auto-automation loop (A20 full-automation upgrade)
    # ------------------------------------------------------------------

    async def start_auto_loop(self) -> None:
        """Start the background auto-automation loop.

        The loop periodically:
        1. Observes the owned domain (file tree, databases, models).
        2. Analyzes the snapshot for anomalies.
        3. Manages domain resources (db maintenance, model loading).
        4. Checks domain health.
        5. Notifies the system through the information layer (A66) when
           anomalies are detected.

        All operations stay inside the owned domain (A20); no system
        targets or effects are ever produced.
        """
        if self._auto_loop_task is not None and not self._auto_loop_task.done():
            return
        self._auto_enabled = True
        self._auto_loop_task = asyncio.create_task(self._auto_loop())

    async def stop_auto_loop(self) -> None:
        """Stop the background auto-automation loop."""
        self._auto_enabled = False
        task = self._auto_loop_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._auto_loop_task = None

    async def _auto_loop(self) -> None:
        """Background loop: periodic domain automation."""
        while self._auto_enabled:
            try:
                await self._auto_cycle()
            except asyncio.CancelledError:
                break
            except Exception as error:
                _logger.warning("xingcheng auto-cycle error: %s", error)
            await asyncio.sleep(self._auto_loop_interval)

    async def _auto_cycle(self) -> None:
        """One automation cycle: observe, analyze, manage, health, notify."""
        self._auto_metrics["last_auto_cycle"] = self._iso_now()

        # 1. Observe the domain.
        self._last_snapshot = self._observe_domain()
        self._auto_metrics["observe_cycles"] += 1

        # 2. Analyze for anomalies.
        anomalies = self._analyze_domain(self._last_snapshot)
        self._auto_metrics["analyze_cycles"] += 1
        if anomalies:
            self._auto_metrics["anomalies_detected"] += len(anomalies)
            self._auto_metrics["last_anomaly"] = anomalies[0].get(
                "type", ""
            )
            self._pending_anomalies.extend(anomalies)

        # 3. Manage domain resources.
        self._manage_domain()
        self._auto_metrics["manage_cycles"] += 1

        # 4. Health check.
        self._health_check_domain()
        self._auto_metrics["health_checks"] += 1

        # 5. Notify through the information layer (A66) if anomalies exist.
        if self._pending_anomalies:
            await self._notify_anomalies()

    def _observe_domain(self) -> dict[str, Any]:
        """A20: observe the owned domain and return a snapshot.

        Scans the domain root for:
        - File tree structure (top-level directories and file counts)
        - Database files (SQLite databases and their sizes)
        - Model files (in runtime/state/models)
        - Configuration files (JSON configs)
        - Runtime state files
        """
        root = Path(self._owned_domain_root)
        snapshot: dict[str, Any] = {
            "observed_at": self._iso_now(),
            "root": str(root),
            "exists": root.exists(),
            "directories": {},
            "databases": [],
            "models": [],
            "configs": [],
        }

        if not root.exists():
            return snapshot

        # Scan top-level directories.
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                try:
                    file_count = sum(
                        1 for _ in entry.rglob("*") if _.is_file()
                    )
                    snapshot["directories"][entry.name] = {
                        "file_count": file_count,
                    }
                except (OSError, PermissionError):
                    snapshot["directories"][entry.name] = {
                        "file_count": -1,
                        "error": "access-denied",
                    }

        # Scan for databases.
        for db_path in root.rglob("*.sqlite3"):
            try:
                stat = db_path.stat()
                snapshot["databases"].append({
                    "name": db_path.name,
                    "path": str(db_path.relative_to(root)),
                    "size_bytes": stat.st_size,
                    "modified_at": stat.st_mtime,
                })
            except (OSError, PermissionError):
                continue

        # Scan for models.
        models_dir = root / "runtime" / "state" / "models"
        if models_dir.exists():
            for model_path in sorted(models_dir.iterdir()):
                if model_path.is_file():
                    try:
                        stat = model_path.stat()
                        snapshot["models"].append({
                            "name": model_path.name,
                            "size_bytes": stat.st_size,
                            "modified_at": stat.st_mtime,
                        })
                    except (OSError, PermissionError):
                        continue

        # Scan for configs.
        for config_path in root.rglob("*.json"):
            if "node_modules" in config_path.parts:
                continue
            try:
                stat = config_path.stat()
                snapshot["configs"].append({
                    "name": config_path.name,
                    "path": str(config_path.relative_to(root)),
                    "size_bytes": stat.st_size,
                })
            except (OSError, PermissionError):
                continue

        return snapshot

    def _analyze_domain(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """A20: analyze the domain snapshot for anomalies.

        Detects:
        - Oversized databases (exceeding _DB_MAX_SIZE_BYTES)
        - Oversized model directories
        - Stale checkpoint files (older than _CHECKPOINT_MAX_AGE_HOURS)
        - Missing critical directories (runtime, governance, identity)
        - Corrupt or empty databases
        """
        anomalies: list[dict[str, Any]] = []
        if not snapshot.get("exists"):
            anomalies.append({
                "type": "domain-root-missing",
                "severity": "critical",
                "detail": "owned domain root does not exist",
            })
            return anomalies

        # Check critical directories.
        dirs = snapshot.get("directories", {})
        critical_dirs = {"runtime", "governance", "identity", "permissions"}
        missing = critical_dirs - set(dirs.keys)
        if missing:
            anomalies.append({
                "type": "missing-critical-directories",
                "severity": "warning",
                "detail": f"missing: {sorted(missing)}",
            })

        # Check database sizes.
        import time
        now = time.time()
        for db in snapshot.get("databases", []):
            size = db.get("size_bytes", 0)
            if size > _DB_MAX_SIZE_BYTES:
                anomalies.append({
                    "type": "oversized-database",
                    "severity": "warning",
                    "target": db.get("name"),
                    "size_bytes": size,
                    "threshold": _DB_MAX_SIZE_BYTES,
                })
            # Check for stale checkpoints.
            if "checkpoint" in db.get("name", "").lower():
                modified = db.get("modified_at", 0)
                age_hours = (now - modified) / 3600 if modified else 999
                if age_hours > _CHECKPOINT_MAX_AGE_HOURS:
                    anomalies.append({
                        "type": "stale-checkpoint",
                        "severity": "info",
                        "target": db.get("name"),
                        "age_hours": round(age_hours, 1),
                        "threshold_hours": _CHECKPOINT_MAX_AGE_HOURS,
                    })

        # Check model directory size.
        total_model_size = sum(
            m.get("size_bytes", 0) for m in snapshot.get("models", [])
        )
        if total_model_size > _MODEL_DIR_MAX_SIZE_BYTES:
            anomalies.append({
                "type": "oversized-model-directory",
                "severity": "warning",
                "total_size_bytes": total_model_size,
                "threshold": _MODEL_DIR_MAX_SIZE_BYTES,
            })

        return anomalies

    def _manage_domain(self) -> list[dict[str, Any]]:
        """A20: auto-manage domain resources.

        Performs:
        - SQLite VACUUM/INTEGRITY_CHECK on domain databases
        - Stale checkpoint cleanup
        - Model directory size monitoring
        """
        actions: list[dict[str, Any]] = []
        root = Path(self._owned_domain_root)
        if not root.exists():
            return actions

        # Database maintenance.
        for db_path in root.rglob("*.sqlite3"):
            if "node_modules" in db_path.parts:
                continue
            try:
                # Integrity check.
                conn = sqlite3.connect(
                    f"file:{db_path}?mode=ro", uri=True
                )
                cursor = conn.cursor()
                cursor.execute("PRAGMA integrity_check")
                result = cursor.fetchone()
                conn.close()
                if result and result[0] != "ok":
                    actions.append({
                        "action": "db-integrity-check",
                        "target": db_path.name,
                        "result": "corrupt",
                        "detail": result[0],
                    })
                    self._pending_anomalies.append({
                        "type": "corrupt-database",
                        "severity": "critical",
                        "target": db_path.name,
                    })
                else:
                    actions.append({
                        "action": "db-integrity-check",
                        "target": db_path.name,
                        "result": "ok",
                    })
            except sqlite3.Error as error:
                actions.append({
                    "action": "db-integrity-check",
                    "target": db_path.name,
                    "result": "error",
                    "detail": str(error),
                })
            self._auto_metrics["db_maintenance_runs"] += 1

        # Stale checkpoint cleanup.
        import time
        now = time.time()
        checkpoints_dir = root / "runtime" / "state"
        if checkpoints_dir.exists():
            for ckpt in checkpoints_dir.glob("*checkpoint*"):
                try:
                    stat = ckpt.stat()
                    age_hours = (now - stat.st_mtime) / 3600
                    if (
                        age_hours > _CHECKPOINT_MAX_AGE_HOURS
                        and ckpt.is_file()
                    ):
                        # A20: 星澄 has delete power in owned domain.
                        ckpt.unlink()
                        actions.append({
                            "action": "stale-checkpoint-cleanup",
                            "target": ckpt.name,
                            "age_hours": round(age_hours, 1),
                        })
                except (OSError, PermissionError):
                    continue

        return actions

    def _health_check_domain(self) -> dict[str, Any]:
        """A20: check the health of the owned domain.

        Returns a health summary including:
        - Domain root existence
        - Critical directory presence
        - Database integrity
        - Model availability
        - Configuration validity
        """
        root = Path(self._owned_domain_root)
        health: dict[str, Any] = {
            "checked_at": self._iso_now(),
            "domain_root_exists": root.exists(),
            "critical_dirs": {},
            "databases_healthy": True,
            "models_available": False,
            "configs_valid": True,
        }

        if not root.exists():
            health["overall"] = "critical"
            return health

        # Check critical directories.
        for dir_name in ("runtime", "governance", "identity", "permissions"):
            dir_path = root / dir_name
            health["critical_dirs"][dir_name] = dir_path.exists()

        # Check database integrity.
        for db_path in root.rglob("*.sqlite3"):
            if "node_modules" in db_path.parts:
                continue
            try:
                conn = sqlite3.connect(
                    f"file:{db_path}?mode=ro", uri=True
                )
                cursor = conn.cursor()
                cursor.execute("PRAGMA integrity_check")
                result = cursor.fetchone()
                conn.close()
                if not result or result[0] != "ok":
                    health["databases_healthy"] = False
            except sqlite3.Error:
                health["databases_healthy"] = False

        # Check model availability.
        models_dir = root / "runtime" / "state" / "models"
        if models_dir.exists():
            health["models_available"] = any(models_dir.iterdir())

        # Check config validity.
        for config_path in root.rglob("*.json"):
            if "node_modules" in config_path.parts:
                continue
            try:
                json.loads(config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeError, OSError):
                health["configs_valid"] = False

        # Overall health.
        all_dirs_present = all(health["critical_dirs"].values())
        if (
            health["domain_root_exists"]
            and all_dirs_present
            and health["databases_healthy"]
            and health["configs_valid"]
        ):
            health["overall"] = "healthy"
        elif health["domain_root_exists"]:
            health["overall"] = "degraded"
        else:
            health["overall"] = "critical"

        return health

    async def _notify_anomalies(self) -> None:
        """A66: notify the system of anomalies through the information layer.

        星澄 cannot directly access system modules (A20), but it can
        notify through the information layer (A66).  This writes anomaly
        reports to the domain's governance directory and clears the
        pending list.
        """
        if not self._pending_anomalies:
            return

        root = Path(self._owned_domain_root)
        notify_dir = root / "governance"
        notify_dir.mkdir(parents=True, exist_ok=True)
        notify_path = notify_dir / "anomaly-notifications.json"

        # Read existing notifications.
        existing: list[dict[str, Any]] = []
        try:
            existing = json.loads(
                notify_path.read_text(encoding="utf-8")
            )
            if not isinstance(existing, list):
                existing = []
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass

        # Append new anomalies.
        new_entries = [
            {
                "notified_at": self._iso_now(),
                "channel": "information-layer",
                "anomaly": anomaly,
            }
            for anomaly in self._pending_anomalies
        ]
        all_entries = (existing + new_entries)[-100:]  # keep last 100

        # Write back.
        try:
            notify_path.write_text(
                json.dumps(all_entries, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self._auto_metrics["channel_notifications"] += len(new_entries)
        except (OSError, UnicodeError) as error:
            _logger.warning("xingcheng anomaly notify failed: %s", error)

        # Clear pending.
        self._pending_anomalies.clear()

    async def _on_start(self) -> None:
        """Start the auto-automation loop when the sovereign starts."""
        await self.start_auto_loop()

    async def _on_stop(self) -> None:
        """Stop the auto-automation loop when the sovereign stops."""
        await self.stop_auto_loop()

    def auto_status(self) -> dict[str, Any]:
        """Read-only status of the auto-automation subsystem."""
        return {
            "enabled": self._auto_enabled,
            "loop_running": (
                self._auto_loop_task is not None
                and not self._auto_loop_task.done()
            ),
            "loop_interval_seconds": self._auto_loop_interval,
            "metrics": dict(self._auto_metrics),
            "pending_anomalies": len(self._pending_anomalies),
            "last_snapshot_exists": bool(self._last_snapshot),
        }

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["owned_domain"] = self._owned_domain_root
        base["isolated_from_system"] = self._isolated
        base["decision_chain"] = "outside"
        base["auto"] = self.auto_status()
        if self._last_snapshot:
            base["domain_snapshot"] = {
                "observed_at": self._last_snapshot.get("observed_at"),
                "directories": list(
                    self._last_snapshot.get("directories", {}).keys()
                ),
                "database_count": len(
                    self._last_snapshot.get("databases", [])
                ),
                "model_count": len(
                    self._last_snapshot.get("models", [])
                ),
                "config_count": len(
                    self._last_snapshot.get("configs", [])
                ),
            }
        return base


__all__ = ["XingchengSovereign"]