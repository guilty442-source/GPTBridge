"""Per-provider adapter runtime with an explicit state machine.

Each registered provider gets its own ``ProviderRuntime``: its own state,
its own request/generation tracking and its own DOM adapter.  Providers
never share login state, capture state, in-flight requests, selectors or
operation generations.

State machine::

    UNINITIALIZED -> OPENING -> READY
                       |         |
                       |         v
                       |   LOGIN_REQUIRED (input missing / login wall)
                       v
    READY -> SUBMITTING -> GENERATING -> CAPTURING -> COMPLETED
                |              |             |
                +-------> FAILED <-----------+
                +-------> CANCELLED <--------+
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .browser_automation import BrowserAutomationSession
from .provider_adapters import ProviderAdapter, adapter_for

PROVIDER_STATES = frozenset(
    {
        "uninitialized",
        "opening",
        "login_required",
        "ready",
        "submitting",
        "generating",
        "capturing",
        "completed",
        "failed",
        "cancelled",
    }
)

_TERMINAL = frozenset({"completed", "failed", "cancelled"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ProviderTransition:
    """One recorded state change — the audit unit of the state machine."""

    provider_id: str
    from_state: str
    to_state: str
    request_id: str
    generation: int
    timestamp: str
    fault_reference: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "request_id": self.request_id,
            "generation": self.generation,
            "timestamp": self.timestamp,
            "fault_reference": self.fault_reference,
        }


@dataclass
class ProviderRuntimeInfo:
    """Serializable snapshot of one provider's runtime."""

    provider_id: str
    display_name: str
    registered_url: str
    adapter_version: str
    supported_capabilities: list[str]
    state: str
    health_state: str
    request_id: str
    generation: int
    session_state: str
    last_success: str
    last_error: str
    fault_reference: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "registered_url": self.registered_url,
            "adapter_version": self.adapter_version,
            "supported_capabilities": list(self.supported_capabilities),
            "state": self.state,
            "health_state": self.health_state,
            "request_id": self.request_id,
            "generation": self.generation,
            "session_state": self.session_state,
            "last_success": self.last_success,
            "last_error": self.last_error,
            "fault_reference": self.fault_reference,
        }


