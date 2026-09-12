from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any


class MarketMixin:
    """Market search recording for instrument identity, observations, and distributions."""

    def record_market_search(
        self,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        if self.database_scope != "main":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        retrieved_at = str(response.get("searched_at") or self._utc_now())
        request_json = json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        request_hash = self._request_hash(request)
        response_summary = json.dumps(
            self._search_response_summary(response),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO web_search_log(
                    query_type, request_json, response_json, ok, created_at,
                    request_hash, occurrence_count, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(query_type, request_hash) WHERE request_hash <> '' DO UPDATE SET
                    request_json=excluded.request_json,
                    response_json=excluded.response_json,
                    ok=excluded.ok,
                    occurrence_count=web_search_log.occurrence_count + 1,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    "investment_market_search",
                    request_json,
                    response_summary,
                    int(response.get("ok") is True),
                    retrieved_at,
                    request_hash,
                    retrieved_at,
                ),
            )
            for result in response.get("results", []):
                if not isinstance(result, dict):
                    continue
                identity_key = str(result.get("identity_key") or "").strip()
                if not identity_key:
                    continue
                sources = [
                    item for item in result.get("sources", []) if isinstance(item, dict)
                ]
                primary_source = sources[0] if sources else {}
                connection.execute(
                    """
                    INSERT INTO instrument_identity(
                        identity_key, requested_symbol, requested_name, resolved_symbol,
                        official_code, isin, market, asset_type, currency,
                        source_name, source_url, confidence, parameters_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(identity_key) DO UPDATE SET
                        requested_symbol=excluded.requested_symbol,
                        requested_name=excluded.requested_name,
                        resolved_symbol=excluded.resolved_symbol,
                        official_code=excluded.official_code,
                        isin=excluded.isin,
                        market=excluded.market,
                        asset_type=excluded.asset_type,
                        currency=excluded.currency,
                        source_name=excluded.source_name,
                        source_url=excluded.source_url,
                        confidence=excluded.confidence,
                        parameters_json=excluded.parameters_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        identity_key,
                        str(result.get("requested_symbol") or ""),
                        str(result.get("requested_name") or ""),
                        str(result.get("resolved_symbol") or ""),
                        str(result.get("official_code") or ""),
                        str(result.get("isin") or ""),
                        str(result.get("market") or ""),
                        str(result.get("asset_type") or ""),
                        str(result.get("currency") or ""),
                        str(primary_source.get("name") or ""),
                        str(primary_source.get("url") or ""),
                        float(result.get("confidence") or 0),
                        json.dumps(result.get("parameters") or {}, ensure_ascii=False),
                        retrieved_at,
                    ),
                )
                parameters = result.get("parameters") or {}
                if isinstance(parameters, dict):
                    for key, value in parameters.items():
                        numeric_value = float(value) if isinstance(value, (int, float)) else None
                        text_value = None if numeric_value is not None else json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value or "")
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO market_observation(
                                identity_key, parameter_key, numeric_value, text_value,
                                unit, currency, observed_at, retrieved_at,
                                source_name, source_url, confidence, raw_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                identity_key,
                                str(key),
                                numeric_value,
                                text_value,
                                str((result.get("parameter_units") or {}).get(key) or ""),
                                str(result.get("currency") or ""),
                                str(result.get("observed_at") or retrieved_at),
                                retrieved_at,
                                str(primary_source.get("name") or ""),
                                str(primary_source.get("url") or ""),
                                float(result.get("confidence") or 0),
                                json.dumps(result, ensure_ascii=False),
                            ),
                        )
                for event in result.get("distribution_events", []):
                    if not isinstance(event, dict) or not self._meaningful_distribution_event(event):
                        continue
                    event = dict(event)
                    if not event.get("source_url"):
                        event["source_url"] = primary_source.get("url") or ""
                    if not event.get("currency"):
                        event["currency"] = result.get("currency") or ""
                    event_fingerprint = self._event_fingerprint(identity_key, event)
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO distribution_event(
                            identity_key, ex_date, record_date, payment_date,
                            amount_per_unit, currency, frequency, title,
                            source_name, source_url, observed_at, retrieved_at, raw_json,
                            event_fingerprint
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            identity_key,
                            str(event.get("ex_date") or ""),
                            str(event.get("record_date") or ""),
                            str(event.get("payment_date") or ""),
                            event.get("amount_per_unit"),
                            str(event.get("currency") or result.get("currency") or ""),
                            str(event.get("frequency") or ""),
                            str(event.get("title") or ""),
                            str(event.get("source_name") or primary_source.get("name") or ""),
                            str(event.get("source_url") or primary_source.get("url") or ""),
                            str(event.get("observed_at") or event.get("record_date") or retrieved_at),
                            retrieved_at,
                            json.dumps(event, ensure_ascii=False),
                            event_fingerprint,
                        ),
                    )

            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=self.SEARCH_LOG_RETENTION_DAYS)
            ).isoformat()
            connection.execute(
                "DELETE FROM web_search_log WHERE COALESCE(NULLIF(last_seen_at, ''), created_at) < ?",
                (cutoff,),
            )
            connection.execute(
                """
                DELETE FROM web_search_log WHERE id NOT IN (
                    SELECT id FROM web_search_log
                    ORDER BY COALESCE(NULLIF(last_seen_at, ''), created_at) DESC
                    LIMIT ?
                )
                """,
                (self.MAX_SEARCH_LOGS,),
            )
