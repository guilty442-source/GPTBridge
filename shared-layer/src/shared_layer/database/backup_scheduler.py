"""Backup Scheduler (Blueprint D5).

Schedules backups per RPO/RTO class (migration 028/029).
Integrates with backup_orchestrator and backup_catalog.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from psycopg import Connection

from .backup import BackupOrchestrator, BackupResult
from .config import DatabaseSettings

_logger = logging.getLogger("gptbridge.backup_scheduler")


@dataclass
class ScheduledBackup:
    """A scheduled backup job."""
    engine: str
    rpo_seconds: int
    last_backup: Optional[datetime] = None
    next_backup: Optional[datetime] = None
    consecutive_failures: int = 0


class BackupScheduler:
    """Periodic backup scheduler driven by RPO/RTO classes."""

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        orchestrator: Optional[BackupOrchestrator] = None,
        backup_root: Optional[str] = None,
        check_interval_seconds: int = 60,
    ) -> None:
        self.settings = settings
        self.orchestrator = orchestrator or BackupOrchestrator(settings)
        self.backup_root = backup_root or settings.backup_root
        self.check_interval = check_interval_seconds
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._jobs: dict[str, ScheduledBackup] = {}
        self._load_rpo_rto()

    def _load_rpo_rto(self) -> None:
        """Load RPO/RTO classes from database."""
        try:
            with self._get_connection() as conn:
                rows = conn.execute(
                    "SELECT engine, rpo_seconds, rto_seconds, backup_frequency_seconds "
                    "FROM gptbridge_index.rpo_rto_class ORDER BY engine"
                ).fetchall()
                for r in rows:
                    engine = str(r[0])
                    rpo = int(r[1])
                    rto = int(r[2])
                    freq = int(r[3]) if r[3] else rpo
                    self._jobs[engine] = ScheduledBackup(
                        engine=engine,
                        rpo_seconds=rpo,
                    )
                    _logger.info("BackupScheduler: loaded job for %s (RPO=%ss, freq=%ss)", engine, rpo, freq)
        except Exception as e:
            _logger.warning("BackupScheduler: failed to load RPO/RTO classes: %s", e)

    def _get_connection(self) -> Connection[Any]:
        """Get a database connection."""
        from .connection import database_dsn
        return Connection.connect(database_dsn(self.settings.admin_dsn, self.settings.database))

    def _record_backup_catalog(
        self,
        engine: str,
        result: BackupResult,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        """Record backup result in backup_catalog."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO gptbridge_audit.backup_catalog
                    (engine, backup_path, size_bytes, status, error, started_at, completed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        engine,
                        str(result.path),
                        result.size,
                        status,
                        error,
                        datetime.now(timezone.utc),
                        datetime.now(timezone.utc),
                    ),
                )
        except Exception as e:
            _logger.error("BackupScheduler: failed to record backup catalog: %s", e)

    def _run_backup(self, job: ScheduledBackup) -> bool:
        """Execute a single backup job. Returns True on success."""
        engine = job.engine
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = f"{self.backup_root}/{engine}/{timestamp}.dump"

        try:
            _logger.info("BackupScheduler: starting backup for %s", engine)
            result = self.orchestrator.backup(backup_path)
            self._record_backup_catalog(engine, result, "success")
            job.last_backup = datetime.now(timezone.utc)
            job.consecutive_failures = 0
            _logger.info("BackupScheduler: backup completed for %s (%d bytes)", engine, result.size)
            return True
        except Exception as e:
            _logger.error("BackupScheduler: backup failed for %s: %s", engine, e)
            job.consecutive_failures += 1
            self._record_backup_catalog(engine, BackupResult(path="", size=0), "failed", str(e))
            return False

    def _schedule_next(self, job: ScheduledBackup) -> None:
        """Calculate next backup time based on frequency."""
        freq = job.rpo_seconds  # Use RPO as frequency for now
        job.next_backup = datetime.now(timezone.utc).replace(microsecond=0)
        job.next_backup = job.next_backup.replace(second=job.next_backup.second + freq)

    def _scheduler_loop(self) -> None:
        """Main scheduler loop."""
        while self._running:
            try:
                with self._lock:
                    now = datetime.now(timezone.utc)
                    for job in self._jobs.values():
                        if job.next_backup is None or now >= job.next_backup:
                            self._run_backup(job)
                            self._schedule_next(job)
            except Exception as e:
                _logger.error("BackupScheduler: loop error: %s", e)

            time.sleep(self.check_interval)

    def start(self) -> None:
        """Start the scheduler in a background thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
            # Initialize next backup times
            for job in self._jobs.values():
                self._schedule_next(job)
            self._thread = threading.Thread(target=self._scheduler_loop, daemon=True, name="backup-scheduler")
            self._thread.start()
            _logger.info("BackupScheduler: started")

    def stop(self) -> None:
        """Stop the scheduler."""
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        _logger.info("BackupScheduler: stopped")

    def trigger_backup(self, engine: str) -> bool:
        """Manually trigger a backup for an engine."""
        with self._lock:
            job = self._jobs.get(engine)
            if not job:
                _logger.warning("BackupScheduler: no job for engine %s", engine)
                return False
        return self._run_backup(job)

    def get_status(self) -> dict[str, Any]:
        """Get scheduler status."""
        with self._lock:
            return {
                "running": self._running,
                "jobs": {
                    engine: {
                        "rpo_seconds": job.rpo_seconds,
                        "last_backup": job.last_backup.isoformat() if job.last_backup else None,
                        "next_backup": job.next_backup.isoformat() if job.next_backup else None,
                        "consecutive_failures": job.consecutive_failures,
                    }
                    for engine, job in self._jobs.items()
                }
            }


# Global instance
_scheduler: BackupScheduler | None = None
_scheduler_lock = threading.Lock()


def get_backup_scheduler(
    settings: Optional[DatabaseSettings] = None,
    **kwargs: Any,
) -> BackupScheduler:
    """Get or create the global backup scheduler."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            if settings is None:
                raise ValueError("settings required on first call")
            _scheduler = BackupScheduler(settings, **kwargs)
        return _scheduler


__all__ = [
    "BackupScheduler",
    "ScheduledBackup",
    "get_backup_scheduler",
]