class ProviderRuntime:
    """Adapter-facing runtime for exactly one provider."""

    def __init__(
        self,
        provider_id: str,
        adapter: ProviderAdapter,
        browser: BrowserAutomationSession,
        *,
        registered_url: str = "",
    ) -> None:
        self.provider_id = provider_id
        self.adapter = adapter
        self.browser = browser
        self.registered_url = registered_url
        self.state = "uninitialized"
        self.request_id = ""
        self.generation = 0
        self.session_id = ""
        self.last_success = ""
        self.last_error = ""
        self.fault_reference = ""
        self.transitions: list[ProviderTransition] = []
        self._submit_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # state machine
    # ------------------------------------------------------------------
    def transition(
        self,
        to_state: str,
        *,
        request_id: str = "",
        fault_reference: str = "",
    ) -> ProviderTransition:
        if to_state not in PROVIDER_STATES:
            raise ValueError(f"unknown provider state: {to_state}")
        record = ProviderTransition(
            provider_id=self.provider_id,
            from_state=self.state,
            to_state=to_state,
            request_id=request_id or self.request_id,
            generation=self.generation,
            timestamp=_utc_now(),
            fault_reference=fault_reference,
        )
        self.state = to_state
        if fault_reference:
            self.fault_reference = fault_reference
        self.transitions.append(record)
        if len(self.transitions) > 64:
            self.transitions = self.transitions[-64:]
        return record

    # ------------------------------------------------------------------
    # unified adapter operations
    # ------------------------------------------------------------------
    async def open(self, agent: dict[str, Any], request_id: str = "") -> dict[str, Any]:
        self._begin_request(request_id)
        self.transition("opening")
        result = await self.browser.open_agent(agent)
        if result.get("ok") is True:
            self.session_id = str(result.get("session_id") or "")
            self.transition("ready")
            return result
        fault = str(result.get("error_code") or result.get("message") or "")
        self.transition("failed", fault_reference=fault)
        self.last_error = fault
        return result

    async def navigate(self, url: str, request_id: str = "") -> dict[str, Any]:
        if not self.session_id:
            return {"ok": False, "message": "EMBEDDED_BROWSER_SESSION_FAILED"}
        result = self.browser._browser("navigate", self.session_id, url)
        if isinstance(result, dict) and result.get("ok") is False:
            fault = str(result.get("message") or "EMBEDDED_BROWSER_SESSION_FAILED")
            self.transition("failed", fault_reference=fault)
            return {"ok": False, "message": fault}
        return {"ok": True}

    async def check_session(self) -> dict[str, Any]:
        if not self.session_id:
            return {"alive": False, "session_state": "closed"}
        url = self.browser._get_url(self.session_id)
        if url is None:
            return {"alive": False, "session_state": "expired"}
        return {"alive": True, "session_state": "open", "url": url}

    async def check_ready(self) -> bool:
        """Readiness is a DOM fact: the composer must actually exist."""
        if not self.session_id:
            return False
        return await self.browser.probe_ready(self.session_id, self.adapter)

    async def submit(
        self,
        agent: dict[str, Any],
        prompt: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Full provider round-trip under this runtime's own state machine."""
        self._begin_request(request_id)
        async with self._submit_lock:
            self.transition("submitting")
            try:
                prepared = await self.browser.prepare_send(agent, self.adapter)
            except Exception as exc:
                self.transition("failed", fault_reference=str(exc))
                self.last_error = str(exc)
                raise
            if isinstance(prepared, dict):
                return self._early_result(prepared)
            self.session_id, _adapter = prepared

            early = await self.browser.submit_prompt(
                self.session_id, self.provider_id, prompt, self.adapter
            )
            if early is not None:
                return self._early_result(early)
            self.transition("generating")

            self.transition("capturing")
            state, content, marker = await self.browser.wait_for_response(
                self.session_id, self.adapter
            )
            result = self.browser._capture_outcome(
                self.provider_id, self.session_id, state, content, marker
            )
            return self._finish(result)

    async def cancel(self, request_id: str = "") -> dict[str, Any]:
        """Stop local capture work; remote generation state is unconfirmed."""
        if self.session_id:
            self.browser.request_cancel(self.session_id)
        if self.state not in _TERMINAL:
            self.transition("cancelled", fault_reference="REQUEST_CANCELLED")
        return {
            "ok": True,
            "provider_id": self.provider_id,
            "cancel_remote_state": "unconfirmed",
        }

    def get_health(self) -> dict[str, Any]:
        probe = (
            self.browser.session_state(self.provider_id)
            if self.session_id
            else {"session_state": "closed"}
        )
        health = (
            "failed"
            if self.state == "failed"
            else "healthy"
            if self.state in {"ready", "completed"}
            else "busy"
            if self.state in {"submitting", "generating", "capturing", "opening"}
            else "idle"
        )
        return ProviderRuntimeInfo(
            provider_id=self.provider_id,
            display_name=self.adapter.display_name,
            registered_url=self.registered_url,
            adapter_version=self.adapter.adapter_version,
            supported_capabilities=[
                self.adapter.send_capability,
                self.adapter.response_capture_capability,
            ],
            state=self.state,
            health_state=health,
            request_id=self.request_id,
            generation=self.generation,
            session_state=str(probe.get("session_state") or "closed"),
            last_success=self.last_success,
            last_error=self.last_error,
            fault_reference=self.fault_reference,
        ).as_dict()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _begin_request(self, request_id: str) -> None:
        self.request_id = str(request_id or uuid.uuid4().hex[:12])
        self.generation += 1

    def _early_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Map a browser-layer early result onto the state machine."""
        error_code = str(result.get("error_code") or "")
        status = str(result.get("status") or "")
        if error_code in {
            "BROWSER_LOGIN_OR_INPUT_REQUIRED",
            "BROWSER_VERIFICATION_REQUIRED",
        } or status == "waiting_verification":
            self.transition("login_required", fault_reference=error_code)
        elif error_code:
            self.transition("failed", fault_reference=error_code)
        self.last_error = error_code
        result.setdefault("provider_runtime", self.get_health())
        return result

    def _finish(self, result: dict[str, Any]) -> dict[str, Any]:
        status = str(result.get("status") or "")
        error_code = str(result.get("error_code") or "")
        if status == "completed":
            self.transition("completed")
            self.last_success = _utc_now()
        elif status == "cancelled":
            self.transition("cancelled", fault_reference=error_code or "REQUEST_CANCELLED")
        elif status in {"awaiting-user", "waiting_verification"}:
            target = (
                "login_required"
                if error_code == "BROWSER_LOGIN_OR_INPUT_REQUIRED"
                or status == "waiting_verification"
                else "ready"
            )
            self.transition(target, fault_reference=error_code)
        else:
            self.transition("failed", fault_reference=error_code or "FAILED")
        if error_code:
            self.last_error = error_code
        result.setdefault("provider_runtime", self.get_health())
        return result


class ProviderRuntimePool:
    """Owns one ProviderRuntime per registered provider.

    The pool is the only place that maps provider_id → runtime; adapters
    and their state are never shared or mixed.
    """

    def __init__(self, browser: BrowserAutomationSession) -> None:
        self.browser = browser
        self._runtimes: dict[str, ProviderRuntime] = {}

    def runtime_for(
        self, provider_id: str, *, registered_url: str = ""
    ) -> ProviderRuntime | None:
        provider = str(provider_id or "").strip().casefold()
        adapter = adapter_for(provider)
        if adapter is None:
            return None
        runtime = self._runtimes.get(provider)
        if runtime is None:
            runtime = ProviderRuntime(
                provider, adapter, self.browser, registered_url=registered_url
            )
            self._runtimes[provider] = runtime
        elif registered_url and not runtime.registered_url:
            runtime.registered_url = registered_url
        return runtime

    def health(self) -> list[dict[str, Any]]:
        return [runtime.get_health() for runtime in self._runtimes.values()]

    async def cancel_all(self) -> None:
        for runtime in self._runtimes.values():
            await runtime.cancel()
