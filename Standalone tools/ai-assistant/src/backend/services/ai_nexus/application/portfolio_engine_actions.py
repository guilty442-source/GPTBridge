from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from ..infrastructure.analytics_repository import (
    number,
    parse_datetime,
    utc_now,
    utc_text,
)
from ..infrastructure.privacy import protect_text, unprotect_text


class PortfolioEngineActionsMixin:
    def add_corporate_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        symbol = str(payload.get("symbol") or "").strip().upper()
        action_type = str(payload.get("action_type") or payload.get("event_type") or "").strip().lower()
        effective = parse_datetime(payload.get("effective_at") or payload.get("scheduled_at")) or utc_now()
        if not symbol or action_type not in {"dividend", "split", "symbol_change", "merger", "delisting", "fund_distribution"}:
            raise ValueError("invalid corporate action")
        ratio = number(payload.get("ratio")) if payload.get("ratio") is not None else None
        cash_amount = number(payload.get("cash_amount")) if payload.get("cash_amount") is not None else None
        issues = self._corporate_action_issues(action_type, ratio, cash_amount)
        row = self._corporate_action_row(payload, symbol, action_type, effective, ratio, cash_amount, issues)
        stored, existing = self._store_corporate_action(row, issues)
        self.store.audit("corporate_action_ingested", {"symbol": symbol, "action_type": action_type, "status": row["status"], "created": not bool(existing)})
        public = dict(stored or row)
        public["details"] = unprotect_text(public.pop("details_encrypted"))
        public["issues"] = issues
        public["created"] = not bool(existing)
        return public

    @staticmethod
    def _corporate_action_issues(
        action_type: str,
        ratio: float | None,
        cash_amount: float | None,
    ) -> list[str]:
        issues = []
        if action_type == "split" and (ratio is None or ratio <= 0):
            issues.append("拆併股比例必須大於零")
        if action_type in {"dividend", "fund_distribution"} and (cash_amount is None or cash_amount < 0):
            issues.append("現金金額不可小於零")
        return issues

    @staticmethod
    def _corporate_action_row(
        payload: dict[str, Any],
        symbol: str,
        action_type: str,
        effective: Any,
        ratio: float | None,
        cash_amount: float | None,
        issues: list[str],
    ) -> dict[str, Any]:
        dedupe_source = f"{symbol}|{action_type}|{utc_text(effective)[:10]}|{ratio}|{cash_amount}"
        return {
            "action_id": uuid.uuid4().hex,
            "dedupe_key": hashlib.sha256(dedupe_source.encode("utf-8")).hexdigest(),
            "symbol": symbol,
            "action_type": action_type,
            "effective_at": utc_text(effective),
            "ratio": ratio,
            "cash_amount": cash_amount,
            "currency": str(payload.get("currency") or "").upper(),
            "old_symbol": str(payload.get("old_symbol") or "").upper(),
            "new_symbol": str(payload.get("new_symbol") or "").upper(),
            "source": str(payload.get("source") or "manual"),
            "confidence": max(0.0, min(1.0, number(payload.get("confidence"), 0.5))),
            "status": "needs_correction" if issues else "pending_review",
            "details_encrypted": protect_text(json.dumps(payload.get("details") or {}, ensure_ascii=False, default=str)),
            "created_at": utc_text(),
            "reviewed_at": "",
        }

    def _store_corporate_action(
        self,
        row: dict[str, Any],
        issues: list[str],
    ) -> tuple[Any, Any]:
        with self.store.connect() as connection:
            existing = connection.execute(
                "SELECT action_id FROM corporate_actions WHERE dedupe_key=?",
                (row["dedupe_key"],),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO corporate_actions VALUES(
                    :action_id, :dedupe_key, :symbol, :action_type, :effective_at,
                    :ratio, :cash_amount, :currency, :old_symbol, :new_symbol,
                    :source, :confidence, :status, :details_encrypted, :created_at, :reviewed_at
                ) ON CONFLICT(dedupe_key) DO UPDATE SET
                    source=excluded.source, confidence=MAX(corporate_actions.confidence, excluded.confidence)
                """,
                row,
            )
            if issues and not existing:
                connection.executemany(
                    "INSERT INTO data_quality_issues VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (uuid.uuid4().hex, utc_text(), "corporate_action_validation",
                         row["symbol"], "critical", "企業行動資料異常",
                         protect_text(issue), "open", "")
                        for issue in issues
                    ],
                )
            stored = connection.execute(
                "SELECT action_id, dedupe_key, symbol, action_type, effective_at, ratio, cash_amount, currency, old_symbol, new_symbol, source, confidence, status, details_encrypted, created_at, reviewed_at FROM corporate_actions WHERE dedupe_key=?",
                (row["dedupe_key"],),
            ).fetchone()
        return stored, existing

    def review_corporate_action(self, action_id: str, status: str) -> bool:
        normalized = status if status in {"approved", "rejected", "pending_review", "needs_correction"} else "pending_review"
        with self.store.connect() as connection:
            current = connection.execute(
                "SELECT symbol, status FROM corporate_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if current and normalized == "approved" and current["status"] == "needs_correction":
                raise ValueError("資料品質問題尚未修正，不能核准公司行動")
            cursor = connection.execute(
                "UPDATE corporate_actions SET status = ?, reviewed_at = ? WHERE action_id = ?",
                (normalized, utc_text(), action_id),
            )
            if current and normalized in {"approved", "rejected"}:
                connection.execute(
                    "UPDATE data_quality_issues SET status='resolved', resolved_at=? WHERE symbol=? AND issue_type='corporate_action_validation' AND status='open'",
                    (utc_text(), current["symbol"]),
                )
        if cursor.rowcount:
            self.store.audit("corporate_action_reviewed", {"action_id": action_id, "status": normalized})
        return bool(cursor.rowcount)

    def corporate_action_governance(self) -> dict[str, Any]:
        with self.store.connect() as connection:
            action_rows = connection.execute("SELECT action_id, dedupe_key, symbol, action_type, effective_at, ratio, cash_amount, currency, old_symbol, new_symbol, source, confidence, status, details_encrypted, created_at, reviewed_at FROM corporate_actions ORDER BY effective_at DESC LIMIT 500").fetchall()
            issue_rows = connection.execute("SELECT issue_id, created_at, issue_type, symbol, severity, title, detail_encrypted, status, resolved_at FROM data_quality_issues ORDER BY created_at DESC LIMIT 500").fetchall()
        actions = []
        for row in action_rows:
            item = dict(row)
            details = unprotect_text(str(item.pop("details_encrypted", "") or ""))
            try:
                item["details"] = json.loads(details)
            except (TypeError, ValueError, json.JSONDecodeError):
                item["details"] = details
            actions.append(item)
        issues = []
        for row in issue_rows:
            item = dict(row)
            item["detail"] = unprotect_text(str(item.pop("detail_encrypted", "") or ""))
            issues.append(item)
        return {
            "status": "attention" if any(item.get("status") == "needs_correction" for item in actions) else "ready",
            "actions": actions,
            "issues": issues,
            "pending_count": sum(1 for item in actions if item.get("status") in {"pending_review", "needs_correction"}),
            "approved_count": sum(1 for item in actions if item.get("status") == "approved"),
            "policy": "raw transactions and holdings are never changed before human approval",
        }

    def snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": "1.0.0",
            "generated_at": utc_text(),
            "multi_currency": self.multi_currency_accounting(state),
            "regime": self.regime_detection(state),
            "factors": self.factor_attribution(state),
            "corporate_actions": self.corporate_action_governance(),
        }
