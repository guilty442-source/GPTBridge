from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from .investment_privacy import (
    decode_binary_document,
    decode_json_document,
    encode_binary_document,
    privacy_status,
    protect_text,
    unprotect_text,
)
from .investment_repository import (
    _iter_migration_files,
    _validated_storage_path,
)
from .investment_contract import INVESTMENT_ANALYTICS_SCHEMA_VERSION


SCHEMA_VERSION = INVESTMENT_ANALYTICS_SCHEMA_VERSION
TRADING_DAYS = 252
DEFAULT_BENCHMARK = "^GSPC"
DEFAULT_ALERT_COOLDOWN_MINUTES = 240
OPENING_BALANCE_PREFIX = "opening-balance:"
RECONCILIATION_PREFIX = "ledger-reconciliation:"
DATA_ROOT_ENV = "GPTBRIDGE_AI_ASSISTANT_DATA_ROOT"
PROFILE_ENV = "GPTBRIDGE_AI_ASSISTANT_PROFILE"
DEFAULT_BACKUP_RETENTION = 1
DEFAULT_AUTOMATIC_BACKUP_RETENTION = 1
FetchJson = Callable[[str], Any]


class InvestmentAnalyticsUpgradeRequired(RuntimeError):
    """Raised when a database requires a newer investment manager."""


def _portable_sqlite_image(data: bytes) -> bytes:
    """Make serialized WAL databases self-contained for memory deserialization."""
    if len(data) >= 20 and data.startswith(b"SQLite format 3\x00") and data[18:20] == b"\x02\x02":
        normalized = bytearray(data)
        normalized[18] = 1
        normalized[19] = 1
        return bytes(normalized)
    return data


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat()


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromtimestamp(float(text), timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def normalized_probability(value: Any, default: float = 0.5) -> float:
    """Accept legacy percentages or canonical 0..1 probabilities."""
    parsed = number(value, default)
    if parsed > 1:
        parsed /= 100
    return max(0.0, min(1.0, parsed))


def rounded(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _normalized_profile(value: Any) -> str:
    profile = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "default").strip())
    return profile.strip(".-")[:64] or "default"


def _pid_is_alive(pid: int) -> bool:
    """Compatibility diagnostic; ownership decisions use OS file locks."""

    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if process:
            try:
                exit_code = ctypes.c_ulong()
                if ctypes.windll.kernel32.GetExitCodeProcess(
                    process,
                    ctypes.byref(exit_code),
                ):
                    return exit_code.value == 259  # STILL_ACTIVE
                return True
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _try_lock_descriptor(descriptor: int) -> bool:
    """Acquire an OS-owned exclusive lock without trusting a PID file."""

    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_descriptor(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


class _RuntimeOwnerLock:
    def __init__(self, path: Path, component: str) -> None:
        self.path = path
        self.component = component
        self.token = uuid.uuid4().hex
        self.acquired = False
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self.acquired:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _validated_storage_path(
            self.path.parent,
            label=f"{self.component} ownership lock directory",
            require_exists=True,
            expected_kind="directory",
        )
        _validated_storage_path(
            self.path,
            label=f"{self.component} ownership lock",
            boundary=self.path.parent,
            expected_kind="file",
        )
        descriptor = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        descriptor_locked = False
        try:
            descriptor_locked = _try_lock_descriptor(descriptor)
            if not descriptor_locked:
                owner_pid = ""
                try:
                    owner = json.loads(self.path.read_text(encoding="utf-8"))
                    parsed_pid = int(owner.get("pid") or 0)
                    owner_pid = f" (pid={parsed_pid})" if parsed_pid > 0 else ""
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
                raise RuntimeError(
                    f"Another process is already active for {self.component}"
                    f"{owner_pid}"
                )
            document = {
                "pid": os.getpid(),
                "token": self.token,
                "component": self.component,
                "acquired_at": utc_text(),
            }
            payload = (json.dumps(document, ensure_ascii=False) + "\n").encode("utf-8")
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, payload)
            os.ftruncate(descriptor, len(payload))
            os.fsync(descriptor)
        except Exception:
            try:
                if descriptor_locked:
                    _unlock_descriptor(descriptor)
            except OSError:
                pass
            finally:
                os.close(descriptor)
            raise
        _fsync_directory(self.path.parent)
        self._descriptor = descriptor
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        descriptor = self._descriptor
        self._descriptor = None
        self.acquired = False
        if descriptor is None:
            return
        try:
            _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)


def _runtime_root(tool_root: Path) -> Path:
    configured_root = str(os.environ.get(DATA_ROOT_ENV) or "").strip()
    profile = _normalized_profile(os.environ.get(PROFILE_ENV))
    if configured_root:
        base = _validated_storage_path(
            Path(configured_root),
            label="Configured AI investment data root",
            expected_kind="directory",
        )
        return _validated_storage_path(
            base / profile,
            label="AI investment analytics profile root",
            boundary=base,
            expected_kind="directory",
        )
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app_data and tool_root.name.lower() == "ai-assistant":
        base = _validated_storage_path(
            Path(local_app_data),
            label="Local application data root",
            expected_kind="directory",
        )
        return _validated_storage_path(
            base / "GPTBridge" / "ai-assistant" / profile,
            label="AI investment analytics profile root",
            boundary=base,
            expected_kind="directory",
        )
    return _validated_storage_path(
        tool_root / "runtime",
        label="AI investment analytics runtime root",
        boundary=tool_root,
        expected_kind="directory",
    )


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    _validated_storage_path(
        path.parent,
        label="Investment analytics data directory",
        expected_kind="directory",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _validated_storage_path(
        path.parent,
        label="Investment analytics data directory",
        require_exists=True,
        expected_kind="directory",
    )
    _validated_storage_path(
        path,
        label="Investment analytics data file",
        boundary=path.parent,
        expected_kind="file",
    )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _copy_verified(source: Path, destination: Path) -> bool:
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if destination.exists():
        return hashlib.sha256(destination.read_bytes()).hexdigest() == digest
    _atomic_write_bytes(destination, payload)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        raise OSError(f"runtime migration verification failed: {destination}")
    return True


def _decoded_json(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _variance(values: Sequence[float]) -> float:
    return statistics.variance(values) if len(values) > 1 else 0.0


def _covariance(left: Sequence[float], right: Sequence[float]) -> float:
    count = min(len(left), len(right))
    if count < 2:
        return 0.0
    left_values = list(left[-count:])
    right_values = list(right[-count:])
    left_mean = _mean(left_values)
    right_mean = _mean(right_values)
    return sum(
        (left_values[index] - left_mean) * (right_values[index] - right_mean)
        for index in range(count)
    ) / (count - 1)


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    covariance = _covariance(left, right)
    denominator = math.sqrt(_variance(left) * _variance(right))
    return covariance / denominator if denominator > 0 else None


def _returns(values: Sequence[float]) -> list[float]:
    output: list[float] = []
    for previous, current in zip(values, values[1:]):
        if previous > 0:
            output.append(current / previous - 1)
    return output


def _max_drawdown(values: Sequence[float]) -> float | None:
    if not values:
        return None
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1)
    return worst


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, int(math.floor(percentile * (len(ordered) - 1)))))
    return ordered[position]


def _annualized_return(values: Sequence[float]) -> float | None:
    if len(values) < 2 or values[0] <= 0 or values[-1] <= 0:
        return None
    years = (len(values) - 1) / TRADING_DAYS
    return (values[-1] / values[0]) ** (1 / years) - 1 if years > 0 else None


def _xnpv(rate: float, cashflows: Sequence[tuple[datetime, float]]) -> float:
    if not cashflows:
        return 0.0
    origin = cashflows[0][0]
    return sum(
        amount / ((1 + rate) ** max(0.0, (date - origin).total_seconds() / 31_557_600))
        for date, amount in cashflows
    )


def xirr(cashflows: Sequence[tuple[datetime, float]]) -> float | None:
    ordered = sorted(cashflows, key=lambda item: item[0])
    if not ordered or not any(amount < 0 for _, amount in ordered) or not any(amount > 0 for _, amount in ordered):
        return None
    low, high = -0.9999, 100.0
    low_value, high_value = _xnpv(low, ordered), _xnpv(high, ordered)
    if low_value * high_value > 0:
        return None
    for _ in range(160):
        middle = (low + high) / 2
        value = _xnpv(middle, ordered)
        if abs(value) < 1e-8:
            return middle
        if value * low_value > 0:
            low, low_value = middle, value
        else:
            high, high_value = middle, value
    return (low + high) / 2


