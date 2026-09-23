from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from .analytics_common import (
    _decoded_json,
    _json,
    normalized_probability,
    number,
    parse_datetime,
    protect_text,
    rounded,
    unprotect_text,
    utc_now,
    utc_text,
)

_DECISION_DIRECTION_ALIASES = {
    "up": "bullish",
    "positive": "bullish",
    "long": "bullish",
    "buy": "bullish",
    "down": "bearish",
    "negative": "bearish",
    "short": "bearish",
    "sell": "bearish",
    "flat": "neutral",
    "sideways": "neutral",
    "hold": "neutral",
    "monitor": "neutral",
}


class AnalyticsStoreDecisionsMixin:
    """Decision recording, outcome evaluation, and calibration inputs."""

    @staticmethod
    def _decision_prediction(
        action: dict[str, Any],
        assessment: dict[str, Any],
    ) -> dict[str, Any]:
        explicit = (
            action.get("prediction")
            if isinstance(action.get("prediction"), dict)
            else assessment.get("prediction")
            if isinstance(assessment.get("prediction"), dict)
            else {}
        )
        direction, explicitly_directional = (
            AnalyticsStoreDecisionsMixin._prediction_direction(action, explicit)
        )
        horizon_days, threshold = (
            AnalyticsStoreDecisionsMixin._prediction_window(
                action,
                explicit,
                direction,
            )
        )
        if "eligible_for_calibration" in explicit:
            eligible = bool(explicit.get("eligible_for_calibration"))
        else:
            # Inferred hold/monitor text is guidance, not a directional forecast.
            eligible = direction in {"bullish", "bearish"} or (
                direction == "neutral" and explicitly_directional
            )
        return {
            "direction": direction,
            "horizon_days": horizon_days,
            "return_threshold_percent": threshold,
            "eligible_for_calibration": bool(
                eligible and direction in {"bullish", "bearish", "neutral"}
            ),
            "source": "explicit" if explicitly_directional else "action_text_inference",
        }

    @staticmethod
    def _prediction_direction(
        action: dict[str, Any],
        explicit: dict[str, Any],
    ) -> tuple[str, bool]:
        supplied_direction = str(
            explicit.get("direction")
            or action.get("prediction_direction")
            or ""
        ).strip().lower()
        direction = _DECISION_DIRECTION_ALIASES.get(
            supplied_direction,
            supplied_direction,
        )
        explicitly_directional = direction in {"bullish", "bearish", "neutral"}
        if explicitly_directional:
            return direction, True
        return (
            AnalyticsStoreDecisionsMixin._inferred_action_direction(action),
            False,
        )

    @staticmethod
    def _inferred_action_direction(action: dict[str, Any]) -> str:
        action_text = " ".join(
            str(action.get(key) or "") for key in ("action", "title", "detail")
        ).lower()
        if any(
            token in action_text
            for token in (
                "buy", "add", "increase", "accumulate",
                "買進", "加碼", "增持", "看漲",
            )
        ):
            return "bullish"
        if any(
            token in action_text
            for token in (
                "sell", "reduce", "decrease", "exit",
                "賣出", "減碼", "清倉", "看跌",
            )
        ):
            return "bearish"
        if any(
            token in action_text
            for token in (
                "hold", "monitor", "observe", "wait",
                "持有", "觀察", "監控", "等待",
            )
        ):
            return "neutral"
        return "abstain"

    @staticmethod
    def _prediction_window(
        action: dict[str, Any],
        explicit: dict[str, Any],
        direction: str,
    ) -> tuple[int, float]:
        horizon_days = max(
            1,
            min(
                3650,
                int(
                    number(
                        explicit.get("horizon_days")
                        if explicit.get("horizon_days") is not None
                        else action.get("horizon_days"),
                        30,
                    )
                ),
            ),
        )
        threshold_default = 2.0 if direction == "neutral" else 0.0
        threshold = max(
            0.0,
            min(
                100.0,
                number(
                    explicit.get("return_threshold_percent")
                    if explicit.get("return_threshold_percent") is not None
                    else explicit.get("threshold_percent")
                    if explicit.get("threshold_percent") is not None
                    else action.get("return_threshold_percent"),
                    threshold_default,
                ),
            ),
        )
        return horizon_days, threshold

    def record_decisions(self, analysis: dict[str, Any]) -> int:
        command = analysis.get("command_result") if isinstance(analysis.get("command_result"), dict) else {}
        assessments = {
            str(item.get("symbol") or "").upper(): item
            for item in command.get("assessments", [])
            if isinstance(item, dict)
        }
        reports = {
            str(item.get("symbol") or "").upper(): item
            for item in analysis.get("holdings", [])
            if isinstance(item, dict)
        }
        created = parse_datetime(analysis.get("generated_at")) or utc_now()
        count = 0
        # G102: one durable transaction for the whole action plan instead of
        # a commit + durable persist per action.
        with self.batch_updates():
            rows = [
                self._decision_row(action, assessments, reports, command, created)
                for action in command.get("action_plan", [])
                if isinstance(action, dict)
            ]
            with self.connect() as connection:
                cursor = connection.executemany(
                    """
                    INSERT OR IGNORE INTO decisions(
                        decision_id, dedupe_key, created_at, symbol, action,
                        confidence, score, risk_level, reference_price,
                        evidence_encrypted, snapshot_encrypted, user_status,
                        outcome_due_at, outcome_encrypted, prediction_direction,
                        horizon_days, return_threshold_percent,
                        eligible_for_calibration
                    ) VALUES(
                        :decision_id, :dedupe_key, :created_at, :symbol, :action,
                        :confidence, :score, :risk_level, :reference_price,
                        :evidence_encrypted, :snapshot_encrypted, :user_status,
                        :outcome_due_at, :outcome_encrypted, :prediction_direction,
                        :horizon_days, :return_threshold_percent,
                        :eligible_for_calibration
                    )
                    """,
                    rows,
                )
            # executemany rowcount counts inserted rows (INSERT OR IGNORE skips dupes)
            count = max(int(cursor.rowcount), 0)
        return count

    def _decision_row(
        self,
        action: dict[str, Any],
        assessments: dict[str, dict[str, Any]],
        reports: dict[str, dict[str, Any]],
        command: dict[str, Any],
        created: datetime,
    ) -> dict[str, Any]:
        symbol = str(action.get("symbol") or "").upper()
        assessment = assessments.get(symbol, {})
        report = reports.get(symbol, {})
        quote = report.get("quote") if isinstance(report.get("quote"), dict) else {}
        action_text = str(action.get("action") or action.get("title") or "monitor")
        prediction = self._decision_prediction(action, assessment)
        dedupe_source = (
            f"{created.isoformat()[:16]}|{symbol}|{action_text}|"
            f"{prediction['direction']}|{prediction['horizon_days']}|"
            f"{prediction['return_threshold_percent']}"
        )
        dedupe_key = hashlib.sha256(dedupe_source.encode("utf-8")).hexdigest()
        confidence = assessment.get("confidence") if isinstance(assessment.get("confidence"), dict) else {}
        return {
            "decision_id": uuid.uuid4().hex,
            "dedupe_key": dedupe_key,
            "created_at": utc_text(created),
            "symbol": symbol,
            "action": action_text,
            "confidence": normalized_probability(confidence.get("score"), 0.5),
            "score": number(assessment.get("score")),
            "risk_level": str(assessment.get("risk_level") or ""),
            "reference_price": number(quote.get("price")) or None,
            "evidence_encrypted": protect_text(_json({"reasons": assessment.get("reasons", []), "risk_flags": assessment.get("risk_flags", []), "source": quote.get("provider"), "as_of": quote.get("as_of"), "prediction": prediction, "market": report.get("market"), "asset_type": report.get("asset_type")})),
            "snapshot_encrypted": protect_text(_json({"decision_brief": command.get("decision_brief"), "portfolio_score": command.get("portfolio_score"), "action": action, "prediction": prediction, "context": {"market": report.get("market"), "asset_type": report.get("asset_type")}})),
            "user_status": "pending",
            "outcome_due_at": utc_text(
                created + timedelta(days=prediction["horizon_days"])
            ),
            "outcome_encrypted": "",
            "prediction_direction": prediction["direction"],
            "horizon_days": prediction["horizon_days"],
            "return_threshold_percent": prediction["return_threshold_percent"],
            "eligible_for_calibration": int(prediction["eligible_for_calibration"]),
        }

    def update_decision_outcomes(self) -> int:
        now = utc_now()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT decision_id, dedupe_key, created_at, symbol, action, confidence, score, risk_level, reference_price, evidence_encrypted, snapshot_encrypted, user_status, outcome_due_at, outcome_encrypted, prediction_direction, horizon_days, return_threshold_percent, eligible_for_calibration FROM decisions WHERE outcome_encrypted = '' AND outcome_due_at <= ? LIMIT 500",
                (utc_text(now),),
            ).fetchall()
        updated = 0
        # G102: one durable transaction for the whole due-outcomes batch —
        # previously each row paid a commit + durable persist.
        evaluation_bars = self._evaluation_bars([str(r["decision_id"]) for r in rows])
        updates: list[tuple[str, str]] = []
        for row in rows:
            evaluation_bar = evaluation_bars.get(str(row["decision_id"]))
            outcome = self._decision_outcome(row, evaluation_bar, now)
            if outcome is None:
                continue
            updates.append((protect_text(_json(outcome)), row["decision_id"]))
        with self.batch_updates():
            if updates:
                with self.connect() as connection:
                    connection.executemany(
                        "UPDATE decisions SET outcome_encrypted = ? WHERE decision_id = ?",
                        updates,
                    )
        return len(updates)

    def _evaluation_bars(
        self, decision_ids: list[str]
    ) -> dict[str, sqlite3.Row]:
        """One query for every due decision's evaluation bar (G102: replaces
        the per-row prices lookup — window function picks the same first bar
        the old ORDER BY/LIMIT 1 did)."""
        if not decision_ids:
            return {}
        placeholders = ",".join("?" for _ in decision_ids)
        with self.connect() as connection:
            rows = connection.execute(  # sql-ok: code-controlled SQL composition
                f"""
                SELECT decision_id, close, observed_at, provider, verified
                FROM (
                    SELECT d.decision_id AS decision_id, p.close AS close,
                           p.observed_at AS observed_at, p.provider AS provider,
                           p.verified AS verified,
                           ROW_NUMBER() OVER (
                               PARTITION BY d.decision_id
                               ORDER BY p.observed_at ASC, p.verified DESC, p.provider
                           ) AS rn
                    FROM decisions d
                    JOIN prices p
                      ON p.symbol = d.symbol
                     AND p.observed_at >= d.outcome_due_at
                    WHERE d.decision_id IN ({placeholders})
                )
                WHERE rn = 1
                """,
                tuple(decision_ids),
            ).fetchall()
        return {str(row["decision_id"]): row for row in rows}

    def _decision_outcome(
        self,
        row: sqlite3.Row,
        evaluation_bar: sqlite3.Row | None,
        now: datetime,
    ) -> dict[str, Any] | None:
        if evaluation_bar is None:
            return None
        price = number(evaluation_bar["close"], -1)
        reference = number(row["reference_price"], -1)
        if price <= 0 or reference <= 0:
            return None
        return_percent = (price / reference - 1) * 100
        direction = str(row["prediction_direction"] or "abstain")
        threshold = max(0.0, number(row["return_threshold_percent"]))
        eligible = bool(row["eligible_for_calibration"])
        success: bool | None = None
        if eligible and direction == "bullish":
            success = return_percent >= threshold
        elif eligible and direction == "bearish":
            success = return_percent <= -threshold
        elif eligible and direction == "neutral":
            success = abs(return_percent) <= threshold
        return {
            "evaluated_at": utc_text(now),
            "price": price,
            "price_observed_at": str(evaluation_bar["observed_at"]),
            "price_provider": str(evaluation_bar["provider"]),
            "return_percent": rounded(return_percent, 2),
            "direction": direction,
            "horizon_days": int(row["horizon_days"]),
            "return_threshold_percent": threshold,
            "evaluable": success is not None,
            "success": success,
            "evaluation_status": (
                "evaluated" if success is not None else "not_a_directional_forecast"
            ),
        }

    def set_decision_status(self, decision_id: str, status: str) -> bool:
        normalized = status if status in {"pending", "accepted", "rejected", "executed"} else "pending"
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE decisions SET user_status = ? WHERE decision_id = ?",
                (normalized, decision_id),
            )
        return bool(cursor.rowcount)

    def decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT decision_id, dedupe_key, created_at, symbol, action, confidence, score, risk_level, reference_price, evidence_encrypted, snapshot_encrypted, user_status, outcome_due_at, outcome_encrypted FROM decisions ORDER BY created_at DESC LIMIT ?",
                (max(1, min(2000, limit)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = _decoded_json(unprotect_text(item.pop("evidence_encrypted", "")), {})
            item["snapshot"] = _decoded_json(unprotect_text(item.pop("snapshot_encrypted", "")), {})
            item["outcome"] = _decoded_json(unprotect_text(item.pop("outcome_encrypted", "")), {})
            result.append(item)
        return result


__all__ = ['AnalyticsStoreDecisionsMixin']
