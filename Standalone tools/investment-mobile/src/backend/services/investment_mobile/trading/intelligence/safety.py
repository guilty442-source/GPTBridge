"""AI safety boundary — hard separation between analysis and execution.

The intelligence engine can produce recommendations and structured
proposals; it physically cannot reach OrderRequest creation, broker
adapters, account mutation, risk-limit mutation, or mode transitions.
This module both documents and *enforces* the boundary: any payload
carrying execution-attempting keys is refused, and model output is
treated as untrusted input (news/documents can never issue commands).
"""

from __future__ import annotations

from typing import Any

# Keys that must never appear inside an AI/analysis payload.
_FORBIDDEN_KEYS = frozenset({
    "order", "order_request", "broker_order_id", "execute", "submit_order",
    "risk_limits", "set_limit", "circuit_breaker", "disable_risk",
    "set_mode", "mode", "account_balance", "set_cash", "position_override",
    "grant_authorization", "live_enable",
})

# Fields allowed in a structured AI output.
_ALLOWED_PROPOSAL_KEYS = frozenset({
    "instrument_id", "market", "side", "quantity", "price", "notional",
    "strategy_id", "strategy_version", "signal_id", "account_id",
    "evidence_refs", "reasoning", "expires_in_seconds",
})

PROMPT_INJECTION_MARKERS = (
    "忽略先前", "ignore previous", "ignore all", "system prompt",
    "系統指令", "下單", "立即買進", "立即賣出", "place order",
    "modify risk", "修改風控", "解除", "授予權限",
)


class AISafetyError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AISafetyBoundary:
    """Gatekeeper on every AI-produced artifact entering the pipeline."""

    def sanitize_proposal(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Strip/refuse execution-attempting keys; return clean fields."""
        lowered = {str(k).lower() for k in payload}
        bad = lowered & _FORBIDDEN_KEYS
        if bad:
            return {"ok": False, "error_code": "AI_FORBIDDEN_FIELD",
                    "fields": sorted(bad)}
        clean = {k: v for k, v in payload.items()
                 if str(k).lower() in _ALLOWED_PROPOSAL_KEYS}
        return {"ok": True, "proposal": clean}

    def inspect_external_text(self, text: str) -> dict[str, Any]:
        """Flag prompt-injection style content in news/docs/research."""
        low = str(text or "").lower()
        hits = [m for m in PROMPT_INJECTION_MARKERS if m in low]
        return {
            "ok": not hits,
            "injection_markers": hits,
            "note": "外部資料為不可信輸入；永不成為系統指令",
        }

    def boundary_manifest(self) -> dict[str, Any]:
        return {
            "ai_cannot": [
                "call broker adapters / create OrderRequest",
                "modify account balances or positions",
                "release circuit breakers",
                "modify risk limits",
                "start LIVE mode",
                "expand trading permissions",
                "treat news as system commands",
                "trigger trades via analysis text",
            ],
            "model_failure": "risk controls remain active — model is advisory only",
        }
