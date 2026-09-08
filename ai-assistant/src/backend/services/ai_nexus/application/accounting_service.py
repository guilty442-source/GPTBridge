from __future__ import annotations

import asyncio
from typing import Any


class InvestmentAccountingServiceMixin:
    """Governed accounting boundary owned by Star.

    Star reviews a minimal reconciliation snapshot and decides whether safe
    estimated adjustments may be applied. This tool validates the response and
    remains the only writer of its isolated investment database.
    """

    async def _run_star_accounting(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        if self.ai_connections is None or not bool(
            getattr(self.ai_connections, "is_configured", False)
        ):
            return {
                "ok": False,
                "queued": False,
                "error_code": "STAR_AI_CHANNEL_NOT_CONNECTED",
                "message": "AI投資管家 AI 通道尚未連線，帳務未執行且不排隊。",
                "state": self._state_with_analytics(state),
            }

        reconciliation = self.analytics_store.reconcile_ledger_holdings(state)
        star_result = await asyncio.to_thread(
            self.ai_connections.manage_accounting_sync,
            reconciliation,
            self.analytics_store.ledger_summary(),
            trigger=str(payload.get("trigger") or "manual"),
        )
        if star_result.get("ok") is not True:
            return {
                "ok": False,
                "queued": False,
                "message": str(star_result.get("message") or "AI投資管家帳務服務失敗。"),
                "star_accounting": star_result,
                "ledger_reconciliation": reconciliation,
                "state": self._state_with_analytics(state),
            }

        applied = None
        safety_backup = None
        if star_result.get("apply_reconciliation") is True:
            approved_count = int(star_result.get("approved_action_count") or 0)
            difference_count = int(reconciliation.get("difference_count") or 0)
            if approved_count != difference_count or difference_count <= 0:
                raise ValueError("AI投資管家帳務核准數與本機對帳差異不一致，已拒絕寫入")
            with self._snapshot_coordinator() as locked_state:
                current_reconciliation = self.analytics_store.reconcile_ledger_holdings(
                    locked_state
                )
                approved_fingerprint = [
                    (
                        str(item.get("symbol") or ""),
                        float(item.get("difference_quantity") or 0),
                    )
                    for item in reconciliation.get("differences", [])
                    if isinstance(item, dict)
                ]
                current_fingerprint = [
                    (
                        str(item.get("symbol") or ""),
                        float(item.get("difference_quantity") or 0),
                    )
                    for item in current_reconciliation.get("differences", [])
                    if isinstance(item, dict)
                ]
                if current_fingerprint != approved_fingerprint:
                    raise ValueError("帳務資料在AI投資管家核准後已變更，已拒絕過期決策")
                safety_backup = self.analytics_store.backup_database(
                    "before-star-accounting"
                )
                applied = self.analytics_store.apply_ledger_reconciliation(
                    locked_state,
                    confirmed=True,
                )
                state = locked_state
                self._invalidate_v3_snapshot()

        self.analytics_store.audit(
            "star_accounting_completed",
            {
                "decision": star_result.get("decision"),
                "difference_count": reconciliation.get("difference_count", 0),
                "applied_count": (applied or {}).get("applied_count", 0),
                "trigger": str(payload.get("trigger") or "manual"),
            },
        )
        final_reconciliation = applied or reconciliation
        response = self._state_response(state)
        response.update(
            {
                "ok": True,
                "queued": False,
                "message": str(star_result.get("message") or "AI投資管家帳務已完成。"),
                "star_accounting": star_result,
                "ledger_reconciliation": final_reconciliation,
                "safety_backup": safety_backup,
            }
        )
        return response

    def _schedule_star_accounting_background(
        self,
        *,
        trigger: str,
    ) -> dict[str, Any]:
        if self.ai_connections is None or not bool(
            getattr(self.ai_connections, "is_configured", False)
        ):
            return {
                "ok": False,
                "queued": False,
                "error_code": "STAR_AI_CHANNEL_NOT_CONNECTED",
            }
        current = getattr(self, "_star_accounting_task", None)
        if current is not None and not current.done():
            return {"ok": True, "queued": True, "coalesced": True}
        task = asyncio.create_task(self._run_star_accounting({"trigger": trigger}))
        self._star_accounting_task = task
        self._background_jobs.add(task)
        task.add_done_callback(self._background_job_finished)
        return {"ok": True, "queued": True, "coalesced": False}
