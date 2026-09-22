"""Automatic cleanup run mixin (A185 split).

Contains the ``run_if_due`` method of the main-system internal
automatic-cleanup service (A533/A534: the retired standalone
global-cleaner is never spawned; cleanup is executed in-process).

The run is fail-open: every phase is individually guarded, the scheduler
loop never raises, and a failed phase is recorded as evidence rather than
aborting the cycle.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any


class DailyGlobalCleanerRunMixin:
    """Main-system internal automatic cleanup run-if-due execution."""

    app: Any
    _run_lock: object
    CYCLE_BYTE_QUOTA: int
    MAX_MODULES_PER_CYCLE: int
    TRASH_MAX_MODULES_PER_CYCLE: int

    def is_due(self) -> bool:
        raise NotImplementedError

    def _load_state(self) -> dict[str, Any]:
        raise NotImplementedError

    def _save_state(self, state: dict[str, Any]) -> None:
        raise NotImplementedError

    def _iso_now(self) -> str:
        raise NotImplementedError

    async def _run_module_self_cleanup_sweep(
        self, byte_budget: int | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _trash_candidates(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _record_cleanup_audit(self, outcome: dict[str, Any]) -> str:
        raise NotImplementedError

    async def _cleanup_retired_trash(self, byte_budget: int) -> dict[str, Any]:
        """Clean ephemeral residue of registry-classified retired modules.

        The candidate list is revalidated against the current architecture
        registry on every cycle (classification never authorizes deletion
        by itself); each root is swept with the bounded tool-local cleanup
        rules that never touch protected state, business data or
        git-tracked content.
        """
        from governance_rule.execution.tool_runtime.tool_local_cleanup import (
            run_local_cleanup,
        )

        started_at = self._iso_now()
        candidates = self._trash_candidates()
        results: list[dict[str, Any]] = []
        cleaned_bytes = 0
        remaining = max(0, int(byte_budget))
        for candidate in candidates[: self.TRASH_MAX_MODULES_PER_CYCLE]:
            tool_id = str(candidate.get("component_id") or "").strip()
            raw_path = str(candidate.get("physical_path") or "").strip()
            if not tool_id or not raw_path:
                continue
            tool_root = Path(self.app.project_root) / raw_path
            if not tool_root.is_dir():
                results.append(
                    {"component_id": tool_id, "ok": True, "skipped": True,
                     "reason": "NO_RESIDUE"}
                )
                continue
            try:
                result = await asyncio.to_thread(
                    run_local_cleanup,
                    tool_id,
                    tool_root,
                    max_cleaned_bytes=remaining,
                )
            except Exception as error:
                results.append(
                    {"component_id": tool_id, "ok": False,
                     "error_code": "TRASH_CLEANUP_EXCEPTION",
                     "message": f"{type(error).__name__}: {error}"}
                )
                continue
            module_bytes = int(result.get("cleaned_bytes") or 0)
            cleaned_bytes += module_bytes
            remaining = max(0, remaining - module_bytes)
            results.append(
                {
                    "component_id": tool_id,
                    "ok": bool(result.get("ok")),
                    "cleaned_bytes": module_bytes,
                    "cleaned_files": len(result.get("cleaned_files") or []),
                    "cleaned_directories": len(
                        result.get("cleaned_directories") or []
                    ),
                    "quota_reached": bool(
                        any(
                            str(item.get("reason") or "").startswith(
                                "cleanup byte quota"
                            )
                            for item in result.get("skipped") or []
                        )
                    ),
                }
            )
        return {
            "operation": "retired-trash-residue-cleanup",
            "started_at": started_at,
            "completed_at": self._iso_now(),
            "candidate_count": len(candidates),
            "processed_count": len(results),
            "max_modules_per_cycle": self.TRASH_MAX_MODULES_PER_CYCLE,
            "cleaned_bytes": cleaned_bytes,
            "ok": all(item.get("ok") for item in results),
            "results": results,
        }

    async def _purge_stale_quarantine(self) -> dict[str, Any]:
        """Bounded retention sweep for aged crash-quarantine evidence.

        A root-scoped manager is constructed instead of the process
        singleton so a cleanup cycle never acts on another checkout's
        state.  Retention is configured by ``max_quarantine_age_days``
        (default 14) and never touches recent records.
        """

        from core_system.tool_isolation import ToolIsolationManager

        manager = ToolIsolationManager(Path(self.app.project_root))
        return await asyncio.to_thread(manager.purge_stale_quarantine)

    async def _reconcile_release_retention(self) -> dict[str, Any]:
        """§10.67 排程刪除——release 保留階層對帳與過期 ARCHIVE 清除。

        只刪除「已通過三閘門降為 ARCHIVE 且超過保留期」的 release 目錄；
        PREVIOUS／ACTIVE 永不動。對帳本身 fail-closed：未登錄目錄先註冊
        PREVIOUS，pointer 缺失時不昇 ACTIVE、不刪除任何東西。
        """

        from core_system.active_release_persistence import ACTIVE_POINTER_PATH
        from core_system.release_retention import (
            ReleaseRetentionRegistry,
            reconcile_release_retention,
        )

        project_root = Path(self.app.project_root)
        registry = ReleaseRetentionRegistry(
            project_root / "runtime" / "state" / "release-retention.json",
            audit_path=(
                project_root
                / "runtime"
                / "logs"
                / "release-retention.jsonl"
            ),
        )
        pointer = (
            ACTIVE_POINTER_PATH
            if ACTIVE_POINTER_PATH.is_file()
            else None
        )
        return await asyncio.to_thread(
            reconcile_release_retention,
            project_root / "runtime" / "releases",
            registry,
            active_pointer_path=pointer,
        )

    async def _cleanup_rag_generations(self) -> dict[str, Any]:
        """§10.67 排程刪除——RAG 索引世代清除。

        只在 RAG runtime 已啟動時才執行（lazy contract：清理循環不得
        為了掃除而喚醒 RAG）。實際刪除走
        ``GenerationManager.cleanup_old_generations``——只動 RETIRED／
        FAILED 世代、保留 rollback 視窗、容量閘未知時 fail-closed。
        世代管理器活在 pipeline 的專用 loop 上，因此 coroutine 經
        ``_loop_worker.submit`` 派回該 loop 並有界等待。
        """

        runtime = getattr(self.app, "rag_runtime", None)
        pipeline = getattr(runtime, "_pipeline", None)
        manager = getattr(pipeline, "generation_manager", None)
        worker = getattr(runtime, "_loop_worker", None)
        if manager is None or worker is None:
            return {
                "ok": True,
                "operation": "rag-generation-cleanup",
                "skipped": True,
                "reason": "rag-not-started",
            }
        submit = getattr(worker, "submit", None)
        if not callable(submit):
            return {
                "ok": False,
                "operation": "rag-generation-cleanup",
                "error_code": "RAG_LOOP_UNAVAILABLE",
            }
        try:
            future = submit(manager.cleanup_old_generations())
            result = await asyncio.to_thread(future.result, 120)
        except Exception as error:
            return {
                "ok": False,
                "operation": "rag-generation-cleanup",
                "error_code": "RAG_GENERATION_CLEANUP_EXCEPTION",
                "message": f"{type(error).__name__}: {error}",
            }
        record = (
            result.to_dict() if hasattr(result, "to_dict") else dict(result)
        )
        record["ok"] = record.get("status") in ("OK", "ok", True)
        record["operation"] = "rag-generation-cleanup"
        return record

    def _classify_orphan_roots(self) -> dict[str, Any]:
        """Revalidate unregistered physical roots (classification only).

        The architecture registry is the single authority: this phase only
        refreshes the garbage/review classification and never authorizes
        removal, movement or reuse (A534: automatic cleanup is bounded and
        revalidation is mandatory before any later destructive action).
        """

        from tasks.repair_inspection import (
            classify_orphan_component_roots,
        )

        try:
            candidates = classify_orphan_component_roots(
                Path(self.app.project_root)
            )
        except Exception as error:
            return {
                "ok": False,
                "operation": "orphan-root-classification",
                "error_code": "ORPHAN_CLASSIFICATION_EXCEPTION",
                "message": f"{type(error).__name__}: {error}",
            }
        return {
            "ok": True,
            "operation": "orphan-root-classification",
            "classification_only": True,
            "removal_authorized": False,
            "candidate_count": len(candidates),
            "candidates": candidates,
        }

    async def run_if_due(self, *, force: bool = False) -> dict[str, Any]:
        """Run one bounded automatic-cleanup cycle when due (or forced)."""
        async with self._run_lock:
            if not force and not self.is_due():
                return {"ok": True, "skipped": True, "reason": "NOT_DUE"}
            request_id = f"automatic-cleanup-{time.time_ns()}"
            started_monotonic = time.monotonic()
            state = self._load_state()
            state.update(
                {
                    "last_started_epoch": time.time(),
                    "last_started_at": self._iso_now(),
                    "last_request_id": request_id,
                    "last_ok": False,
                    "last_status": "running",
                }
            )
            state.pop("last_error", None)
            self._save_state(state)

            findings: dict[str, Any] = {}
            try:
                findings["trash"] = await self._cleanup_retired_trash(
                    self.CYCLE_BYTE_QUOTA
                )
            except Exception as error:
                findings["trash"] = {
                    "ok": False,
                    "error_code": "TRASH_CLEANUP_EXCEPTION",
                    "message": f"{type(error).__name__}: {error}",
                }
            trash_bytes = int(findings["trash"].get("cleaned_bytes") or 0)
            try:
                findings["modules"] = await self._run_module_self_cleanup_sweep(
                    max(0, self.CYCLE_BYTE_QUOTA - trash_bytes)
                )
            except Exception as error:
                findings["modules"] = {
                    "ok": False,
                    "error_code": "MODULE_CLEANUP_EXCEPTION",
                    "message": f"{type(error).__name__}: {error}",
                }
            try:
                findings["quarantine"] = await self._purge_stale_quarantine()
            except Exception as error:
                findings["quarantine"] = {
                    "ok": False,
                    "error_code": "QUARANTINE_RETENTION_EXCEPTION",
                    "message": f"{type(error).__name__}: {error}",
                }
            try:
                findings["releases"] = await self._reconcile_release_retention()
            except Exception as error:
                findings["releases"] = {
                    "ok": False,
                    "error_code": "RELEASE_RETENTION_EXCEPTION",
                    "message": f"{type(error).__name__}: {error}",
                }
            try:
                findings["rag_generations"] = await self._cleanup_rag_generations()
            except Exception as error:
                findings["rag_generations"] = {
                    "ok": False,
                    "error_code": "RAG_GENERATION_CLEANUP_EXCEPTION",
                    "message": f"{type(error).__name__}: {error}",
                }
            try:
                findings["orphans"] = self._classify_orphan_roots()
            except Exception as error:
                findings["orphans"] = {
                    "ok": False,
                    "error_code": "ORPHAN_CLASSIFICATION_EXCEPTION",
                    "message": f"{type(error).__name__}: {error}",
                }
            module_bytes = int(findings["modules"].get("cleaned_bytes_total") or 0)
            cleaned_bytes = trash_bytes + module_bytes
            outcome = {
                "operation": "automatic-cleanup",
                "authority": "health-maintenance-test-sub-sovereign",
                "executor": "main-system-internal-cleanup",
                "request_id": request_id,
                "scope": (
                    "temp",
                    "cache",
                    "expired-log",
                    "orphan-classification",
                    "retired-trash-residue",
                    "crash-quarantine-retention",
                    "release-retention-reconciliation",
                ),
                "byte_quota": self.CYCLE_BYTE_QUOTA,
                "cleaned_bytes": cleaned_bytes,
                "duration_seconds": round(
                    time.monotonic() - started_monotonic, 3
                ),
                "findings": findings,
            }
            outcome["audit_outcome"] = self._record_cleanup_audit(outcome)
            ok = (
                findings["trash"].get("ok") is not False
                and bool(findings["modules"].get("ok"))
                and findings["quarantine"].get("ok") is not False
                and findings["releases"].get("ok") is not False
                and findings["orphans"].get("ok") is not False
            )
            state.update(
                {
                    "last_completed_at": self._iso_now(),
                    "last_ok": ok,
                    "last_status": "completed" if ok else "degraded",
                    "last_outcome": outcome,
                    "module_cleanup": findings["modules"],
                    "quarantine_retention": findings["quarantine"],
                    "orphan_classification": findings["orphans"],
                }
            )
            if not ok:
                state["last_error"] = {
                    "error_code": "AUTOMATIC_CLEANUP_PARTIAL",
                    "message": "one or more cleanup phases reported failure",
                }
            self._save_state(state)
            return {
                "ok": ok,
                "stage": "completed",
                "request_id": request_id,
                "detail": outcome,
            }


__all__ = ["DailyGlobalCleanerRunMixin"]
