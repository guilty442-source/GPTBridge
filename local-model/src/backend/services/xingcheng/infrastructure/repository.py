from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .local_command_parser import LocalCommandParser
from .repo_capability import CapabilityMixin
from .repo_investment import InvestmentMixin
from .repo_language import LanguageTrainingMixin
from .repo_market import MarketMixin
from .repo_mathematical import MathematicalMixin
from .repo_memory import MemoryMixin
from .repo_migration import MigrationMixin
from .repo_status import StatusMixin


class LocalAiRepository(
    MigrationMixin,
    LanguageTrainingMixin,
    MemoryMixin,
    CapabilityMixin,
    InvestmentMixin,
    MathematicalMixin,
    MarketMixin,
    StatusMixin,
):
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

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()
