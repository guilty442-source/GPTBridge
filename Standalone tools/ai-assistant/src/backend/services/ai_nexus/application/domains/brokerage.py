"""券商管理 domain — read-only mirror of the offline broker layer.

Broker profiles, connection states, offline accounts, import batches and
broker-simulation events are mirrored from investment-mobile through the
governed submit-only channel. This domain never commands the engine and
never exposes a connect/login affordance — real broker integration is
disabled this phase by the offline gate in investment-mobile.
"""

from __future__ import annotations

from typing import Any

from ...domain.contract import DOMAIN_BROKERAGE
from .base import BusinessDomain


class BrokerageDomain(BusinessDomain):
    domain_id = DOMAIN_BROKERAGE
    label = "券商管理"
    commands = frozenset(
        {
            "investment_broker_status",
            "investment_broker_accounts",
            "investment_broker_imports",
            "investment_broker_portfolio",
            "investment_broker_sims",
            "investment_broker_import_submit",
            "investment_broker_import_rollback",
        }
    )

    async def handle(self, command, payload, *, store, ai_connections):
        if command == "investment_broker_status":
            return {
                "ok": True,
                "domain": self.domain_id,
                "brokers": store.kv_get(self.domain_id,
                                        "broker_status", []),
                "offline_gate": store.kv_get(self.domain_id,
                                             "offline_gate", {}),
                "integration": "disabled — offline phase",
            }
        if command == "investment_broker_accounts":
            return {
                "ok": True,
                "domain": self.domain_id,
                "accounts": store.kv_get(self.domain_id,
                                         "offline_accounts", []),
                "note": "manual/imported data — broker_confirmed=false",
            }
        if command == "investment_broker_imports":
            return {
                "ok": True,
                "domain": self.domain_id,
                "batches": store.kv_get(self.domain_id,
                                        "import_batches", []),
            }
        if command == "investment_broker_portfolio":
            return {
                "ok": True,
                "domain": self.domain_id,
                "portfolio": store.kv_get(self.domain_id,
                                          "unified_portfolio", {}),
            }
        if command == "investment_broker_sims":
            return {
                "ok": True,
                "domain": self.domain_id,
                "simulated": True,
                "events": store.kv_get(self.domain_id,
                                       "broker_sim_events", []),
            }
        if command in ("investment_broker_import_submit",
                       "investment_broker_import_rollback"):
            # Import execution lives in investment-mobile, which is a
            # submit-only channel participant — there is no governed
            # route into it, so this UI cannot trigger a real import.
            # Record the intent for audit and answer honestly.
            store.record_audit({
                "type": "import_control_intent",
                "command": command,
                "target": str(payload.get("target") or ""),
                "batch_id": str(payload.get("batch_id") or ""),
                "file_name": str(payload.get("file_name") or ""),
                "actor": "ui-user",
                "result": "CONTROL_CHANNEL_UNAVAILABLE",
            })
            return {
                "ok": False,
                "error_code": "CONTROL_CHANNEL_UNAVAILABLE",
                "note": "匯入執行於投資引擎端——ai-assistant 為 submit-only "
                        "通道端點，尚無授權路由可觸發匯入；"
                        "操作意圖已記入審計。",
                "integration": "incomplete",
            }
        raise PermissionError("PERMISSION_DENIED")
