"""§10.65 dual-track act-1 shadow harness for ``maintenance_controller``.

Runs the C ``NativeMaintenance`` job table in parallel with the Python
``MaintenanceScheduler``.  Python remains the authoritative path: admission
gates, next-due selection, terminal transitions and the TTL probe cache are
compared item-by-item and divergences are appended to
``runtime/logs/native-shadow/maintenance-controller.jsonl`` as governed
audit evidence.

Fail-closed semantics mirror ``connection_watchdog_native_shadow``:

  * missing/invalid policy or missing native extension -> no shadow;
  * any native error -> the shadow disables itself after one
    ``shadow-disabled`` audit record; the Python path is unaffected;
  * modes other than ``"shadow"`` are refused (``"primary"``/``"retire"``
    are later acts).

Flag mapping (caller folds Python's policy context into the two C inputs):

  * ``system_blocked`` — the union of ``evaluate_policy``'s global early
    returns: blocking recovery state, shutdown draining, cooldown active,
    active lease conflict.  Refuses every class, matching Python.
  * ``system_idle`` — the M1 health gate: ``pg_healthy`` and all load
    thresholds within ``DEFAULT_POLICY`` bounds.  Consulted by M1 and M2.
  * ``authorized`` — ``governed_authorization`` for M2.
  * generation mismatch is checked inside the C layer itself.

Known intentional modeling gaps (recorded, not hidden):

  * ``get_next_job`` pops FIFO from a queue already filled in priority
    order; ``next_due`` picks min(priority, scheduled_at) with expiry —
    ordering divergences under equal timestamps are evidence, not noise.
  * Python expires queued jobs by ``admitted_at`` age inside ``tick``;
    the C side expires them inside ``next_due``.  Same bound, different
    trigger point.
  * ``requeue_job`` is only reached by startup recovery (and currently
    no-ops on an empty ``_running`` map); it is not mirrored.
  * A non-default ``policy_evaluator`` makes the flag folding approximate;
    the integration uses ``DEFAULT_POLICY`` either way.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import MaintenanceRiskClass, MaintenanceJobStatus
from .policies import DEFAULT_POLICY, SystemRecoveryState

_COMPONENT = "maintenance_controller"
_POLICY_REL = Path("main-system") / "config" / "native-shadow.json"
_LOG_REL = (
    Path("main-system")
    / "runtime"
    / "logs"
    / "native-shadow"
    / "maintenance-controller.jsonl"
)

_RISK_TO_C = {
    MaintenanceRiskClass.M0_OBSERVE: 0,
    MaintenanceRiskClass.M1_SAFE_AUTO: 1,
    MaintenanceRiskClass.M2_GOVERNED_AUTO: 2,
    MaintenanceRiskClass.M3_APPROVAL_REQUIRED: 3,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


def _fold_flags(policy_context: dict[str, Any]) -> tuple[bool, bool, bool]:
    """Fold Python's policy context into the C gate inputs.

    Returns ``(system_idle, authorized, system_blocked)``.  This is an
    independent restatement of ``evaluate_policy``'s non-class-specific
    gates under ``DEFAULT_POLICY`` — the comparison target for the C
    risk-class switch.
    """
    recovery = policy_context.get("recovery_state", SystemRecoveryState.NORMAL)
    if isinstance(recovery, str):
        try:
            recovery = SystemRecoveryState(recovery)
        except ValueError:
            recovery = SystemRecoveryState.NORMAL
    system_blocked = (
        recovery in DEFAULT_POLICY.recovery_states_blocking
        or bool(policy_context.get("shutdown_draining", False))
        or bool(policy_context.get("maintenance_cooldown_active", False))
        or bool(policy_context.get("active_lease_conflict", False))
    )
    system_idle = (
        bool(policy_context.get("pg_healthy", True))
        and float(policy_context.get("pg_latency_ms", 0) or 0)
            <= DEFAULT_POLICY.pg_max_latency_ms
        and float(policy_context.get("pg_lock_pressure", 0) or 0)
            <= DEFAULT_POLICY.pg_max_lock_pressure
        and float(policy_context.get("transport_backlog", 0) or 0)
            <= DEFAULT_POLICY.transport_max_backlog
        and float(policy_context.get("transport_oldest_pending_age_seconds", 0) or 0)
            <= DEFAULT_POLICY.transport_max_oldest_age_seconds
        and float(policy_context.get("disk_pressure", 0) or 0)
            <= DEFAULT_POLICY.disk_max_pressure
    )
    authorized = bool(policy_context.get("governed_authorization", False))
    return system_idle, authorized, system_blocked


class MaintenanceNativeShadow:
    """Parallel C-job-table observer for ``MaintenanceScheduler``."""

    def __init__(self, log_path: Path, native_mt: Any) -> None:
        self._log_path = log_path
        self._mt = native_mt
        self._disabled = False
        self._error_emitted = False

    @classmethod
    def from_policy(
        cls,
        project_root: Path,
        *,
        tick_interval_ms: int,
        max_job_age_ms: int,
        max_retry_attempts: int,
        retry_backoff_ms: int,
        generation: int,
    ) -> Optional["MaintenanceNativeShadow"]:
        """Build a shadow when the governed policy enables ``shadow`` mode.

        Returns ``None`` for absent/invalid policy, any mode other than
        ``"shadow"``, or when the native extension is unavailable —
        every refusal path leaves the component Python-only.
        """
        root = Path(project_root)
        try:
            policy = json.loads(
                (root / _POLICY_REL).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None
        components = policy.get("components")
        entry = (components or {}).get(_COMPONENT) if isinstance(components, dict) else None
        mode = str((entry or {}).get("mode") or "off").strip().lower()
        if mode != "shadow":
            return None
        try:
            from shared_layer.performance.native_dispatcher import (
                _load_native,
            )

            native = _load_native()
            mt = native.NativeMaintenance(
                tick_interval_ms,
                max_job_age_ms,
                max_retry_attempts,
                retry_backoff_ms,
                generation,
            )
        except Exception:
            return None
        return cls(root / _LOG_REL, mt)

    # --- generation ---

    def observe_generation(self, generation: int) -> None:
        """Mirror ``set_generation`` into the C job table."""
        if self._disabled:
            return
        try:
            self._mt.set_generation(int(generation))
        except Exception as exc:
            self._disable("native-generation-error", exc)

    # --- admission gate ---

    def observe_admit(
        self,
        *,
        job_id: str,
        action_id: str,
        risk_class: MaintenanceRiskClass,
        priority: int,
        generation: int,
        policy_context: dict[str, Any],
        py_allowed: bool,
        now_s: float,
    ) -> bool:
        """Compare the C admission gate with Python's ``decision.allowed``.

        Returns the native verdict so the caller can veto the mirrored
        job when a downstream Python gate (budget/lease/cooldown) refuses
        a policy-admitted candidate.
        """
        if self._disabled:
            return False
        try:
            system_idle, authorized, system_blocked = _fold_flags(policy_context)
            native_allowed = bool(
                self._mt.admit(
                    job_id,
                    action_id,
                    _RISK_TO_C.get(risk_class, -1),
                    int(priority),
                    int(generation),
                    system_idle,
                    authorized,
                    _ms(now_s),
                    system_blocked=system_blocked,
                )
            )
            if native_allowed != bool(py_allowed):
                self._emit(
                    {
                        "kind": "admission-divergence",
                        "job_id": job_id,
                        "action_id": action_id,
                        "mismatches": ["allowed"],
                        "inputs": {
                            "risk_class": risk_class.value,
                            "system_idle": system_idle,
                            "authorized": authorized,
                            "system_blocked": system_blocked,
                            "generation": int(generation),
                        },
                        "python": {"allowed": bool(py_allowed)},
                        "native": {"allowed": native_allowed},
                    }
                )
                if native_allowed:
                    # Resync the mirror: the phantom native job would
                    # otherwise spam dispatch-divergence on every tick.
                    self._mt.cancel(job_id)
            return native_allowed
        except Exception as exc:  # fail-closed: native never affects Python
            self._disable("native-admit-error", exc)
            return False

    def observe_admit_veto(self, job_id: str) -> None:
        """Python refused a policy-admitted job on a downstream gate
        (budget / lease conflict / cooldown) — withdraw the mirrored
        native job so the queues stay comparable."""
        if self._disabled:
            return
        try:
            self._mt.cancel(job_id)
        except Exception as exc:
            self._disable("native-veto-error", exc)

    # --- dispatch / terminal transitions ---

    def observe_next_due(
        self,
        *,
        py_job_id: Optional[str],
        now_s: float,
    ) -> None:
        """Compare the C priority/expiry pick with Python's FIFO pop."""
        if self._disabled:
            return
        try:
            picked = self._mt.next_due(_ms(now_s))
            native_id = str(picked["job_id"]) if picked else None
            if native_id != py_job_id:
                self._emit(
                    {
                        "kind": "dispatch-divergence",
                        "mismatches": ["job_id"],
                        "python": {"job_id": py_job_id},
                        "native": {"job_id": native_id},
                    }
                )
        except Exception as exc:
            self._disable("native-next-due-error", exc)

    def observe_complete(
        self,
        *,
        job_id: str,
        py_status: MaintenanceJobStatus,
    ) -> None:
        """Mirror ``complete_job``: success → COMPLETED, failure →
        terminal withdrawal (Python does not requeue failed jobs, so a
        C ``fail`` deferral would wrongly resurrect them)."""
        if self._disabled:
            return
        try:
            if py_status == MaintenanceJobStatus.SUCCEEDED:
                native_ok = bool(self._mt.complete(job_id))
            else:
                native_ok = bool(self._mt.cancel(job_id))
            if not native_ok:
                self._emit(
                    {
                        "kind": "transition-divergence",
                        "job_id": job_id,
                        "detail": "native had no such job",
                        "python": {"status": py_status.value},
                    }
                )
        except Exception as exc:
            self._disable("native-complete-error", exc)

    # --- TTL probe cache ---

    def observe_probe_cache(
        self,
        *,
        now_s: float,
        ttl_s: float,
        py_hit: bool,
    ) -> None:
        """Compare the C TTL cache decision with the Python probe cache."""
        if self._disabled:
            return
        try:
            native_hit = self._mt.cache_get(_ms(now_s), _ms(ttl_s))
            if (native_hit is not None) != bool(py_hit):
                self._emit(
                    {
                        "kind": "cache-divergence",
                        "mismatches": ["hit"],
                        "python": {"hit": bool(py_hit)},
                        "native": {"hit": native_hit is not None},
                    }
                )
        except Exception as exc:
            self._disable("native-cache-error", exc)

    def observe_probe_store(self, *, now_s: float, probe_ok: bool) -> None:
        """Mirror a Python cache store after a fresh probe."""
        if self._disabled:
            return
        try:
            self._mt.cache_set(_ms(now_s), bool(probe_ok))
        except Exception as exc:
            self._disable("native-cache-error", exc)

    # --- fail-closed plumbing (identical contract to the other shadows) ---

    def _disable(self, reason: str, exc: Exception) -> None:
        self._disabled = True
        if not self._error_emitted:
            self._error_emitted = True
            self._emit(
                {
                    "kind": "shadow-disabled",
                    "reason": reason,
                    "detail": f"{type(exc).__name__}: {exc}"[:200],
                }
            )

    def _emit(self, record: dict[str, Any]) -> None:
        record = {
            "schema": "native-shadow-divergence/v1",
            "component": _COMPONENT,
            "at": _utc_now(),
            "monotonic_s": round(time.monotonic(), 3),
            **record,
        }
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


__all__ = ["MaintenanceNativeShadow"]
