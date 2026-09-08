from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .generative_language_model import MIN_TRAINING_GROUNDING_COVERAGE
from .local_command_parser import LocalCommandParser


class LocalAiRepository:
    SEARCH_LOG_RETENTION_DAYS = 7
    MAX_SEARCH_LOGS = 1000
    MAX_MODEL_MEMORIES = 500
    MAX_LANGUAGE_TRAINING_EXAMPLES = 500
    ADJUSTABLE_INVESTMENT_PARAMETERS = {
        "max_single_position_percent": (20.0, 1.0, 100.0),
        "missing_data_warning_percent": (5.0, 0.0, 100.0),
        "source_confidence_minimum": (0.72, 0.5, 1.0),
        "dividend_frequency_confidence_minimum": (0.55, 0.5, 1.0),
        "market_cache_seconds": (60.0, 30.0, 3600.0),
    }

    MODEL_DATABASE_SCOPES = frozenset({"main", "investment", "mathematical", "coding"})
    MODEL_ID_BY_SCOPE = {
        "main": "star-main-native-model",
        "investment": "star-investment-native-model",
        "mathematical": "star-mathematical-native-model",
        "coding": "star-coding-native-model",
    }

    def __init__(self, tool_root: Path, database_scope: str) -> None:
        scope = str(database_scope).strip().casefold()
        if scope not in self.MODEL_DATABASE_SCOPES:
            raise ValueError("a supported isolated model database scope is required")
        self.database_scope = scope
        self.owner_model_id = self.MODEL_ID_BY_SCOPE[scope]
        self.database_path = (
            Path(tool_root) / "xingcheng" / "runtime" / "state" / "models" / f"{scope}.sqlite3"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.command_parser = LocalCommandParser(self.database_path)
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
                CREATE TABLE IF NOT EXISTS common_command (
                    command_id TEXT PRIMARY KEY,
                    command_hash TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    command_text TEXT NOT NULL,
                    intent TEXT NOT NULL DEFAULT 'conversation',
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_common_command_usage
                    ON common_command(enabled, usage_count DESC, last_used_at DESC);
                CREATE TABLE IF NOT EXISTS command_tag (
                    tag_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    color TEXT NOT NULL DEFAULT 'slate',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS common_command_tag (
                    command_id TEXT NOT NULL,
                    tag_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(command_id, tag_id),
                    FOREIGN KEY(command_id) REFERENCES common_command(command_id),
                    FOREIGN KEY(tag_id) REFERENCES command_tag(tag_id)
                );
                CREATE TABLE IF NOT EXISTS model_memory (
                    memory_id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    business_scope TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_model_id TEXT NOT NULL,
                    broker_model_id TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    expires_at TEXT NOT NULL DEFAULT '',
                    review_status TEXT NOT NULL DEFAULT 'approved',
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    review_reason TEXT NOT NULL DEFAULT '',
                    revoked_at TEXT NOT NULL DEFAULT '',
                    provenance_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_model_memory_scope_time
                    ON model_memory(business_scope, updated_at DESC);
                CREATE TABLE IF NOT EXISTS language_training_example (
                    revision INTEGER PRIMARY KEY AUTOINCREMENT,
                    example_id TEXT NOT NULL UNIQUE,
                    content_hash TEXT NOT NULL UNIQUE,
                    intent TEXT NOT NULL,
                    input_text TEXT NOT NULL,
                    target_text TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    quality_score REAL NOT NULL,
                    validation_json TEXT NOT NULL DEFAULT '{}',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_language_training_active_revision
                    ON language_training_example(active, revision DESC);
                CREATE TABLE IF NOT EXISTS language_model_maintenance (
                    run_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS code_upgrade_proposal (
                    revision INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id TEXT NOT NULL UNIQUE,
                    target_path TEXT NOT NULL,
                    language TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    validation_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'proposed',
                    created_at TEXT NOT NULL,
                    UNIQUE(target_path, source_sha256)
                );
                CREATE TABLE IF NOT EXISTS capability_composition (
                    revision INTEGER PRIMARY KEY AUTOINCREMENT,
                    composition_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    blueprint_json TEXT NOT NULL,
                    model_assignments_json TEXT NOT NULL DEFAULT '{}',
                    votes_json TEXT NOT NULL DEFAULT '[]',
                    inspections_json TEXT NOT NULL DEFAULT '[]',
                    implementation_target TEXT NOT NULL DEFAULT '',
                    source_sha256 TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
                CREATE TABLE IF NOT EXISTS investment_model_definition (
                    model_key TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    version TEXT NOT NULL,
                    parameter_keys_json TEXT NOT NULL DEFAULT '[]',
                    description TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS investment_parameter_adjustment (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parameter_key TEXT NOT NULL,
                    previous_value REAL NOT NULL,
                    applied_value REAL NOT NULL,
                    minimum_value REAL NOT NULL,
                    maximum_value REAL NOT NULL,
                    recommendation_source TEXT NOT NULL,
                    rationale TEXT NOT NULL DEFAULT '',
                    applied_by TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_investment_parameter_adjustment_latest
                    ON investment_parameter_adjustment(parameter_key, applied_at DESC);
                CREATE TABLE IF NOT EXISTS mathematical_capability_definition (
                    capability_key TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    display_name TEXT NOT NULL,
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
            if self.database_scope == "main":
                self.command_parser.initialize(connection)
            if self.database_scope in {"legacy", "investment"}:
                self._seed_parameter_definitions(connection)
                self._seed_investment_model_definitions(connection)
            if self.database_scope == "mathematical":
                self._seed_mathematical_capability_definitions(connection)
            self._enforce_database_scope(connection)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _enforce_database_scope(self, connection: sqlite3.Connection) -> None:
        if self.database_scope != "main":
            connection.execute("DELETE FROM capability_composition")
        if self.database_scope == "main":
            connection.execute("DELETE FROM investment_parameter_definition")
            connection.execute("DELETE FROM investment_model_definition")
            connection.execute("DELETE FROM mathematical_capability_definition")
            connection.execute("DELETE FROM investment_parameter_adjustment")
        elif self.database_scope == "investment":
            connection.execute("DELETE FROM mathematical_capability_definition")
        elif self.database_scope == "mathematical":
            connection.execute("DELETE FROM investment_parameter_definition")
            connection.execute("DELETE FROM investment_model_definition")
            connection.execute("DELETE FROM investment_parameter_adjustment")
        elif self.database_scope == "coding":
            connection.execute("DELETE FROM investment_parameter_definition")
            connection.execute("DELETE FROM investment_model_definition")
            connection.execute("DELETE FROM investment_parameter_adjustment")
            connection.execute("DELETE FROM mathematical_capability_definition")

    def record_common_command(self, command_text: str, intent: str = "conversation") -> None:
        with self._connect() as connection:
            self.command_parser.record(connection, command_text, intent)

    def common_commands(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return self.command_parser.list(connection, limit)

    def investment_parameter_values(self) -> dict[str, float]:
        if self.database_scope != "investment":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        values = {
            key: definition[0]
            for key, definition in self.ADJUSTABLE_INVESTMENT_PARAMETERS.items()
        }
        with self._connect() as connection:
            for key in values:
                row = connection.execute(
                    """
                    SELECT applied_value FROM investment_parameter_adjustment
                    WHERE parameter_key = ? ORDER BY id DESC LIMIT 1
                    """,
                    (key,),
                ).fetchone()
                if row is not None:
                    values[key] = float(row[0])
        return values

    def apply_chatgpt_parameter_recommendations(
        self, recommendations: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if self.database_scope != "investment":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        current = self.investment_parameter_values()
        applied: list[dict[str, Any]] = []
        with self._connect() as connection:
            for recommendation in recommendations[:10]:
                key = str(recommendation.get("parameter_key") or "").strip()
                definition = self.ADJUSTABLE_INVESTMENT_PARAMETERS.get(key)
                if definition is None:
                    continue
                try:
                    value = float(recommendation.get("value"))
                except (TypeError, ValueError):
                    continue
                _default, minimum, maximum = definition
                if not minimum <= value <= maximum:
                    continue
                previous = float(current[key])
                rationale = str(recommendation.get("rationale") or "").strip()[:1000]
                connection.execute(
                    """
                    INSERT INTO investment_parameter_adjustment(
                        parameter_key, previous_value, applied_value,
                        minimum_value, maximum_value, recommendation_source,
                        rationale, applied_by, applied_at
                    ) VALUES (?, ?, ?, ?, ?, 'chatgpt', ?, 'star-main-native-model', ?)
                    """,
                    (key, previous, value, minimum, maximum, rationale, self._utc_now()),
                )
                current[key] = value
                applied.append(
                    {
                        "parameter_key": key,
                        "previous_value": previous,
                        "applied_value": value,
                        "minimum_value": minimum,
                        "maximum_value": maximum,
                        "recommendation_source": "chatgpt",
                        "applied_by": "star-main-native-model",
                    }
                )
        return applied

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
        memory_columns = self._columns(connection, "model_memory")
        memory_migrations = {
            "review_status": "TEXT NOT NULL DEFAULT 'approved'",
            "reviewed_by": "TEXT NOT NULL DEFAULT ''",
            "reviewed_at": "TEXT NOT NULL DEFAULT ''",
            "review_reason": "TEXT NOT NULL DEFAULT ''",
            "revoked_at": "TEXT NOT NULL DEFAULT ''",
            "provenance_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for column, declaration in memory_migrations.items():
            if column not in memory_columns:
                connection.execute(
                    f"ALTER TABLE model_memory ADD COLUMN {column} {declaration}"
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
        if str(model).strip() != self.owner_model_id:
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO inference_log(model, request_json, response_json) VALUES (?, ?, ?)",
                (
                    model,
                    json.dumps(request, ensure_ascii=False),
                    json.dumps(response, ensure_ascii=False),
                ),
            )

    def store_language_training_example(
        self,
        *,
        intent: str,
        input_text: str,
        target_text: str,
        source_type: str,
        quality_score: float,
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_intent = str(intent or "capabilities").strip().casefold()[:64]
        normalized_input = str(input_text or "").strip()[:16_000]
        normalized_target = str(target_text or "").strip()[:16_000]
        normalized_source = str(source_type or "self-distillation-grounded").strip()[:96]
        quality = max(0.0, min(1.0, float(quality_score)))
        if not normalized_input or not normalized_target:
            raise ValueError("language training input and target are required")
        if quality < 0.8:
            raise ValueError("language training quality gate rejected the example")
        content_hash = hashlib.sha256(
            f"{normalized_intent}\0{normalized_input}\0{normalized_target}".encode("utf-8")
        ).hexdigest()
        example_id = f"star-train-{content_hash[:24]}"
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO language_training_example(
                    example_id, content_hash, intent, input_text, target_text,
                    source_type, quality_score, validation_json, active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    example_id,
                    content_hash,
                    normalized_intent,
                    normalized_input,
                    normalized_target,
                    normalized_source,
                    quality,
                    json.dumps(validation, ensure_ascii=False, sort_keys=True),
                    created_at,
                ),
            )
            inserted = cursor.rowcount > 0
            connection.execute(
                """
                UPDATE language_training_example SET active = 0
                WHERE revision NOT IN (
                    SELECT revision FROM language_training_example
                    WHERE active = 1 ORDER BY revision DESC LIMIT ?
                )
                """,
                (self.MAX_LANGUAGE_TRAINING_EXAMPLES,),
            )
            row = connection.execute(
                """
                SELECT revision, example_id, content_hash, intent, input_text,
                       target_text, source_type, quality_score, validation_json,
                       active, created_at
                FROM language_training_example WHERE content_hash = ?
                """,
                (content_hash,),
            ).fetchone()
        if row is None:
            raise RuntimeError("language training example was not stored")
        return {
            "revision": int(row[0]),
            "example_id": str(row[1]),
            "content_hash": str(row[2]),
            "intent": str(row[3]),
            "input_text": str(row[4]),
            "target_text": str(row[5]),
            "source_type": str(row[6]),
            "quality_score": float(row[7]),
            "validation": json.loads(str(row[8]) or "{}"),
            "active": bool(row[9]),
            "created_at": str(row[10]),
            "inserted": inserted,
            "owner_model_id": self.owner_model_id,
        }

    def language_training_examples(self, *, limit: int = 500) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(self.MAX_LANGUAGE_TRAINING_EXAMPLES, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT revision, example_id, intent, input_text, target_text,
                       source_type, quality_score, validation_json, created_at
                FROM language_training_example
                WHERE active = 1 AND quality_score >= 0.8
                ORDER BY revision ASC LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        return [
            {
                "revision": int(row[0]),
                "example_id": str(row[1]),
                "intent": str(row[2]),
                "input_text": str(row[3]),
                "target_text": str(row[4]),
                "source_type": str(row[5]),
                "quality_score": float(row[6]),
                "validation": json.loads(str(row[7]) or "{}"),
                "created_at": str(row[8]),
            }
            for row in rows
        ]

    def native_private_context(self, *, limit: int = 6) -> dict[str, Any]:
        if self.database_scope != "main":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        bounded_limit = max(1, min(12, int(limit)))
        with self._connect() as connection:
            training_rows = connection.execute(
                """
                SELECT example_id, intent, input_text, target_text, source_type,
                       quality_score, created_at
                FROM language_training_example
                WHERE active = 1 AND quality_score >= 0.8
                ORDER BY revision DESC LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
            capability_rows = connection.execute(
                """
                SELECT composition_id, status, implementation_target, updated_at
                FROM capability_composition
                ORDER BY updated_at DESC LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
            operation_rows = connection.execute(
                """
                SELECT id, model, request_json, response_json, created_at
                FROM inference_log
                ORDER BY id DESC LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        return {
            "owner_model_id": self.owner_model_id,
            "database_scope": self.database_scope,
            "training_examples": [
                {
                    "example_id": str(row[0]),
                    "intent": str(row[1]),
                    "input_text": str(row[2])[:2_000],
                    "target_text": str(row[3])[:2_000],
                    "source_type": str(row[4]),
                    "quality_score": float(row[5]),
                    "created_at": str(row[6]),
                }
                for row in training_rows
            ],
            "capability_compositions": [
                {
                    "composition_id": str(row[0]),
                    "status": str(row[1]),
                    "implementation_target": str(row[2]),
                    "updated_at": str(row[3]),
                }
                for row in capability_rows
            ],
            "operation_records": [
                {
                    "id": int(row[0]),
                    "model": str(row[1]),
                    "request": json.loads(str(row[2]) or "{}"),
                    "response": json.loads(str(row[3]) or "{}"),
                    "created_at": str(row[4]),
                }
                for row in operation_rows
            ],
        }

    def maintain_language_model(self) -> dict[str, Any]:
        """Audit local training data and compact it without touching other scopes."""

        deactivated: list[int] = []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT revision, input_text, target_text, quality_score, validation_json
                FROM language_training_example WHERE active = 1
                ORDER BY revision ASC
                """
            ).fetchall()
            for row in rows:
                try:
                    validation = json.loads(str(row[4]) or "{}")
                except json.JSONDecodeError:
                    validation = {}
                facts_preserved = validation.get("facts_preserved")
                if facts_preserved is None:
                    facts_preserved = validation.get("fact_preservation_verified")
                grounding_coverage = validation.get("grounding_coverage")
                if grounding_coverage is None:
                    grounding_coverage = validation.get("semantic_grounding")
                bounded_output = validation.get("bounded_output")
                if bounded_output is None:
                    bounded_output = 8 <= len(str(row[2])) <= 16_000
                valid = (
                    bool(str(row[1]).strip())
                    and bool(str(row[2]).strip())
                    and len(str(row[1])) <= 16_000
                    and len(str(row[2])) <= 16_000
                    and float(row[3]) >= 0.8
                    and isinstance(validation, dict)
                    and facts_preserved is True
                    and bounded_output is True
                    and float(grounding_coverage or 0)
                    >= MIN_TRAINING_GROUNDING_COVERAGE
                )
                if not valid:
                    connection.execute(
                        "UPDATE language_training_example SET active = 0 WHERE revision = ?",
                        (int(row[0]),),
                    )
                    deactivated.append(int(row[0]))
            connection.execute(
                """
                UPDATE language_training_example SET active = 0
                WHERE revision NOT IN (
                    SELECT revision FROM language_training_example
                    WHERE active = 1 ORDER BY revision DESC LIMIT ?
                )
                """,
                (self.MAX_LANGUAGE_TRAINING_EXAMPLES,),
            )
            active_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM language_training_example WHERE active = 1"
                ).fetchone()[0]
            )
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            connection.execute("PRAGMA optimize")
            result = {
                "ok": integrity.casefold() == "ok",
                "owner_model_id": self.owner_model_id,
                "database_scope": self.database_scope,
                "active_example_count": active_count,
                "deactivated_revisions": deactivated,
                "deactivated_count": len(deactivated),
                "sqlite_integrity": integrity,
                "weights_rebuild_required": bool(deactivated),
            }
            run_id = f"star-maintain-{uuid.uuid4().hex[:24]}"
            created_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                INSERT INTO language_model_maintenance(
                    run_id, action, result_json, ok, created_at
                ) VALUES (?, 'audit-compact-optimize', ?, ?, ?)
                """,
                (
                    run_id,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    int(result["ok"]),
                    created_at,
                ),
            )
            connection.execute(
                """
                DELETE FROM language_model_maintenance WHERE run_id NOT IN (
                    SELECT run_id FROM language_model_maintenance
                    ORDER BY created_at DESC LIMIT 100
                )
                """
            )
        return {**result, "run_id": run_id, "created_at": created_at}

    def record_internal_training_run(self, result: dict[str, Any]) -> dict[str, Any]:
        if self.database_scope != "main":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        run_id = str(result.get("training_run_id") or f"internal-{uuid.uuid4().hex[:24]}")
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO language_model_maintenance(
                    run_id, action, result_json, ok, created_at
                ) VALUES (?, 'ollama-internal-training', ?, ?, ?)
                """,
                (
                    run_id[:160],
                    json.dumps(result, ensure_ascii=False, sort_keys=True)[:512_000],
                    int(result.get("ok") is True),
                    created_at,
                ),
            )
        return {
            "run_id": run_id,
            "created_at": created_at,
            "ok": result.get("ok") is True,
        }

    def latest_internal_training_run(self) -> dict[str, Any]:
        if self.database_scope != "main":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, result_json, ok, created_at
                FROM language_model_maintenance
                WHERE action = 'ollama-internal-training'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            return {}
        try:
            result = json.loads(str(row[1]) or "{}")
        except json.JSONDecodeError:
            result = {}
        return {
            "run_id": str(row[0]),
            "result": result if isinstance(result, dict) else {},
            "ok": bool(row[2]),
            "created_at": str(row[3]),
        }

    def store_code_upgrade_proposal(
        self,
        *,
        target_path: str,
        language: str,
        source_text: str,
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        if self.database_scope != "coding":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        target = str(target_path or "").strip()[:1_000]
        normalized_language = str(language or "").strip().casefold()[:32]
        source = str(source_text or "")[:128_000]
        if not target or not source or validation.get("ok") is not True:
            raise ValueError("a validated code upgrade proposal is required")
        source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
        proposal_id = f"star-upgrade-{source_sha256[:24]}"
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO code_upgrade_proposal(
                    proposal_id, target_path, language, source_text,
                    source_sha256, validation_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)
                """,
                (
                    proposal_id,
                    target,
                    normalized_language,
                    source,
                    source_sha256,
                    json.dumps(validation, ensure_ascii=False, sort_keys=True),
                    created_at,
                ),
            )
            inserted = cursor.rowcount > 0
            row = connection.execute(
                """
                SELECT revision, proposal_id, target_path, language,
                       source_sha256, status, created_at
                FROM code_upgrade_proposal
                WHERE target_path = ? AND source_sha256 = ?
                """,
                (target, source_sha256),
            ).fetchone()
        if row is None:
            raise RuntimeError("code upgrade proposal was not stored")
        return {
            "revision": int(row[0]),
            "proposal_id": str(row[1]),
            "target_path": str(row[2]),
            "language": str(row[3]),
            "source_sha256": str(row[4]),
            "status": str(row[5]),
            "created_at": str(row[6]),
            "inserted": inserted,
            "owner_model_id": self.owner_model_id,
        }

    def store_capability_composition(
        self, composition: dict[str, Any]
    ) -> dict[str, Any]:
        if self.database_scope != "main":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        composition_id = str(composition.get("composition_id") or "").strip()[:160]
        if not composition_id:
            raise ValueError("composition_id is required")
        discussion = composition.get("model_discussion")
        discussion = discussion if isinstance(discussion, dict) else {}
        assignments = composition.get("model_assignments")
        assignments = assignments if isinstance(assignments, dict) else {}
        status = str(composition.get("status") or "proposed").strip()[:64]
        target = str(composition.get("implementation_target") or "")[:1_000]
        source_sha256 = str(composition.get("source_sha256") or "")[:64]
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(composition, ensure_ascii=False, sort_keys=True)[:512_000]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO capability_composition(
                    composition_id, status, blueprint_json,
                    model_assignments_json, votes_json, inspections_json,
                    implementation_target, source_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(composition_id) DO UPDATE SET
                    status = excluded.status,
                    blueprint_json = excluded.blueprint_json,
                    model_assignments_json = excluded.model_assignments_json,
                    votes_json = excluded.votes_json,
                    inspections_json = excluded.inspections_json,
                    implementation_target = excluded.implementation_target,
                    source_sha256 = excluded.source_sha256,
                    updated_at = excluded.updated_at
                """,
                (
                    composition_id,
                    status,
                    encoded,
                    json.dumps(assignments, ensure_ascii=False, sort_keys=True),
                    json.dumps(discussion.get("voters") or [], ensure_ascii=False),
                    json.dumps(
                        discussion.get("inspection_results") or [], ensure_ascii=False
                    ),
                    target,
                    source_sha256,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT revision, status, created_at, updated_at
                FROM capability_composition WHERE composition_id = ?
                """,
                (composition_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("capability composition was not stored")
        return {
            "revision": int(row[0]),
            "composition_id": composition_id,
            "status": str(row[1]),
            "created_at": str(row[2]),
            "updated_at": str(row[3]),
            "owner_model_id": self.owner_model_id,
            "database_scope": self.database_scope,
        }

    def store_brokered_memory(
        self,
        *,
        memory_id: str = "",
        kind: str,
        title: str,
        content: str,
        business_scope: str,
        source_type: str,
        source_id: str,
        source_model_id: str,
        broker_model_id: str,
        confidence: float = 0.5,
        expires_at: str = "",
        review_status: str = "approved",
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if str(broker_model_id).strip() != self.MODEL_ID_BY_SCOPE["main"]:
            raise PermissionError("MODEL_MEMORY_BROKER_DENIED")
        scope = str(business_scope or "general").strip().casefold()
        if scope not in {"general", "investment"}:
            raise ValueError("unsupported memory business scope")
        normalized_content = str(content or "").strip()[:4_000]
        if not normalized_content:
            raise ValueError("memory content is required")
        normalized_kind = str(kind or "context").strip()[:64] or "context"
        normalized_title = str(title or normalized_content[:80]).strip()[:160]
        normalized_source_type = str(source_type or "internal").strip()[:64]
        normalized_source_id = str(source_id or "unknown").strip()[:96]
        normalized_source_model = str(source_model_id or "unknown").strip()[:96]
        normalized_confidence = max(0.0, min(1.0, float(confidence)))
        normalized_review_status = str(review_status or "pending-review").strip().casefold()
        if normalized_review_status not in {
            "pending-review",
            "approved",
            "rejected",
            "revoked",
        }:
            raise ValueError("unsupported memory review status")
        provenance_json = json.dumps(
            dict(provenance or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )[:4_000]
        digest = hashlib.sha256(
            json.dumps(
                {
                    "kind": normalized_kind,
                    "content": normalized_content,
                    "scope": scope,
                    "source": normalized_source_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        now = self._utc_now()
        normalized_memory_id = str(memory_id or "").strip()[:64] or uuid.uuid4().hex[:24]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO model_memory(
                    memory_id, content_hash, kind, title, content,
                    business_scope, source_type, source_id, source_model_id,
                    broker_model_id, confidence, expires_at, review_status,
                    reviewed_by, reviewed_at, review_reason, revoked_at,
                    provenance_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '', '', ?, ?, ?)
                ON CONFLICT(content_hash) DO UPDATE SET
                    title = excluded.title,
                    content = excluded.content,
                    confidence = MAX(model_memory.confidence, excluded.confidence),
                    expires_at = excluded.expires_at,
                    review_status = CASE
                        WHEN model_memory.review_status = 'approved' THEN 'approved'
                        ELSE excluded.review_status
                    END,
                    provenance_json = excluded.provenance_json,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_memory_id,
                    digest,
                    normalized_kind,
                    normalized_title,
                    normalized_content,
                    scope,
                    normalized_source_type,
                    normalized_source_id,
                    normalized_source_model,
                    broker_model_id,
                    normalized_confidence,
                    str(expires_at or "").strip(),
                    normalized_review_status,
                    provenance_json,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT memory_id, content_hash, kind, title, content,
                       business_scope, source_type, source_id, source_model_id,
                       broker_model_id, confidence, expires_at, review_status,
                       reviewed_by, reviewed_at, review_reason, revoked_at,
                       provenance_json, created_at, updated_at
                FROM model_memory WHERE content_hash = ?
                """,
                (digest,),
            ).fetchone()
            connection.execute(
                "DELETE FROM model_memory WHERE expires_at <> '' AND expires_at < ?",
                (now,),
            )
            connection.execute(
                """
                DELETE FROM model_memory WHERE memory_id NOT IN (
                    SELECT memory_id FROM model_memory
                    ORDER BY updated_at DESC LIMIT ?
                )
                """,
                (self.MAX_MODEL_MEMORIES,),
            )
        values = tuple(row) if row is not None else ()
        keys = (
            "memory_id",
            "content_hash",
            "kind",
            "title",
            "content",
            "business_scope",
            "source_type",
            "source_id",
            "source_model_id",
            "broker_model_id",
            "confidence",
            "expires_at",
            "review_status",
            "reviewed_by",
            "reviewed_at",
            "review_reason",
            "revoked_at",
            "provenance_json",
            "created_at",
            "updated_at",
        )
        return {
            **dict(zip(keys, values)),
            "owner_model_id": self.owner_model_id,
            "database_shared": False,
        }

    def memory_context(
        self,
        business_scope: str,
        *,
        limit: int = 10,
        include_pending: bool = False,
    ) -> list[dict[str, Any]]:
        scope = str(business_scope or "general").strip().casefold()
        if scope not in {"general", "investment"}:
            raise ValueError("unsupported memory business scope")
        now = self._utc_now()
        with self._connect() as connection:
            status_filter = "review_status IN ('approved', 'pending-review')" if include_pending else "review_status = 'approved'"
            rows = connection.execute(
                f"""
                SELECT memory_id, kind, title, content, business_scope,
                       source_model_id, confidence, updated_at, review_status,
                       source_type, source_id, expires_at, provenance_json
                FROM model_memory
                WHERE business_scope IN ('general', ?)
                  AND (expires_at = '' OR expires_at >= ?)
                  AND revoked_at = ''
                  AND {status_filter}
                ORDER BY confidence DESC, updated_at DESC
                LIMIT ?
                """,
                (scope, now, max(1, min(20, int(limit)))),
            ).fetchall()
        return [
            {
                "memory_id": str(row[0]),
                "kind": str(row[1]),
                "title": str(row[2]),
                "content": str(row[3]),
                "business_scope": str(row[4]),
                "source_model_id": str(row[5]),
                "origin_model_id": self.owner_model_id,
                "confidence": float(row[6]),
                "updated_at": str(row[7]),
                "review_status": str(row[8]),
                "source_type": str(row[9]),
                "source_id": str(row[10]),
                "expires_at": str(row[11]),
                "provenance": json.loads(str(row[12]) or "{}"),
            }
            for row in rows
        ]

    def review_memory(
        self,
        memory_id: str,
        *,
        action: str,
        reviewer: str,
        reason: str = "",
    ) -> dict[str, Any]:
        normalized_action = str(action or "").strip().casefold()
        target_status = {
            "approve": "approved",
            "reject": "rejected",
            "revoke": "revoked",
        }.get(normalized_action)
        if target_status is None:
            raise ValueError("memory review action must be approve, reject, or revoke")
        normalized_reviewer = str(reviewer or "").strip()[:96]
        if not normalized_reviewer:
            raise ValueError("memory reviewer is required")
        now = self._utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE model_memory
                SET review_status = ?, reviewed_by = ?, reviewed_at = ?,
                    review_reason = ?, revoked_at = ?, updated_at = ?
                WHERE memory_id = ?
                """,
                (
                    target_status,
                    normalized_reviewer,
                    now,
                    str(reason or "").strip()[:1_000],
                    now if target_status == "revoked" else "",
                    now,
                    str(memory_id or "").strip(),
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError("memory not found")
        records = self.memory_records(include_inactive=True, limit=500)
        return next(item for item in records if item["memory_id"] == memory_id)

    def memory_records(
        self,
        *,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = "" if include_inactive else "WHERE review_status IN ('pending-review', 'approved') AND revoked_at = ''"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT memory_id, kind, title, content, business_scope,
                       source_type, source_id, source_model_id, broker_model_id,
                       confidence, expires_at, review_status, reviewed_by,
                       reviewed_at, review_reason, revoked_at, provenance_json,
                       created_at, updated_at
                FROM model_memory {where}
                ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, min(500, int(limit))),),
            ).fetchall()
        keys = (
            "memory_id", "kind", "title", "content", "business_scope",
            "source_type", "source_id", "source_model_id", "broker_model_id",
            "confidence", "expires_at", "review_status", "reviewed_by",
            "reviewed_at", "review_reason", "revoked_at", "provenance",
            "created_at", "updated_at",
        )
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(zip(keys, row))
            try:
                item["provenance"] = json.loads(str(item["provenance"] or "{}"))
            except json.JSONDecodeError:
                item["provenance"] = {}
            item["owner_model_id"] = self.owner_model_id
            output.append(item)
        return output

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

    def _seed_investment_model_definitions(self, connection: sqlite3.Connection) -> None:
        now = self._utc_now()
        definitions = (
            (
                "valuation",
                "valuation",
                "估值模型",
                "star-investment-parameter-engine",
                "1.0",
                ("price", "previous_close", "fund_size"),
                "依價格、淨值與規模進行相對估值；資料不足時保留未知值。",
            ),
            (
                "income-distribution",
                "income",
                "收益與配息模型",
                "star-investment-parameter-engine",
                "1.0",
                (
                    "distribution_amount",
                    "distribution_frequency",
                    "annual_distribution_per_unit",
                    "distribution_yield_percent",
                ),
                "分析現金分配、頻率與近一年配息率。",
            ),
            (
                "risk-volatility",
                "risk",
                "風險與波動模型",
                "star-investment-parameter-engine",
                "1.0",
                ("risk_level", "volatility_percent"),
                "評估風險等級與可驗證價格序列的波動。",
            ),
            (
                "portfolio-concentration",
                "portfolio",
                "投資組合集中度模型",
                "star-investment-analysis-engine",
                "1.0",
                ("region_exposure", "industry_exposure"),
                "檢查標的、區域與產業集中風險。",
            ),
            (
                "asset-allocation",
                "portfolio",
                "資產配置模型",
                "star-investment-analysis-engine",
                "1.0",
                ("region_exposure", "industry_exposure", "risk_level"),
                "依資產、區域、產業與風險層級檢查配置。",
            ),
            (
                "scenario-stress",
                "risk",
                "情境壓力模型",
                "star-mathematical-delegation-engine",
                "1.0",
                ("volatility_percent", "change_percent", "risk_level"),
                "由主模型協調數理專家執行情境與壓力計算。",
            ),
            (
                "fee-efficiency",
                "fee",
                "費用效率模型",
                "star-investment-parameter-engine",
                "1.0",
                ("management_fee_percent", "custody_fee_percent"),
                "分析經理費與保管費對持有成本的影響。",
            ),
            (
                "return-trend",
                "return",
                "報酬趨勢模型",
                "star-investment-parameter-engine",
                "1.0",
                ("change_percent", "ytd_return_percent"),
                "比較短期變動與年初至今報酬，不補造缺少的歷史值。",
            ),
        )
        connection.executemany(
            """
            INSERT INTO investment_model_definition(
                model_key, category, display_name, engine, version,
                parameter_keys_json, description, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_key) DO UPDATE SET
                category=excluded.category,
                display_name=excluded.display_name,
                engine=excluded.engine,
                version=excluded.version,
                parameter_keys_json=excluded.parameter_keys_json,
                description=excluded.description,
                updated_at=excluded.updated_at
            """,
            [
                (*item[:5], json.dumps(item[5], ensure_ascii=False), item[6], now)
                for item in definitions
            ],
        )

    def investment_model_catalog(self) -> list[dict[str, Any]]:
        if self.database_scope != "investment":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT model_key, category, display_name, engine, version,
                       parameter_keys_json, description, enabled
                FROM investment_model_definition
                WHERE enabled = 1 ORDER BY category, model_key
                """
            ).fetchall()
        return [
            {
                "model_key": str(row[0]),
                "category": str(row[1]),
                "display_name": str(row[2]),
                "engine": str(row[3]),
                "version": str(row[4]),
                "parameter_keys": json.loads(str(row[5]) or "[]"),
                "description": str(row[6]),
                "enabled": bool(row[7]),
            }
            for row in rows
        ]

    def _seed_mathematical_capability_definitions(
        self, connection: sqlite3.Connection
    ) -> None:
        now = self._utc_now()
        definitions = (
            ("arithmetic", "calculation", "一般運算", "四則、次方、餘數與括號運算。"),
            ("formula", "calculation", "公式計算", "依明確輸入與公式執行可重現計算。"),
            ("logic", "reasoning", "邏輯推理", "區分前提、推導與結論並檢查矛盾。"),
            ("descriptive-statistics", "statistics", "描述統計", "計算樣本數、平均、中位數與離散程度。"),
            ("comparative-statistics", "statistics", "比較統計", "比較群組與期間差異，保留樣本限制。"),
            ("data-cleaning", "data", "資料清理", "辨識缺漏、型別與重複資料，不猜測補值。"),
            ("data-aggregation", "data", "資料彙整", "依欄位分類、計數與彙整。"),
            ("result-validation", "validation", "結果驗證", "檢查有限值、範圍、除零與輸入完整性。"),
            ("xirr", "finance", "不規則現金流報酬", "依日期與現金流計算可重現 XIRR。"),
            ("time-weighted-return", "finance", "時間加權報酬", "依期間報酬鏈結計算時間加權報酬。"),
            ("maximum-drawdown", "risk", "最大回撤", "依價格序列計算峰值至谷底的最大回撤。"),
            ("risk-adjusted-return", "risk", "風險調整報酬", "計算年化波動、Sharpe 與 Sortino。"),
            ("covariance-correlation", "statistics", "共變異與相關性", "比較多資產報酬序列的相關性。"),
            ("scenario-testing", "risk", "情境壓力測試", "套用明確曝險與衝擊參數估算情境變化。"),
            ("portfolio-rebalancing", "portfolio", "再平衡計算", "依目前與目標權重計算調整金額。"),
            ("calculation-audit-trace", "validation", "計算稽核軌跡", "保留輸入欄位、公式引擎與可重現狀態。"),
        )
        connection.executemany(
            """
            INSERT INTO mathematical_capability_definition(
                capability_key, category, display_name, description, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(capability_key) DO UPDATE SET
                category=excluded.category,
                display_name=excluded.display_name,
                description=excluded.description,
                updated_at=excluded.updated_at
            """,
            [(*item, now) for item in definitions],
        )

    def mathematical_capability_catalog(self) -> list[dict[str, Any]]:
        if self.database_scope != "mathematical":
            raise PermissionError("MODEL_DATABASE_ISOLATION_DENIED")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT capability_key, category, display_name, description, enabled
                FROM mathematical_capability_definition
                WHERE enabled = 1 ORDER BY category, capability_key
                """
            ).fetchall()
        return [
            {
                "capability_key": str(row[0]),
                "category": str(row[1]),
                "display_name": str(row[2]),
                "description": str(row[3]),
                "enabled": bool(row[4]),
            }
            for row in rows
        ]

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

    def database_status(self) -> dict[str, Any]:
        with self._connect() as connection:
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "inference_log",
                    "instrument_identity",
                    "market_observation",
                    "distribution_event",
                    "investment_parameter_definition",
                    "investment_model_definition",
                    "investment_parameter_adjustment",
                    "mathematical_capability_definition",
                    "web_search_log",
                    "model_memory",
                    "language_training_example",
                    "language_model_maintenance",
                    "code_upgrade_proposal",
                    "capability_composition",
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
            search_success_count = int(
                connection.execute(
                    "SELECT COALESCE(SUM(CASE WHEN ok = 1 THEN occurrence_count ELSE 0 END), 0) FROM web_search_log"
                ).fetchone()[0]
            )
            search_failure_count = int(
                connection.execute(
                    "SELECT COALESCE(SUM(CASE WHEN ok = 0 THEN occurrence_count ELSE 0 END), 0) FROM web_search_log"
                ).fetchone()[0]
            )
            memory_review_counts = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT review_status, COUNT(*) FROM model_memory GROUP BY review_status"
                ).fetchall()
            }
            latest_market_observation_at = str(
                connection.execute(
                    "SELECT COALESCE(MAX(observed_at), '') FROM market_observation"
                ).fetchone()[0]
                or ""
            )
            latest_inference_at = str(
                connection.execute(
                    "SELECT COALESCE(MAX(created_at), '') FROM inference_log"
                ).fetchone()[0]
                or ""
            )
            language_training_quality = connection.execute(
                """
                SELECT COUNT(*), COALESCE(AVG(quality_score), 0),
                       COALESCE(MAX(revision), 0), COALESCE(MAX(created_at), '')
                FROM language_training_example WHERE active = 1
                """
            ).fetchone()
            latest_language_maintenance = connection.execute(
                """
                SELECT run_id, action, ok, created_at
                FROM language_model_maintenance ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
            integrity_check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        return {
            "engine": "local-sqlite3-degraded",
            "role": "owner-private-state-cache-checkpoint-or-bounded-reconciled-degraded-transport-only",
            "canonical_central_engine": "postgresql",
            "canonical": False,
            "authority": "non-canonical-reconciliation-required",
            "reconciliation_required": True,
            "path": str(self.database_path),
            "database_scope": self.database_scope,
            "owner_model_id": self.owner_model_id,
            "isolation_enforced": True,
            "tables": counts,
            "size_bytes": page_count * page_size,
            "quality": {
                "blank_distribution_events": blank_distribution_events,
                "duplicate_distribution_events": duplicate_distribution_events,
                "search_request_occurrences": search_occurrences,
                "search_success_count": search_success_count,
                "search_failure_count": search_failure_count,
                "search_success_rate_percent": round(
                    search_success_count / search_occurrences * 100, 4
                )
                if search_occurrences
                else None,
                "search_payload_chars": search_payload_chars,
                "max_search_response_chars": max_search_response_chars,
                "search_log_retention_days": self.SEARCH_LOG_RETENTION_DAYS,
                "search_log_limit": self.MAX_SEARCH_LOGS,
                "memory_review_counts": memory_review_counts,
                "latest_market_observation_at": latest_market_observation_at,
                "latest_inference_at": latest_inference_at,
                "language_training": {
                    "active_example_count": int(language_training_quality[0]),
                    "average_quality_score": round(float(language_training_quality[1]), 4),
                    "latest_revision": int(language_training_quality[2]),
                    "latest_trained_at": str(language_training_quality[3]),
                    "maximum_examples": self.MAX_LANGUAGE_TRAINING_EXAMPLES,
                },
                "latest_language_maintenance": {
                    "run_id": str(latest_language_maintenance[0]),
                    "action": str(latest_language_maintenance[1]),
                    "ok": bool(latest_language_maintenance[2]),
                    "created_at": str(latest_language_maintenance[3]),
                }
                if latest_language_maintenance is not None
                else None,
                "sqlite_integrity": integrity_check,
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
