from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class LocalAiRepository:
    SEARCH_LOG_RETENTION_DAYS = 7
    MAX_SEARCH_LOGS = 1000

    def __init__(self, tool_root: Path) -> None:
        self.database_path = Path(tool_root) / "runtime" / "state" / "local-ai.sqlite3"
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inference_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS instrument_identity (
                    identity_key TEXT PRIMARY KEY,
                    requested_symbol TEXT NOT NULL DEFAULT '',
                    requested_name TEXT NOT NULL DEFAULT '',
                    resolved_symbol TEXT NOT NULL DEFAULT '',
                    official_code TEXT NOT NULL DEFAULT '',
                    isin TEXT NOT NULL DEFAULT '',
                    market TEXT NOT NULL DEFAULT '',
                    asset_type TEXT NOT NULL DEFAULT '',
                    currency TEXT NOT NULL DEFAULT '',
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_observation (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    identity_key TEXT NOT NULL,
                    parameter_key TEXT NOT NULL,
                    numeric_value REAL,
                    text_value TEXT,
                    unit TEXT NOT NULL DEFAULT '',
                    currency TEXT NOT NULL DEFAULT '',
                    observed_at TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(identity_key, parameter_key, observed_at, source_url)
                );
                CREATE INDEX IF NOT EXISTS idx_market_observation_latest
                    ON market_observation(identity_key, parameter_key, observed_at DESC);
                CREATE TABLE IF NOT EXISTS distribution_event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    identity_key TEXT NOT NULL,
                    ex_date TEXT NOT NULL DEFAULT '',
                    record_date TEXT NOT NULL DEFAULT '',
                    payment_date TEXT NOT NULL DEFAULT '',
                    amount_per_unit REAL,
                    currency TEXT NOT NULL DEFAULT '',
                    frequency TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    event_fingerprint TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_distribution_event_latest
                    ON distribution_event(identity_key, observed_at DESC);
                CREATE TABLE IF NOT EXISTS investment_parameter_definition (
                    parameter_key TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    value_type TEXT NOT NULL,
                    unit TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_search_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query_type TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    request_hash TEXT NOT NULL DEFAULT '',
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    last_seen_at TEXT NOT NULL DEFAULT ''
                );
                """
            )
            self._migrate_and_compact(connection)
            self._seed_parameter_definitions(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _request_hash(request: dict[str, Any] | str) -> str:
        if isinstance(request, str):
            try:
                value = json.loads(request)
            except json.JSONDecodeError:
                value = request
        else:
            value = request
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _event_fingerprint(identity_key: str, event: dict[str, Any]) -> str:
        event_date = next(
            (
                str(event.get(key) or "").strip()
                for key in ("ex_date", "record_date", "payment_date", "observed_at")
                if str(event.get(key) or "").strip()
            ),
            "",
        )
        amount = event.get("amount_per_unit")
        semantic = {
            "identity_key": identity_key.strip(),
            "event_date": event_date,
            "amount_per_unit": amount if isinstance(amount, (int, float)) else None,
            "currency": str(event.get("currency") or "").strip().upper(),
            "frequency": str(event.get("frequency") or "").strip().lower(),
            "title": " ".join(str(event.get("title") or "").split()).casefold(),
            "source_url": str(event.get("source_url") or "").strip(),
        }
        encoded = json.dumps(
            semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _meaningful_distribution_event(event: dict[str, Any]) -> bool:
        return bool(
            any(
                str(event.get(key) or "").strip()
                for key in ("ex_date", "record_date", "payment_date", "title")
            )
            or isinstance(event.get("amount_per_unit"), (int, float))
        )

    @staticmethod
    def _search_response_summary(response: dict[str, Any]) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for item in response.get("results", [])[:50]:
            if not isinstance(item, dict):
                continue
            sources = [
                {
                    "name": str(source.get("name") or ""),
                    "url": str(source.get("url") or ""),
                    "observed_at": str(source.get("observed_at") or ""),
                }
                for source in item.get("sources", [])
                if isinstance(source, dict)
            ][:3]
            results.append(
                {
                    "identity_key": str(item.get("identity_key") or ""),
                    "requested_symbol": str(item.get("requested_symbol") or ""),
                    "resolved_symbol": str(item.get("resolved_symbol") or ""),
                    "official_code": str(item.get("official_code") or ""),
                    "market": str(item.get("market") or ""),
                    "asset_type": str(item.get("asset_type") or ""),
                    "currency": str(item.get("currency") or ""),
                    "observed_at": str(item.get("observed_at") or ""),
                    "confidence": item.get("confidence"),
                    "parameters": item.get("parameters") or {},
                    "distribution": item.get("distribution") or {},
                    "sources": sources,
                }
            )
        return {
            "ok": response.get("ok") is True,
            "searched_at": str(response.get("searched_at") or ""),
            "provider": str(response.get("provider") or ""),
            "requested_count": int(response.get("requested_count") or 0),
            "updated_count": int(response.get("updated_count") or 0),
            "error_count": int(response.get("error_count") or 0),
            "results": results,
            "errors": [item for item in response.get("errors", []) if isinstance(item, dict)][:50],
        }

    def _migrate_and_compact(self, connection: sqlite3.Connection) -> dict[str, int]:
        before_distribution = int(
            connection.execute("SELECT COUNT(*) FROM distribution_event").fetchone()[0]
        )
        before_search = int(
            connection.execute("SELECT COUNT(*) FROM web_search_log").fetchone()[0]
        )
        if "event_fingerprint" not in self._columns(connection, "distribution_event"):
            connection.execute(
                "ALTER TABLE distribution_event ADD COLUMN event_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        search_columns = self._columns(connection, "web_search_log")
        if "request_hash" not in search_columns:
            connection.execute(
                "ALTER TABLE web_search_log ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''"
            )
        if "occurrence_count" not in search_columns:
            connection.execute(
                "ALTER TABLE web_search_log ADD COLUMN occurrence_count INTEGER NOT NULL DEFAULT 1"
            )
        if "last_seen_at" not in search_columns:
            connection.execute(
                "ALTER TABLE web_search_log ADD COLUMN last_seen_at TEXT NOT NULL DEFAULT ''"
            )

        connection.execute(
            """
            DELETE FROM distribution_event
            WHERE trim(ex_date) = '' AND trim(record_date) = ''
              AND trim(payment_date) = '' AND amount_per_unit IS NULL
              AND trim(title) = ''
            """
        )
        rows = connection.execute(
            """
            SELECT id, identity_key, ex_date, record_date, payment_date,
                   amount_per_unit, currency, frequency, title, source_url, observed_at
            FROM distribution_event
            """
        ).fetchall()
        for row in rows:
            event = {
                "ex_date": row[2],
                "record_date": row[3],
                "payment_date": row[4],
                "amount_per_unit": row[5],
                "currency": row[6],
                "frequency": row[7],
                "title": row[8],
                "source_url": row[9],
                "observed_at": row[10],
            }
            connection.execute(
                "UPDATE distribution_event SET event_fingerprint = ? WHERE id = ?",
                (self._event_fingerprint(str(row[1]), event), int(row[0])),
            )
        connection.execute(
            """
            DELETE FROM distribution_event
            WHERE event_fingerprint <> ''
              AND id NOT IN (
                  SELECT MIN(id) FROM distribution_event
                  WHERE event_fingerprint <> '' GROUP BY event_fingerprint
              )
            """
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_distribution_event_fingerprint ON distribution_event(event_fingerprint) WHERE event_fingerprint <> ''"
        )

        search_rows = connection.execute(
            """
            SELECT id, query_type, request_json, response_json, created_at,
                   occurrence_count, last_seen_at
            FROM web_search_log ORDER BY id
            """
        ).fetchall()
        grouped: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
        for row in search_rows:
            request_hash = self._request_hash(str(row[2]))
            grouped.setdefault((str(row[1]), request_hash), []).append(row)
        for (_query_type, request_hash), duplicates in grouped.items():
            keeper = duplicates[-1]
            try:
                raw_response = json.loads(str(keeper[3]))
            except json.JSONDecodeError:
                raw_response = {}
            summary = self._search_response_summary(
                raw_response if isinstance(raw_response, dict) else {}
            )
            occurrence_count = sum(max(1, int(row[5] or 1)) for row in duplicates)
            created_at = min(str(row[4] or "") for row in duplicates)
            last_seen_at = max(str(row[6] or row[4] or "") for row in duplicates)
            keeper_id = int(keeper[0])
            connection.execute(
                """
                UPDATE web_search_log
                SET response_json = ?, request_hash = ?, occurrence_count = ?,
                    created_at = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                    request_hash,
                    occurrence_count,
                    created_at,
                    last_seen_at,
                    keeper_id,
                ),
            )
            if len(duplicates) > 1:
                connection.executemany(
                    "DELETE FROM web_search_log WHERE id = ?",
                    [(int(row[0]),) for row in duplicates[:-1]],
                )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_web_search_request_hash ON web_search_log(query_type, request_hash) WHERE request_hash <> ''"
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.SEARCH_LOG_RETENTION_DAYS)).isoformat()
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
        after_distribution = int(
            connection.execute("SELECT COUNT(*) FROM distribution_event").fetchone()[0]
        )
        after_search = int(
            connection.execute("SELECT COUNT(*) FROM web_search_log").fetchone()[0]
        )
        return {
            "distribution_events_removed": before_distribution - after_distribution,
            "search_logs_removed": before_search - after_search,
        }

    def record(self, model: str, request: dict[str, Any], response: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO inference_log(model, request_json, response_json) VALUES (?, ?, ?)",
                (
                    model,
                    json.dumps(request, ensure_ascii=False),
                    json.dumps(response, ensure_ascii=False),
                ),
            )

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _seed_parameter_definitions(self, connection: sqlite3.Connection) -> None:
        now = self._utc_now()
        definitions = (
            ("price", "valuation", "市價／淨值", "number", "currency", "最近可驗證的市價或基金淨值"),
            ("previous_close", "valuation", "前收／前次淨值", "number", "currency", "前一有效觀測值"),
            ("change_percent", "return", "漲跌幅", "number", "%", "相對前一有效觀測值的變動"),
            ("ytd_return_percent", "return", "年初至今報酬", "number", "%", "公開來源揭露的年初至今變動"),
            ("distribution_amount", "income", "每單位配息", "number", "currency", "已公告的每單位現金分配"),
            ("distribution_frequency", "income", "配息頻率", "text", "", "公開來源揭露或由歷史事件推估"),
            ("annual_distribution_per_unit", "income", "近一年每單位配息", "number", "currency", "近 366 日現金分配合計"),
            ("distribution_yield_percent", "income", "近一年配息率", "number", "%", "近一年每單位配息除以最近價格"),
            ("risk_level", "risk", "風險等級", "text", "", "公開來源揭露的風險等級"),
            ("volatility_percent", "risk", "波動率", "number", "%", "依可用價格序列計算"),
            ("management_fee_percent", "fee", "經理費", "number", "%", "公開資料揭露的管理費率"),
            ("custody_fee_percent", "fee", "保管費", "number", "%", "公開資料揭露的保管費率"),
            ("fund_size", "profile", "基金規模", "number", "currency", "公開資料揭露的基金規模"),
            ("region_exposure", "exposure", "區域曝險", "json", "%", "公開資料揭露的區域配置"),
            ("industry_exposure", "exposure", "產業曝險", "json", "%", "公開資料揭露的產業配置"),
        )
        connection.executemany(
            """
            INSERT INTO investment_parameter_definition(
                parameter_key, category, display_name, value_type, unit, description, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(parameter_key) DO UPDATE SET
                category=excluded.category,
                display_name=excluded.display_name,
                value_type=excluded.value_type,
                unit=excluded.unit,
                description=excluded.description,
                updated_at=excluded.updated_at
            """,
            [(*item, now) for item in definitions],
        )

    def record_market_search(
        self,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
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

    def database_status(self) -> dict[str, Any]:
        with self._connect() as connection:
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "instrument_identity",
                    "market_observation",
                    "distribution_event",
                    "investment_parameter_definition",
                    "web_search_log",
                )
            }
            blank_distribution_events = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM distribution_event
                    WHERE trim(ex_date) = '' AND trim(record_date) = ''
                      AND trim(payment_date) = '' AND amount_per_unit IS NULL
                      AND trim(title) = ''
                    """
                ).fetchone()[0]
            )
            duplicate_distribution_events = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(total - 1), 0) FROM (
                        SELECT COUNT(*) AS total FROM distribution_event
                        WHERE event_fingerprint <> '' GROUP BY event_fingerprint
                        HAVING COUNT(*) > 1
                    )
                    """
                ).fetchone()[0]
            )
            max_search_response_chars = int(
                connection.execute(
                    "SELECT COALESCE(MAX(length(response_json)), 0) FROM web_search_log"
                ).fetchone()[0]
            )
            search_payload_chars = int(
                connection.execute(
                    "SELECT COALESCE(SUM(length(request_json) + length(response_json)), 0) FROM web_search_log"
                ).fetchone()[0]
            )
            search_occurrences = int(
                connection.execute(
                    "SELECT COALESCE(SUM(occurrence_count), 0) FROM web_search_log"
                ).fetchone()[0]
            )
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        return {
            "path": str(self.database_path),
            "tables": counts,
            "size_bytes": page_count * page_size,
            "quality": {
                "blank_distribution_events": blank_distribution_events,
                "duplicate_distribution_events": duplicate_distribution_events,
                "search_request_occurrences": search_occurrences,
                "search_payload_chars": search_payload_chars,
                "max_search_response_chars": max_search_response_chars,
                "search_log_retention_days": self.SEARCH_LOG_RETENTION_DAYS,
                "search_log_limit": self.MAX_SEARCH_LOGS,
            },
        }

    def repair_data(self, *, vacuum: bool = True) -> dict[str, Any]:
        with self._connect() as connection:
            changes = self._migrate_and_compact(connection)
        vacuumed = False
        if vacuum:
            with self._connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
                vacuumed = True
        return {**changes, "vacuumed": vacuumed, "database": self.database_status()}
