"""§10.65 dual-track act-1 shadow harness for ``runtime_state_registry``.

Runs the C ``NativeRuntimeStateRegistry`` module-state table in parallel
with the Python ``RuntimeStateRegistry``.  Python remains the
authoritative path: every mutation is mirrored into the C model and the
resulting per-module record is compared field-by-field (runtime_state /
capability_state / health / release_id / last_error / recovery_attempts /
updated_at); ``aggregate`` compares the full count/failed views.
Divergences are appended to
``runtime/logs/native-shadow/runtime-state.jsonl`` as governed audit
evidence.

Fail-closed semantics mirror the other act-1 shadows:

  * missing/invalid policy or missing native extension -> no shadow;
  * any native error -> the shadow disables itself after one
    ``shadow-disabled`` audit record; the Python path is unaffected;
  * modes other than ``"shadow"`` are refused (``"primary"``/``"retire"``
    are later acts).

Known intentional modeling gaps (recorded, not hidden): the Python
``ModuleRuntimeRecord.metadata`` dict and ``last_heartbeat`` ordering vs
``updated_at`` have no C counterpart; heartbeat parity is limited to
record presence since both sides stamp their own clocks.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_COMPONENT = "runtime_state_registry"
_POLICY_REL = Path("main-system") / "config" / "native-shadow.json"
_LOG_REL = (
    Path("main-system")
    / "runtime"
    / "logs"
    / "native-shadow"
    / "runtime-state.jsonl"
)

_COMPARE_FIELDS = (
    "runtime_state",
    "capability_state",
    "health",
    "release_id",
    "last_error",
    "recovery_attempts",
    "updated_at",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_project_root() -> Path:
    # main-system/src-core/core_system/<this file> -> repo root
    return Path(__file__).resolve().parents[3]


class RuntimeStateNativeShadow:
    """Parallel C registry observer for ``RuntimeStateRegistry``."""

    def __init__(self, log_path: Path, native_reg: Any) -> None:
        self._log_path = log_path
        self._reg = native_reg
        self._disabled = False
        self._error_emitted = False

    @classmethod
    def from_policy(
        cls, project_root: Optional[Path] = None
    ) -> Optional["RuntimeStateNativeShadow"]:
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

            reg = native.NativeRuntimeStateRegistry()
        except Exception:
            return None
        return cls(root / _LOG_REL, reg)

    # -- mutation mirrors --------------------------------------------------

    def observe_set_runtime(
        self,
        module_id: str,
        state: str,
        *,
        health: Optional[str],
        release_id: Optional[str],
        error: Optional[str],
        now_str: str,
        py_record: Any,
    ) -> None:
        if self._disabled:
            return
        try:
            ok = bool(
                self._reg.set_runtime_state(
                    str(module_id),
                    str(state),
                    health,
                    release_id,
                    error,
                    str(now_str),
                )
            )
            if not ok:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "set_runtime_state",
                        "module_id": str(module_id),
                        "python": {"accepted": True},
                        "native": {"accepted": False},
                    }
                )
                return
            self._compare_record(module_id, py_record, "set_runtime_state")
        except Exception as exc:
            self._disable("native-runtime-error", exc)

    def observe_set_capability(
        self, module_id: str, state: str, *, now_str: str, py_record: Any
    ) -> None:
        if self._disabled:
            return
        try:
            ok = bool(
                self._reg.set_capability_state(
                    str(module_id), str(state), str(now_str)
                )
            )
            if not ok:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "set_capability_state",
                        "module_id": str(module_id),
                        "python": {"accepted": True},
                        "native": {"accepted": False},
                    }
                )
                return
            self._compare_record(module_id, py_record, "set_capability_state")
        except Exception as exc:
            self._disable("native-capability-error", exc)

    def observe_heartbeat(self, module_id: str, *, now_str: str) -> None:
        """Mirror a heartbeat; only record presence is compared."""
        if self._disabled:
            return
        try:
            ok = bool(self._reg.heartbeat(str(module_id), str(now_str)))
            if not ok:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "heartbeat",
                        "module_id": str(module_id),
                        "python": {"accepted": True},
                        "native": {"accepted": False},
                    }
                )
        except Exception as exc:
            self._disable("native-heartbeat-error", exc)

    def observe_record_error(
        self, module_id: str, error: str, *, now_str: str, py_record: Any
    ) -> None:
        if self._disabled:
            return
        try:
            ok = bool(
                self._reg.record_error(
                    str(module_id), str(error), str(now_str)
                )
            )
            if not ok:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "record_error",
                        "module_id": str(module_id),
                        "python": {"accepted": True},
                        "native": {"accepted": False},
                    }
                )
                return
            self._compare_record(module_id, py_record, "record_error")
        except Exception as exc:
            self._disable("native-error-record-error", exc)

    # -- aggregate parity ---------------------------------------------------

    def observe_aggregate(self, py_aggregate: dict[str, Any]) -> None:
        if self._disabled:
            return
        try:
            native = self._reg.aggregate()
            python = {
                "module_count": int(py_aggregate.get("module_count", -1)),
                "by_runtime_state": dict(
                    py_aggregate.get("by_runtime_state") or {}
                ),
                "by_capability_state": dict(
                    py_aggregate.get("by_capability_state") or {}
                ),
                "failed_modules": sorted(
                    py_aggregate.get("failed_modules") or []
                ),
            }
            native_cmp = {
                "module_count": int(native.get("module_count", -1)),
                "by_runtime_state": dict(native.get("by_runtime_state") or {}),
                "by_capability_state": dict(
                    native.get("by_capability_state") or {}
                ),
                "failed_modules": sorted(native.get("failed_modules") or []),
            }
            mismatches = [
                key for key in python if python[key] != native_cmp[key]
            ]
            if mismatches:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "aggregate",
                        "mismatches": mismatches,
                        "python": python,
                        "native": native_cmp,
                    }
                )
        except Exception as exc:
            self._disable("native-aggregate-error", exc)

    # -- internals ----------------------------------------------------------

    def _compare_record(
        self, module_id: str, py_record: Any, op: str
    ) -> None:
        native = self._reg.get(str(module_id))
        if native is None:
            self._emit(
                {
                    "kind": "divergence",
                    "op": op,
                    "module_id": str(module_id),
                    "python": {"present": True},
                    "native": {"present": False},
                }
            )
            return
        python = {
            f: getattr(py_record, f, None) for f in _COMPARE_FIELDS
        }
        native_cmp = {f: native.get(f) for f in _COMPARE_FIELDS}
        mismatches = [f for f in _COMPARE_FIELDS if python[f] != native_cmp[f]]
        if mismatches:
            self._emit(
                {
                    "kind": "divergence",
                    "op": op,
                    "module_id": str(module_id),
                    "mismatches": mismatches,
                    "python": python,
                    "native": native_cmp,
                }
            )

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


__all__ = ["RuntimeStateNativeShadow"]
