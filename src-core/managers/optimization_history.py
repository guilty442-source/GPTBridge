from __future__ import annotations

import sqlite3
from pathlib import Path


class OptimizationHistoryManager:
    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            # Default to the centralized SQLite database location
            self.db_path = Path(__file__).resolve().parent.parent.parent / "runtime" / "state" / "gptbridge.sqlite3"
        else:
            self.db_path = db_path
            
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS optimization_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    entry TEXT NOT NULL
                )
                """
            )
            # Create an index for faster date range queries
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_optimization_history_timestamp 
                ON optimization_history(timestamp)
                """
            )
            conn.commit()

    def record(self, entry: str) -> Path:
        """Records a new entry in the SQLite database."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO optimization_history (entry) VALUES (?)",
                (entry,)
            )
            conn.commit()
        return self.db_path

    def read_history(self, limit: int | None = None) -> str:
        """Reads recent history from the database, returning it as a formatted string for compatibility."""
        with self._connect() as conn:
            query = "SELECT timestamp, entry FROM optimization_history ORDER BY id ASC"
            params: tuple = ()
            
            # If limit is provided, we still want chronological order, 
            # so we fetch the last N rows using a subquery
            if limit and limit > 0:
                query = """
                    SELECT timestamp, entry FROM (
                        SELECT * FROM optimization_history 
                        ORDER BY id DESC LIMIT ?
                    ) ORDER BY id ASC
                """
                params = (limit,)
                
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            
            lines = []
            for row in rows:
                timestamp = row["timestamp"]
                entry = row["entry"]
                lines.append(f"[{timestamp}] {entry}")
                
            return "\n".join(lines) + ("\n" if lines else "")
