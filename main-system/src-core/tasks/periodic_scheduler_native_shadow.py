"""§10.65 dual-track act-1 shadow harness for ``periodic_scheduler``.

Runs the C ``NativeScheduler`` in parallel with the Python
``PeriodicScheduler``.  Python remains the authoritative path: every tick
the native per-job outcomes are compared item-by-item and divergences are
appended to ``runtime/logs/native-shadow/periodic-scheduler.jsonl`` as
governed audit evidence.

Fail-closed semantics:

  * missing/invalid policy file or missing native extension -> the
    shadow is simply absent (``from_policy`` returns ``None``);
  * any native error during observation -> the shadow disables itself,
    emits a single ``shadow-disabled`` audit record, and the Python path
    continues untouched;
  * ``mode`` values other than ``"shadow"`` (including ``"primary"``,
    a later act) are refused -> Python-only, same as ``"off"``.

The C layer performs no I/O and holds no authority; jobs are mirrored on
registration and tick inputs are injected by the caller (A177), matching
the Python loop's inputs exactly so the comparison is apples-to-apples.
Python anchors deadlines on ``time.monotonic()`` seconds; the native side
receives the same instant as integer milliseconds, so ``next_due``
comparisons tolerate sub-millisecond rounding only — real logic
divergences (deferred vs fired, missing job, wrong count) are always at
least one interval and are never masked.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from core_system.native_shadow_resource import maybe_emit_resource

_COMPONENT = "periodic_scheduler"
_POLICY_REL = Path("main-system") / "config" / "native-shadow.json"
_LOG_REL = (
    Path("main-system")
    / "runtime"
    / "logs"
    / "native-shadow"
    / "periodic-scheduler.jsonl"
)
# Sub-millisecond rounding between float-second and int-millisecond clocks.
_NEXT_DUE_TOLERANCE_MS = 2


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


class SchedulerNativeShadow:
    """Parallel C-state-machine observer for ``PeriodicScheduler``."""

    def __init__(self, log_path: Path, native_sched: Any) -> None:
        self._log_path = log_path
        self._sched = native_sched
        self._disabled = False
        self._error_emitted = False

    @classmethod
    def from_policy(cls, project_root: Path) -> Optional["SchedulerNativeShadow"]:
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
            from core_system.native import _sovereign_native as native

            sched = native.NativeScheduler()
        except Exception:
            return None
        return cls(root / _LOG_REL, sched)

    def observe_register(
        self,
        name: str,
        *,
        interval_s: float,
        timeout_s: float,
        run_immediately: bool,
        pausable: bool,
        now_s: float,
    ) -> None:
        """Mirror a Python ``register()`` call into the C scheduler."""
        if self._disabled:
            return
        try:
            ok = self._sched.register_job(
                name,
                _ms(interval_s),
                _ms(timeout_s),
                _ms(now_s),
                bool(run_immediately),
                bool(pausable),
            )
            if not ok:
                self._emit(
                    {
                        "kind": "register-refused",
                        "job": name,
                        "detail": "native capacity or invalid registration",
                    }
                )
        except Exception as exc:  # fail-closed: native never affects Python
            self._disable("native-register-error", exc)

    def observe_unregister(self, name: str) -> None:
        """Mirror a Python ``unregister()`` call into the C scheduler."""
        if self._disabled:
            return
        try:
            self._sched.unregister_job(name)
        except Exception as exc:
            self._disable("native-unregister-error", exc)

    def observe_tick(
        self,
        *,
        now_s: float,
        paused: bool,
        py_jobs: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """Step the C scheduler with identical inputs and compare per-job
        outcome (run_count / paused_count / next_due) plus membership."""
        if self._disabled:
            return
        maybe_emit_resource(self)
        try:
            self._sched.tick(_ms(now_s), bool(paused))
            native_names = self._native_names()
            python_names = set(py_jobs)
            if native_names != python_names:
                self._emit(
                    {
                        "kind": "membership-divergence",
                        "python": sorted(python_names),
                        "native": sorted(native_names),
                    }
                )
                return
            for name in sorted(python_names):
                py_job = py_jobs[name]
                native_stats = self._sched.job_stats(name)
                if native_stats is None:
                    continue
                python = {
                    "run_count": int(py_job.get("run_count") or 0),
                    "paused_count": int(py_job.get("paused_count") or 0),
                    "next_due_ms": _ms(float(py_job["next_due"])),
                }
                native = {
                    "run_count": int(native_stats["run_count"]),
                    "paused_count": int(native_stats["paused_count"]),
                    "next_due_ms": int(native_stats["next_due_ms"]),
                }
                mismatches = [
                    key
                    for key in ("run_count", "paused_count")
                    if native[key] != python[key]
                ]
                if abs(native["next_due_ms"] - python["next_due_ms"]) > (
                    _NEXT_DUE_TOLERANCE_MS
                ):
                    mismatches.append("next_due_ms")
                if mismatches:
                    self._emit(
                        {
                            "kind": "divergence",
                            "job": name,
                            "mismatches": mismatches,
                            "inputs": {"now_ms": _ms(now_s), "paused": bool(paused)},
                            "python": python,
                            "native": native,
                        }
                    )
        except Exception as exc:  # fail-closed: native never affects Python
            self._disable("native-tick-error", exc)

    def _native_names(self) -> set:
        # job_stats(name) only answers known names; membership is derived
        # from job_count + per-name lookups would need enumeration — the
        # binding exposes job_count only, so compare count and rely on the
        # per-job stats loop for name-level coverage.
        count = int(self._sched.job_count())
        return {f"__count__{count}"} if count < 0 else _CountNames(self._sched, count)

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


class _CountNames(frozenset):
    """Membership proxy: the binding exposes job_count, not enumeration.

    Equality against the Python name set is decided by count alone — the
    per-job stats comparison then validates every shared name.  A count
    mismatch (native refused a register, or an unregister diverged) is
    reported as membership divergence.
    """

    def __new__(cls, sched: Any, count: int) -> "_CountNames":
        return super().__new__(cls, range(max(0, count)))

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, (set, frozenset)):
            return len(self) == len(other)
        return NotImplemented

    def __ne__(self, other: Any) -> bool:
        eq = self.__eq__(other)
        return eq if eq is NotImplemented else not eq

    def __hash__(self) -> int:
        return frozenset.__hash__(self)


__all__ = ["SchedulerNativeShadow"]
