"""§10.65 dual-track act-1 shadow harness for ``state_outbox``.

Runs the C ``NativeOutbox`` session registry in parallel with the Python
``OutboxPublisher``.  Python remains the authoritative path: hello/ack/
resync/drain-plan decisions are compared item-by-item and divergences are
appended to ``runtime/logs/native-shadow/state-outbox.jsonl`` as governed
audit evidence.

Fail-closed semantics mirror ``connection_watchdog_native_shadow``:

  * missing/invalid policy or missing native extension -> no shadow;
  * any native error -> the shadow disables itself after one
    ``shadow-disabled`` audit record; the Python path is unaffected;
  * modes other than ``"shadow"`` are refused (``"primary"``/``"retire"``
    are later acts).

Known intentional modeling gaps (recorded, not hidden): the C prototype
clamps ``start_after`` to ``acked`` when the cursor raced ahead of
``sent_upto``, and refreshes ``last_attempt`` only on ``mark_sent``
where Python refreshes it every drain pass — retry-timing divergences
of that shape are the evidence this act exists to collect.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from core_system.native_shadow_resource import maybe_emit_resource

_COMPONENT = "state_outbox"
_POLICY_REL = Path("main-system") / "config" / "native-shadow.json"
_LOG_REL = (
    Path("main-system")
    / "runtime"
    / "logs"
    / "native-shadow"
    / "state-outbox.jsonl"
)
_RETRY_MS = 2000  # tasks.state_outbox_store.RETRY_INTERVAL_SECONDS


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def load_primary(project_root: Optional[Path] = None) -> Any:
    """Return the authoritative ``NativeOutbox`` when policy mode is
    ``primary``; ``None`` otherwise."""
    from core_system.native_shadow_resource import load_native_primary

    def _make() -> Any:
        from core_system.native import _sovereign_native as native

        return native.NativeOutbox()

    return load_native_primary(
        _COMPONENT, project_root, _make, log_rel=_LOG_REL
    )


class OutboxNativeShadow:
    """Parallel C-registry observer for ``OutboxPublisher``."""

    def __init__(self, log_path: Path, native_ob: Any) -> None:
        self._log_path = log_path
        self._ob = native_ob
        self._disabled = False
        self._error_emitted = False
        self._known_sessions: set[str] = set()

    @classmethod
    def from_policy(cls, project_root: Path) -> Optional["OutboxNativeShadow"]:
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

            ob = native.NativeOutbox()
        except Exception:
            return None
        return cls(root / _LOG_REL, ob)

    def observe_register(self, session_id: str) -> None:
        if self._disabled or session_id in self._known_sessions:
            return
        try:
            self._ob.register_session(session_id)
            self._known_sessions.add(session_id)
        except Exception as exc:
            self._disable("native-register-error", exc)

    def observe_unregister(self, session_id: str) -> None:
        if self._disabled or session_id not in self._known_sessions:
            return
        try:
            self._ob.unregister_session(session_id)
            self._known_sessions.discard(session_id)
        except Exception as exc:
            self._disable("native-unregister-error", exc)

    def _ensure(self, session_id: str) -> bool:
        """Mirror the publisher's implicit session creation."""
        if self._disabled:
            return False
        if session_id not in self._known_sessions:
            self.observe_register(session_id)
        return not self._disabled

    def observe_hello(
        self,
        session_id: str,
        *,
        cursor: int,
        generation_matches: bool,
        latest_sequence: int,
        py_reset: bool,
        py_cursor: int,
    ) -> None:
        if not self._ensure(session_id):
            return
        try:
            result = self._ob.hello(
                session_id, cursor, generation_matches, latest_sequence
            )
            self._compare(
                "hello",
                session_id,
                python={"reset": bool(py_reset), "effective_cursor": py_cursor},
                native={
                    "reset": bool(result.get("reset")),
                    "effective_cursor": int(result.get("effective_cursor", -1)),
                },
            )
        except Exception as exc:
            self._disable("native-hello-error", exc)

    def observe_ack(
        self, session_id: str, *, cursor: int, py_accepted: bool
    ) -> None:
        if not self._ensure(session_id):
            return
        try:
            accepted = bool(self._ob.ack(session_id, cursor))
            if accepted != bool(py_accepted):
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "ack",
                        "session_id": session_id,
                        "python": {"accepted": bool(py_accepted)},
                        "native": {"accepted": accepted},
                    }
                )
        except Exception as exc:
            self._disable("native-ack-error", exc)

    def observe_resync(self, session_id: str, *, cursor: int) -> None:
        if not self._ensure(session_id):
            return
        try:
            self._ob.resync(session_id, cursor)
        except Exception as exc:
            self._disable("native-resync-error", exc)

    def observe_drain_plan(
        self,
        session_id: str,
        *,
        py_window_open: bool,
        py_start_after: int,
        py_limit: int,
    ) -> None:
        if not self._ensure(session_id):
            return
        try:
            plan = self._ob.drain_plan(session_id, _now_ms(), _RETRY_MS)
            native = {
                "window_open": bool(plan.get("has_work")),
                "start_after": int(plan.get("start_after", -1)),
                "limit": int(plan.get("limit", -1)),
            }
            python = {
                "window_open": bool(py_window_open),
                "start_after": py_start_after,
                "limit": py_limit,
            }
            mismatches = [
                key for key in native if native[key] != python[key]
            ]
            if mismatches:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "drain_plan",
                        "session_id": session_id,
                        "mismatches": mismatches,
                        "python": python,
                        "native": native,
                    }
                )
        except Exception as exc:
            self._disable("native-drain-error", exc)

    def observe_mark_sent(self, session_id: str, *, sequence: int) -> None:
        if not self._ensure(session_id):
            return
        try:
            self._ob.mark_sent(session_id, sequence, _now_ms())
        except Exception as exc:
            self._disable("native-mark-sent-error", exc)

    def observe_retry_deadline(self, py_deadline_s: Optional[float]) -> None:
        if self._disabled:
            return
        maybe_emit_resource(self)
        try:
            native_ms = int(self._ob.next_retry_deadline(_RETRY_MS) or 0)
            py_ms = (
                int(round(py_deadline_s * 1000))
                if py_deadline_s is not None
                else 0
            )
            # Python deadlines are monotonic-float seconds; C returns ms.
            # Compare against the same monotonic base.
            if native_ms != py_ms:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "next_retry_deadline",
                        "python": {"deadline_ms": py_ms},
                        "native": {"deadline_ms": native_ms},
                    }
                )
        except Exception as exc:
            self._disable("native-retry-deadline-error", exc)

    def observe_prune_floor(
        self, *, py_floor: int, latest_sequence: int
    ) -> None:
        if self._disabled:
            return
        try:
            native_floor = int(self._ob.prune_floor(latest_sequence))
            if native_floor != py_floor:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "prune_floor",
                        "python": {"floor": py_floor},
                        "native": {"floor": native_floor},
                    }
                )
        except Exception as exc:
            self._disable("native-prune-floor-error", exc)

    def _compare(
        self,
        op: str,
        session_id: str,
        *,
        python: dict[str, Any],
        native: dict[str, Any],
    ) -> None:
        mismatches = [key for key in native if native[key] != python[key]]
        if mismatches:
            self._emit(
                {
                    "kind": "divergence",
                    "op": op,
                    "session_id": session_id,
                    "mismatches": mismatches,
                    "python": python,
                    "native": native,
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


__all__ = ["OutboxNativeShadow"]
