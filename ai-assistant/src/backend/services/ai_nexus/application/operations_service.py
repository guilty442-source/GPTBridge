from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..infrastructure.analytics_repository import number


class InvestmentOperationsServiceMixin:
    async def _run_stress_test(self, payload: dict[str, Any]) -> dict[str, Any]:
        scenarios = payload.get("scenarios")
        if scenarios is not None and not isinstance(scenarios, list):
            raise ValueError("scenarios 必須是情境陣列")
        state = self.repository.load_state()
        result = self.analytics_store.stress_test(state, scenarios)
        response = self._state_response(state)
        response.update({"message": "壓力測試完成。", "stress_test": result})
        return response

    async def _run_backtest(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        raw_symbols = payload.get("symbols")
        symbols = (
            [str(item).strip().upper() for item in raw_symbols if str(item).strip()]
            if isinstance(raw_symbols, list)
            else [str(item.get("symbol") or "").upper() for item in state.get("holdings", []) if isinstance(item, dict)]
        )
        result = self.analytics_store.backtest(
            symbols,
            strategy=str(payload.get("strategy") or "buy_and_hold"),
            initial_capital=float(payload.get("initial_capital") or 1_000_000),
            fee_percent=float(payload.get("fee_percent") or 0.1425),
            slippage_percent=float(payload.get("slippage_percent") or 0.05),
        )
        response = self._state_response(state)
        response.update({"message": "歷史回測完成。", "backtest": result})
        return response

    async def _plan_rebalance(self, payload: dict[str, Any]) -> dict[str, Any]:
        targets = payload.get("targets")
        if targets is not None and not isinstance(targets, dict):
            raise ValueError("targets 必須是標的與目標權重物件")
        state = self.repository.load_state()
        policy = self._investment_policy()
        forbidden = {
            str(item).strip().upper()
            for item in (policy.get("forbidden_assets") or [])
            if str(item).strip()
        }
        normalized_targets = (
            {str(key).upper(): float(value) for key, value in targets.items()}
            if isinstance(targets, dict)
            else None
        )
        if normalized_targets is not None and forbidden:
            holding_types = {
                str(item.get("symbol") or "").upper(): str(
                    item.get("asset_type") or ""
                ).upper()
                for item in state.get("holdings", [])
                if isinstance(item, dict)
            }
            for symbol in list(normalized_targets):
                if symbol in forbidden or holding_types.get(symbol) in forbidden:
                    normalized_targets[symbol] = 0.0
        capacity_caps = {
            "conservative": 15.0,
            "balanced": 25.0,
            "growth": 35.0,
            "aggressive": 50.0,
        }
        risk_capacity = str(policy.get("risk_capacity") or "balanced")
        policy_cap = capacity_caps.get(risk_capacity, 25.0)
        max_position_percent = min(
            float(payload.get("max_position_percent") or 35),
            policy_cap,
        )
        cash_reserve_percent = max(
            float(payload.get("cash_reserve_percent") or 5),
            number(policy.get("cash_need_percent"), 0),
        )
        result = self.analytics_store.rebalance(
            state,
            targets=normalized_targets,
            max_position_percent=max_position_percent,
            cash_reserve_percent=cash_reserve_percent,
            min_trade_value=float(payload.get("min_trade_value") or 1000),
            fee_percent=float(payload.get("fee_percent") or 0.1425),
        )
        result["policy_constraints"] = {
            "risk_capacity": risk_capacity,
            "max_position_percent": max_position_percent,
            "cash_reserve_percent": cash_reserve_percent,
            "forbidden_assets": sorted(forbidden),
            "human_approval_required": True,
        }
        response = self._state_response(state)
        response.update({"message": "再平衡模擬完成，尚未送出任何交易。", "rebalance": result})
        return response

    async def _add_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = self.analytics_store.add_event(payload)
        response = self._state_response(self.repository.load_state())
        response.update({"message": "市場事件已加入行事曆。", "event": event})
        return response

    async def _add_alert_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        rule = self.analytics_store.add_alert_rule(payload)
        response = self._state_response(self.repository.load_state())
        response.update({"message": "持久警示規則已儲存。", "alert_rule": rule})
        return response

    async def _acknowledge_alert(self, payload: dict[str, Any]) -> dict[str, Any]:
        alert_event_id = str(payload.get("alert_event_id") or "").strip()
        if not alert_event_id:
            raise ValueError("缺少 alert_event_id")
        acknowledged = self.analytics_store.acknowledge_alert(alert_event_id)
        response = self._state_response(self.repository.load_state())
        response.update({"message": "警示已確認。" if acknowledged else "找不到警示。", "acknowledged": acknowledged})
        return response

    async def _set_decision_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        decision_id = str(payload.get("decision_id") or "").strip()
        if not decision_id:
            raise ValueError("缺少 decision_id")
        updated = self.analytics_store.set_decision_status(
            decision_id,
            str(payload.get("status") or "pending"),
        )
        response = self._state_response(self.repository.load_state())
        response.update({"message": "決策狀態已更新。" if updated else "找不到決策。", "updated": updated})
        return response

    async def _update_v2_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "base_currency",
            "benchmark",
            "max_position_percent",
            "cash_reserve_percent",
            "investment_goal",
            "time_horizon_years",
            "risk_capacity",
            "cash_need_percent",
            "forbidden_assets",
            "target_return_percent",
        }
        saved: dict[str, Any] = {}
        for key in allowed:
            if key in payload:
                value = payload[key]
                if key == "risk_capacity":
                    normalized = str(value or "").strip().casefold()
                    if normalized not in {
                        "conservative",
                        "balanced",
                        "growth",
                        "aggressive",
                    }:
                        raise ValueError("risk_capacity 不在允許範圍")
                    value = normalized
                elif key == "investment_goal":
                    value = str(value or "").strip()[:500]
                elif key == "forbidden_assets":
                    values = value if isinstance(value, list) else str(value or "").split(",")
                    value = sorted(
                        {
                            str(item).strip().upper()
                            for item in values
                            if str(item).strip()
                        }
                    )[:100]
                elif key == "time_horizon_years":
                    value = max(1.0, min(80.0, number(value, 5)))
                elif key in {"cash_need_percent", "cash_reserve_percent"}:
                    value = max(0.0, min(100.0, number(value, 0)))
                elif key == "max_position_percent":
                    value = max(1.0, min(100.0, number(value, 35)))
                elif key == "target_return_percent":
                    value = (
                        None
                        if value in {None, ""}
                        else max(-100.0, min(1000.0, number(value)))
                    )
                self.analytics_store.set_setting(key, value)
                saved[key] = value
        self._invalidate_v3_snapshot()
        response = self._state_response(self.repository.load_state())
        response.update({"message": "投資管家設定已更新。", "settings": saved})
        return response

    def _invalidate_v3_snapshot(self) -> None:
        self._v3_snapshot_cache = None
        self._v3_snapshot_cached_at = 0.0

    async def _get_v3(self, _payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        return {"ok": True, "version": self.VERSION, "v3": self._v3_snapshot(state, force=True)}

    async def _add_fx_rates(self, payload: dict[str, Any]) -> dict[str, Any]:
        rates = payload.get("rates")
        if not isinstance(rates, list):
            rates = [payload]
        count = self.v3.add_fx_rates([item for item in rates if isinstance(item, dict)])
        self._invalidate_v3_snapshot()
        response = self._state_response(self.repository.load_state())
        response.update({"message": f"已寫入 {count} 筆歷史匯率。", "fx_rate_count": count})
        return response

    async def _import_broker_statement(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_path = str(payload.get("path") or payload.get("file_path") or "").strip()
        if not raw_path:
            raise ValueError("請選擇券商 CSV、Excel 或 PDF 明細")
        source = Path(raw_path).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"找不到檔案：{source}")
        if source.suffix.casefold() not in {".csv", ".tsv", ".txt", ".xlsx", ".pdf"}:
            raise ValueError("券商明細僅支援 CSV、TSV、XLSX 或文字型 PDF")
        snapshot = self._create_import_snapshot(source)
        try:
            result = self.broker_reconciliation.import_statement(
                snapshot,
                source_name=source.name,
                broker=str(payload.get("broker") or ""),
            )
        finally:
            # Complete source snapshots are append-only recovery evidence.
            pass
        self._invalidate_v3_snapshot()
        return {
            "ok": True,
            "message": f"已比對 {result['row_count']} 筆，發現 {result['difference_count']} 筆差異；尚未自動入帳。",
            "broker_import": result,
            "source_file_released": True,
            "v3": self._v3_snapshot(self.repository.load_state(), force=True),
            "state": self._state_with_analytics(self.repository.load_state()),
        }

    async def _approve_broker_rows(self, payload: dict[str, Any]) -> dict[str, Any]:
        row_ids = payload.get("row_ids")
        if not isinstance(row_ids, list):
            raise ValueError("row_ids 必須是待入帳差異清單")
        result = self.broker_reconciliation.approve_rows(
            str(payload.get("import_id") or ""), row_ids, confirmed=payload.get("confirmed") is True
        )
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "已將確認的差異寫入本機交易帳本。", "broker_import": result, "state": self._state_with_analytics(self.repository.load_state())}

    async def _optimize_portfolio(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        result = self.v3.optimize_portfolio(
            state,
            method=str(payload.get("method") or "risk_parity"),
            max_position_percent=float(payload.get("max_position_percent") or 35),
            max_turnover_percent=float(payload.get("max_turnover_percent") or 40),
            views=payload.get("views") if isinstance(payload.get("views"), dict) else None,
            seed=int(payload.get("seed") or 73021),
        )
        return {"ok": bool(result.get("ok")), "message": "最佳化草案已完成，沒有送出任何交易。", "optimization": result}

    async def _run_monte_carlo(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.v3.monte_carlo(
            self.repository.load_state(),
            simulations=int(payload.get("simulations") or 2000),
            horizon_days=int(payload.get("horizon_days") or 252),
            target_return_percent=float(payload.get("target_return_percent") or 0),
            seed=int(payload.get("seed") or 73021),
        )
        return {"ok": bool(result.get("ok")), "message": "蒙地卡羅情境模擬完成。", "monte_carlo": result}

    async def _add_corporate_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = self.v3.add_corporate_action(payload)
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "公司行動已進入人工覆核，不會直接改動持股。", "corporate_action": action, "state": self._state_with_analytics(self.repository.load_state())}

    async def _review_corporate_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        updated = self.v3.review_corporate_action(
            str(payload.get("action_id") or ""), str(payload.get("status") or "pending_review")
        )
        self._invalidate_v3_snapshot()
        return {"ok": updated, "message": "公司行動覆核狀態已更新。" if updated else "找不到公司行動。", "state": self._state_with_analytics(self.repository.load_state())}

    async def _configure_scheduler(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = self.automation.configure(int(payload.get("interval_seconds") or 900))
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "背景排程週期已更新。", "automation": status, "state": self._state_with_analytics(self.repository.load_state())}

    async def _run_scheduler(self, _payload: dict[str, Any]) -> dict[str, Any]:
        result = await self.automation.run_once()
        self._invalidate_v3_snapshot()
        return {"ok": result["status"] == "completed", "message": "背景監測週期已完成。", "scheduler_run": result, "state": self._state_with_analytics(self.repository.load_state())}

    async def _configure_notification(self, payload: dict[str, Any]) -> dict[str, Any]:
        channel = self.notifications.configure(
            str(payload.get("channel_id") or ""), payload, confirmed=payload.get("confirmed") is True
        )
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "通知管道已更新。", "channel": channel, "state": self._state_with_analytics(self.repository.load_state())}

    async def _dispatch_notifications(self, _payload: dict[str, Any]) -> dict[str, Any]:
        result = await asyncio.to_thread(self.notifications.dispatch)
        self._invalidate_v3_snapshot()
        return {"ok": result["failed"] == 0, "message": "通知佇列已處理。", "dispatch": result, "state": self._state_with_analytics(self.repository.load_state())}

    async def _record_model_evaluation(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = self.model_governance.record_run(
            model_name=str(payload.get("model_name") or "star-investment-analysis-v1"),
            version=str(payload.get("version") or self.VERSION),
            inputs=payload.get("inputs") or {},
            outputs=payload.get("outputs") or {},
            data_sources=[str(value) for value in payload.get("data_sources", [])] if isinstance(payload.get("data_sources"), list) else [],
            metrics=payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {},
            prompt=str(payload.get("prompt") or ""),
            analysis_run_id=str(payload.get("analysis_run_id") or ""),
            decision_count=int(payload.get("decision_count") or 0),
        )
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "模型評估紀錄已寫入治理帳本。", "governance_run_id": run_id, "state": self._state_with_analytics(self.repository.load_state())}

    async def _create_backup(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._snapshot_coordinator():
            backup = self.analytics_store.backup_database(
                str(payload.get("label") or "manual")
            )
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "加密備份已建立。", "backup": backup, "state": self._state_with_analytics(self.repository.load_state())}

    async def _restore_backup(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise ValueError("還原資料庫前必須明確確認")
        with self._snapshot_coordinator():
            result = self.analytics_store.restore_database(
                str(payload.get("backup_name") or "")
            )
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "資料庫已還原，並保留還原前安全備份。", "restore": result, "state": self._state_with_analytics(self.repository.load_state())}

    async def _rotate_database_key(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise ValueError("更換資料庫保護金鑰前必須明確確認")
        with self._snapshot_coordinator():
            result = self.analytics_store.rotate_database_protection()
        self._invalidate_v3_snapshot()
        return {"ok": True, "message": "資料庫保護金鑰已輪替。", "rotation": result, "state": self._state_with_analytics(self.repository.load_state())}

    async def _run_automation_cycle(self) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [item for item in state.get("holdings", []) if isinstance(item, dict)]
        sync_result: dict[str, Any] = {"status": "no_portfolio"}
        if holdings:
            if self.ai_connections is None:
                sync_result = {
                    "status": "star_ai_channel_not_connected",
                    "queued": False,
                }
            else:
                sync_result = await asyncio.to_thread(
                    self.ai_connections.search_investments_sync, holdings
                )
                sync_result["service_owner"] = "AI投資管家"
                sync_result["transport"] = "governance-authenticated-ai-channel"
        alerts = self.analytics_store.evaluate_alerts(state)
        for alert in alerts:
            self.notifications.queue(
                str(alert.get("title") or "投資風險事件"),
                str(alert.get("detail") or ""),
                severity=str(alert.get("severity") or "warning"),
            )
        self._invalidate_v3_snapshot()
        accounting = await self._run_star_accounting(
            {"trigger": "automation_cycle"}
        )
        return {
            "sync": sync_result,
            "accounting": {
                "ok": accounting.get("ok") is True,
                "message": accounting.get("message"),
                "decision": (
                    accounting.get("star_accounting", {}).get("decision")
                    if isinstance(accounting.get("star_accounting"), dict)
                    else None
                ),
            },
            "alerts_triggered": len(alerts),
        }

    def _ingest_corporate_actions_from_events(self) -> int:
        added = 0
        for event in self.analytics_store.list_events(500):
            event_type = str(event.get("event_type") or "")
            if event_type not in {"dividend", "split"}:
                continue
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            split_ratio = None
            if event_type == "split":
                numerator = float(details.get("numerator") or 0)
                denominator = float(details.get("denominator") or 0)
                split_ratio = numerator / denominator if numerator > 0 and denominator > 0 else None
            action = self.v3.add_corporate_action(
                {
                    "symbol": event.get("symbol"),
                    "action_type": event_type,
                    "effective_at": event.get("scheduled_at"),
                    "cash_amount": details.get("amount") if event_type == "dividend" else None,
                    "currency": details.get("currency") or "",
                    "ratio": split_ratio,
                    "source": event.get("source") or "Yahoo Finance",
                    "confidence": event.get("confidence") or 0.8,
                    "details": details,
                }
            )
            added += int(bool(action.get("created")))
        return added
