"""BrokerGateway + BrokerCredentialService.

Gateway is the ONLY caller of adapter trade functions; every call is
capability-gated first (UNKNOWN ≠ SUPPORTED) then api-verified, then
dispatched. The phase lock lives upstream in LiveOrderManagementSystem —
even a fully capable adapter never receives a real order this phase.

Credentials: only references (``credman:`` suffixes) are stored; secrets
resolve inside the gateway through the governed Windows Credential
Manager store and are returned as opaque handles — they never appear in
payloads, logs, audit rows, or AI-visible state.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .contracts import BROKER_FUNCTIONS, CapabilityStatus, mask_sensitive


class BrokerCredentialService:
    """Broker credential registry — references only, never secrets."""

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "broker-credentials.json"
        self._refs: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._refs = {str(k): str(v) for k, v in data.items()}
        except Exception:
            self._refs = {}

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._refs, indent=1), "utf-8")
        tmp.replace(self._path)

    def register_reference(self, account_id: str, credman_suffix: str
                           ) -> dict[str, Any]:
        """Register a ``credman:`` suffix — plaintext secrets refused."""
        suffix = str(credman_suffix or "").strip()
        if not suffix:
            return {"ok": False, "error_code": "REFERENCE_REQUIRED"}
        if suffix.casefold().startswith("credman:"):
            suffix = suffix[len("credman:"):]
        if suffix.casefold().startswith("gptbridge/"):
            suffix = suffix[len("gptbridge/"):]
        self._refs[str(account_id)] = f"broker/{suffix}"
        self._persist()
        return {"ok": True, "account_id": account_id,
                "target": f"GPTBridge/broker/{suffix}"}

    def status(self, account_id: str) -> dict[str, Any]:
        ref = self._refs.get(str(account_id))
        return {"ok": True, "account_id": account_id,
                "registered": ref is not None,
                "target": f"GPTBridge/{ref}" if ref else ""}

    def resolve(self, account_id: str) -> str | None:
        """INTERNAL — gateway/adapters only; result never leaves them."""
        ref = self._refs.get(str(account_id))
        if not ref:
            return None
        try:
            from shared_layer.security.credential_store import read_secret
            return read_secret(ref)
        except Exception:
            return None


class BrokerGateway:
    """Capability-gated dispatch to per-broker adapters."""

    def __init__(self, adapters: dict[str, Any],
                 credentials: BrokerCredentialService) -> None:
        self._adapters = adapters            # broker_id → adapter
        self._credentials = credentials
        self._connection_state: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    def capability(self, broker_id: str, fn: str) -> str:
        adapter = self._adapters.get(str(broker_id))
        if adapter is None or fn not in BROKER_FUNCTIONS:
            return CapabilityStatus.UNKNOWN.value
        caps = adapter.capabilities() if hasattr(
            adapter, "capabilities") else {}
        return str(caps.get(fn, CapabilityStatus.UNKNOWN.value))

    def capabilities(self, broker_id: str) -> dict[str, str]:
        return {fn: self.capability(broker_id, fn)
                for fn in BROKER_FUNCTIONS}

    # ------------------------------------------------------------------
    def call(self, broker_id: str, fn: str,
             **kwargs: Any) -> dict[str, Any]:
        """Single gated entry — no direct adapter access upstream."""
        adapter = self._adapters.get(str(broker_id))
        if adapter is None:
            return {"ok": False, "error_code": "BROKER_UNKNOWN"}
        cap = self.capability(broker_id, fn)
        if cap != CapabilityStatus.SUPPORTED.value:
            return {"ok": False,
                    "error_code": f"CAPABILITY_{cap}",
                    "function": fn, "broker_id": broker_id}
        verified = getattr(adapter, "api_verified", False)
        if not verified and fn not in ("connect", "disconnect"):
            return {"ok": False, "error_code": "BROKER_API_UNVERIFIED",
                    "function": fn, "broker_id": broker_id}
        handler = getattr(adapter, fn, None)
        if handler is None:
            return {"ok": False, "error_code": "CAPABILITY_UNKNOWN",
                    "function": fn}
        try:
            result = handler(**kwargs)
        except Exception as exc:
            return {"ok": False, "error_code": "BROKER_CALL_FAILED",
                    "function": fn, "detail": type(exc).__name__}
        if isinstance(result, dict) and "ok" not in result:
            result = {"ok": True, **result}
        return result

    # convenience wrappers ------------------------------------------------
    def connect(self, broker_id: str, **kw) -> dict[str, Any]:
        res = self.call(broker_id, "connect", **kw)
        self._connection_state[str(broker_id)] = {
            "connected": bool(res.get("ok")),
            "at": time.time(), "detail": res.get("error_code", "")}
        return res

    def connection_state(self, broker_id: str) -> dict[str, Any]:
        return self._connection_state.get(
            str(broker_id), {"connected": False})
