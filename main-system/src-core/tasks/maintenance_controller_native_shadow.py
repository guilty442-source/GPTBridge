"""§10.65 dual-track act-1 shadow harness for ``maintenance_controller``.

Runs the C ``NativeMaintenance`` admission/queue model in parallel with the
Python ``MaintenanceScheduler``.  Python remains the authoritative path:
the class-ladder admission verdict (M0 admit / M1 idle-gated /
M2 authorized / M3 never-executes) and the dispatch order are compared
item-by-item; divergences are appended to
``runtime/logs/native-shadow/maintenance-controller.jsonl`` as governed
audit evidence.

Fail-closed semantics mirror the other act-1 shadows:

  * missing/invalid policy or missing native extension -> no shadow;
  * any native error -> the shadow disables itself after one
    ``shadow-disabled`` audit record; the Python path is unaffected;
  * modes other than ``"shadow"`` are refused (``"primary"``/``"retire"``
    are later acts).

Flag mapping (the caller supplies the gate inputs; ``policy_context`` is
folded here into ``system_blocked``):

  * ``system_idle`` — the verdict of a parallel ``evaluate_policy(M1)``
    probe: False whenever any global block *or* a health threshold fails.
    Consulted by the C M1 and M2 gates (M2 = authorized ∧ M1-health,
    matching ``evaluate_policy``).
  * ``system_blocked`` — the union of ``evaluate_policy``'s global early
    returns (blocking recovery state, shutdown draining, cooldown, lease
    conflict).  Refuses every class, matching Python — required for exact
    M0 parity.
  * ``authorized`` — ``governed_authorization`` for M2.
  * generation mismatch is checked inside the C layer itself.

Known intentional modeling gaps (recorded, not hidden): ``get_next_job``
pops FIFO from a queue already filled in priority order while ``next_due``
picks min(priority, scheduled_at) with expiry — ordering divergences under
equal timestamps are evidence, not noise; Python expires queued jobs by
``admitted_at`` age inside ``tick`` where the C side expires inside
``next_due``; post-policy vetoes (budget / lease / cooldown) are mirrored
via ``observe_admit_veto`` so the C table tracks the actually-enqueued
set; ``requeue_job`` (startup recovery / explicit retry) is mirrored via
``observe_requeue`` → ``gptbridge_mt_requeue`` (RUNNING→QUEUED, no
backoff — distinct from the C ``fail`` deferral semantics).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from core_system.native_shadow_resource import maybe_emit_resource

_COMPONENT = "maintenance_controller"
_POLICY_REL = Path("main-system") / "config" / "native-shadow.json"
_LOG_REL = (
    Path("main-system")
    / "runtime"
    / "logs"
    / "native-shadow"
    / "maintenance-controller.jsonl"
)

# shared_layer...models.MaintenanceRiskClass -> gptbridge_mt_class_t
_RISK_ORD = {
    "M0_OBSERVE": 0,
    "M1_SAFE_AUTO": 1,
    "M2_GOVERNED_AUTO": 2,
    "M3_APPROVAL_REQUIRED": 3,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _risk_ord(risk_class: Any) -> int:
    value = getattr(risk_class, "value", risk_class)
    return _RISK_ORD.get(str(value), -1)


def _system_blocked(policy_context: Optional[dict[str, Any]]) -> bool:
    """Fold ``evaluate_policy``'s global early returns into one flag.

    ``None`` (older call-site shape, or a stubbed observer test) degrades
    to ``False`` — the divergence record still carries the inputs.
    """
    if not policy_context:
        return False
    try:
        from shared_layer.database.maintenance.policies import (
            DEFAULT_POLICY,
            SystemRecoveryState,
        )
    except Exception:
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
    """Parallel C admission/queue observer for ``MaintenanceScheduler``."""

    def __init__(
        self,
        log_path: Path,
        *,
        tick_interval_ms: int,
        max_job_age_ms: int,
        max_retry_attempts: int,
        retry_backoff_ms: int,
        current_generation: int,
        native_cls: Any,
    ) -> None:
        self._log_path = log_path
        self._ctor = {
            "tick_interval_ms": int(tick_interval_ms),
            "max_job_age_ms": int(max_job_age_ms),
            "max_retry_attempts": int(max_retry_attempts),
            "retry_backoff_ms": int(retry_backoff_ms),
        }
        self._native_cls = native_cls
        self._generation = int(current_generation)
        self._mt = self._build(self._generation)
        self._disabled = False
        self._error_emitted = False
        self._empty_pop_emitted = False

    def _build(self, generation: int) -> Any:
        return self._native_cls(
            self._ctor["tick_interval_ms"],
            self._ctor["max_job_age_ms"],
            self._ctor["max_retry_attempts"],
            self._ctor["retry_backoff_ms"],
            int(generation),
        )

    @classmethod
    def from_policy(
        cls,
        project_root: Path,
        *,
        scheduler_config: Any = None,
        current_generation: int = 0,
    ) -> Optional["MaintenanceNativeShadow"]:
        root = Path(project_root)
        try:
            policy = json.loads(
                (root / _POLICY_REL).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None
        components = policy.get("components")
        entry = (
            (components or {}).get(_COMPONENT)
            if isinstance(components, dict)
            else None
        )
        mode = str((entry or {}).get("mode") or "off").strip().lower()
        if mode != "shadow":
            return None
        try:
            from core_system.native import _sovereign_native as native

            native_cls = native.NativeMaintenance
        except Exception:
            return None
        tick_s = float(getattr(scheduler_config, "tick_interval_seconds", 30.0))
        age_s = float(getattr(scheduler_config, "max_job_age_seconds", 3600.0))
        retries = int(getattr(scheduler_config, "max_retry_attempts", 3))
        backoff_s = float(
            getattr(scheduler_config, "retry_backoff_base_seconds", 60.0)
        )
        try:
            return cls(
                root / _LOG_REL,
                tick_interval_ms=max(1, int(tick_s * 1000)),
                max_job_age_ms=max(1, int(age_s * 1000)),
                max_retry_attempts=max(1, retries),
                retry_backoff_ms=max(1, int(backoff_s * 1000)),
                current_generation=int(current_generation),
                native_cls=native_cls,
            )
        except Exception:
            return None

    def observe_generation(self, generation: int) -> None:
        """Mirror ``set_generation``; the C model re-inits on a new gen."""
        if self._disabled:
            return
        generation = int(generation)
        if generation == self._generation:
            return
        try:
            # A generation change makes every queued native job stale; the C
            # model enforces generation-match at admit, so a fresh table is
            # the faithful mirror of the Python revalidation semantics.
            self._mt = self._build(generation)
            self._generation = generation
        except Exception as exc:
            self._disable("native-generation-error", exc)

    def observe_admit(
        self,
        job_id: str,
        action_id: str,
        *,
        risk_class: Any,
        priority: int,
        generation: int,
        system_idle: bool,
        authorized: bool,
        py_executable: bool,
        policy_context: Optional[dict[str, Any]] = None,
    ) -> None:
        """Compare the class-ladder admission verdict item-by-item."""
        if self._disabled:
            return
        try:
            blocked = _system_blocked(policy_context)
            verdict = bool(
                self._mt.admit(
                    str(job_id),
                    str(action_id),
                    _risk_ord(risk_class),
                    int(priority),
                    int(generation),
                    bool(system_idle),
                    bool(authorized),
                    _now_ms(),
                    system_blocked=blocked,
                )
            )
            if verdict != bool(py_executable):
                if verdict:
                    # Phantom admit: resync the mirror so it cannot spam
                    # dispatch-divergence on every later tick.
                    self._mt.cancel(str(job_id))
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "admit",
                        "job_id": str(job_id),
                        "action_id": str(action_id),
                        "risk_class": str(
                            getattr(risk_class, "value", risk_class)
                        ),
                        "inputs": {
                            "priority": int(priority),
                            "generation": int(generation),
                            "system_idle": bool(system_idle),
                            "authorized": bool(authorized),
                            "system_blocked": blocked,
                        },
                        "python": {"executable": bool(py_executable)},
                        "native": {"admitted": verdict},
                    }
                )
        except Exception as exc:
            self._disable("native-admit-error", exc)

    def observe_dispatch(self, py_job_id: Optional[str]) -> None:
        """Compare the next-due dispatch pick.

        When the Python queue pops nothing the native queue is left
        untouched — ``next_due`` mutates state (RUNNING, attempt_count),
        so a Python-empty dispatch is recorded only when the native model
        still holds queued work.
        """
        if self._disabled:
            return
        maybe_emit_resource(self)
        try:
            if py_job_id is None:
                # The desynced-queue divergence is recorded once per
                # episode — repeating it every tick adds no evidence.
                # live_count() counts non-terminal slots only; job_count()
                # includes terminal slots lingering until slot recycling
                # and would report phantom backlog (P4 parity fix).
                live = getattr(self._mt, "live_count", self._mt.job_count)()
                if live > 0 and not self._empty_pop_emitted:
                    self._empty_pop_emitted = True
                    self._emit(
                        {
                            "kind": "divergence",
                            "op": "dispatch",
                            "python": {"job_id": None},
                            "native": {"queued_jobs": live},
                        }
                    )
                return
            self._empty_pop_emitted = False
            native = self._mt.next_due(_now_ms())
            native_id = None if native is None else str(native.get("job_id"))
            if native_id != str(py_job_id):
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "dispatch",
                        "python": {"job_id": str(py_job_id)},
                        "native": {"job_id": native_id},
                    }
                )
                # Resync: Python dispatched a job the native pick skipped —
                # withdraw that job from the native table or it stays
                # QUEUED forever (phantom backlog, observed 2026-09-23).
                try:
                    self._mt.cancel(str(py_job_id))
                except Exception:
                    pass
        except Exception as exc:
            self._disable("native-dispatch-error", exc)

    def observe_admit_veto(self, job_id: str) -> None:
        """Python refused a policy-admitted job on a downstream gate
        (budget / lease-conflict re-eval / cooldown / lease acquire) —
        withdraw the mirrored native job so the tables stay comparable."""
        if self._disabled:
            return
        try:
            self._mt.cancel(str(job_id))
        except Exception as exc:
            self._disable("native-veto-error", exc)

    def observe_cancelled(self, job_id: str) -> None:
        """Mirror a Python job cancellation into the native queue.

        A cancelled job is terminal-withdrawn in both models; when the
        native side cannot cancel (missing or already terminal) the
        asymmetry is recorded rather than silently dropped.
        """
        if self._disabled:
            return
        try:
            if not self._mt.cancel(str(job_id)):
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "cancel",
                        "job_id": str(job_id),
                        "python": {"cancelled": True},
                        "native": {"cancelled": False},
                    }
                )
        except Exception as exc:
            self._disable("native-cancel-error", exc)

    def observe_requeue(self, job_id: str) -> None:
        """Mirror ``requeue_job`` — RUNNING→QUEUED, immediately due.

        The C table has no requeue entry point by design; ``fail`` would
        add backoff and ``cancel`` would terminate.  A native refusal
        (job missing or not RUNNING) is divergence evidence.
        """
        if self._disabled:
            return
        requeue = getattr(self._mt, "requeue", None)
        if requeue is None:
            # Stale artifact (pre-requeue .pyd): skip the mirror rather
            # than trip _disable on AttributeError — the shadow keeps
            # observing the other lifecycle edges until the rebuild lands.
            return
        try:
            if not requeue(str(job_id), _now_ms()):
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "requeue",
                        "job_id": str(job_id),
                        "python": {"requeued": True},
                        "native": {"requeued": False},
                    }
                )
        except Exception as exc:
            self._disable("native-requeue-error", exc)

    def observe_terminal(self, job_id: str, *, ok: bool) -> None:
        """Mirror job completion/failure into the native queue state.

        Failure is a terminal withdrawal: Python's ``complete_job`` does
        not requeue failed jobs, so a C ``fail`` deferral would wrongly
        resurrect them.
        """
        if self._disabled:
            return
        try:
            done = (
                self._mt.complete(str(job_id))
                if ok
                else self._mt.cancel(str(job_id))
            )
            if not done:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "terminal",
                        "job_id": str(job_id),
                        "python": {"ok": bool(ok)},
                        "native": {"applied": False},
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
            native_hit = self._mt.cache_get(
                int(round(now_s * 1000)), int(round(ttl_s * 1000))
            )
            if (native_hit is not None) != bool(py_hit):
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "probe_cache",
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
            self._mt.cache_set(int(round(now_s * 1000)), bool(probe_ok))
        except Exception as exc:
            self._disable("native-cache-error", exc)

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
