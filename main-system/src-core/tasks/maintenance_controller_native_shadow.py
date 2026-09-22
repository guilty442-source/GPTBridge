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

Known intentional modeling gaps (recorded, not hidden): the C gate composes
M2 as ``authorized`` only, while the Python policy additionally requires
the M1 health conjunction for M2; generation mismatches, queue capacity
and duplicate ids refuse natively where the Python gate has no equivalent,
and the Python budget/lease/cooldown gates sit downstream of the compared
verdict — each shapes a divergence record rather than a silent pass.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
    ) -> None:
        """Compare the class-ladder admission verdict item-by-item."""
        if self._disabled:
            return
        try:
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
                )
            )
            if verdict != bool(py_executable):
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
        try:
            if py_job_id is None:
                # The desynced-queue divergence is recorded once per
                # episode — repeating it every tick adds no evidence.
                if self._mt.job_count() > 0 and not self._empty_pop_emitted:
                    self._empty_pop_emitted = True
                    self._emit(
                        {
                            "kind": "divergence",
                            "op": "dispatch",
                            "python": {"job_id": None},
                            "native": {"queued_jobs": self._mt.job_count()},
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
        except Exception as exc:
            self._disable("native-dispatch-error", exc)

    def observe_terminal(self, job_id: str, *, ok: bool) -> None:
        """Mirror job completion/failure into the native queue state."""
        if self._disabled:
            return
        try:
            done = (
                self._mt.complete(str(job_id))
                if ok
                else self._mt.fail(str(job_id), _now_ms())
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