class InvestmentAnalyticsStore:
    def __init__(self, tool_root: Path) -> None:
        self.tool_root = _validated_storage_path(
            Path(tool_root),
            label="AI assistant tool root",
            require_exists=True,
            expected_kind="directory",
        )
        self.legacy_runtime_root = _validated_storage_path(
            self.tool_root / "runtime",
            label="Legacy AI investment analytics runtime root",
            boundary=self.tool_root,
            expected_kind="directory",
        )
        self.runtime_root = _runtime_root(self.tool_root)
        self.database_path = self.runtime_root / "investment_analytics_v2.sqlite3"
        managed_storage = str(os.environ.get("GPTBRIDGE_MANAGED_STORAGE_ROOT") or "").strip()
        self.managed_storage_root = (
            Path(managed_storage).resolve()
            if managed_storage
            else self.runtime_root
        )
        self.backup_root = (
            self.managed_storage_root
            / "backups"
            / "ai-assistant"
            / "investment"
            if managed_storage
            else self.runtime_root / "investment_backups"
        )
        self.audit_root = (
            self.managed_storage_root / "audit" / "ai-assistant"
            if managed_storage
            else self.runtime_root / "audit"
        )
        self.audit_path = self.audit_root / "investment.jsonl"
        self.recovery_root = self.runtime_root / "recovery"
        self._database_lock = threading.RLock()
        self._database_key_id = ""
        self._batch_depth = 0
        self._pending_persist = False
        self._closed = False
        self._database_connection = sqlite3.connect(":memory:", check_same_thread=False)
        self._database_connection.row_factory = sqlite3.Row
        self._database_connection.execute("PRAGMA foreign_keys = ON")
        self._database_connection.execute("PRAGMA busy_timeout = 10000")
        self._durable_database_image: bytes | None = None
        _validated_storage_path(
            self.runtime_root,
            label="AI investment analytics runtime root",
            boundary=self.runtime_root,
            expected_kind="directory",
        )
        _validated_storage_path(
            self.backup_root,
            label="AI investment analytics backup root",
            boundary=self.managed_storage_root,
            expected_kind="directory",
        )
        _validated_storage_path(
            self.audit_root,
            label="AI investment analytics audit root",
            boundary=self.managed_storage_root,
            expected_kind="directory",
        )
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        _validated_storage_path(
            self.runtime_root,
            label="AI investment analytics runtime root",
            boundary=self.runtime_root,
            require_exists=True,
            expected_kind="directory",
        )
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.audit_root.mkdir(parents=True, exist_ok=True)
        _validated_storage_path(
            self.backup_root,
            label="AI investment analytics backup root",
            boundary=self.managed_storage_root,
            require_exists=True,
            expected_kind="directory",
        )
        _validated_storage_path(
            self.database_path,
            label="AI investment analytics database",
            boundary=self.runtime_root,
            expected_kind="file",
        )
        self._owner_lock = _RuntimeOwnerLock(
            self.runtime_root / ".investment-analytics-owner.lock",
            "AI investment analytics",
        )
        try:
            self._owner_lock.acquire()
            self._migrate_legacy_runtime()
            self._load_database()
            self.initialize()
        except Exception:
            self._database_connection.close()
            self._closed = True
            self._owner_lock.release()
            raise

    def _migrate_legacy_runtime(self) -> None:
        if self.runtime_root == self.legacy_runtime_root:
            return
        if (
            not self.legacy_runtime_root.exists()
            and not self.legacy_runtime_root.is_symlink()
        ):
            return
        legacy_runtime = _validated_storage_path(
            self.legacy_runtime_root,
            label="Legacy AI investment analytics runtime root",
            boundary=self.tool_root,
            require_exists=True,
            expected_kind="directory",
        )
        candidates: list[tuple[Path, Path]] = []
        legacy_database = legacy_runtime / self.database_path.name
        if legacy_database.exists() or legacy_database.is_symlink():
            candidates.append(
                (
                    _validated_storage_path(
                        legacy_database,
                        label="Legacy investment analytics database",
                        boundary=legacy_runtime,
                        require_exists=True,
                        expected_kind="file",
                    ),
                    self.database_path,
                )
            )
        for relative_root in ("investment_backups", "state"):
            source_root = legacy_runtime / relative_root
            if not source_root.exists() and not source_root.is_symlink():
                continue
            source_root = _validated_storage_path(
                source_root,
                label=f"Legacy investment analytics {relative_root} root",
                boundary=legacy_runtime,
                require_exists=True,
                expected_kind="directory",
            )
            for source in _iter_migration_files(
                source_root,
                label=f"Legacy investment analytics {relative_root}",
            ):
                destination = _validated_storage_path(
                    self.runtime_root / source.relative_to(legacy_runtime),
                    label="Migrated investment analytics file",
                    boundary=self.runtime_root,
                    expected_kind="file",
                )
                candidates.append((source, destination))
        copied: list[dict[str, Any]] = []
        conflicts: list[str] = []
        for source, destination in candidates:
            relative = destination.relative_to(self.runtime_root)
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
                    conflicts.append(str(relative))
                continue
            _copy_verified(source, destination)
            copied.append(
                {
                    "path": str(relative),
                    "size": source.stat().st_size,
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                }
            )
        if copied or conflicts:
            manifest = {
                "migration": "investment-runtime-v1",
                "created_at": utc_text(),
                "source": str(self.legacy_runtime_root),
                "destination": str(self.runtime_root),
                "copied": copied,
                "conflicts": conflicts,
                "legacy_preserved": True,
            }
            _atomic_write_bytes(
                self.runtime_root / "runtime-migration-manifest.json",
                (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._database_lock:
            before_changes = self._database_connection.total_changes
            savepoint = (
                f"gptbridge_connect_{uuid.uuid4().hex}"
                if self._batch_depth > 0
                else ""
            )
            if savepoint:
                self._database_connection.execute(f"SAVEPOINT {savepoint}")
            try:
                yield self._database_connection
            except Exception:
                if savepoint:
                    self._database_connection.execute(
                        f"ROLLBACK TO SAVEPOINT {savepoint}"
                    )
                    self._database_connection.execute(
                        f"RELEASE SAVEPOINT {savepoint}"
                    )
                else:
                    self._database_connection.rollback()
                raise
            else:
                changed = (
                    self._database_connection.total_changes != before_changes
                )
                if savepoint:
                    self._database_connection.execute(
                        f"RELEASE SAVEPOINT {savepoint}"
                    )
                    if changed:
                        self._pending_persist = True
                    return
                self._database_connection.commit()
                if not changed:
                    return
                try:
                    self._persist_database()
                except Exception:
                    self._restore_durable_database_image()
                    raise

    @contextmanager
    def exclusive_data_access(self) -> Iterator[None]:
        """Hold the database side of the repository -> database lock order."""

        with self._database_lock:
            yield

    @contextmanager
    def batch_updates(self) -> Iterator[None]:
        """Run nested writes as one durable all-or-nothing transaction."""

        with self._database_lock:
            outermost = self._batch_depth == 0
            savepoint = (
                ""
                if outermost
                else f"gptbridge_batch_{uuid.uuid4().hex}"
            )
            if outermost:
                self._database_connection.execute("BEGIN IMMEDIATE")
                self._pending_persist = False
            else:
                self._database_connection.execute(f"SAVEPOINT {savepoint}")
            self._batch_depth += 1
            try:
                yield
            except Exception:
                self._batch_depth = max(0, self._batch_depth - 1)
                if outermost:
                    self._database_connection.rollback()
                    self._pending_persist = False
                else:
                    self._database_connection.execute(
                        f"ROLLBACK TO SAVEPOINT {savepoint}"
                    )
                    self._database_connection.execute(
                        f"RELEASE SAVEPOINT {savepoint}"
                    )
                raise
            else:
                self._batch_depth = max(0, self._batch_depth - 1)
                if not outermost:
                    self._database_connection.execute(
                        f"RELEASE SAVEPOINT {savepoint}"
                    )
                    return
                changed = self._pending_persist
                self._pending_persist = False
                try:
                    self._database_connection.commit()
                except Exception:
                    self._database_connection.rollback()
                    raise
                if not changed:
                    return
                try:
                    self._persist_database()
                except Exception:
                    self._restore_durable_database_image()
                    raise

    def _restore_durable_database_image(self) -> None:
        if self._durable_database_image is None:
            self._database_connection.close()
            self._database_connection = sqlite3.connect(
                ":memory:",
                check_same_thread=False,
            )
        else:
            self._database_connection.deserialize(self._durable_database_image)
        self._database_connection.row_factory = sqlite3.Row
        self._database_connection.execute("PRAGMA foreign_keys = ON")
        self._database_connection.execute("PRAGMA busy_timeout = 10000")

    def _loaded_schema_version(self) -> int:
        try:
            table = self._database_connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='metadata'"
            ).fetchone()
            if table is None:
                return 0
            row = self._database_connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                return 0
            value = str(row[0]).strip()
            if not value.isdigit():
                raise ValueError("investment analytics schema version is invalid")
            version = int(value)
            if version < 1:
                raise ValueError("investment analytics schema version is invalid")
            return version
        except sqlite3.DatabaseError as exc:
            raise ValueError(
                "investment analytics schema metadata cannot be read"
            ) from exc

    def _preserve_pre_schema_migration(
        self,
        database_bytes: bytes,
        *,
        previous_schema_version: int,
    ) -> None:
        stamp = utc_now().strftime("%Y%m%dT%H%M%S")
        backup_id = uuid.uuid4().hex
        stem = (
            f"pre-schema-v{previous_schema_version}-to-v{SCHEMA_VERSION}-"
            f"{stamp}-{backup_id[:8]}"
        )
        path = self.backup_root / f"{stem}.ivault"
        encoded = encode_binary_document(
            _portable_sqlite_image(database_bytes),
            purpose="investment-database-pre-schema-migration",
        )
        _atomic_write_bytes(path, encoded)
        manifest = {
            "format": "gptbridge-investment-backup-v1",
            "backup_id": backup_id,
            "created_at": utc_text(),
            "label": "pre-schema-migration",
            "schema_version": previous_schema_version,
            "target_schema_version": SCHEMA_VERSION,
            "encrypted": True,
            "files": [
                {
                    "role": "analytics_database",
                    "name": path.name,
                    "size": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
            ],
        }
        _atomic_write_bytes(
            self.backup_root / f"{stem}.manifest.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )

    def _load_database(self) -> None:
        if not self.database_path.exists() or self.database_path.stat().st_size == 0:
            return
        try:
            raw = self.database_path.read_bytes()
            if raw.startswith(b"SQLite format 3\x00"):
                source = sqlite3.connect(self.database_path)
                try:
                    source.execute("PRAGMA busy_timeout = 10000")
                    source.backup(self._database_connection)
                finally:
                    source.close()
                database_bytes = _portable_sqlite_image(
                    self._database_connection.serialize()
                )
            else:
                database_bytes, envelope = decode_binary_document(raw)
                database_bytes = _portable_sqlite_image(database_bytes)
                self._database_key_id = str(envelope.get("key_id") or "")
                self._database_connection.deserialize(database_bytes)
            result = self._database_connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()
            if not result or str(result[0]).lower() != "ok":
                raise ValueError("investment analytics database integrity check failed")
            previous_schema_version = self._loaded_schema_version()
            if previous_schema_version > SCHEMA_VERSION:
                raise InvestmentAnalyticsUpgradeRequired(
                    "投資分析資料庫由較新的程式版本建立；"
                    "為避免舊版覆寫，請先升級投資管家。"
                )
            if previous_schema_version < SCHEMA_VERSION:
                self._preserve_pre_schema_migration(
                    database_bytes,
                    previous_schema_version=previous_schema_version,
                )
            self._durable_database_image = database_bytes
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            _validated_storage_path(
                self.recovery_root,
                label="AI investment analytics recovery root",
                boundary=self.runtime_root,
                expected_kind="directory",
            )
            self.recovery_root.mkdir(parents=True, exist_ok=True)
            _validated_storage_path(
                self.recovery_root,
                label="AI investment analytics recovery root",
                boundary=self.runtime_root,
                require_exists=True,
                expected_kind="directory",
            )
            stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
            preserved = self.recovery_root / f"investment_analytics.{stamp}.corrupt"
            try:
                _copy_verified(self.database_path, preserved)
            except OSError:
                preserved = self.database_path
            marker = {
                "status": "recovery_required",
                "detected_at": utc_text(),
                "source": str(self.database_path),
                "preserved_copy": str(preserved),
                "error_type": type(exc).__name__,
            }
            _atomic_write_bytes(
                self.recovery_root / "latest-database-recovery-required.json",
                (json.dumps(marker, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )
            raise RuntimeError(
                "投資分析資料庫無法解密或通過完整性檢查；原檔已保留，請從備份恢復。"
            ) from exc

    def _persist_database(self, *, rotate_key: bool = False) -> None:
        database_bytes = _portable_sqlite_image(self._database_connection.serialize())
        key_id = uuid.uuid4().hex if rotate_key or not self._database_key_id else self._database_key_id
        encoded = encode_binary_document(
            database_bytes,
            purpose="investment-analytics-database-v3",
            key_id=key_id,
        )
        _atomic_write_bytes(self.database_path, encoded)
        self._database_key_id = key_id
        self._durable_database_image = database_bytes

    def _preserve_incomplete_backup(
        self,
        paths: Sequence[Path],
        *,
        backup_id: str,
        error: Exception,
    ) -> None:
        recovery_dir = self.recovery_root / "incomplete-backups" / backup_id
        _validated_storage_path(
            recovery_dir,
            label="Incomplete investment backup recovery directory",
            boundary=self.runtime_root,
            expected_kind="directory",
        )
        recovery_dir.mkdir(parents=True, exist_ok=True)
        _validated_storage_path(
            recovery_dir,
            label="Incomplete investment backup recovery directory",
            boundary=self.runtime_root,
            require_exists=True,
            expected_kind="directory",
        )
        preserved: list[str] = []
        for path in paths:
            if not path.exists():
                continue
            destination = recovery_dir / path.name
            os.replace(path, destination)
            preserved.append(str(destination))
        marker = {
            "status": "incomplete_backup_preserved",
            "detected_at": utc_text(),
            "backup_id": backup_id,
            "error_type": type(error).__name__,
            "preserved": preserved,
        }
        _atomic_write_bytes(
            recovery_dir / "recovery.json",
            (json.dumps(marker, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
        _fsync_directory(recovery_dir)

    def backup_database(self, label: str = "manual") -> dict[str, Any]:
        normalized_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(label or "manual"))[:24] or "manual"
        with self._database_lock:
            stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
            backup_id = uuid.uuid4().hex
            stem = f"{stamp}-{normalized_label}-{backup_id[:8]}"
            path = self.backup_root / f"{stem}.ivault"
            manifest_path = self.backup_root / f"{stem}.manifest.json"
            state_path = self.backup_root / f"{stem}.statevault"
            created_paths: list[Path] = []
            try:
                encoded_database = encode_binary_document(
                    _portable_sqlite_image(
                        self._database_connection.serialize()
                    ),
                    purpose="investment-analytics-backup-v3",
                )
                _atomic_write_bytes(path, encoded_database)
                created_paths.append(path)
                files = [
                    {
                        "role": "analytics_database",
                        "name": path.name,
                        "size": len(encoded_database),
                        "sha256": hashlib.sha256(
                            encoded_database
                        ).hexdigest(),
                    }
                ]
                state_source = (
                    self.runtime_root
                    / "state"
                    / "investment_watch_state.json"
                )
                if (
                    state_source.is_file()
                    and state_source.stat().st_size > 0
                ):
                    state_payload = state_source.read_bytes()
                    # Validate protected state before publishing its backup.
                    decode_json_document(state_payload.decode("utf-8"))
                    _atomic_write_bytes(state_path, state_payload)
                    created_paths.append(state_path)
                    files.append(
                        {
                            "role": "portfolio_state",
                            "name": state_path.name,
                            "size": len(state_payload),
                            "sha256": hashlib.sha256(
                                state_payload
                            ).hexdigest(),
                        }
                    )
                created_at = utc_text()
                manifest = {
                    "format": "gptbridge-investment-backup-v1",
                    "backup_id": backup_id,
                    "created_at": created_at,
                    "label": normalized_label,
                    "schema_version": SCHEMA_VERSION,
                    "encrypted": True,
                    "files": files,
                }
                _atomic_write_bytes(
                    manifest_path,
                    (
                        json.dumps(
                            manifest,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
                created_paths.append(manifest_path)
            except Exception as backup_error:
                self._preserve_incomplete_backup(
                    created_paths,
                    backup_id=backup_id,
                    error=backup_error,
                )
                raise
        self.audit(
            "database_backup",
            {
                "backup_id": backup_id,
                "path": str(path),
                "manifest_path": str(manifest_path),
                "label": normalized_label,
                "state_included": any(item["role"] == "portfolio_state" for item in files),
            },
        )
        self.prune_backups()
        return {
            "backup_id": backup_id,
            "path": str(path),
            "manifest_path": str(manifest_path),
            "created_at": created_at,
            "encrypted": True,
            "state_included": any(item["role"] == "portfolio_state" for item in files),
            "files": files,
        }

    def list_backups(self) -> list[dict[str, Any]]:
        output = []
        for path in sorted(self.backup_root.glob("*.ivault"), reverse=True)[:100]:
            manifest_path = path.with_name(
                f"{path.stem}.manifest.json"
            )
            manifest: dict[str, Any] = {}
            integrity = None
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    declared = next(
                        (
                            item
                            for item in manifest.get("files", [])
                            if isinstance(item, dict) and item.get("role") == "analytics_database"
                        ),
                        {},
                    )
                    integrity = (
                        int(declared.get("size") or -1) == path.stat().st_size
                        and str(declared.get("sha256") or "") == hashlib.sha256(path.read_bytes()).hexdigest()
                    )
                except (OSError, ValueError, json.JSONDecodeError):
                    integrity = False
            output.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "size": path.stat().st_size,
                    "modified_at": datetime.fromtimestamp(
                        path.stat().st_mtime, timezone.utc
                    ).isoformat(),
                    "backup_id": str(manifest.get("backup_id") or ""),
                    "label": str(manifest.get("label") or ""),
                    "manifest_path": str(manifest_path) if manifest_path.exists() else "",
                    "integrity_verified": integrity,
                    "state_included": any(
                        isinstance(item, dict) and item.get("role") == "portfolio_state"
                        for item in manifest.get("files", [])
                    ),
                }
            )
        return output

    def prune_backups(
        self,
        *,
        max_total: int = DEFAULT_BACKUP_RETENTION,
        max_automatic: int = DEFAULT_AUTOMATIC_BACKUP_RETENTION,
    ) -> dict[str, int]:
        # GPTBridge has one global backup generation.  Callers cannot raise
        # this limit because retention authority belongs to project-cleaner.
        max_total = 1
        max_automatic = 1
        paths = sorted(
            self.backup_root.glob("*.ivault"),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
        removed = 0
        for path in paths[max_total:]:
            sidecars = (
                path.with_name(f"{path.stem}.manifest.json"),
                path.with_name(f"{path.stem}.statevault"),
            )
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
            for sidecar in sidecars:
                try:
                    sidecar.unlink(missing_ok=True)
                except OSError:
                    pass

        retained = len(list(self.backup_root.glob("*.ivault")))
        return {
            "retained": retained,
            "removed": removed,
            "over_total_limit": max(0, retained - max_total),
            "over_automatic_limit": 0,
        }

    def restore_database(self, backup_name: str) -> dict[str, Any]:
        candidate = (self.backup_root / Path(str(backup_name)).name).resolve()
        if candidate.parent != self.backup_root.resolve() or not candidate.exists():
            raise ValueError("backup does not exist")
        candidate_payload = candidate.read_bytes()
        manifest_path = candidate.with_name(
            f"{candidate.stem}.manifest.json"
        )
        state_payload: bytes | None = None
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("format") != "gptbridge-investment-backup-v1":
                raise ValueError("unsupported backup manifest")
            database_entries = [
                item
                for item in manifest.get("files", [])
                if isinstance(item, dict)
                and item.get("role") == "analytics_database"
            ]
            state_entries = [
                item
                for item in manifest.get("files", [])
                if isinstance(item, dict)
                and item.get("role") == "portfolio_state"
            ]
            if (
                len(database_entries) != 1
                or Path(str(database_entries[0].get("name") or "")).name
                != candidate.name
                or len(state_entries) > 1
            ):
                raise ValueError("backup manifest is incomplete or ambiguous")
            for item in manifest.get("files", []):
                if not isinstance(item, dict):
                    raise ValueError("backup manifest is invalid")
                related = (self.backup_root / Path(str(item.get("name") or "")).name).resolve()
                if related.parent != self.backup_root.resolve() or not related.exists():
                    raise ValueError("backup is incomplete")
                payload = related.read_bytes()
                if len(payload) != int(item.get("size") or -1):
                    raise ValueError("backup size verification failed")
                if hashlib.sha256(payload).hexdigest() != str(item.get("sha256") or ""):
                    raise ValueError("backup hash verification failed")
                if item.get("role") == "portfolio_state":
                    decode_json_document(payload.decode("utf-8"))
                    state_payload = payload
        database_bytes, _envelope = decode_binary_document(candidate_payload)
        database_bytes = _portable_sqlite_image(database_bytes)
        validation = sqlite3.connect(":memory:")
        try:
            validation.deserialize(database_bytes)
            result = validation.execute("PRAGMA integrity_check").fetchone()
            if not result or str(result[0]).lower() != "ok":
                raise ValueError("backup integrity check failed")
        finally:
            validation.close()
        safety = self.backup_database("before-restore")
        with self._database_lock:
            original_database_payload = (
                self.database_path.read_bytes()
                if self.database_path.exists()
                else None
            )
            original_database_image = _portable_sqlite_image(
                self._database_connection.serialize()
            )
            original_durable_image = self._durable_database_image
            original_key_id = self._database_key_id
            state_path = (
                self.runtime_root / "state" / "investment_watch_state.json"
            )
            original_state_payload = (
                state_path.read_bytes() if state_path.exists() else None
            )
            try:
                self._database_connection.deserialize(database_bytes)
                self._database_connection.row_factory = sqlite3.Row
                self._database_connection.execute("PRAGMA foreign_keys = ON")
                self._database_connection.execute("PRAGMA busy_timeout = 10000")
                self._persist_database(rotate_key=True)
                if state_payload is not None:
                    _atomic_write_bytes(state_path, state_payload)
            except Exception as restore_error:
                rollback_errors: list[str] = []
                try:
                    self._database_connection.deserialize(
                        original_database_image
                    )
                    self._database_connection.row_factory = sqlite3.Row
                    self._database_connection.execute(
                        "PRAGMA foreign_keys = ON"
                    )
                    self._database_connection.execute(
                        "PRAGMA busy_timeout = 10000"
                    )
                    self._durable_database_image = original_durable_image
                    self._database_key_id = original_key_id
                    if original_database_payload is not None:
                        _atomic_write_bytes(
                            self.database_path,
                            original_database_payload,
                        )
                except Exception as rollback_error:
                    rollback_errors.append(
                        f"database:{type(rollback_error).__name__}"
                    )
                try:
                    if original_state_payload is not None:
                        _atomic_write_bytes(
                            state_path,
                            original_state_payload,
                        )
                    elif state_path.exists():
                        failed_state_root = (
                            self.recovery_root
                            / "failed-restore-state"
                        )
                        _validated_storage_path(
                            failed_state_root,
                            label="Failed restore state recovery root",
                            boundary=self.runtime_root,
                            expected_kind="directory",
                        )
                        failed_state_root.mkdir(
                            parents=True,
                            exist_ok=True,
                        )
                        _validated_storage_path(
                            failed_state_root,
                            label="Failed restore state recovery root",
                            boundary=self.runtime_root,
                            require_exists=True,
                            expected_kind="directory",
                        )
                        preserved_state = (
                            failed_state_root
                            / (
                                f"{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-"
                                f"{uuid.uuid4().hex}.statevault"
                            )
                        )
                        os.replace(state_path, preserved_state)
                        _fsync_directory(failed_state_root)
                        _fsync_directory(state_path.parent)
                except Exception as rollback_error:
                    rollback_errors.append(
                        f"state:{type(rollback_error).__name__}"
                    )
                if rollback_errors:
                    _validated_storage_path(
                        self.recovery_root,
                        label="AI investment analytics recovery root",
                        boundary=self.runtime_root,
                        expected_kind="directory",
                    )
                    self.recovery_root.mkdir(parents=True, exist_ok=True)
                    _validated_storage_path(
                        self.recovery_root,
                        label="AI investment analytics recovery root",
                        boundary=self.runtime_root,
                        require_exists=True,
                        expected_kind="directory",
                    )
                    marker = {
                        "status": "restore_rollback_requires_review",
                        "detected_at": utc_text(),
                        "backup": candidate.name,
                        "safety_backup": safety["path"],
                        "restore_error_type": type(restore_error).__name__,
                        "rollback_errors": rollback_errors,
                    }
                    _atomic_write_bytes(
                        self.recovery_root
                        / "latest-restore-rollback-required.json",
                        (
                            json.dumps(
                                marker,
                                ensure_ascii=False,
                                indent=2,
                            )
                            + "\n"
                        ).encode("utf-8"),
                    )
                raise
        self.audit(
            "database_restore",
            {
                "backup": candidate.name,
                "safety_backup": safety["path"],
                "state_restored": state_payload is not None,
            },
            severity="warning",
        )
        return {
            "restored": True,
            "backup": candidate.name,
            "safety_backup": safety,
            "state_restored": state_payload is not None,
        }

    def rotate_database_protection(self) -> dict[str, Any]:
        previous = self._database_key_id
        safety = self.backup_database("before-key-rotation")
        with self._database_lock:
            self._persist_database(rotate_key=True)
        self.audit(
            "database_key_rotation",
            {"previous_key_id": previous, "new_key_id": self._database_key_id},
        )
        return {
            "rotated": True,
            "previous_key_id": previous,
            "key_id": self._database_key_id,
            "safety_backup": safety,
        }

    def database_security_status(self) -> dict[str, Any]:
        raw_prefix = self.database_path.read_bytes()[:16] if self.database_path.exists() else b""
        return {
            "encrypted_at_rest": bool(raw_prefix and not raw_prefix.startswith(b"SQLite format 3")),
            "protection": privacy_status().get("database_encryption"),
            "key_id": self._database_key_id,
            "backup_count": len(self.list_backups()),
            "database_path": str(self.database_path),
        }

    def audit(self, action: str, details: dict[str, Any], *, severity: str = "info") -> None:
        audit_id = uuid.uuid4().hex
        occurred_at = utc_text()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(audit_id, occurred_at, action, severity, details_encrypted) VALUES(?, ?, ?, ?, ?)",
                (audit_id, occurred_at, action, severity, protect_text(_json(details))),
            )
        self._append_managed_audit_record(
            audit_id=audit_id,
            occurred_at=occurred_at,
            action=action,
            severity=severity,
            details=details,
        )

    def _append_managed_audit_record(
        self,
        *,
        audit_id: str,
        occurred_at: str,
        action: str,
        severity: str,
        details: dict[str, Any],
    ) -> None:
        record = {
            "audit_id": audit_id,
            "occurred_at": occurred_at,
            "tool_id": "ai-assistant",
            "action": action,
            "severity": severity,
            "details_encrypted": protect_text(_json(details)),
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as target:
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
            target.flush()
            os.fsync(target.fileno())

    def list_audit_log(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_log ORDER BY occurred_at DESC LIMIT ?",
                (max(1, min(2000, int(limit))),),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["details"] = _decoded_json(
                unprotect_text(str(item.pop("details_encrypted", "") or "")), {}
            )
            output.append(item)
        return output

    def close(self) -> None:
        with self._database_lock:
            if self._closed:
                return
            try:
                self._persist_database()
            finally:
                try:
                    self._database_connection.close()
                finally:
                    self._closed = True
                    self._owner_lock.release()

    def __del__(self) -> None:
        """Release process resources without performing shutdown-time I/O.

        Every mutation is durably persisted by its operation. Explicit
        ``close()`` performs the final encrypted snapshot; garbage collection
        must not invoke DPAPI or filesystem writes while Python is finalizing.
        """

        if getattr(self, "_closed", True):
            return
        try:
            connection = getattr(self, "_database_connection", None)
            if connection is not None:
                connection.close()
        except BaseException:
            pass
        finally:
            self._closed = True
            try:
                owner_lock = getattr(self, "_owner_lock", None)
                if owner_lock is not None:
                    owner_lock.release()
            except BaseException:
                pass

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = MEMORY")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    transaction_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT '',
                    asset_type TEXT NOT NULL DEFAULT '',
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT '',
                    fee REAL NOT NULL DEFAULT 0,
                    tax REAL NOT NULL DEFAULT 0,
                    note_encrypted TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    deleted_at TEXT NOT NULL DEFAULT '',
                    delete_reason_encrypted TEXT NOT NULL DEFAULT '',
                    delete_audit_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_transactions_time ON transactions(occurred_at);
                CREATE INDEX IF NOT EXISTS idx_transactions_symbol ON transactions(symbol, occurred_at);
                CREATE TABLE IF NOT EXISTS prices (
                    symbol TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL NOT NULL,
                    volume REAL,
                    currency TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    verified INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(symbol, observed_at, provider)
                );
                CREATE INDEX IF NOT EXISTS idx_prices_symbol_time ON prices(symbol, observed_at);
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    total_value REAL NOT NULL,
                    total_cost REAL NOT NULL,
                    base_currency TEXT NOT NULL DEFAULT '',
                    cash_value REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS snapshot_positions (
                    snapshot_id TEXT NOT NULL REFERENCES portfolio_snapshots(snapshot_id) ON DELETE CASCADE,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL DEFAULT '',
                    asset_type TEXT NOT NULL DEFAULT '',
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    market_value REAL NOT NULL,
                    cost_value REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(snapshot_id, symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_time ON portfolio_snapshots(observed_at);
                CREATE TABLE IF NOT EXISTS market_events (
                    event_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    source_url_encrypted TEXT NOT NULL DEFAULT '',
                    sentiment REAL,
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    details_encrypted TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_time ON market_events(scheduled_at);
                CREATE TABLE IF NOT EXISTS alert_rules (
                    rule_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    rule_type TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    operator TEXT NOT NULL DEFAULT '>=',
                    threshold REAL,
                    severity TEXT NOT NULL DEFAULT 'warning',
                    cooldown_minutes INTEGER NOT NULL DEFAULT 240,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alert_events (
                    alert_event_id TEXT PRIMARY KEY,
                    rule_id TEXT NOT NULL REFERENCES alert_rules(rule_id) ON DELETE CASCADE,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    triggered_at TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    value REAL,
                    acknowledged_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_alert_events_time ON alert_events(triggered_at);
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    confidence REAL,
                    score REAL,
                    risk_level TEXT NOT NULL DEFAULT '',
                    reference_price REAL,
                    evidence_encrypted TEXT NOT NULL DEFAULT '',
                    snapshot_encrypted TEXT NOT NULL DEFAULT '',
                    user_status TEXT NOT NULL DEFAULT 'pending',
                    outcome_due_at TEXT NOT NULL,
                    outcome_encrypted TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_decisions_due ON decisions(outcome_due_at);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fx_rates (
                    base_currency TEXT NOT NULL,
                    quote_currency TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    rate REAL NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    verified INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(base_currency, quote_currency, observed_at, provider)
                );
                CREATE INDEX IF NOT EXISTS idx_fx_time
                    ON fx_rates(base_currency, quote_currency, observed_at);
                CREATE TABLE IF NOT EXISTS broker_imports (
                    import_id TEXT PRIMARY KEY,
                    imported_at TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_hash TEXT NOT NULL UNIQUE,
                    broker TEXT NOT NULL DEFAULT '',
                    row_count INTEGER NOT NULL DEFAULT 0,
                    matched_count INTEGER NOT NULL DEFAULT 0,
                    difference_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'review',
                    summary_encrypted TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS broker_import_rows (
                    row_id TEXT PRIMARY KEY,
                    import_id TEXT NOT NULL REFERENCES broker_imports(import_id) ON DELETE CASCADE,
                    occurred_at TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    side TEXT NOT NULL DEFAULT '',
                    quantity REAL NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0,
                    amount REAL NOT NULL DEFAULT 0,
                    fee REAL NOT NULL DEFAULT 0,
                    tax REAL NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT '',
                    match_status TEXT NOT NULL DEFAULT 'unmatched',
                    matched_transaction_id TEXT NOT NULL DEFAULT '',
                    raw_encrypted TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_broker_rows_import
                    ON broker_import_rows(import_id, match_status);
                CREATE TABLE IF NOT EXISTS corporate_actions (
                    action_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    effective_at TEXT NOT NULL,
                    ratio REAL,
                    cash_amount REAL,
                    currency TEXT NOT NULL DEFAULT '',
                    old_symbol TEXT NOT NULL DEFAULT '',
                    new_symbol TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending_review',
                    details_encrypted TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_corporate_actions_time
                    ON corporate_actions(symbol, effective_at);
                CREATE TABLE IF NOT EXISTS data_quality_issues (
                    issue_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    issue_type TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL DEFAULT 'warning',
                    title TEXT NOT NULL,
                    detail_encrypted TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    resolved_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS scheduler_runs (
                    run_id TEXT PRIMARY KEY,
                    job_name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    detail_encrypted TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_scheduler_runs_time
                    ON scheduler_runs(started_at);
                CREATE TABLE IF NOT EXISTS import_operations (
                    operation_id TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL UNIQUE,
                    import_fingerprint TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    payload_encrypted TEXT NOT NULL,
                    result_encrypted TEXT NOT NULL DEFAULT '',
                    error_encrypted TEXT NOT NULL DEFAULT '',
                    history_encrypted TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_import_operations_status
                    ON import_operations(status, updated_at);
                CREATE TABLE IF NOT EXISTS notification_channels (
                    channel_id TEXT PRIMARY KEY,
                    channel_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    config_encrypted TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    notification_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    channel_id TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL DEFAULT 'info',
                    title TEXT NOT NULL,
                    body_encrypted TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    sent_at TEXT NOT NULL DEFAULT '',
                    error_encrypted TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_notification_outbox_status
                    ON notification_outbox(status, created_at);
                CREATE TABLE IF NOT EXISTS model_versions (
                    model_version_id TEXT PRIMARY KEY,
                    model_name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    config_encrypted TEXT NOT NULL DEFAULT '',
                    UNIQUE(model_name, version, prompt_hash)
                );
                CREATE TABLE IF NOT EXISTS model_governance_runs (
                    governance_run_id TEXT PRIMARY KEY,
                    model_version_id TEXT NOT NULL REFERENCES model_versions(model_version_id),
                    analysis_run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    output_hash TEXT NOT NULL,
                    data_sources_encrypted TEXT NOT NULL DEFAULT '',
                    metrics_encrypted TEXT NOT NULL DEFAULT '',
                    decision_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_model_governance_time
                    ON model_governance_runs(created_at);
                CREATE TABLE IF NOT EXISTS audit_log (
                    audit_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'info',
                    details_encrypted TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(occurred_at);
                """
            )
            self._migrate_schema(connection)
            connection.execute(
                "INSERT INTO metadata(key, value, updated_at) VALUES('schema_version', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (str(SCHEMA_VERSION), utc_text()),
            )
        self.ensure_default_alerts()

    def _migrate_schema(self, connection: sqlite3.Connection) -> None:
        transaction_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(transactions)").fetchall()
        }
        transaction_additions = {
            "deleted_at": "TEXT NOT NULL DEFAULT ''",
            "delete_reason_encrypted": "TEXT NOT NULL DEFAULT ''",
            "delete_audit_id": "TEXT NOT NULL DEFAULT ''",
        }
        for column, declaration in transaction_additions.items():
            if column not in transaction_columns:
                connection.execute(
                    f"ALTER TABLE transactions ADD COLUMN {column} {declaration}"
                )

        decision_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(decisions)").fetchall()
        }
        additions = {
            "prediction_direction": "TEXT NOT NULL DEFAULT 'abstain'",
            "horizon_days": "INTEGER NOT NULL DEFAULT 30",
            "return_threshold_percent": "REAL NOT NULL DEFAULT 0",
            "eligible_for_calibration": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in additions.items():
            if column not in decision_columns:
                connection.execute(f"ALTER TABLE decisions ADD COLUMN {column} {declaration}")
        marker = connection.execute(
            "SELECT value FROM metadata WHERE key='decision_confidence_normalized_v4'"
        ).fetchone()
        if marker is None:
            # v3 accepted both scales, then divided all inputs by 100. Values at or
            # below 0.01 are therefore recognizable legacy 0..1 inputs.
            connection.execute(
                """
                UPDATE decisions
                SET confidence = CASE
                    WHEN confidence IS NULL THEN NULL
                    WHEN confidence < 0 THEN 0
                    WHEN confidence > 100 THEN 1
                    WHEN confidence > 1 THEN confidence / 100.0
                    WHEN confidence > 0 AND confidence <= 0.01 THEN confidence * 100.0
                    ELSE confidence
                END
                """
            )
            connection.execute(
                "INSERT INTO metadata(key, value, updated_at) VALUES(?, ?, ?)",
                ("decision_confidence_normalized_v4", "complete", utc_text()),
            )

    def clear_data(self) -> dict[str, Any]:
        safety = self.backup_database("before-clear")
        with self.connect() as connection:
            for table in (
                "snapshot_positions",
                "portfolio_snapshots",
                "transactions",
                "prices",
                "market_events",
                "alert_events",
                "decisions",
            ):
                connection.execute(f"DELETE FROM {table}")
        return safety

    def set_setting(self, key: str, value: Any) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, _json(value), utc_text()),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as connection:
            row = connection.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        return _decoded_json(row["value_json"], default) if row else default

    @staticmethod
    def _public_import_operation(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = _decoded_json(
            unprotect_text(str(item.pop("payload_encrypted", "") or "")),
            {},
        )
        item["result"] = _decoded_json(
            unprotect_text(str(item.pop("result_encrypted", "") or "")),
            {},
        )
        item["error"] = unprotect_text(
            str(item.pop("error_encrypted", "") or "")
        )
        item["history"] = _decoded_json(
            unprotect_text(str(item.pop("history_encrypted", "") or "")),
            [],
        )
        return item

    def get_import_operation(self, operation_id: str) -> dict[str, Any] | None:
        normalized = str(operation_id or "").strip()
        if not normalized:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM import_operations WHERE operation_id = ?",
                (normalized,),
            ).fetchone()
        return self._public_import_operation(row) if row is not None else None

    def create_or_resume_import_operation(
        self,
        request_fingerprint: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        fingerprint = str(request_fingerprint or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("invalid import request fingerprint")
        now = utc_text()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM import_operations WHERE request_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row is None:
                operation_id = uuid.uuid4().hex
                history = [
                    {
                        "status": "queued",
                        "occurred_at": now,
                        "reason": "request_created",
                    }
                ]
                connection.execute(
                    """
                    INSERT INTO import_operations(
                        operation_id, request_fingerprint, import_fingerprint,
                        status, payload_encrypted, result_encrypted,
                        error_encrypted, history_encrypted, attempt_count,
                        created_at, updated_at, started_at, finished_at
                    ) VALUES(?, ?, '', 'queued', ?, '', '', ?, 0, ?, ?, '', '')
                    """,
                    (
                        operation_id,
                        fingerprint,
                        protect_text(_json(payload)),
                        protect_text(_json(history)),
                        now,
                        now,
                    ),
                )
            elif str(row["status"] or "") == "failed":
                history = _decoded_json(
                    unprotect_text(str(row["history_encrypted"] or "")),
                    [],
                )
                if not isinstance(history, list):
                    history = []
                history.append(
                    {
                        "status": "queued",
                        "occurred_at": now,
                        "reason": "explicit_retry",
                    }
                )
                connection.execute(
                    """
                    UPDATE import_operations
                    SET status='queued', payload_encrypted=?, result_encrypted='',
                        error_encrypted='', history_encrypted=?, updated_at=?,
                        finished_at=''
                    WHERE operation_id=?
                    """,
                    (
                        protect_text(_json(payload)),
                        protect_text(_json(history)),
                        now,
                        str(row["operation_id"]),
                    ),
                )
            operation_row = connection.execute(
                "SELECT * FROM import_operations WHERE request_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        if operation_row is None:
            raise RuntimeError("unable to persist import operation")
        return self._public_import_operation(operation_row)

    def update_import_operation(
        self,
        operation_id: str,
        *,
        status: str,
        reason: str = "",
        import_fingerprint: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        increment_attempt: bool = False,
    ) -> dict[str, Any]:
        normalized = str(operation_id or "").strip()
        normalized_status = str(status or "").strip().casefold()
        allowed_statuses = {
            "queued",
            "processing",
            "resume_pending",
            "completed",
            "failed",
        }
        if not normalized or normalized_status not in allowed_statuses:
            raise ValueError("invalid import operation update")
        now = utc_text()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM import_operations WHERE operation_id = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise ValueError("import operation not found")
            current_status = str(row["status"] or "")
            if current_status == "completed" and normalized_status != "completed":
                return self._public_import_operation(row)
            history = _decoded_json(
                unprotect_text(str(row["history_encrypted"] or "")),
                [],
            )
            if not isinstance(history, list):
                history = []
            history.append(
                {
                    "status": normalized_status,
                    "occurred_at": now,
                    "reason": str(reason or ""),
                }
            )
            next_import_fingerprint = (
                str(import_fingerprint or "").strip().lower()
                if import_fingerprint is not None
                else str(row["import_fingerprint"] or "")
            )
            next_result = (
                protect_text(_json(result))
                if result is not None
                else str(row["result_encrypted"] or "")
            )
            next_error = (
                protect_text(str(error or ""))
                if error is not None
                else str(row["error_encrypted"] or "")
            )
            started_at = (
                now
                if normalized_status == "processing"
                and not str(row["started_at"] or "")
                else str(row["started_at"] or "")
            )
            finished_at = (
                now
                if normalized_status in {"completed", "failed"}
                else ""
                if normalized_status in {"queued", "resume_pending"}
                else str(row["finished_at"] or "")
            )
            connection.execute(
                """
                UPDATE import_operations
                SET import_fingerprint=?, status=?, result_encrypted=?,
                    error_encrypted=?, history_encrypted=?,
                    attempt_count=attempt_count+?, updated_at=?,
                    started_at=?, finished_at=?
                WHERE operation_id=?
                """,
                (
                    next_import_fingerprint,
                    normalized_status,
                    next_result,
                    next_error,
                    protect_text(_json(history)),
                    1 if increment_attempt else 0,
                    now,
                    started_at,
                    finished_at,
                    normalized,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM import_operations WHERE operation_id = ?",
                (normalized,),
            ).fetchone()
        if updated is None:
            raise RuntimeError("import operation disappeared after update")
        return self._public_import_operation(updated)

    def resumable_import_operations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM import_operations
                WHERE status IN ('queued', 'processing', 'resume_pending')
                ORDER BY created_at ASC LIMIT ?
                """,
                (max(1, min(1000, int(limit))),),
            ).fetchall()
        return [self._public_import_operation(row) for row in rows]

    def add_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        side = str(payload.get("side") or "BUY").strip().upper()
        if side not in {"BUY", "SELL", "DIVIDEND", "FEE", "CASH_IN", "CASH_OUT"}:
            raise ValueError("unsupported transaction side")
        symbol = str(payload.get("symbol") or "").strip().upper()
        if side in {"BUY", "SELL", "DIVIDEND"} and not symbol:
            raise ValueError("transaction symbol is required")
        quantity = max(0.0, number(payload.get("quantity")))
        price = max(0.0, number(payload.get("price")))
        amount = max(0.0, number(payload.get("amount")))
        if side in {"DIVIDEND", "FEE", "CASH_IN", "CASH_OUT"} and price <= 0:
            price = amount
        if side in {"BUY", "SELL"} and (quantity <= 0 or price <= 0):
            raise ValueError("buy/sell transaction requires positive quantity and price")
        occurred = parse_datetime(payload.get("occurred_at")) or utc_now()
        transaction_id = str(payload.get("transaction_id") or uuid.uuid4().hex)
        row = {
            "transaction_id": transaction_id,
            "occurred_at": utc_text(occurred),
            "symbol": symbol,
            "market": str(payload.get("market") or "").strip().upper(),
            "asset_type": str(payload.get("asset_type") or "").strip().upper(),
            "side": side,
            "quantity": quantity,
            "price": price,
            "currency": str(payload.get("currency") or "").strip().upper(),
            "fee": max(0.0, number(payload.get("fee"))),
            "tax": max(0.0, number(payload.get("tax"))),
            "note_encrypted": protect_text(str(payload.get("note") or "")),
            "created_at": utc_text(),
        }
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO transactions(
                    transaction_id, occurred_at, symbol, market, asset_type, side,
                    quantity, price, currency, fee, tax, note_encrypted, created_at
                ) VALUES(
                    :transaction_id, :occurred_at, :symbol, :market, :asset_type, :side,
                    :quantity, :price, :currency, :fee, :tax, :note_encrypted, :created_at
                )
                ON CONFLICT(transaction_id) DO UPDATE SET
                    occurred_at=excluded.occurred_at, symbol=excluded.symbol,
                    market=excluded.market, asset_type=excluded.asset_type,
                    side=excluded.side, quantity=excluded.quantity,
                    price=excluded.price, currency=excluded.currency,
                    fee=excluded.fee, tax=excluded.tax,
                    note_encrypted=excluded.note_encrypted,
                    deleted_at='', delete_reason_encrypted='',
                    delete_audit_id=''
                """,
                row,
            )
        public = dict(row)
        encrypted_note = str(public.pop("note_encrypted", ""))
        public["note"] = unprotect_text(encrypted_note)
        return public

    def delete_transaction(
        self,
        transaction_id: str,
        *,
        reason: str = "user_requested",
    ) -> bool:
        normalized = str(transaction_id or "").strip()
        if not normalized:
            return False
        deleted_at = utc_text()
        audit_id = uuid.uuid4().hex
        audit_details: dict[str, Any] = {}
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM transactions
                WHERE transaction_id = ? AND deleted_at = ''
                """,
                (normalized,),
            ).fetchone()
            if row is None:
                return False
            cursor = connection.execute(
                """
                UPDATE transactions
                SET deleted_at=?, delete_reason_encrypted=?,
                    delete_audit_id=?
                WHERE transaction_id=? AND deleted_at=''
                """,
                (
                    deleted_at,
                    protect_text(str(reason or "user_requested")),
                    audit_id,
                    normalized,
                ),
            )
            audit_details = {
                "transaction_id": normalized,
                "reason": str(reason or "user_requested"),
                "preserved_row": dict(row),
                "physical_delete": False,
            }
            connection.execute(
                """
                INSERT INTO audit_log(
                    audit_id, occurred_at, action, severity, details_encrypted
                ) VALUES(?, ?, 'transaction_tombstoned', 'warning', ?)
                """,
                (
                    audit_id,
                    deleted_at,
                    protect_text(
                        _json(
                            audit_details
                        )
                    ),
                ),
            )
        changed = bool(cursor.rowcount)
        if changed:
            self._append_managed_audit_record(
                audit_id=audit_id,
                occurred_at=deleted_at,
                action="transaction_tombstoned",
                severity="warning",
                details=audit_details,
            )
        return changed

    def list_transactions(
        self,
        limit: int = 500,
        *,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                (
                    "SELECT * FROM transactions "
                    + ("" if include_deleted else "WHERE deleted_at = '' ")
                    + "ORDER BY occurred_at DESC, created_at DESC LIMIT ?"
                ),
                (max(1, min(5000, int(limit))),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["note"] = unprotect_text(str(item.pop("note_encrypted", "") or ""))
            item["delete_reason"] = unprotect_text(
                str(item.pop("delete_reason_encrypted", "") or "")
            )
            item["deleted"] = bool(item.get("deleted_at"))
            item["is_estimated"] = str(item.get("transaction_id") or "").startswith(
                (OPENING_BALANCE_PREFIX, RECONCILIATION_PREFIX)
            )
            item["source"] = (
                "opening_balance_estimate"
                if str(item.get("transaction_id") or "").startswith(OPENING_BALANCE_PREFIX)
                else "ledger_reconciliation_estimate"
                if str(item.get("transaction_id") or "").startswith(RECONCILIATION_PREFIX)
                else "confirmed_or_manual"
            )
            result.append(item)
        return result

    def sync_opening_balance_transactions(
        self,
        state: dict[str, Any],
        occurred_at: Any,
    ) -> dict[str, Any]:
        opening_at = parse_datetime(occurred_at)
        if opening_at is None:
            raise ValueError("opening balance date is required")
        if opening_at > utc_now():
            raise ValueError("opening balance date cannot be in the future")

        holdings = [
            item for item in state.get("holdings", []) if isinstance(item, dict)
        ]
        active_count = 0
        uncovered_symbols: list[str] = []
        rows: list[dict[str, Any]] = []
        for index, holding in enumerate(holdings):
            quantity = number(holding.get("quantity"))
            if quantity <= 0:
                continue
            active_count += 1
            symbol = str(holding.get("symbol") or "").strip().upper()
            principal_twd = number(holding.get("principal_twd"))
            holding_currency = str(holding.get("currency") or "TWD").strip().upper()
            if principal_twd <= 0 and holding_currency == "TWD":
                principal_twd = number(holding.get("principal_amount"))
                if principal_twd <= 0:
                    principal_twd = number(holding.get("average_cost")) * quantity
            if not symbol or principal_twd <= 0:
                uncovered_symbols.append(symbol or f"ROW-{index + 1}")
                continue
            identity = str(
                holding.get("holding_id")
                or f"{holding.get('market')}|{symbol}|{index}"
            )
            transaction_id = OPENING_BALANCE_PREFIX + hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest()[:24]
            rows.append(
                {
                    "transaction_id": transaction_id,
                    "occurred_at": utc_text(opening_at),
                    "symbol": symbol,
                    "market": str(holding.get("market") or "").strip().upper(),
                    "asset_type": str(holding.get("asset_type") or "").strip().upper(),
                    "side": "BUY",
                    "quantity": quantity,
                    "price": principal_twd / quantity,
                    "currency": "TWD",
                    "fee": 0.0,
                    "tax": 0.0,
                    "note_encrypted": protect_text(
                        "估算期初持股；依目前持股與新台幣本金建立，並非券商成交紀錄。"
                    ),
                    "created_at": utc_text(),
                }
            )

        desired_ids = {row["transaction_id"] for row in rows}
        metadata = {
            "occurred_at": utc_text(opening_at),
            "generated_at": utc_text(),
            "active_holding_count": active_count,
            "generated_count": len(rows),
            "uncovered_count": len(uncovered_symbols),
            "uncovered_symbols": uncovered_symbols[:50],
            "coverage_percent": rounded(len(rows) / active_count * 100, 2)
            if active_count
            else 0.0,
            "methodology": "current_holdings_and_principal_twd",
        }
        with self.connect() as connection:
            confirmed_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM transactions
                    WHERE transaction_id NOT LIKE ? AND deleted_at = ''
                    """,
                    (f"{OPENING_BALANCE_PREFIX}%",),
                ).fetchone()[0]
            )
            if confirmed_count > 0:
                raise ValueError(
                    "confirmed transactions already exist; import broker history instead of rebuilding the opening balance"
                )
            existing_ids = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT transaction_id FROM transactions
                    WHERE transaction_id LIKE ? AND deleted_at = ''
                    """,
                    (f"{OPENING_BALANCE_PREFIX}%",),
                ).fetchall()
            }
            stale_ids = existing_ids - desired_ids
            stale_audit_record: tuple[str, str, dict[str, Any]] | None = None
            if stale_ids:
                stale_audit_id = uuid.uuid4().hex
                stale_at = utc_text()
                connection.executemany(
                    """
                    UPDATE transactions
                    SET deleted_at=?,
                        delete_reason_encrypted=?,
                        delete_audit_id=?
                    WHERE transaction_id=? AND deleted_at=''
                    """,
                    [
                        (
                            stale_at,
                            protect_text("opening_balance_replaced"),
                            stale_audit_id,
                            transaction_id,
                        )
                        for transaction_id in sorted(stale_ids)
                    ],
                )
                stale_details = {
                    "transaction_ids": sorted(stale_ids),
                    "reason": "opening_balance_replaced",
                    "physical_delete": False,
                }
                stale_audit_record = (stale_audit_id, stale_at, stale_details)
                connection.execute(
                    """
                    INSERT INTO audit_log(
                        audit_id, occurred_at, action, severity,
                        details_encrypted
                    ) VALUES(
                        ?, ?, 'opening_transactions_tombstoned',
                        'warning', ?
                    )
                    """,
                    (
                        stale_audit_id,
                        stale_at,
                        protect_text(
                            _json(
                                stale_details
                            )
                        ),
                    ),
                )
            if rows:
                connection.executemany(
                    """
                    INSERT INTO transactions(
                        transaction_id, occurred_at, symbol, market, asset_type, side,
                        quantity, price, currency, fee, tax, note_encrypted, created_at
                    ) VALUES(
                        :transaction_id, :occurred_at, :symbol, :market, :asset_type, :side,
                        :quantity, :price, :currency, :fee, :tax, :note_encrypted, :created_at
                    )
                    ON CONFLICT(transaction_id) DO UPDATE SET
                        occurred_at=excluded.occurred_at, symbol=excluded.symbol,
                        market=excluded.market, asset_type=excluded.asset_type,
                        side=excluded.side, quantity=excluded.quantity,
                        price=excluded.price, currency=excluded.currency,
                        fee=excluded.fee, tax=excluded.tax,
                        note_encrypted=excluded.note_encrypted,
                        deleted_at='', delete_reason_encrypted='',
                        delete_audit_id=''
                    """,
                    rows,
                )
            connection.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                ("opening_ledger", _json(metadata), utc_text()),
            )
        if stale_audit_record is not None:
            stale_audit_id, stale_at, stale_details = stale_audit_record
            self._append_managed_audit_record(
                audit_id=stale_audit_id,
                occurred_at=stale_at,
                action="opening_transactions_tombstoned",
                severity="warning",
                details=stale_details,
            )
        self.audit("opening_ledger_synced", metadata, severity="warning")
        return metadata

    def add_price_bars(self, bars: Iterable[dict[str, Any]]) -> int:
        prepared: list[tuple[Any, ...]] = []
        for bar in bars:
            symbol = str(bar.get("symbol") or "").strip().upper()
            observed = parse_datetime(bar.get("observed_at") or bar.get("timestamp"))
            close = number(bar.get("close"), -1)
            if not symbol or observed is None or close <= 0:
                continue
            prepared.append(
                (
                    symbol,
                    utc_text(observed),
                    number(bar.get("open"), close),
                    number(bar.get("high"), close),
                    number(bar.get("low"), close),
                    close,
                    number(bar.get("volume")),
                    str(bar.get("currency") or "").upper(),
                    str(bar.get("provider") or "manual"),
                    int(bool(bar.get("verified", True))),
                )
            )
        if not prepared:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO prices(symbol, observed_at, open, high, low, close, volume, currency, provider, verified)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, observed_at, provider) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume, currency=excluded.currency,
                    verified=excluded.verified
                """,
                prepared,
            )
        return len(prepared)

    def price_series(self, symbol: str, limit: int = 800) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT observed_at, open, high, low, close, volume, currency, provider, verified
                FROM prices WHERE symbol = ? ORDER BY observed_at DESC LIMIT ?
                """,
                (symbol.strip().upper(), max(2, min(5000, limit))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def latest_prices(self) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.* FROM prices p
                INNER JOIN (
                    SELECT symbol, MAX(observed_at) AS observed_at FROM prices GROUP BY symbol
                ) latest ON latest.symbol=p.symbol AND latest.observed_at=p.observed_at
                ORDER BY p.verified DESC, p.provider
                """
            ).fetchall()
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            output.setdefault(str(row["symbol"]), dict(row))
        return output

    def record_analysis_snapshot(self, analysis: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        reports = analysis.get("holdings") if isinstance(analysis.get("holdings"), list) else []
        observed = parse_datetime(analysis.get("generated_at")) or utc_now()
        bars: list[dict[str, Any]] = []
        for report in reports:
            if not isinstance(report, dict):
                continue
            quote = report.get("quote") if isinstance(report.get("quote"), dict) else {}
            price = number(quote.get("price"), -1)
            symbol = str(report.get("symbol") or "").upper()
            if not symbol or price <= 0:
                continue
            bars.append(
                {
                    "symbol": symbol,
                    "observed_at": quote.get("as_of") or utc_text(observed),
                    "close": price,
                    "currency": quote.get("currency") or report.get("currency"),
                    "provider": quote.get("provider") or "local-risk-ai",
                    "verified": bool(report.get("trusted_quote")),
                }
            )
        self.add_price_bars(bars)
        latest = self.latest_prices()
        positions_by_symbol: dict[str, dict[str, Any]] = {}
        quoted_symbols: set[str] = set()
        for holding in state.get("holdings", []):
            if not isinstance(holding, dict):
                continue
            symbol = str(holding.get("symbol") or "").upper()
            quote = latest.get(symbol, {})
            quoted_price = number(quote.get("close"), -1)
            if quoted_price > 0:
                quoted_symbols.add(symbol)
            price = quoted_price if quoted_price > 0 else number(holding.get("average_cost"))
            quantity = number(holding.get("quantity"))
            average_cost = number(holding.get("average_cost"))
            if not symbol or price <= 0:
                continue
            position = positions_by_symbol.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "market": str(holding.get("market") or ""),
                    "asset_type": str(holding.get("asset_type") or ""),
                    "quantity": 0.0,
                    "price": price,
                    "market_value": 0.0,
                    "cost_value": 0.0,
                    "currency": str(
                        quote.get("currency") or holding.get("currency") or ""
                    ),
                },
            )
            position["quantity"] += quantity
            position["market_value"] += quantity * price
            position["cost_value"] += quantity * average_cost
        positions = list(positions_by_symbol.values())
        if not positions:
            return {"price_count": len(bars), "snapshot_saved": False}
        snapshot_id = uuid.uuid4().hex
        total_value = sum(item["market_value"] for item in positions)
        total_cost = sum(item["cost_value"] for item in positions)
        base_currency = str(self.get_setting("base_currency", "TWD") or "TWD")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO portfolio_snapshots VALUES(?, ?, ?, ?, ?, ?)",
                (snapshot_id, utc_text(observed), total_value, total_cost, base_currency, 0.0),
            )
            connection.executemany(
                """
                INSERT INTO snapshot_positions(
                    snapshot_id, symbol, market, asset_type, quantity, price,
                    market_value, cost_value, currency
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        item["symbol"],
                        item["market"],
                        item["asset_type"],
                        item["quantity"],
                        item["price"],
                        item["market_value"],
                        item["cost_value"],
                        item["currency"],
                    )
                    for item in positions
                ],
            )
        return {
            "price_count": len(bars),
            "snapshot_saved": True,
            "snapshot_id": snapshot_id,
            "quoted_position_count": len(quoted_symbols),
            "position_count": len(positions),
            "estimated_position_count": max(0, len(positions) - len(quoted_symbols)),
        }

    def current_positions(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        latest = self.latest_prices()
        positions: list[dict[str, Any]] = []
        holdings = [
            item for item in state.get("holdings", []) if isinstance(item, dict)
        ]
        use_twd_valuation = any(
            number(item.get(field)) > 0
            for item in holdings
            for field in ("principal_twd", "current_value_twd", "web_current_value_twd")
        )
        for holding in holdings:
            symbol = str(holding.get("symbol") or "").upper()
            quote = latest.get(symbol, {})
            price = number(quote.get("close"), number(holding.get("average_cost")))
            quantity = number(holding.get("quantity"))
            if quantity <= 0:
                continue
            native_cost = number(holding.get("average_cost")) * quantity
            native_value = price * quantity
            if use_twd_valuation:
                cost = number(holding.get("principal_twd"))
                if cost <= 0 and str(holding.get("currency") or "TWD").upper() == "TWD":
                    cost = number(holding.get("principal_amount"), native_cost)
                value = number(
                    holding.get("web_current_value_twd"),
                    number(holding.get("current_value_twd")),
                )
            else:
                cost = native_cost
                value = native_value
            positions.append(
                {
                    **holding,
                    "symbol": symbol,
                    "price": rounded(price),
                    "market_value": rounded(value, 2),
                    "cost_value": rounded(cost, 2),
                    "unrealized_pnl": rounded(value - cost, 2),
                    "unrealized_pnl_percent": rounded((value / cost - 1) * 100, 2) if cost > 0 else None,
                    "price_as_of": quote.get("observed_at"),
                }
            )
        total = sum(number(item.get("market_value")) for item in positions)
        for item in positions:
            item["weight_percent"] = rounded(number(item.get("market_value")) / total * 100, 2) if total > 0 else None
        return sorted(positions, key=lambda item: number(item.get("market_value")), reverse=True)

    def ledger_summary(self) -> dict[str, Any]:
        transactions = list(reversed(self.list_transactions(limit=5000)))
        lots: dict[str, list[list[float]]] = {}
        realized = 0.0
        dividends = 0.0
        fees = 0.0
        cashflows: list[dict[str, Any]] = []
        estimated_count = sum(1 for item in transactions if item.get("is_estimated"))
        for transaction in transactions:
            symbol = str(transaction.get("symbol") or "")
            side = str(transaction.get("side") or "")
            quantity = number(transaction.get("quantity"))
            price = number(transaction.get("price"))
            fee = number(transaction.get("fee"))
            tax = number(transaction.get("tax"))
            fees += fee + tax
            date = str(transaction.get("occurred_at") or "")
            if side == "BUY":
                lots.setdefault(symbol, []).append([quantity, price])
                cashflows.append({"date": date, "amount": -(quantity * price + fee + tax)})
            elif side == "SELL":
                remaining = quantity
                cost = 0.0
                for lot in lots.setdefault(symbol, []):
                    used = min(lot[0], remaining)
                    cost += used * lot[1]
                    lot[0] -= used
                    remaining -= used
                    if remaining <= 1e-10:
                        break
                lots[symbol] = [lot for lot in lots[symbol] if lot[0] > 1e-10]
                proceeds = quantity * price - fee - tax
                realized += proceeds - cost
                cashflows.append({"date": date, "amount": proceeds})
            elif side == "DIVIDEND":
                amount = price if quantity <= 0 else quantity * price
                dividends += amount
                cashflows.append({"date": date, "amount": amount})
            elif side == "CASH_IN":
                cashflows.append({"date": date, "amount": -price})
            elif side in {"CASH_OUT", "FEE"}:
                cashflows.append({"date": date, "amount": price})
        opening_ledger = self.get_setting("opening_ledger", {})
        confirmed_count = len(transactions) - estimated_count
        ledger_quality = (
            "empty"
            if not transactions
            else "estimated_opening"
            if estimated_count
            else "confirmed"
        )
        return {
            "transaction_count": len(transactions),
            "confirmed_transaction_count": confirmed_count,
            "estimated_transaction_count": estimated_count,
            "ledger_quality": ledger_quality,
            "opening_ledger": opening_ledger if isinstance(opening_ledger, dict) else {},
            "realized_pnl": rounded(realized, 2),
            "dividend_income": rounded(dividends, 2),
            "fees_and_taxes": rounded(fees, 2),
            "open_lot_count": sum(len(items) for items in lots.values()),
            "cashflows": cashflows[-500:],
        }

    def reconcile_ledger_holdings(self, state: dict[str, Any]) -> dict[str, Any]:
        holding_positions: dict[str, dict[str, Any]] = {}
        for holding in state.get("holdings", []):
            if not isinstance(holding, dict):
                continue
            symbol = str(holding.get("symbol") or "").strip().upper()
            quantity = number(holding.get("quantity"))
            if not symbol or quantity <= 0:
                continue
            row = holding_positions.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "quantity": 0.0,
                    "market": str(holding.get("market") or "").upper(),
                    "asset_type": str(holding.get("asset_type") or "").upper(),
                    "currency": str(holding.get("currency") or "TWD").upper(),
                    "principal_twd": 0.0,
                    "average_cost_total": 0.0,
                },
            )
            row["quantity"] += quantity
            row["principal_twd"] += number(holding.get("principal_twd"))
            row["average_cost_total"] += number(holding.get("average_cost")) * quantity

        ledger_positions: dict[str, float] = {}
        for transaction in reversed(self.list_transactions(5000)):
            symbol = str(transaction.get("symbol") or "").strip().upper()
            side = str(transaction.get("side") or "").upper()
            quantity = number(transaction.get("quantity"))
            if not symbol or side not in {"BUY", "SELL"}:
                continue
            ledger_positions[symbol] = ledger_positions.get(symbol, 0.0) + (
                quantity if side == "BUY" else -quantity
            )

        differences: list[dict[str, Any]] = []
        matched_count = 0
        all_symbols = sorted(set(holding_positions) | set(ledger_positions))
        for symbol in all_symbols:
            holding = holding_positions.get(symbol, {})
            holding_quantity = number(holding.get("quantity"))
            ledger_quantity = number(ledger_positions.get(symbol))
            delta = holding_quantity - ledger_quantity
            tolerance = max(0.000001, abs(holding_quantity) * 0.00001)
            matched = abs(delta) <= tolerance
            matched_count += int(matched)
            suggestion: dict[str, Any] | None = None
            if not matched:
                quantity = abs(delta)
                principal_twd = number(holding.get("principal_twd"))
                average_total = number(holding.get("average_cost_total"))
                price = (
                    principal_twd / holding_quantity
                    if holding_quantity > 0 and principal_twd > 0
                    else average_total / holding_quantity
                    if holding_quantity > 0 and average_total > 0
                    else 0.0
                )
                suggestion = {
                    "symbol": symbol,
                    "side": "BUY" if delta > 0 else "SELL",
                    "quantity": rounded(quantity, 8),
                    "price": rounded(price, 8),
                    "currency": "TWD" if principal_twd > 0 else str(holding.get("currency") or "TWD"),
                    "market": str(holding.get("market") or ""),
                    "asset_type": str(holding.get("asset_type") or ""),
                }
            differences.append(
                {
                    "symbol": symbol,
                    "holding_quantity": rounded(holding_quantity, 8),
                    "ledger_quantity": rounded(ledger_quantity, 8),
                    "difference_quantity": rounded(delta, 8),
                    "status": "matched" if matched else "difference",
                    "suggestion": suggestion,
                }
            )
        difference_rows = [item for item in differences if item["status"] == "difference"]
        return {
            "status": "ready" if all_symbols and not difference_rows else "differences" if all_symbols else "empty",
            "symbol_count": len(all_symbols),
            "matched_count": matched_count,
            "difference_count": len(difference_rows),
            "coverage_percent": rounded(matched_count / len(all_symbols) * 100, 2) if all_symbols else 0.0,
            "differences": difference_rows[:100],
            "generated_at": utc_text(),
        }

    def apply_ledger_reconciliation(
        self,
        state: dict[str, Any],
        *,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("ledger reconciliation requires confirmation")
        reconciliation = self.reconcile_ledger_holdings(state)
        applied = 0
        stamp = utc_text()
        for item in reconciliation.get("differences", []):
            suggestion = item.get("suggestion") if isinstance(item, dict) else None
            if not isinstance(suggestion, dict):
                continue
            if number(suggestion.get("quantity")) <= 0 or number(suggestion.get("price")) <= 0:
                continue
            identity = f"{stamp}|{suggestion.get('symbol')}|{suggestion.get('side')}|{suggestion.get('quantity')}"
            self.add_transaction(
                {
                    **suggestion,
                    "transaction_id": RECONCILIATION_PREFIX
                    + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                    "occurred_at": stamp,
                    "note": "依目前持股與交易帳本差額建立的估算對帳調整；並非券商成交紀錄。",
                }
            )
            applied += 1
        result = self.reconcile_ledger_holdings(state)
        result["applied_count"] = applied
        self.audit("ledger_reconciliation_applied", result, severity="warning")
        return result

    def performance(self, state: dict[str, Any]) -> dict[str, Any]:
        positions = self.current_positions(state)
        current_value = sum(number(item.get("market_value")) for item in positions)
        current_cost = sum(number(item.get("cost_value")) for item in positions)
        ledger = self.ledger_summary()
        with self.connect() as connection:
            snapshots = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM portfolio_snapshots ORDER BY observed_at LIMIT 2000"
                ).fetchall()
            ]
        values = [number(item.get("total_value")) for item in snapshots if number(item.get("total_value")) > 0]
        daily_returns = _returns(values)
        twr = math.prod(1 + value for value in daily_returns) - 1 if daily_returns else None
        cashflows: list[tuple[datetime, float]] = []
        for item in ledger["cashflows"]:
            date = parse_datetime(item.get("date"))
            if date is not None:
                cashflows.append((date, number(item.get("amount"))))
        if current_value > 0:
            cashflows.append((utc_now(), current_value))
        irr = xirr(cashflows)
        attribution: dict[str, dict[str, float]] = {}
        for position in positions:
            currency = str(position.get("currency") or "UNKNOWN")
            bucket = attribution.setdefault(currency, {"market_value": 0.0, "cost_value": 0.0, "pnl": 0.0})
            bucket["market_value"] += number(position.get("market_value"))
            bucket["cost_value"] += number(position.get("cost_value"))
            bucket["pnl"] += number(position.get("unrealized_pnl"))
        return {
            "status": "ready" if positions else "empty",
            "methodology": "snapshot_twr_and_transaction_xirr",
            "current_value": rounded(current_value, 2),
            "current_cost": rounded(current_cost, 2),
            "unrealized_pnl": rounded(current_value - current_cost, 2),
            "unrealized_pnl_percent": rounded((current_value / current_cost - 1) * 100, 2) if current_cost > 0 else None,
            "realized_pnl": ledger["realized_pnl"],
            "dividend_income": ledger["dividend_income"],
            "fees_and_taxes": ledger["fees_and_taxes"],
            "twr_percent": rounded(twr * 100, 2) if twr is not None else None,
            "xirr_percent": rounded(irr * 100, 2) if irr is not None else None,
            "annualized_volatility_percent": rounded(statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS) * 100, 2) if len(daily_returns) > 1 else None,
            "max_drawdown_percent": rounded((_max_drawdown(values) or 0) * 100, 2) if values else None,
            "snapshot_count": len(snapshots),
            "attribution_by_currency": {key: {name: rounded(value, 2) for name, value in bucket.items()} for key, bucket in attribution.items()},
            "equity_curve": [
                {"date": item["observed_at"], "value": rounded(number(item["total_value"]), 2)}
                for item in snapshots[-365:]
            ],
            "positions": positions,
            "ledger": {key: value for key, value in ledger.items() if key != "cashflows"},
        }

    def risk(self, state: dict[str, Any], benchmark: str | None = None) -> dict[str, Any]:
        positions = self.current_positions(state)
        raw_weights = {
            str(item.get("symbol") or ""): number(item.get("weight_percent")) / 100
            for item in positions
            if item.get("weight_percent") is not None
        }
        series = {
            symbol: self.price_series(symbol, 520)
            for symbol in raw_weights
        }
        returns_by_symbol: dict[str, dict[str, float]] = {}
        for symbol, bars in series.items():
            prices_by_date = {
                str(bar["observed_at"])[:10]: number(bar["close"])
                for bar in bars
                if number(bar.get("close")) > 0
            }
            ordered_dates = sorted(prices_by_date)
            returns_by_symbol[symbol] = {
                current: prices_by_date[current] / prices_by_date[previous] - 1
                for previous, current in zip(ordered_dates, ordered_dates[1:])
                if prices_by_date[previous] > 0
            }
        eligible_symbols = sorted(
            symbol for symbol, values in returns_by_symbol.items() if len(values) >= 20
        )
        common_dates = (
            sorted(
                set.intersection(
                    *(set(returns_by_symbol[symbol]) for symbol in eligible_symbols)
                )
            )
            if eligible_symbols
            else []
        )
        covered_weight = sum(raw_weights.get(symbol, 0.0) for symbol in eligible_symbols)
        weights = (
            {
                symbol: raw_weights.get(symbol, 0.0) / covered_weight
                for symbol in eligible_symbols
            }
            if covered_weight > 0
            else {}
        )
        aligned_returns = {
            symbol: [returns_by_symbol[symbol][date] for date in common_dates]
            for symbol in eligible_symbols
        }
        portfolio_returns_by_date = {
            date: sum(
                weights[symbol] * returns_by_symbol[symbol][date]
                for symbol in eligible_symbols
            )
            for date in common_dates
        }
        portfolio_returns = [
            portfolio_returns_by_date[date] for date in common_dates
        ]
        volatility = statistics.stdev(portfolio_returns) * math.sqrt(TRADING_DAYS) if len(portfolio_returns) > 1 else None
        var_cutoff = _percentile(portfolio_returns, 0.05)
        tail = [value for value in portfolio_returns if var_cutoff is not None and value <= var_cutoff]
        values = [1.0]
        for value in portfolio_returns:
            values.append(values[-1] * (1 + value))
        correlation: list[dict[str, Any]] = []
        symbols = eligible_symbols
        for left_index, left in enumerate(symbols):
            for right in symbols[left_index + 1 :]:
                coefficient = _correlation(
                    aligned_returns[left],
                    aligned_returns[right],
                )
                correlation.append(
                    {
                        "left": left,
                        "right": right,
                        "correlation": rounded(coefficient, 4),
                        "sample_count": len(common_dates),
                    }
                )
        benchmark_symbol = str(benchmark or self.get_setting("benchmark", DEFAULT_BENCHMARK) or DEFAULT_BENCHMARK).upper()
        benchmark_bars = self.price_series(benchmark_symbol, 520)
        benchmark_prices = {
            str(item["observed_at"])[:10]: number(item["close"])
            for item in benchmark_bars
            if number(item.get("close")) > 0
        }
        benchmark_dates = sorted(benchmark_prices)
        benchmark_returns_by_date = {
            current: benchmark_prices[current] / benchmark_prices[previous] - 1
            for previous, current in zip(benchmark_dates, benchmark_dates[1:])
            if benchmark_prices[previous] > 0
        }
        beta = None
        aligned_dates = sorted(set(portfolio_returns_by_date) & set(benchmark_returns_by_date))
        if len(aligned_dates) > 1:
            aligned_portfolio = [portfolio_returns_by_date[date] for date in aligned_dates]
            aligned_benchmark = [benchmark_returns_by_date[date] for date in aligned_dates]
            denominator = _variance(aligned_benchmark)
            beta = _covariance(aligned_portfolio, aligned_benchmark) / denominator if denominator > 0 else None
        exposures: dict[str, dict[str, float]] = {"market": {}, "currency": {}, "asset_type": {}}
        for item in positions:
            weight = number(item.get("weight_percent"))
            for dimension in exposures:
                key = str(item.get(dimension) or "UNKNOWN")
                exposures[dimension][key] = exposures[dimension].get(key, 0.0) + weight
        covariance = {
            (left, right): _covariance(aligned_returns[left], aligned_returns[right])
            for left in symbols
            for right in symbols
        }
        portfolio_variance = sum(
            weights[left] * weights[right] * covariance[(left, right)]
            for left in symbols
            for right in symbols
        )
        risk_contributions: list[dict[str, Any]] = []
        for symbol in symbols:
            covariance_with_portfolio = sum(
                covariance[(symbol, other)] * weights[other] for other in symbols
            )
            component_fraction = (
                weights[symbol] * covariance_with_portfolio / portfolio_variance
                if portfolio_variance > 0
                else None
            )
            risk_contributions.append(
                {
                    "symbol": symbol,
                    "weight_percent": rounded(weights[symbol] * 100, 2),
                    "portfolio_weight_percent": rounded(raw_weights.get(symbol, 0) * 100, 2),
                    "risk_contribution_percent": (
                        rounded(component_fraction * 100, 2)
                        if component_fraction is not None
                        else None
                    ),
                    "marginal_volatility_annualized_percent": (
                        rounded(
                            covariance_with_portfolio
                            / math.sqrt(portfolio_variance)
                            * math.sqrt(TRADING_DAYS)
                            * 100,
                            2,
                        )
                        if portfolio_variance > 0
                        else None
                    ),
                    "annualized_volatility_percent": (
                        rounded(
                            math.sqrt(_variance(aligned_returns[symbol]))
                            * math.sqrt(TRADING_DAYS)
                            * 100,
                            2,
                        )
                        if len(aligned_returns[symbol]) > 1
                        else None
                    ),
                }
            )
        total_market_value = sum(number(item.get("market_value")) for item in positions)
        analyzed_market_value = sum(
            number(item.get("market_value"))
            for item in positions
            if str(item.get("symbol") or "") in eligible_symbols
        )
        excluded_symbols = sorted(set(raw_weights) - set(eligible_symbols))
        return {
            "status": "ready" if len(portfolio_returns) >= 20 else "insufficient_history",
            "sample_count": len(portfolio_returns),
            "annualized_volatility_percent": rounded(volatility * 100, 2) if volatility is not None else None,
            "beta": rounded(beta, 3),
            "var_95_one_day_percent": rounded(-(var_cutoff or 0) * 100, 2) if var_cutoff is not None else None,
            "cvar_95_one_day_percent": rounded(-_mean(tail) * 100, 2) if tail else None,
            "max_drawdown_percent": rounded((_max_drawdown(values) or 0) * 100, 2) if portfolio_returns else None,
            "correlations": sorted(correlation, key=lambda item: abs(number(item.get("correlation"))), reverse=True)[:100],
            "risk_contributions": sorted(risk_contributions, key=lambda item: number(item.get("risk_contribution_percent")), reverse=True),
            "exposures": {dimension: {key: rounded(value, 2) for key, value in values.items()} for dimension, values in exposures.items()},
            "benchmark": benchmark_symbol,
            "weight_methodology": "current_weights_proxy",
            "risk_contribution_methodology": "euler_marginal_contribution_from_covariance",
            "analysis_coverage": {
                "position_count": len(positions),
                "analyzed_position_count": len(eligible_symbols),
                "excluded_symbols": excluded_symbols,
                "market_value_percent": (
                    rounded(analyzed_market_value / total_market_value * 100, 2)
                    if total_market_value > 0
                    else None
                ),
                "requires_common_dates": True,
            },
            "limitations": [
                "Historical holdings are unavailable; current position weights are applied as an explicit proxy.",
                "Positions without at least 20 returns are excluded and remaining weights are renormalized.",
            ],
        }

    def stress_test(self, state: dict[str, Any], scenarios: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        positions = self.current_positions(state)
        configured = scenarios or [
            {"name": "大盤急跌", "market_shocks": {"*": -15}},
            {"name": "科技修正", "asset_type_shocks": {"STOCK": -10, "ETF": -7}},
            {"name": "美元升值", "currency_shocks": {"USD": 5, "TWD": -2}},
            {"name": "流動性壓力", "symbol_shocks": {}, "market_shocks": {"CRYPTO": -25, "*": -8}},
        ]
        total = sum(number(item.get("market_value")) for item in positions)
        results: list[dict[str, Any]] = []
        for scenario in configured:
            loss = 0.0
            impacts = []
            for position in positions:
                symbol = str(position.get("symbol") or "")
                market = str(position.get("market") or "")
                asset_type = str(position.get("asset_type") or "").upper()
                currency = str(position.get("currency") or "")
                symbol_shocks = scenario.get("symbol_shocks", {})
                market_shocks = scenario.get("market_shocks", {})
                asset_shocks = scenario.get("asset_type_shocks", {})
                currency_shocks = scenario.get("currency_shocks", {})
                shock = number(symbol_shocks.get(symbol), number(market_shocks.get(market), number(market_shocks.get("*"))))
                shock += number(asset_shocks.get(asset_type))
                shock += number(currency_shocks.get(currency))
                impact = number(position.get("market_value")) * shock / 100
                loss += impact
                impacts.append({"symbol": symbol, "shock_percent": rounded(shock, 2), "impact": rounded(impact, 2)})
            results.append(
                {
                    "name": str(scenario.get("name") or "自訂情境"),
                    "impact": rounded(loss, 2),
                    "impact_percent": rounded(loss / total * 100, 2) if total > 0 else None,
                    "projected_value": rounded(total + loss, 2),
                    "largest_impacts": sorted(impacts, key=lambda item: abs(number(item.get("impact"))), reverse=True)[:8],
                }
            )
        return {"status": "ready" if positions else "empty", "current_value": rounded(total, 2), "scenarios": results}

    def backtest(self, symbols: Sequence[str], strategy: str = "buy_and_hold", initial_capital: float = 1_000_000, fee_percent: float = 0.1425, slippage_percent: float = 0.05) -> dict[str, Any]:
        if strategy not in {"buy_and_hold", "equal_weight", "momentum"}:
            raise ValueError("unsupported backtest strategy")
        symbol_list = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        series = {
            symbol: {str(item["observed_at"])[:10]: number(item["close"]) for item in self.price_series(symbol, 2000)}
            for symbol in symbol_list
        }
        dates = sorted(set.intersection(*(set(values) for values in series.values()))) if series and all(series.values()) else []
        if len(dates) < 30:
            return {
                "ok": False,
                "status": "insufficient_history",
                "sample_count": len(dates),
                "message": "至少需要 30 個共同交易日。",
                "analysis_coverage": {
                    "requested_symbols": symbol_list,
                    "available_symbols": sorted(symbol for symbol, values in series.items() if values),
                    "common_date_count": len(dates),
                },
            }
        capital = max(1.0, initial_capital)
        target_weights = {symbol: 1 / len(symbol_list) for symbol in symbol_list}
        cost_rate = (max(0.0, fee_percent) + max(0.0, slippage_percent)) / 100
        units = {
            symbol: (capital * target_weights[symbol])
            / (series[symbol][dates[0]] * (1 + cost_rate))
            for symbol in symbol_list
        }
        initial_transaction_cost = sum(
            units[symbol] * series[symbol][dates[0]] * cost_rate
            for symbol in symbol_list
        )
        cash = 0.0
        equity = sum(
            units[symbol] * series[symbol][dates[0]] for symbol in symbol_list
        )
        curve = [{"date": dates[0], "value": rounded(equity, 2)}]
        daily_returns: list[float] = []
        turnover_total = 0.0
        rebalance_cost = 0.0
        with self.connect() as connection:
            action_rows = connection.execute(
                """
                SELECT symbol, action_type, effective_at, ratio, cash_amount
                FROM corporate_actions
                WHERE status='approved' AND symbol IN ({})
                ORDER BY effective_at
                """.format(",".join("?" for _ in symbol_list)),
                symbol_list,
            ).fetchall()
        corporate_actions = [dict(row) for row in action_rows]
        applied_actions: list[dict[str, Any]] = []
        ignored_actions: list[dict[str, Any]] = []
        action_index = 0
        for index in range(1, len(dates)):
            previous_date, current_date = dates[index - 1], dates[index]
            while action_index < len(corporate_actions):
                action = corporate_actions[action_index]
                effective_date = str(action.get("effective_at") or "")[:10]
                if effective_date > current_date:
                    break
                action_index += 1
                if effective_date <= previous_date:
                    continue
                symbol = str(action.get("symbol") or "")
                action_type = str(action.get("action_type") or "")
                if symbol not in units:
                    continue
                if action_type == "split" and number(action.get("ratio")) > 0:
                    units[symbol] *= number(action.get("ratio"))
                    applied_actions.append(
                        {
                            "symbol": symbol,
                            "action_type": action_type,
                            "effective_at": effective_date,
                            "ratio": number(action.get("ratio")),
                        }
                    )
                elif action_type in {"dividend", "fund_distribution"} and number(action.get("cash_amount")) >= 0:
                    cash_amount = units[symbol] * number(action.get("cash_amount"))
                    cash += cash_amount
                    applied_actions.append(
                        {
                            "symbol": symbol,
                            "action_type": action_type,
                            "effective_at": effective_date,
                            "cash_amount": rounded(cash_amount, 4),
                        }
                    )
                else:
                    ignored_actions.append(
                        {
                            "symbol": symbol,
                            "action_type": action_type,
                            "effective_at": effective_date,
                            "reason": "unsupported_in_backtest",
                        }
                    )
            previous_equity = equity
            current_values = {
                symbol: units[symbol] * series[symbol][current_date]
                for symbol in symbol_list
            }
            equity = cash + sum(current_values.values())
            if strategy in {"equal_weight", "momentum"} and index % 21 == 0:
                if strategy == "equal_weight":
                    targets = {symbol: 1 / len(symbol_list) for symbol in symbol_list}
                else:
                    lookback = max(0, index - 60)
                    ranked = sorted(
                        symbol_list,
                        key=lambda symbol: series[symbol][previous_date] / series[symbol][dates[lookback]] - 1,
                        reverse=True,
                    )
                    selected = set(ranked[: max(1, math.ceil(len(ranked) / 2))])
                    targets = {symbol: (1 / len(selected) if symbol in selected else 0.0) for symbol in symbol_list}
                desired_before_cost = {
                    symbol: equity * targets[symbol] for symbol in symbol_list
                }
                traded_notional = sum(
                    abs(desired_before_cost[symbol] - current_values[symbol])
                    for symbol in symbol_list
                )
                transaction_cost = min(equity, traded_notional * cost_rate)
                post_cost_equity = max(0.0, equity - transaction_cost)
                turnover_total += (
                    traded_notional / (2 * equity) if equity > 0 else 0.0
                )
                rebalance_cost += transaction_cost
                units = {
                    symbol: (
                        post_cost_equity * targets[symbol]
                        / series[symbol][current_date]
                        if series[symbol][current_date] > 0
                        else 0.0
                    )
                    for symbol in symbol_list
                }
                cash = 0.0
                equity = post_cost_equity
                target_weights = targets
            daily_returns.append(
                equity / previous_equity - 1 if previous_equity > 0 else 0.0
            )
            curve.append({"date": current_date, "value": rounded(equity, 2)})
        values = [number(item["value"]) for item in curve]
        volatility = statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS) if len(daily_returns) > 1 else 0.0
        years = (len(dates) - 1) / TRADING_DAYS
        annual_return = (
            (equity / capital) ** (1 / years) - 1
            if years > 0 and equity > 0 and capital > 0
            else None
        )
        sharpe = (_mean(daily_returns) / statistics.stdev(daily_returns) * math.sqrt(TRADING_DAYS)) if len(daily_returns) > 1 and statistics.stdev(daily_returns) > 0 else None
        ending_values = {
            symbol: units[symbol] * series[symbol][dates[-1]]
            for symbol in symbol_list
        }
        ending_total = cash + sum(ending_values.values())
        return {
            "ok": True,
            "status": "ready",
            "strategy": strategy,
            "symbols": symbol_list,
            "sample_count": len(dates),
            "initial_capital": rounded(capital, 2),
            "ending_value": rounded(equity, 2),
            "total_return_percent": rounded((equity / capital - 1) * 100, 2),
            "annualized_return_percent": rounded((annual_return or 0) * 100, 2) if annual_return is not None else None,
            "annualized_volatility_percent": rounded(volatility * 100, 2),
            "sharpe_ratio": rounded(sharpe, 3),
            "max_drawdown_percent": rounded((_max_drawdown(values) or 0) * 100, 2),
            "turnover_percent": rounded(turnover_total * 100, 2),
            "ending_weights": {
                symbol: (
                    rounded(ending_values[symbol] / ending_total * 100, 4)
                    if ending_total > 0
                    else None
                )
                for symbol in symbol_list
            },
            "cost_assumptions": {
                "fee_percent": fee_percent,
                "slippage_percent": slippage_percent,
                "initial_transaction_cost": rounded(initial_transaction_cost, 2),
                "rebalance_transaction_cost": rounded(rebalance_cost, 2),
                "total_transaction_cost": rounded(initial_transaction_cost + rebalance_cost, 2),
                "initial_purchase_included": True,
            },
            "methodology": "unit_based_holdings_with_natural_weight_drift",
            "lookahead_protection": "signals use only prices available before each rebalance",
            "corporate_actions": {
                "policy": "approved actions only",
                "applied": applied_actions,
                "ignored": ignored_actions,
            },
            "analysis_coverage": {
                "requested_symbols": symbol_list,
                "available_symbols": symbol_list,
                "common_date_count": len(dates),
                "start_date": dates[0],
                "end_date": dates[-1],
            },
            "assumptions": [
                "Stored close prices are treated as unadjusted prices.",
                "Approved splits adjust units and approved cash distributions enter cash.",
                "Taxes, FX conversion, delistings, survivorship bias and unapproved corporate actions are not modeled.",
            ],
            "equity_curve": curve[-800:],
        }

    def rebalance(self, state: dict[str, Any], targets: dict[str, float] | None = None, max_position_percent: float = 35, cash_reserve_percent: float = 5, min_trade_value: float = 1000, fee_percent: float = 0.1425) -> dict[str, Any]:
        positions = self.current_positions(state)
        total = sum(number(item.get("market_value")) for item in positions)
        if total <= 0:
            return {"ok": False, "status": "empty", "orders": []}
        symbols = [str(item.get("symbol") or "") for item in positions]
        raw_targets = {str(key).upper(): max(0.0, number(value)) for key, value in (targets or {}).items()}
        if not raw_targets:
            raw_targets = {symbol: 100 / len(symbols) for symbol in symbols}
        investable_percent = max(0.0, 100 - cash_reserve_percent)
        position_cap = max(0.0, min(100.0, max_position_percent))
        normalized = {symbol: 0.0 for symbol in symbols}
        active = {symbol for symbol in symbols if raw_targets.get(symbol, 0.0) > 0}
        remaining = investable_percent
        while active and remaining > 1e-9:
            active_total = sum(raw_targets[symbol] for symbol in active)
            if active_total <= 0:
                break
            capped_symbols = {
                symbol
                for symbol in active
                if remaining * raw_targets[symbol] / active_total > position_cap
            }
            if not capped_symbols:
                for symbol in active:
                    normalized[symbol] += remaining * raw_targets[symbol] / active_total
                remaining = 0.0
                break
            for symbol in capped_symbols:
                allocation = min(position_cap - normalized[symbol], remaining)
                normalized[symbol] += max(0.0, allocation)
                remaining -= max(0.0, allocation)
                active.remove(symbol)
        actual_invested_percent = sum(normalized.values())
        orders = []
        estimated_fees = 0.0
        for position in positions:
            symbol = str(position.get("symbol") or "")
            current_value = number(position.get("market_value"))
            target_value = total * normalized.get(symbol, 0) / 100
            difference = target_value - current_value
            if abs(difference) < max(0.0, min_trade_value):
                continue
            price = number(position.get("price"))
            quantity = abs(difference) / price if price > 0 else 0
            fee = abs(difference) * max(0.0, fee_percent) / 100
            estimated_fees += fee
            orders.append(
                {
                    "symbol": symbol,
                    "side": "BUY" if difference > 0 else "SELL",
                    "quantity": rounded(quantity, 4),
                    "estimated_value": rounded(abs(difference), 2),
                    "estimated_fee": rounded(fee, 2),
                    "current_weight_percent": position.get("weight_percent"),
                    "target_weight_percent": rounded(normalized.get(symbol, 0), 2),
                    "price": rounded(price, 4),
                }
            )
        return {
            "ok": True,
            "status": "draft",
            "execution_policy": "simulation_only_human_approval_required",
            "portfolio_value": rounded(total, 2),
            "cash_reserve_percent": rounded(max(0.0, 100 - actual_invested_percent), 2),
            "requested_cash_reserve_percent": cash_reserve_percent,
            "max_position_percent": position_cap,
            "estimated_fees": rounded(estimated_fees, 2),
            "orders": sorted(orders, key=lambda item: number(item.get("estimated_value")), reverse=True),
            "before_risk": self.risk(state),
        }

    def add_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        scheduled = parse_datetime(payload.get("scheduled_at") or payload.get("published_at")) or utc_now()
        symbol = str(payload.get("symbol") or "").strip().upper()
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("event title is required")
        event_type = str(payload.get("event_type") or "news").strip().lower()
        source = str(payload.get("source") or "manual").strip()
        dedupe_source = str(payload.get("dedupe_key") or f"{event_type}|{symbol}|{title}|{utc_text(scheduled)[:16]}")
        dedupe_key = hashlib.sha256(dedupe_source.encode("utf-8")).hexdigest()
        event_id = str(payload.get("event_id") or uuid.uuid4().hex)
        row = {
            "event_id": event_id,
            "dedupe_key": dedupe_key,
            "event_type": event_type,
            "symbol": symbol,
            "title": title,
            "scheduled_at": utc_text(scheduled),
            "source": source,
            "source_url_encrypted": protect_text(str(payload.get("source_url") or payload.get("url") or "")),
            "sentiment": rounded(number(payload.get("sentiment")), 4) if payload.get("sentiment") is not None else None,
            "confidence": max(0.0, min(1.0, number(payload.get("confidence"), 0.5))),
            "status": str(payload.get("status") or "scheduled"),
            "details_encrypted": protect_text(_json(payload.get("details") or {})),
            "created_at": utc_text(),
        }
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM market_events WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            if existing is not None:
                existing_details = _decoded_json(
                    unprotect_text(str(existing["details_encrypted"] or "")),
                    {},
                )
                if (
                    existing["sentiment"] == row["sentiment"]
                    and existing["confidence"] == row["confidence"]
                    and existing["status"] == row["status"]
                    and existing_details == (payload.get("details") or {})
                ):
                    return self._public_event(existing)
            connection.execute(
                """
                INSERT INTO market_events VALUES(
                    :event_id, :dedupe_key, :event_type, :symbol, :title,
                    :scheduled_at, :source, :source_url_encrypted, :sentiment,
                    :confidence, :status, :details_encrypted, :created_at
                ) ON CONFLICT(dedupe_key) DO UPDATE SET
                    sentiment=excluded.sentiment, confidence=excluded.confidence,
                    status=excluded.status, details_encrypted=excluded.details_encrypted
                """,
                row,
            )
        return self._public_event(row)

    def add_events(
        self,
        payloads: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Persist a group of events with one encrypted database write."""

        normalized = [dict(payload) for payload in payloads]
        if not normalized:
            return []
        with self.batch_updates():
            return [self.add_event(payload) for payload in normalized]

    @staticmethod
    def _public_event(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["source_url"] = unprotect_text(str(item.pop("source_url_encrypted", "") or ""))
        details = unprotect_text(str(item.pop("details_encrypted", "") or ""))
        item["details"] = _decoded_json(details, {})
        return item

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM market_events ORDER BY scheduled_at DESC LIMIT ?",
                (max(1, min(2000, limit)),),
            ).fetchall()
        return [self._public_event(row) for row in rows]

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
            rows = connection.execute("SELECT * FROM alert_rules ORDER BY created_at").fetchall()
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
                continue
            operator = str(rule.get("operator") or ">=")
            condition = value <= threshold if rule_type == "event_window" else self._compare(float(value), operator, threshold)
            if not condition:
                continue
            bucket_minutes = max(1, int(rule.get("cooldown_minutes") or DEFAULT_ALERT_COOLDOWN_MINUTES))
            with self.connect() as connection:
                previous = connection.execute(
                    "SELECT triggered_at FROM alert_events WHERE rule_id = ? ORDER BY triggered_at DESC LIMIT 1",
                    (rule["rule_id"],),
                ).fetchone()
            previous_at = parse_datetime(previous["triggered_at"]) if previous else None
            if previous_at is not None and (now - previous_at).total_seconds() < bucket_minutes * 60:
                continue
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
            if cursor.rowcount:
                triggered.append(event)
        return triggered

    def list_alert_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM alert_events ORDER BY triggered_at DESC LIMIT ?",
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
        aliases = {
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
        supplied_direction = str(
            explicit.get("direction")
            or action.get("prediction_direction")
            or ""
        ).strip().lower()
        direction = aliases.get(supplied_direction, supplied_direction)
        explicitly_directional = direction in {"bullish", "bearish", "neutral"}
        if not explicitly_directional:
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
                direction = "bullish"
            elif any(
                token in action_text
                for token in (
                    "sell", "reduce", "decrease", "exit",
                    "賣出", "減碼", "清倉", "看跌",
                )
            ):
                direction = "bearish"
            elif any(
                token in action_text
                for token in (
                    "hold", "monitor", "observe", "wait",
                    "持有", "觀察", "監控", "等待",
                )
            ):
                direction = "neutral"
            else:
                direction = "abstain"
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
        for action in command.get("action_plan", []):
            if not isinstance(action, dict):
                continue
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
            row = {
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
            with self.connect() as connection:
                cursor = connection.execute(
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
                    row,
                )
            count += int(bool(cursor.rowcount))
        return count

    def update_decision_outcomes(self) -> int:
        now = utc_now()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM decisions WHERE outcome_encrypted = '' AND outcome_due_at <= ?",
                (utc_text(now),),
            ).fetchall()
        updated = 0
        for row in rows:
            with self.connect() as connection:
                evaluation_bar = connection.execute(
                    """
                    SELECT close, observed_at, provider, verified
                    FROM prices
                    WHERE symbol = ? AND observed_at >= ?
                    ORDER BY observed_at ASC, verified DESC, provider
                    LIMIT 1
                    """,
                    (str(row["symbol"]), str(row["outcome_due_at"])),
                ).fetchone()
            if evaluation_bar is None:
                continue
            price = number(evaluation_bar["close"], -1)
            reference = number(row["reference_price"], -1)
            if price <= 0 or reference <= 0:
                continue
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
            outcome = {
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
            with self.connect() as connection:
                connection.execute(
                    "UPDATE decisions SET outcome_encrypted = ? WHERE decision_id = ?",
                    (protect_text(_json(outcome)), row["decision_id"]),
                )
            updated += 1
        return updated

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
                "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?",
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

    def calibration(self) -> dict[str, Any]:
        all_decisions = self.decisions(2000)
        evaluated = [
            item
            for item in all_decisions
            if item.get("outcome", {}).get("evaluable") is True
            and bool(item.get("eligible_for_calibration"))
        ]
        scored: list[float] = []
        correct = 0
        bucket_values: dict[str, list[tuple[float, float]]] = {
            "low": [],
            "medium": [],
            "high": [],
        }
        for item in evaluated:
            outcome = (
                1.0 if item.get("outcome", {}).get("success") is True else 0.0
            )
            probability = normalized_probability(item.get("confidence"), 0.5)
            correct += int(outcome == 1.0)
            scored.append((probability - outcome) ** 2)
            bucket = "high" if probability >= 0.75 else "medium" if probability >= 0.55 else "low"
            bucket_values[bucket].append((probability, outcome))
        buckets = [
            {
                "key": key,
                "label": {"low": "低信心", "medium": "中信心", "high": "高信心"}[key],
                "count": len(values),
                "average_confidence": rounded(_mean([value[0] for value in values]) * 100, 2) if values else None,
                "accuracy_percent": rounded(_mean([value[1] for value in values]) * 100, 2) if values else None,
            }
            for key, values in bucket_values.items()
        ]
        average_confidence = _mean(
            [normalized_probability(item.get("confidence"), 0.5) for item in evaluated]
        ) if evaluated else 0.0
        accuracy = correct / len(evaluated) if evaluated else 0.0
        reliability_gap = average_confidence - accuracy
        confidence_multiplier = max(0.65, min(1.05, 1.0 - max(0.0, reliability_gap)))
        slices: list[dict[str, Any]] = []
        grouped: dict[tuple[str, str], list[tuple[float, float]]] = {}
        for item in evaluated:
            probability = normalized_probability(item.get("confidence"), 0.5)
            outcome = 1.0 if item.get("outcome", {}).get("success") is True else 0.0
            evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
            dimensions = {
                "direction": str(item.get("prediction_direction") or "unknown"),
                "market": str(evidence.get("market") or "UNKNOWN"),
                "asset_type": str(evidence.get("asset_type") or "UNKNOWN"),
            }
            for dimension, key in dimensions.items():
                grouped.setdefault((dimension, key), []).append((probability, outcome))
        for (dimension, key), values in sorted(grouped.items()):
            slices.append(
                {
                    "dimension": dimension,
                    "key": key,
                    "evaluated_count": len(values),
                    "brier_score": rounded(
                        _mean([(probability - outcome) ** 2 for probability, outcome in values]),
                        4,
                    ),
                    "accuracy_percent": rounded(
                        _mean([outcome for _probability, outcome in values]) * 100,
                        2,
                    ),
                    "average_confidence_percent": rounded(
                        _mean([probability for probability, _outcome in values]) * 100,
                        2,
                    ),
                }
            )
        sample_version = (
            hashlib.sha256(
                _json(
                    [
                        {
                            "decision_id": item.get("decision_id"),
                            "evaluated_at": item.get("outcome", {}).get("evaluated_at"),
                            "direction": item.get("prediction_direction"),
                            "success": item.get("outcome", {}).get("success"),
                        }
                        for item in evaluated
                    ]
                ).encode("utf-8")
            ).hexdigest()[:24]
            if evaluated
            else ""
        )
        return {
            "status": "ready" if scored else "collecting",
            "evaluated_count": len(scored),
            "pending_count": sum(
                1
                for item in all_decisions
                if bool(item.get("eligible_for_calibration")) and not item.get("outcome")
            ),
            "not_calibrated_count": sum(
                1
                for item in all_decisions
                if not bool(item.get("eligible_for_calibration"))
            ),
            "brier_score": rounded(_mean(scored), 4) if scored else None,
            "accuracy_percent": rounded(accuracy * 100, 2) if scored else None,
            "average_confidence_percent": rounded(average_confidence * 100, 2) if scored else None,
            "reliability_gap_percent": rounded(reliability_gap * 100, 2) if scored else None,
            "confidence_multiplier": rounded(confidence_multiplier, 4),
            "confidence_buckets": buckets,
            "sample_version": sample_version,
            "slices": slices,
            "calibration_label": "穩定" if scored and _mean(scored) <= 0.2 else "需校準" if scored else "累積結果中",
        }

    def analytics_snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        with self.batch_updates():
            return self._analytics_snapshot(state)

    def _analytics_snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        performance = self.performance(state)
        ledger = performance.get("ledger", {})
        risk = self.risk(state)
        self.update_decision_outcomes()
        triggered = self.evaluate_alerts(state, risk)
        with self.connect() as connection:
            counts = {
                table: int(
                    connection.execute(
                        "SELECT COUNT(*) FROM transactions WHERE deleted_at = ''"
                        if table == "transactions"
                        else f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in (
                    "transactions",
                    "prices",
                    "portfolio_snapshots",
                    "market_events",
                    "alert_rules",
                    "alert_events",
                    "decisions",
                )
            }
            tombstone_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM transactions WHERE deleted_at <> ''"
                ).fetchone()[0]
            )
        return {
            "version": "4.0.0",
            "generated_at": utc_text(),
            "database_path": str(self.database_path),
            "data_health": {
                "schema_version": SCHEMA_VERSION,
                "counts": counts,
                "transaction_tombstone_count": tombstone_count,
                "history_ready": counts["prices"] >= max(30, len(state.get("holdings", [])) * 30),
                "ledger_ready": counts["transactions"] > 0,
                "ledger_quality": ledger.get("ledger_quality", "empty"),
                "warnings": [
                    message
                    for condition, message in (
                        (counts["transactions"] == 0, "尚未建立交易帳本，XIRR 與已實現損益可能不完整。"),
                        (risk.get("status") != "ready", "歷史行情不足，專業風險指標暫不完整。"),
                    )
                    if condition
                ],
            },
            "privacy": privacy_status(),
            "performance": performance,
            "risk": risk,
            "stress": self.stress_test(state),
            "ledger": {
                "transactions": self.list_transactions(100),
                **ledger,
                "reconciliation": self.reconcile_ledger_holdings(state),
            },
            "events": self.list_events(100),
            "alerts": {
                "rules": self.list_alert_rules(),
                "events": self.list_alert_events(100),
                "new_count": len(triggered),
                "unacknowledged_count": sum(1 for item in self.list_alert_events(500) if not item.get("acknowledged_at")),
            },
            "decisions": self.decisions(100),
            "calibration": self.calibration(),
        }


POSITIVE_WORDS = {"beat", "growth", "raise", "upgrade", "profit", "surge", "record", "成長", "上修", "獲利", "創高", "優於"}
NEGATIVE_WORDS = {"miss", "cut", "downgrade", "loss", "fall", "risk", "fraud", "下修", "虧損", "衰退", "風險", "裁員"}


def sentiment_score(text: str) -> tuple[float, float]:
    lowered = str(text or "").lower()
    positive = sum(1 for word in POSITIVE_WORDS if word in lowered)
    negative = sum(1 for word in NEGATIVE_WORDS if word in lowered)
    total = positive + negative
    return ((positive - negative) / total if total else 0.0, min(1.0, 0.35 + total * 0.12))


def fetch_json_with_retry(
    url: str,
    *,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    attempts: int = 3,
    timeout_seconds: float = 15,
    base_delay_seconds: float = 0.25,
) -> Any:
    """Fetch public JSON with bounded retry for transient transport failures."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 GPTBridgeInvestmentManager/2.0", "Accept": "application/json"},
    )
    open_url = opener or urllib.request.urlopen
    wait = sleep or time.sleep
    maximum_attempts = max(1, min(5, int(attempts)))
    last_error: Exception | None = None
    for attempt in range(maximum_attempts):
        try:
            with open_url(request, timeout=max(1.0, min(30.0, timeout_seconds))) as response:
                status = int(getattr(response, "status", 200) or 200)
                if status == 429 or 500 <= status <= 599:
                    raise urllib.error.HTTPError(
                        url,
                        status,
                        f"transient HTTP status {status}",
                        getattr(response, "headers", None),
                        None,
                    )
                if status >= 400:
                    raise urllib.error.HTTPError(
                        url,
                        status,
                        f"HTTP status {status}",
                        getattr(response, "headers", None),
                        None,
                    )
                return json.loads(
                    response.read().decode("utf-8", errors="replace")
                )
        except urllib.error.HTTPError as exc:
            last_error = exc
            transient = exc.code == 429 or 500 <= exc.code <= 599
            if not transient or attempt + 1 >= maximum_attempts:
                raise
            retry_after = 0.0
            try:
                retry_after = float(exc.headers.get("Retry-After") or 0)
            except (AttributeError, TypeError, ValueError):
                retry_after = 0.0
            wait(
                min(
                    2.0,
                    max(
                        retry_after,
                        max(0.0, base_delay_seconds) * (2**attempt),
                    ),
                )
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 >= maximum_attempts:
                raise
            wait(min(2.0, max(0.0, base_delay_seconds) * (2**attempt)))
    if last_error is not None:
        raise last_error
    raise RuntimeError("public JSON fetch failed")


def _default_fetch_json(url: str) -> Any:
    return fetch_json_with_retry(url)


def yahoo_symbol(symbol: str, market: str) -> str:
    normalized = symbol.strip().upper()
    if market.upper() == "TW" and not normalized.endswith((".TW", ".TWO")):
        return f"{normalized}.TW"
    if market.upper() == "HK" and not normalized.endswith(".HK"):
        return f"{int(normalized):04d}.HK" if normalized.isdigit() else f"{normalized}.HK"
    if market.upper() == "CRYPTO" and "-" not in normalized:
        return f"{normalized}-USD"
    return normalized


MARKET_SESSION_DEFINITIONS: dict[str, dict[str, Any]] = {
    "TW": {
        "timezone": "Asia/Taipei",
        "sessions": ((9 * 60, 13 * 60 + 30),),
        "schedule": "09:00-13:30",
    },
    "US": {
        "timezone": "America/New_York",
        "sessions": ((9 * 60 + 30, 16 * 60),),
        "schedule": "09:30-16:00",
    },
    "HK": {
        "timezone": "Asia/Hong_Kong",
        "sessions": ((9 * 60 + 30, 12 * 60), (13 * 60, 16 * 60)),
        "schedule": "09:30-12:00 / 13:00-16:00",
    },
}


def market_session_status(now: datetime | None = None) -> dict[str, Any]:
    observed = now or utc_now()
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    observed = observed.astimezone(timezone.utc)
    markets: dict[str, dict[str, Any]] = {}
    open_markets: list[str] = []

    def nth_sunday(year: int, month: int, occurrence: int) -> int:
        first = datetime(year, month, 1, tzinfo=timezone.utc)
        return 1 + (6 - first.weekday()) % 7 + (occurrence - 1) * 7

    def local_market_time(timezone_name: str) -> datetime:
        if timezone_name in {"Asia/Taipei", "Asia/Hong_Kong"}:
            return observed.astimezone(timezone(timedelta(hours=8)))
        year = observed.year
        dst_start = datetime(
            year,
            3,
            nth_sunday(year, 3, 2),
            7,
            tzinfo=timezone.utc,
        )
        dst_end = datetime(
            year,
            11,
            nth_sunday(year, 11, 1),
            6,
            tzinfo=timezone.utc,
        )
        eastern_offset = -4 if dst_start <= observed < dst_end else -5
        return observed.astimezone(timezone(timedelta(hours=eastern_offset)))

    for market, definition in MARKET_SESSION_DEFINITIONS.items():
        local_time = local_market_time(str(definition["timezone"]))
        minute = local_time.hour * 60 + local_time.minute
        weekday = local_time.weekday() < 5
        is_open = weekday and any(
            start <= minute < end for start, end in definition["sessions"]
        )
        if is_open:
            open_markets.append(market)
        markets[market] = {
            "market": market,
            "is_open": is_open,
            "timezone": definition["timezone"],
            "schedule": definition["schedule"],
            "local_time": local_time.isoformat(),
            "weekday": weekday,
        }
    return {
        "as_of": utc_text(observed),
        "open_markets": open_markets,
        "markets": markets,
    }


def sync_yahoo_open_market_quotes(
    store: InvestmentAnalyticsStore,
    holdings: Sequence[dict[str, Any]],
    open_markets: Sequence[str],
    *,
    fetch_json: FetchJson | None = None,
    max_workers: int = 12,
) -> dict[str, Any]:
    fetch = fetch_json or _default_fetch_json
    active_markets = {str(item or "").strip().upper() for item in open_markets}
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for holding in holdings:
        symbol = str(holding.get("symbol") or "").strip().upper()
        market = str(holding.get("market") or "").strip().upper()
        key = (market, symbol)
        if (
            symbol
            and market in active_markets
            and number(holding.get("quantity"), 0) > 0
            and key not in seen
        ):
            seen.add(key)
            candidates.append(holding)

    def fetch_holding(holding: dict[str, Any]) -> dict[str, Any]:
        symbol = str(holding.get("symbol") or "").strip().upper()
        market = str(holding.get("market") or "").strip().upper()
        requested = yahoo_symbol(symbol, market)
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            + urllib.parse.quote(requested, safe="")
            + "?"
            + urllib.parse.urlencode({"range": "1d", "interval": "1m"})
        )
        payload = fetch(url)
        results = payload.get("chart", {}).get("result") or []
        if not results:
            raise ValueError("報價來源未回傳盤中資料")
        result = results[0]
        meta = result.get("meta") or {}
        timestamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        index = next(
            (
                item
                for item in range(min(len(timestamps), len(closes)) - 1, -1, -1)
                if number(closes[item], -1) > 0
            ),
            -1,
        )
        if index >= 0:
            timestamp = int(timestamps[index])
            close = number(closes[index])

            def value(field: str) -> Any:
                values = quote.get(field) or []
                return values[index] if index < len(values) else None

            open_value = value("open")
            high_value = value("high")
            low_value = value("low")
            volume = value("volume")
        else:
            timestamp = int(meta.get("regularMarketTime") or 0)
            close = number(meta.get("regularMarketPrice"), 0)
            open_value = high_value = low_value = close
            volume = None
        if timestamp <= 0 or close <= 0:
            raise ValueError("報價來源沒有有效的盤中價格")
        return {
            "symbol": symbol,
            "market": market,
            "requested_symbol": requested,
            "source_url": url,
            "bar": {
                "symbol": symbol,
                "observed_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                "open": open_value,
                "high": high_value,
                "low": low_value,
                "close": close,
                "volume": volume,
                "currency": meta.get("currency") or holding.get("currency"),
                "provider": "yahoo-intraday",
                "verified": True,
            },
        }

    updates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    workers = max(1, min(int(max_workers or 1), 12))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_holding, holding): holding for holding in candidates
        }
        for future in as_completed(futures):
            holding = futures[future]
            try:
                updates.append(future.result())
            except Exception as exc:
                errors.append(
                    {
                        "symbol": str(holding.get("symbol") or ""),
                        "market": str(holding.get("market") or ""),
                        "message": str(exc),
                    }
                )
    bars = [item["bar"] for item in updates if isinstance(item.get("bar"), dict)]
    prices_added = store.add_price_bars(bars) if bars else 0
    quotes = [
        {
            "symbol": item["symbol"],
            "market": item["market"],
            "current_price": item["bar"]["close"],
            "currency": item["bar"].get("currency"),
            "observed_at": item["bar"]["observed_at"],
            "source_url": item["source_url"],
        }
        for item in updates
        if isinstance(item.get("bar"), dict)
    ]
    return {
        "provider": "Yahoo Finance",
        "updated_at": utc_text(),
        "data_as_of": max(
            (str(item.get("observed_at") or "") for item in quotes),
            default="",
        ),
        "open_markets": sorted(active_markets),
        "requested_count": len(candidates),
        "updated_count": len(updates),
        "coverage_percent": (
            rounded(len(updates) / len(candidates) * 100, 2)
            if candidates
            else 100.0
        ),
        "prices_added": prices_added,
        "quotes": quotes,
        "error_count": len(errors),
        "errors": errors[:50],
        "methodology": "latest valid intraday close for exchanges currently in session",
        "limitations": [
            "Public endpoint availability and exchange delays may affect freshness.",
            "A quote is not a broker-executable price.",
        ],
    }


def sync_yahoo_dividends(
    holdings: Sequence[dict[str, Any]],
    *,
    fetch_json: FetchJson | None = None,
    period: str = "2y",
    max_workers: int = 8,
) -> dict[str, Any]:
    fetch = fetch_json or _default_fetch_json
    candidates = [
        holding
        for holding in holdings
        if str(holding.get("market") or "").upper() in {"TW", "US", "HK"}
        and str(holding.get("symbol") or "").strip()
        and number(holding.get("quantity")) > 0
    ][:300]
    trailing_start = datetime.now(timezone.utc) - timedelta(days=366)

    def infer_frequency(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
        event_dates = sorted(
            {
                parsed
                for event in events
                if (parsed := parse_datetime(event.get("occurred_at"))) is not None
            }
        )
        if len(event_dates) < 2:
            return {
                "dividend_frequency": "unknown",
                "dividend_frequency_label": "待累積資料",
                "dividend_frequency_per_year": None,
                "dividend_frequency_confidence": 0.0,
            }
        intervals = [
            (event_dates[index] - event_dates[index - 1]).total_seconds() / 86400
            for index in range(1, len(event_dates))
        ]
        median_days = statistics.median(intervals)
        definitions = (
            (10, "weekly", "每週", 52),
            (20, "biweekly", "每兩週", 26),
            (45, "monthly", "每月", 12),
            (75, "bimonthly", "每兩月", 6),
            (120, "quarterly", "每季", 4),
            (220, "semiannual", "每半年", 2),
            (420, "annual", "每年", 1),
        )
        code, label, per_year = "irregular", "不定期", None
        for maximum_days, candidate_code, candidate_label, candidate_per_year in definitions:
            if median_days <= maximum_days:
                code, label, per_year = (
                    candidate_code,
                    candidate_label,
                    candidate_per_year,
                )
                break
        tolerance = max(7.0, median_days * 0.45)
        if len(intervals) >= 3 and max(abs(value - median_days) for value in intervals) > tolerance:
            code, label, per_year = "irregular", "不定期", None
        confidence = min(0.98, 0.58 + len(intervals) * 0.1)
        return {
            "dividend_frequency": code,
            "dividend_frequency_label": label,
            "dividend_frequency_per_year": per_year,
            "dividend_frequency_median_days": rounded(median_days, 2),
            "dividend_frequency_confidence": rounded(confidence, 2),
        }

    def fetch_holding(holding: dict[str, Any]) -> dict[str, Any]:
        symbol = str(holding.get("symbol") or "").strip().upper()
        market = str(holding.get("market") or "").strip().upper()
        requested = yahoo_symbol(symbol, market)
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            + urllib.parse.quote(requested, safe="")
            + "?"
            + urllib.parse.urlencode(
                {"range": period, "interval": "1mo", "events": "div"}
            )
        )
        payload = fetch(url)
        results = payload.get("chart", {}).get("result") or []
        if not results:
            error = payload.get("chart", {}).get("error") or {}
            raise ValueError(str(error.get("description") or "No dividend result"))
        result = results[0]
        meta = result.get("meta") or {}
        display_name = str(meta.get("longName") or meta.get("shortName") or "").strip()
        existing_name = str(holding.get("name") or "").strip()
        if not display_name and (not existing_name or existing_name.upper() == symbol):
            search_url = (
                "https://query1.finance.yahoo.com/v1/finance/search?"
                + urllib.parse.urlencode(
                    {"q": requested, "quotesCount": 5, "newsCount": 0}
                )
            )
            search_payload = fetch(search_url)
            quotes = search_payload.get("quotes") or []
            exact = next(
                (
                    quote
                    for quote in quotes
                    if str(quote.get("symbol") or "").upper() == requested.upper()
                ),
                quotes[0] if quotes else {},
            )
            display_name = str(
                exact.get("longname") or exact.get("shortname") or ""
            ).strip()
        events = (result.get("events") or {}).get("dividends") or {}
        trailing_events: list[dict[str, Any]] = []
        frequency_events: list[dict[str, Any]] = []
        for entry in events.values():
            timestamp = int(entry.get("date") or 0)
            amount = number(entry.get("amount"), -1)
            if timestamp <= 0 or amount < 0:
                continue
            occurred_at = datetime.fromtimestamp(timestamp, timezone.utc)
            frequency_events.append(
                {
                    "occurred_at": occurred_at.isoformat(),
                    "amount_per_unit": amount,
                }
            )
            if occurred_at >= trailing_start:
                trailing_events.append(
                    {
                        "occurred_at": occurred_at.isoformat(),
                        "amount_per_unit": amount,
                    }
                )
        trailing_annual_per_unit = sum(
            number(item.get("amount_per_unit")) for item in trailing_events
        )
        frequency = infer_frequency(frequency_events)
        current_price = number(
            meta.get("regularMarketPrice") or meta.get("chartPreviousClose"),
            0,
        )
        yield_percent = (
            trailing_annual_per_unit / current_price * 100.0
            if current_price > 0 and trailing_annual_per_unit > 0
            else None
        )
        return {
            "symbol": symbol,
            "market": market,
            "requested_symbol": requested,
            "currency": str(meta.get("currency") or holding.get("currency") or "").upper(),
            "name": display_name,
            "instrument_type": str(meta.get("instrumentType") or "").upper(),
            "exchange_name": str(
                meta.get("fullExchangeName") or meta.get("exchangeName") or ""
            ),
            "current_price": rounded(current_price, 6),
            "trailing_annual_dividend_per_unit": rounded(
                trailing_annual_per_unit, 6
            ),
            "annual_dividend_yield_percent": rounded(yield_percent, 4),
            "event_count": len(trailing_events),
            "events": trailing_events,
            **frequency,
            "source": "Yahoo Finance",
            "source_url": url,
            "updated_at": utc_text(),
            "status": "updated" if trailing_events else "no_external_dividend",
        }

    updates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    workers = max(1, min(int(max_workers or 1), 12))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_holding, holding): holding for holding in candidates
        }
        for future in as_completed(futures):
            holding = futures[future]
            try:
                updates.append(future.result())
            except Exception as exc:
                errors.append(
                    {
                        "symbol": str(holding.get("symbol") or ""),
                        "message": str(exc),
                    }
                )
    updates.sort(key=lambda item: (str(item.get("market")), str(item.get("symbol"))))
    return {
        "requested_count": len(candidates),
        "updated_count": sum(item.get("status") == "updated" for item in updates),
        "no_dividend_count": sum(
            item.get("status") == "no_external_dividend" for item in updates
        ),
        "error_count": len(errors),
        "updates": updates,
        "errors": errors[:50],
        "provider": "Yahoo Finance",
        "updated_at": utc_text(),
        "coverage_percent": (
            rounded(len(updates) / len(candidates) * 100, 2)
            if candidates
            else 100.0
        ),
        "methodology": "trailing cash distributions from public chart events",
        "limitations": [
            "Distribution currency is not converted in this fetch step.",
            "Historical distributions do not guarantee future payments.",
        ],
    }


def sync_yahoo_intelligence(
    store: InvestmentAnalyticsStore,
    holdings: Sequence[dict[str, Any]],
    *,
    fetch_json: FetchJson | None = None,
    period: str = "1y",
) -> dict[str, Any]:
    fetch = fetch_json or _default_fetch_json
    prices_added = 0
    events_added = 0
    news_added = 0
    errors: list[dict[str, str]] = []
    for holding in holdings[:50]:
        symbol = str(holding.get("symbol") or "").strip().upper()
        market = str(holding.get("market") or "").strip().upper()
        requested = yahoo_symbol(symbol, market)
        if not symbol:
            continue
        try:
            chart_url = (
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                + urllib.parse.quote(requested, safe="")
                + "?"
                + urllib.parse.urlencode({"range": period, "interval": "1d", "events": "div,splits"})
            )
            payload = fetch(chart_url)
            result = payload.get("chart", {}).get("result", [])[0]
            timestamps = result.get("timestamp") or []
            quote = (result.get("indicators", {}).get("quote") or [{}])[0]
            meta = result.get("meta") or {}
            bars = []
            def quote_value(field: str, index: int) -> Any:
                values = quote.get(field) or []
                return values[index] if index < len(values) else None

            for index, timestamp in enumerate(timestamps):
                close_values = quote.get("close") or []
                if index >= len(close_values) or number(close_values[index], -1) <= 0:
                    continue
                bars.append(
                    {
                        "symbol": symbol,
                        "observed_at": datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat(),
                        "open": quote_value("open", index),
                        "high": quote_value("high", index),
                        "low": quote_value("low", index),
                        "close": close_values[index],
                        "volume": quote_value("volume", index),
                        "currency": meta.get("currency") or holding.get("currency"),
                        "provider": "yahoo-history",
                        "verified": True,
                    }
                )
            prices_added += store.add_price_bars(bars)
            chart_events = result.get("events") or {}
            for event_type, entries in chart_events.items():
                for entry in (entries or {}).values():
                    store.add_event(
                        {
                            "event_type": "dividend" if event_type == "dividends" else "split",
                            "symbol": symbol,
                            "title": f"{symbol} {'股息' if event_type == 'dividends' else '拆股'}",
                            "scheduled_at": entry.get("date"),
                            "source": "Yahoo Finance",
                            "confidence": 0.85,
                            "details": entry,
                            "dedupe_key": f"yahoo|{symbol}|{event_type}|{entry.get('date')}",
                        }
                    )
                    events_added += 1
        except Exception as exc:
            errors.append({"symbol": symbol, "phase": "history", "message": str(exc)})
        try:
            search_url = "https://query1.finance.yahoo.com/v1/finance/search?" + urllib.parse.urlencode({"q": requested, "newsCount": 8, "quotesCount": 0})
            payload = fetch(search_url)
            for item in payload.get("news", [])[:8]:
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                score, confidence = sentiment_score(title)
                store.add_event(
                    {
                        "event_type": "news",
                        "symbol": symbol,
                        "title": title,
                        "published_at": item.get("providerPublishTime"),
                        "source": item.get("publisher") or "Yahoo Finance",
                        "source_url": item.get("link") or "",
                        "sentiment": score,
                        "confidence": confidence,
                        "status": "published",
                        "dedupe_key": str(item.get("uuid") or item.get("link") or title),
                        "details": {
                            "sentiment_methodology": "lexical_heuristic",
                            "supported_languages": ["English", "Traditional Chinese keyword subset"],
                            "limitations": [
                                "No sarcasm, negation, context or entity-level interpretation.",
                                "Confidence reflects keyword coverage, not predictive certainty.",
                            ],
                        },
                    }
                )
                news_added += 1
        except Exception as exc:
            errors.append({"symbol": symbol, "phase": "news", "message": str(exc)})
    return {
        "ok": not errors or prices_added > 0,
        "prices_added": prices_added,
        "events_added": events_added,
        "news_added": news_added,
        "errors": errors,
        "provider": "Yahoo Finance",
        "updated_at": utc_text(),
        "requested_count": min(50, len(holdings)),
        "coverage_percent": (
            rounded(
                (min(50, len(holdings)) - len({item.get("symbol") for item in errors}))
                / min(50, len(holdings))
                * 100,
                2,
            )
            if holdings[:50]
            else 100.0
        ),
        "sentiment_methodology": {
            "method": "lexical_heuristic",
            "is_ai_model": False,
            "languages": ["English", "Traditional Chinese keyword subset"],
            "limitations": "Keyword polarity only; do not use as a trading signal.",
        },
        "methodology": "public daily OHLCV, corporate events and headline keyword polarity",
        "message": f"市場情報同步完成：{prices_added} 筆行情、{events_added + news_added} 筆事件。",
    }
