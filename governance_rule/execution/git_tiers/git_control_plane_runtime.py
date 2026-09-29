"""Control-plane snapshot assembly, evaluation and alerts (A185 split).

Extracted from ``git_control_plane`` (source-size contract): the
``_Runtime`` mixin owns snapshot construction (§164), dimensional health
evaluation (§170-172), deterministic global-state evaluation (§163),
edge-triggered events + alert dedup (§186-188) and the read-only
supervisor getters (§197).  State lives on the ``GitControlPlane``
instance; this module only contributes methods.
"""
from __future__ import annotations

from typing import Any, Optional

from .git_control_plane_types import (
    AUDIT_LATENCY_WARN_MS,
    BACKPRESSURE_QUEUE_DEPTH,
    BUSY_ACTIVE_WORKER_FRACTION,
    Dimension,
    DimensionHealth,
    EventType,
    GitEvent,
    GitGlobalState,
    GitSnapshot,
    HealthStatus,
    ReasonCode,
    Severity,
    StateKind,
    _severity_for_status,
)


class _Runtime:
    """Snapshot + evaluation + alert surface of the control plane."""

    # -- snapshot ------------------------------------------------------------

    def snapshot(
        self, *, kind: StateKind = StateKind.VERIFIED
    ) -> GitSnapshot:
        """Build a unified snapshot (§164).

        FAST returns the cached snapshot (invalidated by any event);
        VERIFIED re-reads every source of truth.
        """
        if kind is StateKind.FAST and self._fast_snapshot is not None:
            return self._fast_snapshot
        data = self.collect_all()
        with self._lock:
            self._generation += 1
            generation = self._generation
        snap = self._build_snapshot(generation, kind, data)
        snap.global_state = self.evaluate_global_state(snap).value
        self._edge_trigger(snap)
        self._update_alerts(snap)
        if kind is StateKind.VERIFIED:
            self._persist(snap)
        self._fast_snapshot = snap
        return snap

    def _build_snapshot(
        self, generation: int, kind: StateKind,
        data: dict[str, dict[str, Any]],
    ) -> GitSnapshot:
        state = data["state"]
        workers = data["workers"]
        queue = data["queue"]
        locks = data["locks"]
        audit = data["audit"]
        remotes = data["remotes"]
        recovery = data["recovery"]
        perf = data["performance"]
        health_extra = data["health"]
        snap = GitSnapshot(
            generation=generation,
            timestamp=self._now(),
            state_kind=kind.value,
            global_state=GitGlobalState.ERROR.value,
            repository_mode=state.get("repository_mode", "worktree"),
            main_revision=state.get("main_revision", ""),
            origin_revision=remotes.get("origin_revision", ""),
            main_dirty=bool(state.get("main_dirty")),
            worktree_count=int(state.get("worktree_count", 0)),
            worker_count=int(workers.get("worker_count", 0)),
            active_workers=int(workers.get("active_workers", 0)),
            dirty_workers=int(workers.get("dirty_workers", 0)),
            quarantined_workers=int(workers.get("quarantined_workers", 0)),
            watcher_count=int(workers.get("watcher_count", 0)),
            duplicate_watchers=int(workers.get("duplicate_watchers", 0)),
            stale_watchers=int(workers.get("stale_watchers", 0)),
            queue_depth=int(queue.get("queue_depth", 0)),
            running_merge=queue.get("running_merge", ""),
            oldest_queue_age=float(queue.get("oldest_queue_age", 0.0)),
            active_locks=int(locks.get("active_locks", 0)),
            stale_locks=int(locks.get("stale_locks", 0)),
            audit_sequence=int(audit.get("audit_sequence", 0)),
            audit_chain_valid=bool(audit.get("audit_chain_valid", True)),
            audit_latency_ms=float(audit.get("audit_latency_ms", 0.0)),
            hook_health=(
                HealthStatus.UNKNOWN.value
                if health_extra.get("unknown") else HealthStatus.HEALTHY.value
            ),
            repository_object_health=state.get(
                "repository_object_health", HealthStatus.UNKNOWN.value),
            disk_pressure=bool(health_extra.get("disk_pressure")),
            remote_relations=dict(remotes.get("remote_relations", {})),
            performance_summary=dict(perf.get("performance_summary", {})),
        )
        snap.health = self._evaluate_dimensions(snap, data)
        snap.reason_codes = sorted({
            rc for d in snap.health.values() for rc in d["reason_codes"]
            if rc != ReasonCode.OK.value
        })
        return snap

    # -- dimensional health (§170-172) ---------------------------------------

    def _evaluate_dimensions(
        self, snap: GitSnapshot, data: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        dims: list[DimensionHealth] = []
        state, workers = data["state"], data["workers"]
        queue, locks, audit = data["queue"], data["locks"], data["audit"]
        remotes, recovery = data["remotes"], data["recovery"]
        perf = data["performance"]

        def _add(dim: Dimension, status: HealthStatus,
                 codes: list[ReasonCode], detail: str = "") -> None:
            dims.append(DimensionHealth(
                dim, status, [c.value for c in codes], detail))

        if state.get("unknown"):
            _add(Dimension.REPOSITORY, HealthStatus.UNKNOWN,
                 [ReasonCode.STATE_UNKNOWN])
        elif snap.repository_object_health == HealthStatus.ERROR.value:
            _add(Dimension.REPOSITORY, HealthStatus.ERROR,
                 [ReasonCode.OBJECT_CORRUPTION])
        elif state.get("unknown_main_ref"):
            _add(Dimension.REPOSITORY, HealthStatus.ERROR,
                 [ReasonCode.STATE_UNKNOWN])
        elif snap.main_dirty:
            _add(Dimension.REPOSITORY, HealthStatus.DEGRADED,
                 [ReasonCode.MAIN_DIRTY])
        else:
            _add(Dimension.REPOSITORY, HealthStatus.HEALTHY, [ReasonCode.OK])

        _add(Dimension.WORKTREES, HealthStatus.HEALTHY, [ReasonCode.OK])

        if workers.get("unknown"):
            _add(Dimension.WORKERS, HealthStatus.UNKNOWN,
                 [ReasonCode.STATE_UNKNOWN])
        elif snap.dirty_workers:
            codes = [ReasonCode.WORKER_DIRTY]
            _add(Dimension.WORKERS, HealthStatus.WARN, codes)
        else:
            _add(Dimension.WORKERS, HealthStatus.HEALTHY, [ReasonCode.OK])

        if snap.duplicate_watchers:
            _add(Dimension.WATCHERS, HealthStatus.DEGRADED,
                 [ReasonCode.DUPLICATE_WATCHER])
        elif snap.stale_watchers:
            _add(Dimension.WATCHERS, HealthStatus.WARN,
                 [ReasonCode.STALE_WATCHER])
        else:
            _add(Dimension.WATCHERS, HealthStatus.HEALTHY, [ReasonCode.OK])

        if snap.queue_depth >= BACKPRESSURE_QUEUE_DEPTH:
            _add(Dimension.QUEUE, HealthStatus.DEGRADED,
                 [ReasonCode.QUEUE_DEPTH_HIGH])
        elif snap.queue_depth:
            _add(Dimension.QUEUE, HealthStatus.WARN, [ReasonCode.OK])
        else:
            _add(Dimension.QUEUE, HealthStatus.HEALTHY, [ReasonCode.OK])

        if snap.stale_locks:
            _add(Dimension.LOCKS, HealthStatus.DEGRADED,
                 [ReasonCode.STALE_LOCK])
        else:
            _add(Dimension.LOCKS, HealthStatus.HEALTHY, [ReasonCode.OK])

        if audit.get("unknown"):
            _add(Dimension.AUDIT, HealthStatus.UNKNOWN,
                 [ReasonCode.STATE_UNKNOWN])
        elif not snap.audit_chain_valid:
            _add(Dimension.AUDIT, HealthStatus.ERROR,
                 [ReasonCode.AUDIT_CHAIN_INVALID])
        elif snap.audit_latency_ms > AUDIT_LATENCY_WARN_MS:
            _add(Dimension.AUDIT, HealthStatus.WARN,
                 [ReasonCode.AUDIT_LATENCY_HIGH])
        else:
            _add(Dimension.AUDIT, HealthStatus.HEALTHY, [ReasonCode.OK])

        hook_codes: list[ReasonCode] = [ReasonCode.OK]
        hook_status = HealthStatus.HEALTHY
        if snap.hook_health in (HealthStatus.ERROR.value,
                                HealthStatus.UNKNOWN.value):
            hook_status = HealthStatus.ERROR
            hook_codes = [ReasonCode.HOOK_HASH_MISMATCH]
        _add(Dimension.HOOKS, hook_status, hook_codes)

        relations = remotes.get("remote_relations", {})
        lvo = relations.get("local_vs_origin", "")
        if remotes.get("unknown"):
            _add(Dimension.ORIGIN, HealthStatus.UNKNOWN,
                 [ReasonCode.STATE_UNKNOWN])
        else:
            if lvo == "diverged":
                _add(Dimension.ORIGIN, HealthStatus.DEGRADED,
                     [ReasonCode.ORIGIN_DIVERGED])
            else:
                _add(Dimension.ORIGIN, HealthStatus.HEALTHY, [ReasonCode.OK])

        if recovery.get("recovery_in_progress"):
            _add(Dimension.RECOVERY, HealthStatus.DEGRADED,
                 [ReasonCode.RECOVERY_IN_PROGRESS])
        else:
            _add(Dimension.RECOVERY, HealthStatus.HEALTHY, [ReasonCode.OK])

        if perf.get("unknown"):
            _add(Dimension.PERFORMANCE, HealthStatus.UNKNOWN,
                 [ReasonCode.STATE_UNKNOWN])
        else:
            _add(Dimension.PERFORMANCE, HealthStatus.HEALTHY, [ReasonCode.OK])

        if snap.disk_pressure:
            _add(Dimension.STORAGE, HealthStatus.DEGRADED,
                 [ReasonCode.DISK_PRESSURE])
        else:
            _add(Dimension.STORAGE, HealthStatus.HEALTHY, [ReasonCode.OK])

        return {d.dimension.value: d.to_dict() for d in dims}

    # -- global state (§163, deterministic) -----------------------------------

    def evaluate_global_state(self, snap: GitSnapshot) -> GitGlobalState:
        health = snap.health
        statuses = {d: h["status"] for d, h in health.items()}
        critical_unknown = any(
            statuses.get(d) == HealthStatus.UNKNOWN.value
            for d in (Dimension.REPOSITORY.value, Dimension.AUDIT.value,
                      Dimension.HOOKS.value, Dimension.ORIGIN.value,
                      Dimension.RECOVERY.value)
        )
        if statuses.get(Dimension.REPOSITORY.value) == HealthStatus.ERROR.value:
            return GitGlobalState.ERROR if any(
                h.get("error") for h in ()
            ) else GitGlobalState.READ_ONLY
        if (statuses.get(Dimension.AUDIT.value) == HealthStatus.ERROR.value
                or statuses.get(Dimension.HOOKS.value) == HealthStatus.ERROR.value):
            return GitGlobalState.READ_ONLY
        if statuses.get(Dimension.RECOVERY.value) == HealthStatus.DEGRADED.value:
            return GitGlobalState.RECOVERY
        if critical_unknown:
            return GitGlobalState.DEGRADED
        degraded = (
            snap.main_dirty
            or statuses.get(Dimension.WATCHERS.value) == HealthStatus.DEGRADED.value
            or statuses.get(Dimension.ORIGIN.value) == HealthStatus.DEGRADED.value
            or statuses.get(Dimension.LOCKS.value) == HealthStatus.DEGRADED.value
        )
        if degraded:
            return GitGlobalState.DEGRADED
        if (snap.queue_depth >= BACKPRESSURE_QUEUE_DEPTH or snap.disk_pressure
                or snap.audit_latency_ms > AUDIT_LATENCY_WARN_MS * 4
                or statuses.get(Dimension.STORAGE.value) == HealthStatus.DEGRADED.value):
            return GitGlobalState.BACKPRESSURE
        if snap.queue_depth or snap.running_merge or (
                snap.worker_count and
                snap.active_workers >= snap.worker_count * BUSY_ACTIVE_WORKER_FRACTION):
            return GitGlobalState.BUSY
        if all(s == HealthStatus.HEALTHY.value for s in statuses.values()):
            return GitGlobalState.HEALTHY
        return GitGlobalState.BUSY

    # -- edge-triggered events + alert dedup (§186-188) ------------------------

    def _edge_trigger(self, snap: GitSnapshot) -> None:
        for dim, h in snap.health.items():
            prev = self._prev_dimension_status.get(dim)
            cur = h["status"]
            if prev is not None and prev != cur:
                severity = _severity_for_status(cur)
                self.emit(GitEvent(
                    EventType.STATE_CHANGED, severity,
                    detail=f"{dim}:{prev}->{cur}",
                ))
                if cur == HealthStatus.HEALTHY.value and prev in (
                        HealthStatus.WARN.value, HealthStatus.DEGRADED.value,
                        HealthStatus.ERROR.value):
                    self.emit(GitEvent(
                        EventType.HEALTH_RECOVERED, Severity.INFO,
                        detail=f"{dim}:{prev}->{cur}",
                    ))
            self._prev_dimension_status[dim] = cur

    def _update_alerts(self, snap: GitSnapshot) -> None:
        now = self._now()
        active = set(snap.reason_codes)
        for code in active:
            entry = self._alerts.get(code)
            if entry is None:
                self._alerts[code] = {
                    "alert_key": code, "first_seen": now,
                    "last_seen": now, "occurrence_count": 1,
                    "state": "ACTIVE",
                }
            else:
                entry["last_seen"] = now
                entry["occurrence_count"] += 1
                entry["state"] = "ACTIVE"
        for code, entry in self._alerts.items():
            if code not in active and entry["state"] == "ACTIVE":
                entry["state"] = "RECOVERED"
                self.emit(GitEvent(
                    EventType.HEALTH_RECOVERED, Severity.INFO,
                    detail=f"alert recovered: {code}",
                ))

    def alerts(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._alerts.items()}

    # -- supervisor read-only API (§197) ---------------------------------------

    def get_global_state(self) -> dict[str, Any]:
        snap = self.snapshot(kind=StateKind.FAST) or self.snapshot()
        return {
            "global_state": snap.global_state,
            "generation": snap.generation,
            "reason_codes": snap.reason_codes,
            "health": snap.health,
        }

    def get_worker_state(self) -> dict[str, Any]:
        return self.collect("workers")

    def get_queue_state(self) -> dict[str, Any]:
        return self.collect("queue")

    def get_remote_state(self) -> dict[str, Any]:
        return self.collect("remotes")

    def get_audit_health(self) -> dict[str, Any]:
        return self.collect("audit")

    def get_recovery_health(self) -> dict[str, Any]:
        return self.collect("recovery")

    def get_performance_health(self) -> dict[str, Any]:
        return self.collect("performance")


__all__ = ["_Runtime"]
