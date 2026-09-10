from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


class PortfolioSvcLedgerMixin:
    async def _add_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        transaction = self.analytics_store.add_transaction(payload)
        accounting = self._schedule_star_accounting_background(
            trigger="transaction_change"
        )
        response = self._state_response(self.repository.load_state())
        response.update(
            {
                "message": "交易已寫入本機帳本，投資管家帳務已接續排程。",
                "transaction": transaction,
                "star_accounting": accounting,
            }
        )
        return response

    async def _seed_opening_ledger(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._snapshot_coordinator() as state:
            portfolio = (
                state.get("portfolio")
                if isinstance(state.get("portfolio"), dict)
                else {}
            )
            occurred_at = str(payload.get("occurred_at") or "").strip()
            if not occurred_at:
                source_path = Path(str(portfolio.get("source_path") or ""))
                try:
                    occurred_at = datetime.fromtimestamp(
                        source_path.stat().st_ctime
                    ).astimezone().isoformat()
                except OSError:
                    occurred_at = str(portfolio.get("imported_at") or "")
            safety_backup = self.analytics_store.backup_database(
                "before-opening-ledger"
            )
            result = self.analytics_store.sync_opening_balance_transactions(
                state,
                occurred_at,
            )
            self._invalidate_v3_snapshot()
            response = self._state_response(state)
        accounting = self._schedule_star_accounting_background(
            trigger="opening_ledger_change"
        )
        response.update(
            {
                "message": (
                    f"已建立 {result.get('generated_count', 0)} 筆估算期初交易；"
                    f"覆蓋 {result.get('coverage_percent', 0)}%，XIRR 已改用新台幣基準。"
                ),
                "opening_ledger": result,
                "safety_backup": safety_backup,
                "star_accounting": accounting,
            }
        )
        return response

    async def _reconcile_ledger(self, _payload: dict[str, Any]) -> dict[str, Any]:
        with self._snapshot_coordinator() as state:
            reconciliation = self.analytics_store.reconcile_ledger_holdings(state)
            response = self._state_response(state)
        response.update(
            {
                "message": (
                    f"帳本對帳完成：{reconciliation.get('matched_count', 0)} 筆相符，"
                    f"{reconciliation.get('difference_count', 0)} 筆差異。"
                ),
                "ledger_reconciliation": reconciliation,
            }
        )
        return response

    async def _apply_ledger_reconciliation(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._snapshot_coordinator() as state:
            safety_backup = self.analytics_store.backup_database(
                "before-ledger-reconciliation"
            )
            reconciliation = self.analytics_store.apply_ledger_reconciliation(
                state,
                confirmed=payload.get("confirmed") is True,
            )
            self._invalidate_v3_snapshot()
            response = self._state_response(state)
        response.update(
            {
                "message": (
                    f"已建立 {reconciliation.get('applied_count', 0)} 筆估算對帳調整；"
                    "原始持股與券商檔案均未修改。"
                ),
                "ledger_reconciliation": reconciliation,
                "safety_backup": safety_backup,
            }
        )
        return response

    async def _delete_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        transaction_id = str(payload.get("transaction_id") or "").strip()
        if not transaction_id:
            raise ValueError("缺少 transaction_id")
        deleted = self.analytics_store.delete_transaction(transaction_id)
        response = self._state_response(self.repository.load_state())
        response.update({"message": "交易已刪除。" if deleted else "找不到交易。", "deleted": deleted})
        return response

    async def _import_price_history(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_bars = payload.get("bars")
        if not isinstance(raw_bars, list):
            raise ValueError("bars 必須是歷史行情陣列")
        count = self.analytics_store.add_price_bars(
            item for item in raw_bars if isinstance(item, dict)
        )
        response = self._state_response(self.repository.load_state())
        response.update({"message": f"已匯入 {count} 筆歷史行情。", "price_count": count})
        return response
