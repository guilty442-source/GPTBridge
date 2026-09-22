"""§10.65 dual-track act-1 shadow harness for ``model_service_activation``.

Runs the C ``NativeActivationBroker`` decision ladder in parallel with
the Python ``ModelServiceActivationBroker``.  Python remains the
authoritative path: the same facts Python gathered (pending probe,
maintenance/shutdown/hold gates, owner liveness, regulation state,
monotonic clock) are fed to ``gptbridge_act_ensure`` and the returned
decision name is compared item-by-item with the Python decision; the
post-call results (``on_start_result``/``on_release_result``),
``note_explicit_stop`` bookkeeping, the poll interval and the state
write-due predicate are mirrored the same way.  Divergences are
appended to ``runtime/logs/native-shadow/model-service-activation.jsonl``
as governed audit evidence and never alter the Python path.

Fail-closed semantics mirror the other act-1 shadows:

  * missing/invalid policy or missing native extension -> no shadow;
  * any native error -> the shadow disables itself after one
    ``shadow-disabled`` audit record; the Python path is unaffected;
  * modes other than ``"shadow"`` are refused (``"primary"``/``"retire"``
    are later acts).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_COMPONENT = "model_service_activation"
_POLICY_REL = Path("main-system") / "config" / "native-shadow.json"
_LOG_REL = (
    Path("main-system")
    / "runtime"
    / "logs"
    / "native-shadow"
    / "model-service-activation.jsonl"
)

_STATUS_FIELDS = (
    "attempts",
    "backoff_seconds",
    "next_attempt_at",
    "next_release_at",
    "broker_started_owner",
    "explicit_stop_at",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_project_root() -> Path:
    # main-system/src-core/tasks/<this file> -> repo root
    return Path(__file__).resolve().parents[3]


class ActivationBrokerNativeShadow:
    """Parallel C activation-broker observer."""

    def __init__(self, log_path: Path, native_broker: Any) -> None:
        self._log_path = log_path
        self._broker = native_broker
        self._disabled = False
        self._error_emitted = False

    @classmethod
    def from_policy(
        cls,
        project_root: Optional[Path] = None,
        *,
        cooldown_s: float = 20.0,
        min_backoff_s: float = 15.0,
        max_backoff_s: float = 180.0,
    ) -> Optional["ActivationBrokerNativeShadow"]:
        root = Path(project_root) if project_root else _default_project_root()
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

            broker = native.NativeActivationBroker(
                float(cooldown_s), float(min_backoff_s), float(max_backoff_s)
            )
        except Exception:
            return None
        return cls(root / _LOG_REL, broker)

    # -- decision-ladder mirrors --------------------------------------------

    def observe_ensure(
        self, inputs: dict[str, Any], *, py_decision: str
    ) -> None:
        """Mirror one ``_ensure_inner``/``_maybe_release_owner`` decision.

        ``inputs`` carries exactly the facts the Python path gathered;
        gates Python never reached are fed as ``False``/``0.0`` — the C
        ladder short-circuits at the same points so unfed flags are never
        consulted.
        """
        if self._disabled:
            return
        try:
            native_decision = str(self._broker.ensure(dict(inputs)))
            if native_decision != str(py_decision):
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "ensure",
                        "python": {
                            "decision": str(py_decision),
                            "inputs": _safe_inputs(inputs),
                        },
                        "native": {"decision": native_decision},
                    }
                )
        except Exception as exc:
            self._disable("native-ensure-error", exc)

    def observe_start_result(
        self, ok: bool, now_monotonic: float, *, py_decision: str
    ) -> None:
        if self._disabled:
            return
        try:
            native_decision = str(
                self._broker.on_start_result(bool(ok), float(now_monotonic))
            )
            if native_decision != str(py_decision):
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "start_result",
                        "python": {"decision": str(py_decision), "ok": bool(ok)},
                        "native": {"decision": native_decision},
                    }
                )
        except Exception as exc:
            self._disable("native-start-result-error", exc)

    def observe_release_result(
        self, ok: bool, now_monotonic: float, *, py_decision: str
    ) -> None:
        if self._disabled:
            return
        try:
            native_decision = str(
                self._broker.on_release_result(bool(ok), float(now_monotonic))
            )
            if native_decision != str(py_decision):
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "release_result",
                        "python": {"decision": str(py_decision), "ok": bool(ok)},
                        "native": {"decision": native_decision},
                    }
                )
        except Exception as exc:
            self._disable("native-release-result-error", exc)

    def observe_explicit_stop(
        self, now_monotonic: float, wall_time: float
    ) -> None:
        if self._disabled:
            return
        try:
            self._broker.note_explicit_stop(
                float(now_monotonic), float(wall_time)
            )
        except Exception as exc:
            self._disable("native-explicit-stop-error", exc)

    # -- bookkeeping mirrors -------------------------------------------------

    def observe_status(self, py_state: dict[str, Any]) -> None:
        """Compare the C broker counters against the Python fields."""
        if self._disabled:
            return
        try:
            native = self._broker.status()
            mismatches = []
            for field in _STATUS_FIELDS:
                py_val = py_state.get(field)
                nv = native.get(field)
                if isinstance(py_val, bool) or isinstance(nv, bool):
                    same = bool(py_val) == bool(nv)
                else:
                    try:
                        same = abs(float(py_val) - float(nv)) < 1e-6
                    except (TypeError, ValueError):
                        same = py_val == nv
                if not same:
                    mismatches.append(field)
            if mismatches:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "status",
                        "mismatches": mismatches,
                        "python": {
                            f: py_state.get(f) for f in _STATUS_FIELDS
                        },
                        "native": {f: native.get(f) for f in _STATUS_FIELDS},
                    }
                )
        except Exception as exc:
            self._disable("native-status-error", exc)

    def observe_poll_interval(
        self,
        pending: bool,
        idle_s: float,
        pending_s: float,
        *,
        py_interval: float,
    ) -> None:
        if self._disabled:
            return
        try:
            native = float(
                self._broker.poll_interval(
                    bool(pending), float(idle_s), float(pending_s)
                )
            )
            if abs(native - float(py_interval)) > 1e-6:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "poll_interval",
                        "python": {"interval": float(py_interval)},
                        "native": {"interval": native},
                    }
                )
        except Exception as exc:
            self._disable("native-poll-interval-error", exc)

    def observe_state_write_due(
        self,
        fingerprint_changed: bool,
        now_monotonic: float,
        last_write_at: float,
        heartbeat_s: float,
        *,
        py_due: bool,
    ) -> None:
        if self._disabled:
            return
        try:
            native = bool(
                self._broker.state_write_due(
                    bool(fingerprint_changed),
                    float(now_monotonic),
                    float(last_write_at),
                    float(heartbeat_s),
                )
            )
            if native != bool(py_due):
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "state_write_due",
                        "python": {"due": bool(py_due)},
                        "native": {"due": native},
                    }
                )
        except Exception as exc:
            self._disable("native-write-due-error", exc)

    # -- internals ----------------------------------------------------------

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


def _safe_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in inputs.items():
        try:
            json.dumps(value)
            out[key] = value
        except (TypeError, ValueError):
            out[key] = str(value)
    return out


__all__ = ["ActivationBrokerNativeShadow"]
