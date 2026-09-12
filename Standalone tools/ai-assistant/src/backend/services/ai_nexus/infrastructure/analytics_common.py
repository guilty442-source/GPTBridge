from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .privacy import (
    decode_binary_document,
    decode_json_document,
    encode_binary_document,
    privacy_status,
    protect_text,
    unprotect_text,
)
from .watch_repository import (
    _iter_migration_files,
    _validated_storage_path,
)
from ..domain.contract import INVESTMENT_ANALYTICS_SCHEMA_VERSION


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
