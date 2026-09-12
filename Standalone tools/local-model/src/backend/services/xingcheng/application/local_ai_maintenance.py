from __future__ import annotations

import time
from typing import Any, ClassVar

from ..infrastructure.model_engines import StarModelEngines


class LocalAiMaintenanceMixin:
    def release_idle_resources(self) -> None:
        now = time.monotonic()
        self._search_cache = {
            key: value
            for key, value in self._search_cache.items()
            if value[0] > now
        }
        self.market_data.release_idle_resources(now)
        if (
            now - self._last_self_maintenance_at
            >= self.SELF_MAINTENANCE_INTERVAL_SECONDS
        ):
            self._run_self_maintenance()

    def _new_model_engines(self) -> StarModelEngines:
        return StarModelEngines(
            self.models,
            learned_examples_by_model={
                model_id: repository.language_training_examples()
                for model_id, repository in self.repositories.items()
            },
        )

    def _run_self_maintenance(self) -> dict[str, Any]:
        started = time.perf_counter()
        reports = {
            profile.model_id: self._repository_for(profile).maintain_language_model()
            for profile in self.models.profiles
        }
        transformer_training_database = (
            self.transformer_training_repository.maintain()
        )
        rebuild_required = any(
            report.get("weights_rebuild_required") is True
            for report in reports.values()
        )
        if rebuild_required:
            self.model_engines = self._new_model_engines()
            self.native_model = self.model_engines.main
        ok = all(report.get("ok") is True for report in reports.values()) and (
            transformer_training_database.get("ok") is True
        )
        result = {
            "ok": ok,
            "mode": "autonomous-bounded-model-maintenance",
            "source_code_modified": False,
            "model_weights_rebuilt": rebuild_required,
            "model_reports": reports,
            "transformer_training_database": transformer_training_database,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }
        self._last_self_maintenance_at = time.monotonic()
        self._latest_self_maintenance = result
        self._runtime_metrics["self_maintenance_run_count"] = int(
            self._runtime_metrics["self_maintenance_run_count"]
        ) + 1
        if not ok:
            self._runtime_metrics["self_maintenance_failure_count"] = int(
                self._runtime_metrics["self_maintenance_failure_count"]
            ) + 1
        return result

    # Pending legacy data produced by frozen functionality (codex boundary).
    # Entries remain readable; no new execution is permitted. Await
    # governance-directed processing via the governed channel.
    FROZEN_PENDING_LEGACY_DATA: ClassVar[tuple[dict[str, str], ...]] = (
        {
            "id": "composed-capability-source-modules",
            "path": "src/backend/services/xingcheng/application/composed_capabilities/",
            "action": "modules written by the frozen path must be quarantined for governance review before use",
        },
        {
            "id": "capability-backups",
            "path": "runtime/state/capability-backups/",
            "action": "backups produced by the frozen path must be quarantined for governance review",
        },
        {
            "id": "self-repair-records",
            "path": "role-isolated learning databases (self_maintenance/repair records)",
            "action": "existing records stay readable; new repair execution awaits main-system central repair via governed channel",
        },
    )

    def _execute_self_repair_command(self) -> dict[str, Any]:
        # FROZEN (codex boundary): repair/rescue functionality is owned by
        # main-system central repair via governed execution; an independent tool must not
        # perform self-repair. The command is isolated and reports frozen.
        return {
            "ok": False,
            "executed": False,
            "frozen": True,
            "status": "frozen",
            "error_code": "SELF_REPAIR_FROZEN_GOVERNANCE_BOUNDARY",
            "pending_legacy_data": list(self.FROZEN_PENDING_LEGACY_DATA),
            "message": (
                "Self-repair is owned by system-rescue under governed "
                "execution; independent tools are not permitted to perform "
                "repair. Request repair through the governed channel."
            ),
            "governance_rule_modified": False,
            "source_write_performed": False,
            "version": "1.0",
        }

    def _execute_self_repair_command_legacy(self) -> dict[str, Any]:
        maintenance = self._run_self_maintenance()
        databases = {
            profile.role: self._repository_for(profile).database_status()
            for profile in self.models.profiles
        }
        evaluation = self._evaluate_upgrade(databases)
        recommendations = list(evaluation.get("recommendations") or [])
        required = [
            item
            for item in recommendations
            if isinstance(item, dict) and item.get("severity") == "required"
        ]
        ok = maintenance.get("ok") is True and evaluation.get("ok") is True
        return {
            "ok": ok,
            "executed": True,
            "status": "completed" if ok and not required else "attention-required",
            "actions": [
                "audit-and-compact-role-isolated-learning-databases",
                "verify-transformer-training-audit-chain",
                "rebuild-learning-layer-when-required",
                "evaluate-runtime-model-memory-and-capability-health",
            ],
            "maintenance": maintenance,
            "evaluation": evaluation,
            "required_recommendations": required,
            "source_write_performed": False,
            "source_write_reason": "no-validated-source-patch-was-supplied",
            "governance_rule_modified": False,
            "investment_database_write_performed": False,
            "version": "1.0",
        }
