from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping


class TransformerRuntimeCheckpointRepository:
    """Durable, temporary checkpoints for local Transformer workflows."""

    DATABASE_NAME = "transformer-runtime-checkpoints.sqlite3"

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = Path(tool_root).resolve() / "xingcheng"
        self.database_path = (
            self.tool_root / "runtime" / "state" / self.DATABASE_NAME
        ).resolve()
        if not self.database_path.is_relative_to(self.tool_root):
            raise PermissionError("TRANSFORMER_CHECKPOINT_DATABASE_SCOPE_DENIED")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 15000")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS transformer_pipeline_run (
                    run_id TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL UNIQUE,
                    pipeline_kind TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active', 'completed')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transformer_pipeline_stage (
                    run_id TEXT NOT NULL,
                    stage_index INTEGER NOT NULL CHECK(stage_index >= 1),
                    stage_name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    output_text TEXT NOT NULL,
                    completed INTEGER NOT NULL CHECK(completed IN (0, 1)),
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(run_id, stage_index),
                    FOREIGN KEY(run_id) REFERENCES transformer_pipeline_run(run_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS transformer_model_context_state (
                    model TEXT PRIMARY KEY,
                    context_ceiling INTEGER NOT NULL,
                    success_streak INTEGER NOT NULL DEFAULT 0,
                    memory_pressure_count INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                """
            )

    @staticmethod
    def fingerprint(request: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            dict(request),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def start_or_resume(
        self, *, pipeline_kind: str, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        request_json = json.dumps(
            dict(request),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        fingerprint = self.fingerprint(request)
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, created_at
                FROM transformer_pipeline_run
                WHERE request_fingerprint = ? AND status = 'active'
                """,
                (fingerprint,),
            ).fetchone()
            resumed = row is not None
            run_id = str(row["run_id"]) if row is not None else uuid.uuid4().hex
            if row is None:
                connection.execute(
                    """
                    INSERT INTO transformer_pipeline_run(
                        run_id, request_fingerprint, pipeline_kind, request_json,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (run_id, fingerprint, pipeline_kind, request_json, now, now),
                )
            else:
                connection.execute(
                    """
                    UPDATE transformer_pipeline_run
                    SET request_json = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (request_json, now, run_id),
                )
            stages = connection.execute(
                """
                SELECT stage_index, stage_name, model, prompt, result_json,
                       output_text, completed
                FROM transformer_pipeline_stage
                WHERE run_id = ?
                ORDER BY stage_index
                """,
                (run_id,),
            ).fetchall()
        return {
            "run_id": run_id,
            "resumed": resumed,
            "stages": [
                {
                    "stage_index": int(stage["stage_index"]),
                    "stage_name": str(stage["stage_name"]),
                    "model": str(stage["model"]),
                    "prompt": str(stage["prompt"]),
                    "result": json.loads(str(stage["result_json"])),
                    "output_text": str(stage["output_text"]),
                    "completed": bool(stage["completed"]),
                }
                for stage in stages
            ],
        }

    def record_stage(
        self,
        *,
        run_id: str,
        stage_index: int,
        stage_name: str,
        model: str,
        prompt: str,
        result: Mapping[str, Any],
    ) -> None:
        result_json = json.dumps(
            dict(result), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            default=str,
        )
        output_text = str(result.get("text") or "")
        completed = int(result.get("ok") is True)
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transformer_pipeline_stage(
                    run_id, stage_index, stage_name, model, prompt, result_json,
                    output_text, completed, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, stage_index) DO UPDATE SET
                    stage_name = excluded.stage_name,
                    model = excluded.model,
                    prompt = excluded.prompt,
                    result_json = excluded.result_json,
                    output_text = excluded.output_text,
                    completed = excluded.completed,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    int(stage_index),
                    stage_name,
                    model,
                    prompt,
                    result_json,
                    output_text,
                    completed,
                    now,
                ),
            )
            connection.execute(
                "UPDATE transformer_pipeline_run SET updated_at = ? WHERE run_id = ?",
                (now, run_id),
            )

    def complete(self, run_id: str) -> None:
        # Checkpoints are temporary; a completed workflow no longer needs recovery data.
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM transformer_pipeline_run WHERE run_id = ?", (run_id,)
            )

    def load_context_state(self, model: str) -> dict[str, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT context_ceiling, success_streak, memory_pressure_count
                FROM transformer_model_context_state WHERE model = ?
                """,
                (model,),
            ).fetchone()
        if row is None:
            return None
        return {
            "context_ceiling": int(row["context_ceiling"]),
            "success_streak": int(row["success_streak"]),
            "memory_pressure_count": int(row["memory_pressure_count"]),
        }

    def save_context_state(self, model: str, state: Mapping[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transformer_model_context_state(
                    model, context_ceiling, success_streak,
                    memory_pressure_count, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(model) DO UPDATE SET
                    context_ceiling = excluded.context_ceiling,
                    success_streak = excluded.success_streak,
                    memory_pressure_count = excluded.memory_pressure_count,
                    updated_at = excluded.updated_at
                """,
                (
                    model,
                    int(state.get("context_ceiling") or 0),
                    int(state.get("success_streak") or 0),
                    int(state.get("memory_pressure_count") or 0),
                    time.time(),
                ),
            )


__all__ = ["TransformerRuntimeCheckpointRepository"]
