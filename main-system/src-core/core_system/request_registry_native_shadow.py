"""§10.65 dual-track act-1 shadow harness for ``request_registry``.

Runs the C ``NativeIpcRegistry`` request table in parallel with the Python
``RequestRegistry``.  Python remains the authoritative path: creation,
status transitions and cancellation are mirrored into the C model and the
verdicts are compared item-by-item; divergences are appended to
``runtime/logs/native-shadow/request-registry.jsonl`` as governed audit
evidence.

Fail-closed semantics mirror the other act-1 shadows:

  * missing/invalid policy or missing native extension -> no shadow;
  * any native error -> the shadow disables itself after one
    ``shadow-disabled`` audit record; the Python path is unaffected;
  * modes other than ``"shadow"`` are refused (``"primary"``/``"retire"``
    are later acts).

Known intentional modeling gaps (recorded, not hidden):

  * the C transition rule locks only the four terminal states
    (COMPLETED/FAILED/CANCELLED/TIMED_OUT); Python additionally enforces
    the §10.16 transition graph — a Python refusal that C would accept is
    recorded as an ``admission-gap`` divergence;
  * ``upsert`` rewrites ``status`` unconditionally on existing records
    (legacy merge path) where C refuses terminal overwrite;
  * ``cancellation_state="requested"`` has no C counterpart (the C
    ``cancelled`` flag is only set by terminal ``cancel``) — only the
    terminal cancellation transition is mirrored;
  * ``backend_generation`` is a free-form string on the Python side and
    ``int32`` in C — non-numeric values mirror as ``0``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from .native_shadow_resource import maybe_emit_resource

_COMPONENT = "request_registry"
_POLICY_REL = Path("main-system") / "config" / "native-shadow.json"
_LOG_REL = (
    Path("main-system")
    / "runtime"
    / "logs"
    / "native-shadow"
    / "request-registry.jsonl"
)

# §10.16 status -> gptbridge_req_status_t (binding REQ_STATES order).
_STATUS_ORD = {
    "CREATED": 0,
    "QUEUED": 1,
    "RUNNING": 2,
    "COMPLETED": 3,
    "FAILED": 4,
    "CANCELLED": 5,
    "TIMED_OUT": 6,
    "INTERRUPTED": 7,
}
_TERMINAL_ORD = {3, 4, 5, 6}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000.0)


def _gen_int(backend_generation: Any) -> int:
    try:
        return int(str(backend_generation or "0"))
    except (TypeError, ValueError):
        return 0


def _default_project_root() -> Path:
    # main-system/src-core/core_system/<this file> -> repo root
    return Path(__file__).resolve().parents[3]


class RequestRegistryNativeShadow:
    """Parallel C request-table observer for ``RequestRegistry``."""

    def __init__(self, log_path: Path, native_registry: Any) -> None:
        self._log_path = log_path
        self._reg = native_registry
        self._disabled = False
        self._error_emitted = False

    @classmethod
    def from_policy(
        cls, project_root: Optional[Path] = None
    ) -> Optional["RequestRegistryNativeShadow"]:
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

            reg = native.NativeIpcRegistry()
        except Exception:
            return None
        return cls(root / _LOG_REL, reg)

    def observe_create(self, request_id: str, backend_generation: Any) -> None:
        """Mirror a newly created record; native dup refusal is evidence."""
        if self._disabled:
            return
        maybe_emit_resource(self)
        try:
            ok = bool(
                self._reg.create(
                    str(request_id),
                    _gen_int(backend_generation),
                    _now_ms(),
                )
            )
            if not ok:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "create",
                        "request_id": str(request_id),
                        "python": {"created": True},
                        "native": {"created": False},
                    }
                )
        except Exception as exc:
            self._disable("native-create-error", exc)

    def observe_timeout(self, request_id: str, timeout_s: Any) -> None:
        """Mirror a timeout update; the C deadline becomes comparable."""
        if self._disabled:
            return
        maybe_emit_resource(self)
        try:
            timeout_ms = int(float(timeout_s or 0.0) * 1000.0)
            ok = bool(self._reg.set_timeout(str(request_id), timeout_ms))
            found = self._reg.find(str(request_id))
            native_deadline = int((found or {}).get("deadline_ms") or 0)
            if (not ok) or (timeout_ms > 0 and native_deadline <= 0):
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "set_timeout",
                        "request_id": str(request_id),
                        "python": {"timeout_s": float(timeout_s or 0.0)},
                        "native": {
                            "accepted": ok,
                            "timeout_ms": timeout_ms,
                            "deadline_ms": native_deadline,
                        },
                    }
                )
        except Exception as exc:
            self._disable("native-timeout-error", exc)

    def observe_status(
        self, request_id: str, status: str, *, py_ok: bool
    ) -> None:
        """Mirror an accepted transition; CANCELLED routes to ``cancel``."""
        if self._disabled or not py_ok:
            return
        try:
            if status == "CANCELLED":
                ok = bool(self._reg.cancel(str(request_id), _now_ms()))
            else:
                ok = bool(
                    self._reg.set_status(
                        str(request_id),
                        _STATUS_ORD.get(str(status), -1),
                        _now_ms(),
                    )
                )
            if not ok:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "set_status",
                        "request_id": str(request_id),
                        "python": {"status": str(status), "accepted": True},
                        "native": {"accepted": False},
                    }
                )
            else:
                # Timestamp dimension (P4 gap): the C record must carry a
                # populated created_at_ms and monotonic started/completed
                # stamps — a zero or inverted ordering is a divergence.
                found = self._reg.find(str(request_id)) or {}
                created = int(found.get("created_at_ms") or 0)
                started = int(found.get("started_at_ms") or 0)
                completed = int(found.get("completed_at_ms") or 0)
                ordering_ok = (
                    created > 0
                    and (not started or started >= created)
                    and (not completed or completed >= created)
                )
                if not ordering_ok:
                    self._emit(
                        {
                            "kind": "divergence",
                            "op": "timestamps",
                            "request_id": str(request_id),
                            "python": {"status": str(status)},
                            "native": {
                                "created_at_ms": created,
                                "started_at_ms": started,
                                "completed_at_ms": completed,
                            },
                        }
                    )
        except Exception as exc:
            self._disable("native-status-error", exc)

    def observe_refused(
        self, request_id: str, status: str, *, reason: str
    ) -> None:
        """Compare a Python refusal against the C lock rule.

        ``set_status`` mutates on success, so the would-be verdict is
        derived from ``find`` + the C rule (terminal states lock to
        themselves) instead of probing the real call.
        """
        if self._disabled:
            return
        maybe_emit_resource(self)
        try:
            found = self._reg.find(str(request_id))
            if found is None:
                # Native table lacks the row entirely — a membership gap
                # upstream already explains the refusal asymmetry.
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "refused",
                        "request_id": str(request_id),
                        "python": {"accepted": False, "reason": str(reason)},
                        "native": {"present": False},
                    }
                )
                return
            native_status = str(found.get("status"))
            native_ord = _STATUS_ORD.get(native_status, -1)
            target_ord = _STATUS_ORD.get(str(status), -1)
            native_would_allow = not (
                native_ord in _TERMINAL_ORD and target_ord != native_ord
            )
            if native_would_allow:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "admission-gap",
                        "request_id": str(request_id),
                        "python": {"accepted": False, "reason": str(reason)},
                        "native": {
                            "would_accept": True,
                            "current_status": native_status,
                            "target_status": str(status),
                        },
                    }
                )
        except Exception as exc:
            self._disable("native-refused-error", exc)

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


__all__ = ["RequestRegistryNativeShadow"]
