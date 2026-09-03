from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class OllamaModelRepository:
    """Isolated operational database owned by exactly one Ollama model."""

    def __init__(self, tool_root: Path, model_id: str) -> None:
        self.model_id = str(model_id or "").strip()
        if not self.model_id:
            raise ValueError("model_id is required")
        slug = re.sub(r"[^a-z0-9]+", "-", self.model_id.casefold()).strip("-")[:64]
        digest = hashlib.sha256(self.model_id.encode("utf-8")).hexdigest()[:12]
        self.database_path = (
            Path(tool_root)
            / "runtime"
            / "state"
            / "ollama-models"
            / f"{slug}-{digest}.sqlite3"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inference_record (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent TEXT NOT NULL,
                    model_role TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    response_digest TEXT NOT NULL DEFAULT '',
                    ok INTEGER NOT NULL,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capability_vote (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    composition_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(composition_id, decision, reason)
                );
                CREATE TABLE IF NOT EXISTS training_contribution (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    contribution_role TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, contribution_role, content_digest)
                );
                """
            )

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

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def record_inference(
        self,
        *,
        intent: str,
        model_role: str,
        request: Any,
        response: dict[str, Any],
    ) -> None:
        if str(response.get("model") or "") != self.model_id:
            raise PermissionError("OLLAMA_MODEL_DATABASE_ISOLATION_DENIED")
        metrics = {
            key: response.get(key)
            for key in (
                "latency_ms",
                "load_duration_ns",
                "total_duration_ns",
                "prompt_eval_count",
                "eval_count",
                "reasoning_effort",
                "residency",
            )
            if response.get(key) is not None
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO inference_record(
                    intent, model_role, request_digest, response_digest,
                    ok, metrics_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(intent)[:64],
                    str(model_role)[:96],
                    self._digest(request),
                    self._digest(response),
                    int(response.get("ok") is True),
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def record_capability_vote(
        self, *, composition_id: str, decision: str, reason: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO capability_vote(
                    composition_id, decision, reason, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    str(composition_id)[:160],
                    str(decision)[:32],
                    str(reason)[:2_000],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def record_training_contribution(
        self,
        *,
        run_id: str,
        contribution_role: str,
        content: Any,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO training_contribution(
                    run_id, contribution_role, content_digest,
                    status, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run_id)[:160],
                    str(contribution_role)[:96],
                    self._digest(content),
                    str(status)[:32],
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "inference_record",
                    "capability_vote",
                    "training_contribution",
                )
            }
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        return {
            "owner_model_id": self.model_id,
            "database_path": str(self.database_path),
            "isolation": "one-database-per-ollama-model",
            "investment_database_access": False,
            "tables": counts,
            "integrity_check": integrity,
        }


__all__ = ["OllamaModelRepository"]
