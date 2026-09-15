from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from .analytics_common import (
    DEFAULT_ALERT_COOLDOWN_MINUTES,
    _decoded_json,
    _json,
    number,
    parse_datetime,
    rounded,
    utc_now,
    utc_text,
)


class AnalyticsStoreAlertsMixin:
    """Alert rule management and evaluation methods."""

    def ensure_default_alerts(self) -> None:
        with self.connect() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0])
        if count:
            return
        for rule in (
            {"name": "單一部位超過 35%", "rule_type": "concentration", "operator": ">=", "threshold": 35, "severity": "warning"},
            {"name": "單日 VaR 超過 5%", "rule_type": "var", "operator": ">=", "threshold": 5, "severity": "critical"},
            {"name": "七日內重大事件", "rule_type": "event_window", "operator": "<=", "threshold": 7, "severity": "info"},
        ):
            self.add_alert_rule(rule)

    def add_alert_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        rule_id = str(payload.get("rule_id") or uuid.uuid4().hex)
        now = utc_text()
        row = {
            "rule_id": rule_id,
            "name": str(payload.get("name") or payload.get("rule_type") or "告警規則"),
            "rule_type": str(payload.get("rule_type") or "price_below"),
            "symbol": str(payload.get("symbol") or "").upper(),
            "operator": str(payload.get("operator") or ">="),
            "threshold": number(payload.get("threshold")) if payload.get("threshold") is not None else None,
            "severity": str(payload.get("severity") or "warning"),
            "cooldown_minutes": max(1, int(number(payload.get("cooldown_minutes"), DEFAULT_ALERT_COOLDOWN_MINUTES))),
            "enabled": int(bool(payload.get("enabled", True))),
            "config_json": _json(payload.get("config") or {}),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO alert_rules VALUES(
                    :rule_id, :name, :rule_type, :symbol, :operator, :threshold,
                    :severity, :cooldown_minutes, :enabled, :config_json,
                    :created_at, :updated_at
                ) ON CONFLICT(rule_id) DO UPDATE SET
                    name=excluded.name, rule_type=excluded.rule_type, symbol=excluded.symbol,
                    operator=excluded.operator, threshold=excluded.threshold,
                    severity=excluded.severity, cooldown_minutes=excluded.cooldown_minutes,
                    enabled=excluded.enabled, config_json=excluded.config_json,
                    updated_at=excluded.updated_at
                """,
                row,
            )
        return {**row, "enabled": bool(row["enabled"]), "config": _decoded_json(row["config_json"], {})}

    def list_alert_rules(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT rule_id, name, enabled, config_json, created_at, updated_at FROM alert_rules ORDER BY created_at LIMIT 500"
            ).fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"]), "config": _decoded_json(row["config_json"], {})} for row in rows]

    @staticmethod
    def _compare(value: float, operator: str, threshold: float) -> bool:
        return {
            ">": value > threshold,
            ">=": value >= threshold,
            "<": value < threshold,
            "<=": value <= threshold,
            "==": abs(value - threshold) < 1e-9,
        }.get(operator, False)

    def evaluate_alerts(self, state: dict[str, Any], risk: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        positions = self.current_positions(state)
        position_map = {str(item.get("symbol") or ""): item for item in positions}
        risk_data = risk or self.risk(state)
        now = utc_now()
        triggered: list[dict[str, Any]] = []
        events = self.list_events(500)
        for rule in self.list_alert_rules():
            if not rule.get("enabled"):
                continue
            evaluated = self._alert_rule_value(
                rule,
                positions,
                position_map,
                risk_data,
                events,
                now,
            )
            if evaluated is None:
                continue
            value, detail = evaluated
            event = self._trigger_alert_rule(rule, value, detail, now)
            if event is not None:
                triggered.append(event)
        return triggered

    def _alert_rule_value(
        self,
        rule: dict[str, Any],
        positions: list[dict[str, Any]],
        position_map: dict[str, dict[str, Any]],
        risk_data: dict[str, Any],
        events: list[dict[str, Any]],
        now: datetime,
    ) -> tuple[float, str] | None:
        rule_type = str(rule.get("rule_type") or "")
        threshold = number(rule.get("threshold"))
        value: float | None = None
        detail = ""
        if rule_type in {"price_below", "price_above"}:
            position = position_map.get(str(rule.get("symbol") or ""))
            value = number(position.get("price")) if position else None
        elif rule_type == "concentration":
            candidates = [number(item.get("weight_percent")) for item in positions]
            value = max(candidates) if candidates else None
        elif rule_type == "var":
            value = risk_data.get("var_95_one_day_percent")
        elif rule_type == "drawdown":
            value = abs(number(risk_data.get("max_drawdown_percent")))
        elif rule_type == "event_window":
            upcoming = []
            for event in events:
                scheduled = parse_datetime(event.get("scheduled_at"))
                if scheduled is None:
                    continue
                days = (scheduled - now).total_seconds() / 86400
                if 0 <= days <= threshold and (not rule.get("symbol") or rule.get("symbol") == event.get("symbol")):
                    upcoming.append((days, event))
            if upcoming:
                upcoming.sort(key=lambda item: item[0])
                value = upcoming[0][0]
                detail = str(upcoming[0][1].get("title") or "")
        if value is None:
            return None
        return float(value), detail

    def _trigger_alert_rule(
        self,
        rule: dict[str, Any],
        value: float,
        detail: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        rule_type = str(rule.get("rule_type") or "")
        threshold = number(rule.get("threshold"))
        operator = str(rule.get("operator") or ">=")
        condition = value <= threshold if rule_type == "event_window" else self._compare(float(value), operator, threshold)
        if not condition:
            return None
        bucket_minutes = max(1, int(rule.get("cooldown_minutes") or DEFAULT_ALERT_COOLDOWN_MINUTES))
        with self.connect() as connection:
            previous = connection.execute(
                "SELECT triggered_at FROM alert_events WHERE rule_id = ? ORDER BY triggered_at DESC LIMIT 1",
                (rule["rule_id"],),
            ).fetchone()
        previous_at = parse_datetime(previous["triggered_at"]) if previous else None
        if previous_at is not None and (now - previous_at).total_seconds() < bucket_minutes * 60:
            return None
        bucket = int(now.timestamp() // (bucket_minutes * 60))
        dedupe_key = f"{rule['rule_id']}:{bucket}"
        event = {
            "alert_event_id": uuid.uuid4().hex,
            "rule_id": rule["rule_id"],
            "dedupe_key": dedupe_key,
            "triggered_at": utc_text(now),
            "severity": rule.get("severity") or "warning",
            "title": rule.get("name") or rule_type,
            "detail": detail or f"目前值 {rounded(float(value), 4)}，條件 {operator} {threshold}",
            "value": float(value),
            "acknowledged_at": "",
        }
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO alert_events VALUES(:alert_event_id, :rule_id, :dedupe_key, :triggered_at, :severity, :title, :detail, :value, :acknowledged_at)",
                event,
            )
        return event if cursor.rowcount else None

    def list_alert_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT alert_event_id, rule_id, dedupe_key, triggered_at, severity, title, detail, value, acknowledged_at FROM alert_events ORDER BY triggered_at DESC LIMIT ?",
                (max(1, min(2000, limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def acknowledge_alert(self, alert_event_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE alert_events SET acknowledged_at = ? WHERE alert_event_id = ?",
                (utc_text(), alert_event_id),
            )
        return bool(cursor.rowcount)


__all__ = ['AnalyticsStoreAlertsMixin']
