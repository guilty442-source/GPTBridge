"""TradingAuthorizationService — scoped, expiring trade grants.

Grants are dimensional: user × account × market × broker × instrument ×
strategy × mode × side × notional caps × validity window. Two accounts
never share额度 implicitly — an authorization binds to one account_id.

Issue path: ``issued_by`` must be in the governed issuer allowlist
(``runtime/state/live-issuers.json``; default ["governor","user"]).
AI/model issuers are structurally refused — 星澄 cannot create, extend,
or modify its own trading authority.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import TradingAuthorization

_AI_ISSUERS = frozenset({
    "ai", "xingcheng", "model", "llm", "assistant", "agent",
    "local-model", "star", "星澄",
})


class TradingAuthorizationService:
    def __init__(self, state_dir: Path, journal) -> None:
        """``journal(kind, row)`` — persistence callback."""
        self._dir = Path(state_dir)
        self._issuers_path = self._dir / "live-issuers.json"
        self._journal = journal
        self._auths: dict[str, TradingAuthorization] = {}
        self._revoked: set[str] = set()
        self._load()

    # ------------------------------------------------------------------
    def _issuers(self) -> set[str]:
        try:
            data = json.loads(
                self._issuers_path.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return {str(x) for x in data}
        except Exception:
            pass
        return {"governor", "user"}

    def _load(self) -> None:
        for row in self._journal_read():
            auth = TradingAuthorization(**{
                k: v for k, v in row.items()
                if k in TradingAuthorization.__dataclass_fields__})
            if auth.status == "REVOKED":
                self._revoked.add(auth.authorization_id)
            else:
                self._auths[auth.authorization_id] = auth

    def _journal_read(self) -> list[dict[str, Any]]:
        return self._journal("live_authorizations", None)

    # ------------------------------------------------------------------
    def issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        issued_by = str(payload.get("issued_by") or "")
        if issued_by.lower() in _AI_ISSUERS:
            return {"ok": False, "error_code": "AI_ISSUER_DENIED"}
        if issued_by not in self._issuers():
            return {"ok": False, "error_code": "ISSUER_NOT_AUTHORIZED",
                    "issuer": issued_by}
        auth = TradingAuthorization(
            account_id=str(payload.get("account_id") or ""),
            user_id=str(payload.get("user_id") or ""),
            market=str(payload.get("market") or ""),
            broker_id=str(payload.get("broker_id") or ""),
            instrument_ids=[str(x) for x in
                            payload.get("instrument_ids") or []],
            strategy_ids=[str(x) for x in
                          payload.get("strategy_ids") or []],
            modes=[str(x).upper() for x in
                   payload.get("modes") or ["LIVE"]],
            sides=[str(x).lower() for x in
                   payload.get("sides") or ["buy", "sell"]],
            max_order_notional=str(payload.get("max_order_notional")
                                   or "0"),
            max_daily_notional=str(payload.get("max_daily_notional")
                                   or "0"),
            currency=str(payload.get("currency") or ""),
            issued_by=issued_by,
            expires_at=float(payload.get("expires_at") or 0),
        )
        if not auth.account_id:
            return {"ok": False, "error_code": "ACCOUNT_REQUIRED"}
        if auth.expires_at <= auth.issued_at:
            return {"ok": False, "error_code": "EXPIRY_REQUIRED"}
        self._auths[auth.authorization_id] = auth
        self._journal("live_authorizations", auth.to_dict())
        return {"ok": True, "authorization": auth.to_dict()}

    def revoke(self, authorization_id: str,
               by: str = "") -> dict[str, Any]:
        if str(by).lower() in _AI_ISSUERS:
            return {"ok": False, "error_code": "AI_ISSUER_DENIED"}
        if str(by) not in self._issuers():
            return {"ok": False, "error_code": "ISSUER_NOT_AUTHORIZED"}
        auth = self._auths.get(str(authorization_id))
        if auth is None:
            return {"ok": False, "error_code": "AUTHORIZATION_NOT_FOUND"}
        auth.status = "REVOKED"
        self._revoked.add(auth.authorization_id)
        self._journal("live_authorizations", {
            **auth.to_dict(),
            "revoked_by": str(by), "revoked_at": time.time()})
        return {"ok": True, "authorization_id": auth.authorization_id}

    # ------------------------------------------------------------------
    def check(self, *, account_id: str, market: str, broker_id: str,
              instrument_id: str, strategy_id: str, mode: str,
              side: str, notional, user_id: str = "") -> dict[str, Any]:
        """Return the first ACTIVE grant covering every dimension."""
        notional = Decimal(str(notional or 0))
        for auth in self._auths.values():
            if auth.status != "ACTIVE" or \
                    auth.authorization_id in self._revoked:
                continue
            if auth.expired():
                continue
            if auth.account_id != account_id:
                continue
            if auth.market and auth.market != market:
                continue
            if auth.broker_id and auth.broker_id != broker_id:
                continue
            if auth.instrument_ids and \
                    instrument_id not in auth.instrument_ids:
                continue
            if auth.strategy_ids and strategy_id and \
                    strategy_id not in auth.strategy_ids:
                continue
            if auth.modes and mode.upper() not in auth.modes:
                continue
            if auth.sides and side.lower() not in auth.sides:
                continue
            if auth.user_id and user_id and auth.user_id != user_id:
                continue
            cap = Decimal(auth.max_order_notional or "0")
            if cap > 0 and notional > cap:
                continue
            return {"ok": True, "authorization": auth.to_dict()}
        return {"ok": False, "error_code": "NO_AUTHORIZATION"}

    def list(self, account_id: str | None = None) -> list[dict[str, Any]]:
        out = []
        for a in self._auths.values():
            if a.expired() and a.status == "ACTIVE":
                a.status = "EXPIRED"
            if account_id and a.account_id != account_id:
                continue
            out.append(a.to_dict())
        return out
