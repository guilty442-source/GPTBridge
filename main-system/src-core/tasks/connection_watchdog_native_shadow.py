"""§10.65 dual-track act-1 shadow harness for ``connection_watchdog``.

Runs the C ``NativeWatchdog`` state machine in parallel with the Python
``ConnectionWatchdog``.  Python remains the authoritative path: every
probe cycle the native outputs are compared item-by-item and divergences
are appended to ``runtime/logs/native-shadow/connection-watchdog.jsonl``
as governed audit evidence.

Fail-closed semantics:

  * missing/invalid policy file or missing native extension -> the
    shadow is simply absent (``from_policy`` returns ``None``);
  * any native error during observation -> the shadow disables itself,
    emits a single ``shadow-disabled`` audit record, and the Python path
    continues untouched;
  * ``mode`` values other than ``"shadow"`` (including ``"primary"``,
    a later act) are refused -> Python-only, same as ``"off"``.

The C layer performs no I/O and holds no authority; probe results are
injected by the caller (A177), matching the Python probes' inputs
exactly so the comparison is apples-to-apples.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from core_system.native_shadow_resource import maybe_emit_resource

_COMPONENT = "connection_watchdog"
_POLICY_REL = Path("main-system") / "config" / "native-shadow.json"
_LOG_REL = (
    Path("main-system")
    / "runtime"
    / "logs"
    / "native-shadow"
    / "connection-watchdog.jsonl"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_primary(
    project_root: Path,
    *,
    min_interval_ms: int,
    max_interval_ms: int,
    dead_threshold: int,
    retry_grace: int,
) -> Any:
    """Return the authoritative ``NativeWatchdog`` when policy mode is
    ``primary``; ``None`` otherwise or when the native extension is
    unavailable (caller falls back to the Python path and emits a
    ``primary-unavailable`` audit record so the substitution failure is
    observable, never silent)."""
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
    if mode != "primary":
        return None
    try:
        from core_system.native import _sovereign_native as native

        return native.NativeWatchdog(
            min_interval_ms, max_interval_ms, dead_threshold, retry_grace
        )
    except Exception as exc:
        _emit_primary_unavailable(root, exc)
        return None


def _emit_primary_unavailable(root: Path, exc: Exception) -> None:
    record = {
        "schema": "native-shadow-divergence/v1",
        "component": _COMPONENT,
        "at": _utc_now(),
        "monotonic_s": round(time.monotonic(), 3),
        "kind": "primary-unavailable",
        "detail": f"{type(exc).__name__}: {exc}"[:200],
    }
    log_path = root / _LOG_REL
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


class WatchdogNativeShadow:
    """Parallel C-state-machine observer for ``ConnectionWatchdog``."""

    def __init__(self, log_path: Path, native_wd: Any) -> None:
        self._log_path = log_path
        self._wd = native_wd
        self._disabled = False
        self._error_emitted = False

    @classmethod
    def from_policy(
        cls,
        project_root: Path,
        *,
        min_interval_ms: int,
        max_interval_ms: int,
        dead_threshold: int,
        retry_grace: int,
    ) -> Optional["WatchdogNativeShadow"]:
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

            wd = native.NativeWatchdog(
                min_interval_ms, max_interval_ms, dead_threshold, retry_grace
            )
        except Exception:
            return None
        return cls(root / _LOG_REL, wd)

    def observe_probe(
        self,
        *,
        backend_process_alive: bool,
        backend_http_healthy: bool,
        frontend_connected: bool,
        py_state: str,
        py_consecutive_dead: int,
        py_repair_trigger: bool,
        now_ms: int,
    ) -> None:
        """Step the C FSM with identical inputs and compare the outcome."""
        if self._disabled:
            return
        maybe_emit_resource(self)
        try:
            event = self._wd.probe(
                backend_process_alive,
                backend_http_healthy,
                frontend_connected,
                now_ms,
            )
            native = {
                "state": self._wd.state(),
                "consecutive_dead": self._wd.consecutive_dead(),
                "repair_trigger": bool(event and event.get("repair_fired")),
            }
            python = {
                "state": py_state,
                "consecutive_dead": py_consecutive_dead,
                "repair_trigger": bool(py_repair_trigger),
            }
            mismatches = [
                key for key in native if native[key] != python[key]
            ]
            if mismatches:
                self._emit(
                    {
                        "kind": "divergence",
                        "mismatches": mismatches,
                        "inputs": {
                            "backend_process_alive": backend_process_alive,
                            "backend_http_healthy": backend_http_healthy,
                            "frontend_connected": frontend_connected,
                        },
                        "python": python,
                        "native": native,
                    }
                )
        except Exception as exc:  # fail-closed: native never affects Python
            self._disable("native-probe-error", exc)

    def observe_interval(self, py_interval_ms: int) -> None:
        """Compare the C adaptive interval with the Python one."""
        if self._disabled:
            return
        try:
            native_ms = int(self._wd.next_interval_ms())
            if native_ms != int(py_interval_ms):
                self._emit(
                    {
                        "kind": "interval-divergence",
                        "python": {"interval_ms": int(py_interval_ms)},
                        "native": {"interval_ms": native_ms},
                    }
                )
        except Exception as exc:
            self._disable("native-interval-error", exc)

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


__all__ = ["WatchdogNativeShadow"]
