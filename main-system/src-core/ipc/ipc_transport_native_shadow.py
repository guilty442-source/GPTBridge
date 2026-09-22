"""§10.65 dual-track act-1 shadow harness for ``ipc_server`` transport.

Mirrors each inbound WebSocket frame through the C
``NativeIpcRegistry.transport_send``/``transport_recv`` prototype and
compares the echoed payload item-by-item.  Python remains the
authoritative path: divergences are appended to
``runtime/logs/native-shadow/ipc-transport.jsonl`` as governed audit
evidence and never alter message handling.

Fail-closed semantics mirror the other act-1 shadows:

  * missing/invalid policy or missing native extension -> no shadow;
  * any native error -> the shadow disables itself after one
    ``shadow-disabled`` audit record; the Python path is unaffected;
  * modes other than ``"shadow"`` are refused (``"primary"``/``"retire"``
    are later acts).

Known intentional modeling gap (recorded, not hidden): the C transport
prototype is a single-slot 512-byte store — payloads that do not fit are
truncated by ``snprintf``, so oversized frames surface as
``transport-truncated`` divergence evidence rather than being silently
dropped.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_COMPONENT = "ipc_server"
_POLICY_REL = Path("main-system") / "config" / "native-shadow.json"
_LOG_REL = (
    Path("main-system")
    / "runtime"
    / "logs"
    / "native-shadow"
    / "ipc-transport.jsonl"
)

# gptbridge_ipc_transport_msg_t.payload is char[512]; snprintf keeps at
# most 511 bytes + NUL.
_SLOT_PAYLOAD_BYTES = 511


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_project_root() -> Path:
    # main-system/src-core/ipc/<this file> -> repo root
    return Path(__file__).resolve().parents[3]


def _as_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, (bytes, bytearray)):
        return bytes(message).decode("utf-8", "replace")
    return str(message)


class IpcTransportNativeShadow:
    """Parallel C transport-slot observer for inbound IPC frames."""

    def __init__(self, log_path: Path, native_registry: Any) -> None:
        self._log_path = log_path
        self._reg = native_registry
        self._seq = 0
        self._disabled = False
        self._error_emitted = False

    @classmethod
    def from_policy(
        cls, project_root: Optional[Path] = None
    ) -> Optional["IpcTransportNativeShadow"]:
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

    def observe_inbound(self, message: Any) -> None:
        """Mirror one inbound frame through the C transport slot.

        The real socket frame is pushed through ``transport_send`` and
        immediately drained by ``transport_recv``; the echoed payload and
        sequence number are compared item-by-item.  Both calls are
        synchronous on the backend's single event loop, so the shared
        slot cannot interleave mid-pair inside one process.
        """
        if self._disabled:
            return
        text = _as_text(message)
        self._seq += 1
        seq = self._seq
        try:
            if not self._reg.transport_send(text, seq):
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "transport-send",
                        "seq": seq,
                        "python": {"accepted": True},
                        "native": {"accepted": False},
                    }
                )
                return
            got = self._reg.transport_recv()
            if got is None:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "transport-recv",
                        "seq": seq,
                        "python": {"payload": text[:200]},
                        "native": {"received": False},
                    }
                )
                return
            native_seq = int(got.get("seq") or 0)
            native_payload = str(got.get("payload") or "")
            if native_seq != seq:
                self._emit(
                    {
                        "kind": "divergence",
                        "op": "transport-seq",
                        "seq": seq,
                        "python": {"seq": seq},
                        "native": {"seq": native_seq},
                    }
                )
                return
            if native_payload != text:
                oversized = (
                    len(text.encode("utf-8", "replace")) > _SLOT_PAYLOAD_BYTES
                )
                self._emit(
                    {
                        "kind": "divergence",
                        "op": (
                            "transport-truncated"
                            if oversized
                            else "transport-mismatch"
                        ),
                        "seq": seq,
                        "python": {"payload_bytes": len(text.encode("utf-8", "replace"))},
                        "native": {"payload_bytes": len(native_payload.encode("utf-8", "replace"))},
                    }
                )
        except Exception as exc:
            self._disable("native-transport-error", exc)

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


__all__ = ["IpcTransportNativeShadow"]
