"""§10.65 dual-track act-1 shadow harness for ``maintenance_controller``.

Runs the C ``NativeMaintenance`` job table in parallel with the Python
``MaintenanceScheduler``.  Python remains the authoritative path: admission
gates, dispatch selection, terminal transitions and the TTL probe cache are
compared item-by-item and divergences are appended to
``runtime/logs/native-shadow/maintenance-controller.jsonl`` as governed
audit evidence.

Fail-closed semantics mirror ``connection_watchdog_native_shadow``:

  * missing/invalid policy or missing native extension -> no shadow;
  * any native error -> the shadow disables itself after one
    ``shadow-disabled`` audit record; the Python path is unaffected;
  * modes other than ``"shadow"`` are refused (``"primary"``/``"retire"``
    are later acts).

Flag mapping (the caller supplies the two C gate inputs):

  * ``system_idle`` — the verdict of a parallel ``evaluate_policy(M1)``
    probe: False whenever any global block (recovery / drain / cooldown /
    lease-conflict / generation mismatch) *or* a health threshold fails.
    Consulted by the C M1 and M2 gates.
  * ``system_blocked`` — folded from ``policy_context`` here: the union of
    ``evaluate_policy``'s global early returns (blocking recovery state,
    shutdown draining, cooldown, lease conflict).  Refuses every class,
    matching Python — required for exact M0 parity.
  * ``authorized`` — ``governed_authorization`` for M2.
  * generation mismatch is checked inside the C layer itself.

Known intentional modeling gaps (recorded, not hidden):

  * ``get_next_job`` pops FIFO from a queue already filled in priority
    order; ``next_due`` picks min(priority, scheduled_at) with expiry —
    ordering divergences under equal timestamps are evidence, not noise.
  * Python expires queued jobs by ``admitted_at`` age inside ``tick``;
    the C side expires them inside ``next_due``.  Same bound, different
    trigger point.
  * Post-policy vetoes (budget / lease-conflict re-eval / cooldown /
    lease-acquire failure) are mirrored via ``observe_admit_veto`` so the
    C table tracks the actually-enqueued set, not just policy admits.
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

from .models import MaintenanceRiskClass
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


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _system_blocked(policy_context: Optional[dict[str, Any]]) -> bool:
    """Fold ``evaluate_policy``'s global early returns into one flag.

    ``None`` (older call-site shape, or a stubbed observer test) degrades
    to ``False`` — the divergence record still carries the inputs.
    """
    if not policy_context:
        return False
    recovery = policy_context.get("recovery_state", SystemRecoveryState.NORMAL)
    if isinstance(recovery, str):
        try:
            recovery = SystemRecoveryState(recovery)
        except ValueError:
            recovery = SystemRecoveryState.NORMAL
    return (
        recovery in DEFAULT_POLICY.recovery_states_blocking
        or bool(policy_context.get("shutdown_draining", False))
        or bool(policy_context.get("maintenance_cooldown_active", False))
        or bool(policy_context.get("active_lease_conflict", False))
    )


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
        job_id: str,
        action_id: str,
        *,
        risk_class: MaintenanceRiskClass,
        priority: int,
        generation: int,
        system_idle: bool,
        authorized: bool,
        py_executable: bool,
        policy_context: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Compare the C admission gate with Python's executable verdict.

        ``py_executable`` is ``decision.allowed`` minus the M3
        candidate-only case — both sides treat M3 as never-executing.
        Returns the native verdict; a phantom admit is cancelled so it
        cannot spam dispatch comparisons on later ticks.
        """
        if self._disabled:
            return False
        try:
            blocked = _system_blocked(policy_context)
            native_allowed = bool(
                self._mt.admit(
                    job_id,
                    action_id,
                    _RISK_TO_C.get(risk_class, -1),
                    int(priority),
                    int(generation),
                    bool(system_idle),
                    bool(authorized),
                    _now_ms(),
                    system_blocked=blocked,
                )
            )
            if native_allowed != bool(py_executable):
                self._emit(
                    {
                        "kind": "admission-divergence",
                        "job_id": job_id,
                        "action_id": action_id,
                        "mismatches": ["allowed"],
                        "inputs": {
                            "risk_class": risk_class.value,
                            "system_idle": bool(system_idle),
                            "authorized": bool(authorized),
                            "system_blocked": blocked,
                            "generation": int(generation),
                        },
                        "python": {"executable": bool(py_executable)},
                        "native": {"allowed": native_allowed},
                    }
                )
                if native_allowed:
                    # Resync the mirror: the phantom job would otherwise
                    # spam dispatch-divergence on every later tick.
                    self._mt.cancel(job_id)
            return native_allowed
        except Exception as exc:  # fail-closed: native never affects Python
            self._disable("native-admit-error", exc)
            return False

    def observe_admit_veto(self, job_id: str) -> None:
        """Python refused a policy-admitted job on a downstream gate
        (budget / lease-conflict re-eval / cooldown / lease acquire) —
        withdraw the mirrored native job so the queues stay comparable."""
        if self._disabled:
            return
        try:
            self._mt.cancel(job_id)
        except Exception as exc:
            self._disable("native-veto-error", exc)

    # --- dispatch / terminal transitions ---

    def observe_dispatch(self, py_job_id: Optional[str]) -> None:
        """Compare the C priority/expiry pick with Python's FIFO pop
        (``None`` when the Python queue was empty)."""
        if self._disabled:
            return
        try:
            picked = self._mt.next_due(_now_ms())
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
            self._disable("native-dispatch-error", exc)

    def observe_terminal(self, job_id: str, *, ok: bool) -> None:
        """Mirror ``complete_job``: ``ok`` → COMPLETED, failure →
        terminal withdrawal (Python does not requeue failed jobs, so a
        C ``fail`` deferral would wrongly resurrect them)."""
        if self._disabled:
            return
        try:
            native_ok = bool(
                self._mt.complete(job_id) if ok else self._mt.cancel(job_id)
            )
            if not native_ok:
                self._emit(
                    {
                        "kind": "transition-divergence",
                        "job_id": job_id,
                        "detail": "native had no such job",
                        "python": {"succeeded": bool(ok)},
                    }
                )
        except Exception as exc:
            self._disable("native-terminal-error", exc)

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
