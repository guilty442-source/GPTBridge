from __future__ import annotations

import hashlib
import json
import importlib.util
import os
import re
import stat
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    from .privacy import decode_json_document, encode_json_document
    from ..domain.contract import (
        INVESTMENT_APP_VERSION,
        INVESTMENT_STATE_MIN_SUPPORTED_SCHEMA_VERSION,
        INVESTMENT_STATE_SCHEMA_VERSION,
    )
except ImportError:
    _privacy_path = Path(__file__).with_name("privacy.py")
    _privacy_spec = importlib.util.spec_from_file_location(
        "investment_watch_privacy",
        _privacy_path,
    )
    if _privacy_spec is None or _privacy_spec.loader is None:
        raise
    _privacy_module = importlib.util.module_from_spec(_privacy_spec)
    sys.modules[_privacy_spec.name] = _privacy_module
    _privacy_spec.loader.exec_module(_privacy_module)
    decode_json_document = _privacy_module.decode_json_document
    encode_json_document = _privacy_module.encode_json_document
    _contract_path = Path(__file__).resolve().parent.parent / "domain" / "contract.py"
    _contract_spec = importlib.util.spec_from_file_location(
        "investment_watch_contract",
        _contract_path,
    )
    if _contract_spec is None or _contract_spec.loader is None:
        raise
    _contract_module = importlib.util.module_from_spec(_contract_spec)
    sys.modules[_contract_spec.name] = _contract_module
    _contract_spec.loader.exec_module(_contract_module)
    INVESTMENT_APP_VERSION = _contract_module.INVESTMENT_APP_VERSION
    INVESTMENT_STATE_MIN_SUPPORTED_SCHEMA_VERSION = (
        _contract_module.INVESTMENT_STATE_MIN_SUPPORTED_SCHEMA_VERSION
    )
    INVESTMENT_STATE_SCHEMA_VERSION = (
        _contract_module.INVESTMENT_STATE_SCHEMA_VERSION
    )


def utc_now() -> str:
    return datetime.now().astimezone().isoformat()


DATA_ROOT_ENV = "GPTBRIDGE_AI_ASSISTANT_DATA_ROOT"
PROFILE_ENV = "GPTBRIDGE_AI_ASSISTANT_PROFILE"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class InvestmentStateRecoveryRequired(RuntimeError):
    """Raised when persisted state exists but cannot be safely decoded."""


class InvestmentStateUpgradeRequired(RuntimeError):
    """Raised when state was written by a newer, unsupported application."""


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
    """Acquire an OS-owned exclusive lock without relying on PID metadata."""

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
            payload = {
                "pid": os.getpid(),
                "token": self.token,
                "component": self.component,
                "acquired_at": utc_now(),
            }
            encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, encoded)
            os.ftruncate(descriptor, len(encoded))
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


def _normalized_profile(value: Any) -> str:
    profile = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "default").strip())
    return profile.strip(".-")[:64] or "default"


def _same_path_identity(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _is_link_or_reparse(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RuntimeError(f"Cannot safely inspect investment path {path}") from error
    attributes = int(getattr(path_stat, "st_file_attributes", 0) or 0)
    return path.is_symlink() or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _lexical_absolute(path: Path, *, label: str) -> Path:
    requested = Path(path).expanduser()
    if not requested.is_absolute():
        raise RuntimeError(f"{label} must be an absolute path: {requested}")
    normalized = Path(os.path.abspath(str(requested)))
    if not _same_path_identity(requested, normalized):
        raise RuntimeError(f"{label} must not contain traversal: {requested}")
    return normalized


def _validated_storage_path(
    path: Path,
    *,
    label: str,
    boundary: Path | None = None,
    require_exists: bool = False,
    expected_kind: str | None = None,
) -> Path:
    """Validate a lexical path without resolving links outside its boundary."""

    requested = _lexical_absolute(path, label=label)
    if boundary is not None:
        validated_boundary = _lexical_absolute(boundary, label=f"{label} boundary")
        try:
            requested.relative_to(validated_boundary)
        except ValueError as error:
            raise RuntimeError(
                f"{label} escaped its storage boundary: {requested}"
            ) from error

    chain = [*reversed(requested.parents), requested]
    for candidate in chain:
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RuntimeError(f"Cannot safely inspect {label}: {candidate}") from error
        if _is_link_or_reparse(candidate):
            raise RuntimeError(
                f"{label} cannot use a symlink, junction, or reparse point: "
                f"{candidate}"
            )
        try:
            canonical = candidate.resolve(strict=True)
        except OSError as error:
            raise RuntimeError(f"Cannot safely resolve {label}: {candidate}") from error
        if not _same_path_identity(candidate, canonical):
            raise RuntimeError(
                f"{label} has a non-canonical ancestor: {candidate}"
            )

    exists = requested.exists() or requested.is_symlink()
    if require_exists and not exists:
        raise RuntimeError(f"{label} does not exist: {requested}")
    if exists:
        requested_stat = requested.lstat()
        if expected_kind == "directory" and not stat.S_ISDIR(
            requested_stat.st_mode
        ):
            raise RuntimeError(f"{label} is not a directory: {requested}")
        if expected_kind == "file" and not stat.S_ISREG(requested_stat.st_mode):
            raise RuntimeError(f"{label} is not a regular file: {requested}")
    return requested


def _iter_migration_files(
    root: Path,
    *,
    label: str = "Legacy investment state",
) -> Iterator[Path]:
    validated_root = _validated_storage_path(
        root,
        label=f"{label} root",
        require_exists=True,
        expected_kind="directory",
    )

    def migration_error(error: OSError) -> None:
        raise RuntimeError(
            f"Cannot safely enumerate {label} root: {validated_root}"
        ) from error

    for current_root, directory_names, file_names in os.walk(
        validated_root,
        topdown=True,
        onerror=migration_error,
        followlinks=False,
    ):
        directory_names.sort()
        file_names.sort()
        current = _validated_storage_path(
            Path(current_root),
            label=f"{label} directory",
            boundary=validated_root,
            require_exists=True,
            expected_kind="directory",
        )
        for name in directory_names:
            _validated_storage_path(
                current / name,
                label=f"{label} directory",
                boundary=validated_root,
                require_exists=True,
                expected_kind="directory",
            )
        for name in file_names:
            yield _validated_storage_path(
                current / name,
                label=f"{label} file",
                boundary=validated_root,
                require_exists=True,
                expected_kind="file",
            )


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
            label="AI investment profile root",
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
            label="AI investment profile root",
            boundary=base,
            expected_kind="directory",
        )
    # Preserve embedders and isolated test roots that intentionally own their runtime.
    return _validated_storage_path(
        tool_root / "runtime",
        label="AI investment runtime root",
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
        label="Investment data directory",
        expected_kind="directory",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _validated_storage_path(
        path.parent,
        label="Investment data directory",
        require_exists=True,
        expected_kind="directory",
    )
    _validated_storage_path(
        path,
        label="Investment data file",
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


class InvestmentWatchRepository:
    def __init__(self, tool_root: Path) -> None:
        self.tool_root = _validated_storage_path(
            Path(tool_root),
            label="AI assistant tool root",
            require_exists=True,
            expected_kind="directory",
        )
        self.legacy_runtime_root = _validated_storage_path(
            self.tool_root / "runtime",
            label="Legacy AI investment runtime root",
            boundary=self.tool_root,
            expected_kind="directory",
        )
        self.runtime_root = _runtime_root(self.tool_root)
        self.state_root = self.runtime_root / "state"
        self.state_path = self.state_root / "investment_watch_state.json"
        self.history_root = self.state_root / "investment-watch-history"
        self.recovery_root = self.state_root / "recovery"
        self._state_lock = threading.RLock()
        for path, label in (
            (self.runtime_root, "AI investment runtime root"),
            (self.state_root, "AI investment state root"),
            (self.history_root, "AI investment history root"),
        ):
            _validated_storage_path(
                path,
                label=label,
                boundary=self.runtime_root,
                expected_kind="directory",
            )
        self.state_root.mkdir(parents=True, exist_ok=True)
        _validated_storage_path(
            self.state_root,
            label="AI investment state root",
            boundary=self.runtime_root,
            require_exists=True,
            expected_kind="directory",
        )
        self.history_root.mkdir(parents=True, exist_ok=True)
        for path, label in (
            (self.runtime_root, "AI investment runtime root"),
            (self.history_root, "AI investment history root"),
        ):
            _validated_storage_path(
                path,
                label=label,
                boundary=self.runtime_root,
                require_exists=True,
                expected_kind="directory",
            )
        _validated_storage_path(
            self.state_path,
            label="AI investment state file",
            boundary=self.state_root,
            expected_kind="file",
        )
        self._owner_lock = _RuntimeOwnerLock(
            self.runtime_root / ".investment-state-owner.lock",
            "AI investment state",
        )
        self._owner_lock.acquire()
        try:
            self._migrate_legacy_runtime()
        except Exception:
            self._owner_lock.release()
            raise

    def close(self) -> None:
        self._owner_lock.release()

    @contextmanager
    def exclusive_data_access(self) -> Iterator[None]:
        """Coordinate multi-file backup/restore operations in this process."""

        with self._state_lock:
            yield

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _migrate_legacy_runtime(self) -> None:
        legacy_state_root = self.legacy_runtime_root / "state"
        if self.runtime_root == self.legacy_runtime_root:
            return
        _validated_storage_path(
            self.legacy_runtime_root,
            label="Legacy AI investment runtime root",
            boundary=self.tool_root,
            expected_kind="directory",
        )
        if not legacy_state_root.exists() and not legacy_state_root.is_symlink():
            return
        legacy_state_root = _validated_storage_path(
            legacy_state_root,
            label="Legacy investment state root",
            boundary=self.legacy_runtime_root,
            require_exists=True,
            expected_kind="directory",
        )
        copied: list[dict[str, Any]] = []
        conflicts: list[str] = []
        for source in _iter_migration_files(legacy_state_root):
            relative = source.relative_to(legacy_state_root)
            destination = _validated_storage_path(
                self.state_root / relative,
                label="Migrated investment state file",
                boundary=self.state_root,
                expected_kind="file",
            )
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
            marker = {
                "migration": "investment-state-runtime-v1",
                "created_at": utc_now(),
                "source": str(legacy_state_root),
                "destination": str(self.state_root),
                "copied": copied,
                "conflicts": conflicts,
                "legacy_preserved": True,
            }
            _atomic_write_bytes(
                self.state_root / "runtime-migration-manifest.json",
                (json.dumps(marker, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )

    def load_state(self) -> dict[str, Any]:
        with self._state_lock:
            if not self.state_path.exists() and not self.state_path.is_symlink():
                return self._empty_state_with_memory()
            _validated_storage_path(
                self.state_path,
                label="AI investment state file",
                boundary=self.state_root,
                require_exists=True,
                expected_kind="file",
            )
            try:
                payload = decode_json_document(self.state_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("investment state must be an object")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._record_recovery_required(exc)
                raise InvestmentStateRecoveryRequired(
                    "投資資料無法解密或驗證；原檔已保留，請從狀態版本或備份恢復。"
                ) from exc
            payload = self._migrate_state_if_required(payload)
            state = self._empty_state()
            state.update(payload)
            raw_holdings = [dict(item) for item in state.get("holdings", []) if isinstance(item, dict)]
            if any(not str(item.get("holding_id") or "").strip() for item in raw_holdings):
                state["holdings"] = self._with_holding_ids(raw_holdings)
                portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else None
                if portfolio is not None:
                    portfolio["holding_count"] = len(state["holdings"])
                return self.save_state(state)
            state["shared_memory"] = self._shared_memory(state)
            return state

    @staticmethod
    def _state_schema_version(state: dict[str, Any]) -> int:
        raw_version = state.get("schema_version", 1)
        if isinstance(raw_version, bool):
            raise ValueError("investment state schema version is invalid")
        if isinstance(raw_version, float) and not raw_version.is_integer():
            raise ValueError("investment state schema version is invalid")
        if isinstance(raw_version, str) and not raw_version.strip().isdigit():
            raise ValueError("investment state schema version is invalid")
        try:
            version = int(raw_version)
        except (TypeError, ValueError) as exc:
            raise ValueError("investment state schema version is invalid") from exc
        if version < 1:
            raise ValueError("investment state schema version is invalid")
        return version

    def _migrate_state_if_required(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            source_schema = self._state_schema_version(payload)
        except ValueError as exc:
            self._record_recovery_required(exc)
            raise InvestmentStateRecoveryRequired(
                "投資資料格式版本無效；原檔已保留，請從狀態版本或備份恢復。"
            ) from exc
        if source_schema > INVESTMENT_STATE_SCHEMA_VERSION:
            raise InvestmentStateUpgradeRequired(
                "投資資料由較新的程式版本建立；為避免舊版覆寫，請先升級投資管家。"
            )
        if source_schema < INVESTMENT_STATE_MIN_SUPPORTED_SCHEMA_VERSION:
            raise InvestmentStateUpgradeRequired(
                "投資資料格式過舊，無法安全自動遷移；請使用相容版本先完成升級。"
            )
        if source_schema == INVESTMENT_STATE_SCHEMA_VERSION:
            return payload

        snapshot = self._create_state_version_locked(
            payload,
            reason=(
                f"before_schema_upgrade_v{source_schema}"
                f"_to_v{INVESTMENT_STATE_SCHEMA_VERSION}"
            ),
            allow_empty=True,
        )
        migrated = dict(payload)
        migrated["schema_version"] = INVESTMENT_STATE_SCHEMA_VERSION
        migrated["version"] = INVESTMENT_APP_VERSION
        migrated["upgrade"] = {
            "migrated_at": utc_now(),
            "from_schema": source_schema,
            "to_schema": INVESTMENT_STATE_SCHEMA_VERSION,
            "snapshot_version_id": str(
                (snapshot or {}).get("version_id") or ""
            ),
        }
        settings = migrated.get("mobile_sync_settings")
        if not isinstance(settings, dict):
            migrated["mobile_sync_settings"] = {
                "remote_base_url": "",
                "updated_at": "",
            }
        return self.save_state(migrated)

    def _record_recovery_required(self, error: Exception) -> None:
        _validated_storage_path(
            self.recovery_root,
            label="AI investment recovery root",
            boundary=self.state_root,
            expected_kind="directory",
        )
        self.recovery_root.mkdir(parents=True, exist_ok=True)
        _validated_storage_path(
            self.recovery_root,
            label="AI investment recovery root",
            boundary=self.state_root,
            require_exists=True,
            expected_kind="directory",
        )
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        quarantine = self.recovery_root / f"investment_watch_state.{stamp}.corrupt"
        try:
            _copy_verified(self.state_path, quarantine)
        except OSError:
            quarantine = self.state_path
        candidates = [item["version_id"] for item in self.list_state_versions(limit=10)]
        marker = {
            "status": "recovery_required",
            "detected_at": utc_now(),
            "source": str(self.state_path),
            "preserved_copy": str(quarantine),
            "error_type": type(error).__name__,
            "recovery_candidates": candidates,
        }
        _atomic_write_bytes(
            self.recovery_root / "latest-recovery-required.json",
            (json.dumps(marker, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    def save_state(self, state: dict[str, Any]) -> dict[str, Any]:
        with self._state_lock:
            if not isinstance(state, dict):
                raise TypeError("investment state must be a dict")
            source_schema = self._state_schema_version(state)
            if source_schema > INVESTMENT_STATE_SCHEMA_VERSION:
                raise InvestmentStateUpgradeRequired(
                    "拒絕以舊版程式覆寫較新的投資資料格式。"
                )
            state["schema_version"] = INVESTMENT_STATE_SCHEMA_VERSION
            state["version"] = INVESTMENT_APP_VERSION
            state["updated_at"] = utc_now()
            state["shared_memory"] = self._shared_memory(state)
            encoded = encode_json_document(state).encode("utf-8")
            _atomic_write_bytes(self.state_path, encoded)
            return state

    def update_state(
        self,
        mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Atomically load, mutate and durably persist state within this process."""
        if not callable(mutator):
            raise TypeError("state mutator must be callable")
        with self._state_lock:
            current = self.load_state()
            candidate = mutator(current)
            updated = current if candidate is None else candidate
            if not isinstance(updated, dict):
                raise TypeError("state mutator must return a dict or None")
            return self.save_state(updated)

    def save_portfolio(
        self,
        source_path: Path,
        holdings: list[dict[str, Any]],
        workbook_scan: dict[str, Any] | None = None,
        excel_import_profile: dict[str, Any] | None = None,
        import_fingerprint: str = "",
    ) -> dict[str, Any]:
        with self._state_lock:
            return self._save_portfolio_locked(
                source_path,
                holdings,
                workbook_scan=workbook_scan,
                excel_import_profile=excel_import_profile,
                import_fingerprint=import_fingerprint,
            )

    def _save_portfolio_locked(
        self,
        source_path: Path,
        holdings: list[dict[str, Any]],
        workbook_scan: dict[str, Any] | None = None,
        excel_import_profile: dict[str, Any] | None = None,
        import_fingerprint: str = "",
    ) -> dict[str, Any]:
        state = self.load_state()
        normalized_fingerprint = str(import_fingerprint or "").strip()
        current_portfolio = (
            state.get("portfolio")
            if isinstance(state.get("portfolio"), dict)
            else {}
        )
        if (
            normalized_fingerprint
            and current_portfolio.get("import_fingerprint")
            == normalized_fingerprint
        ):
            return state
        self.create_state_version(state, reason="before_import")
        previous_holdings = [
            item for item in state.get("holdings", []) if isinstance(item, dict)
        ]
        previous_by_key = {
            self._holding_import_key(item): item for item in previous_holdings
        }
        merged_holdings: list[dict[str, Any]] = []
        preserved_fields = (
            "web_current_price",
            "web_current_price_currency",
            "web_current_value_twd",
            "market_data_source",
            "market_data_source_url",
            "market_data_updated_at",
            "dividend_source",
            "dividend_source_url",
            "dividend_updated_at",
            "dividend_status",
            "dividend_frequency",
            "dividend_frequency_label",
            "dividend_frequency_per_year",
            "dividend_frequency_median_days",
            "dividend_frequency_confidence",
            "dividend_frequency_source",
            "external_annual_dividend_per_unit",
            "fund_code",
            "fund_isin",
            "fund_share_class",
            "fund_quote_symbol",
            "fund_candidate_symbol",
            "fund_identity_status",
            "fund_identity_confidence",
            "fund_identity_source",
            "fund_identity_source_url",
            "fund_identity_candidates",
            "fund_identity_checked_at",
            "fund_identity_confirmation",
        )
        for source in holdings:
            item = dict(source)
            previous = previous_by_key.get(self._holding_import_key(item))
            if previous is not None:
                item["holding_id"] = previous.get("holding_id")
                for field in preserved_fields:
                    if field in previous:
                        item[field] = previous[field]
                if (
                    item.get("average_cost") is None
                    and previous.get("average_cost_method")
                    == "principal_twd_huanan_current_fx_estimate"
                ):
                    for field in (
                        "average_cost",
                        "average_cost_currency",
                        "average_cost_method",
                        "average_cost_fx_rate",
                    ):
                        item[field] = previous.get(field)
            merged_holdings.append(item)
        normalized_holdings = self._with_holding_ids(merged_holdings)
        try:
            source_created_at = datetime.fromtimestamp(
                source_path.stat().st_ctime
            ).astimezone().isoformat()
        except OSError:
            source_created_at = ""
        state["portfolio"] = {
            "source_path": str(source_path),
            "file_name": source_path.name,
            "holding_count": len(normalized_holdings),
            "imported_at": utc_now(),
            "import_fingerprint": normalized_fingerprint,
            "source_created_at": source_created_at,
            "manually_modified_at": "",
            "manual_revision": 0,
        }
        state["holdings"] = normalized_holdings
        state["workbook_scan"] = self._compact_workbook_scan(workbook_scan)
        state["workbook_scan_quality"] = self._workbook_scan_quality(workbook_scan)
        state["excel_import_profile"] = (
            {
                **excel_import_profile,
                "source_path": str(source_path),
                "updated_at": utc_now(),
            }
            if isinstance(excel_import_profile, dict)
            else None
        )
        state["local_ai_product_status"] = None
        state["local_ai_summary"] = None
        state["local_ai_risk_warnings"] = []
        state["local_ai_command_result"] = None
        state["local_ai_action_plan"] = []
        state["local_ai_watch_triggers"] = []
        state["local_ai_confidence"] = None
        state["local_ai_decision_brief"] = ""
        state["local_ai_network_context"] = None
        state["local_ai_analysis_cache"] = None
        state["local_ai_external_discussion"] = None
        state["local_ai_explanation"] = None
        state["dividend_sync"] = None
        state["market_quote_sync"] = None
        state["portfolio_memory"] = self._portfolio_memory(normalized_holdings)
        return self.save_state(state)

    @staticmethod
    def _holding_import_key(holding: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(holding.get("market") or "").strip().upper(),
            str(holding.get("symbol") or "").strip().upper(),
            str(holding.get("source_row") or "").strip(),
        )

    def replace_holdings(
        self,
        holdings: list[dict[str, Any]],
        *,
        change: dict[str, Any],
    ) -> dict[str, Any]:
        with self._state_lock:
            return self._replace_holdings_locked(holdings, change=change)

    def _replace_holdings_locked(
        self,
        holdings: list[dict[str, Any]],
        *,
        change: dict[str, Any],
    ) -> dict[str, Any]:
        state = self.load_state()
        self.create_state_version(
            state,
            reason=str(change.get("action") or "manual_change"),
        )
        normalized_holdings = self._with_holding_ids(holdings)
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        revision = int(portfolio.get("manual_revision") or 0) + 1
        state["portfolio"] = {
            **portfolio,
            "source_path": str(portfolio.get("source_path") or ""),
            "file_name": str(portfolio.get("file_name") or "手動建立持股"),
            "holding_count": len(normalized_holdings),
            "imported_at": str(portfolio.get("imported_at") or utc_now()),
            "manually_modified_at": utc_now(),
            "manual_revision": revision,
            "last_manual_change": dict(change),
        }
        state["holdings"] = normalized_holdings
        state["local_ai_product_status"] = None
        state["local_ai_summary"] = None
        state["local_ai_risk_warnings"] = []
        state["local_ai_command_result"] = None
        state["local_ai_action_plan"] = []
        state["local_ai_watch_triggers"] = []
        state["local_ai_confidence"] = None
        state["local_ai_decision_brief"] = ""
        state["local_ai_network_context"] = None
        state["local_ai_analysis_cache"] = None
        state["local_ai_external_discussion"] = None
        state["local_ai_explanation"] = None
        state["portfolio_memory"] = self._portfolio_memory(normalized_holdings)
        return self.save_state(state)

    @staticmethod
    def _with_holding_ids(holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in holdings:
            item = dict(source)
            holding_id = str(item.get("holding_id") or "").strip()
            if not holding_id or holding_id in seen:
                holding_id = uuid.uuid4().hex
            item["holding_id"] = holding_id
            seen.add(holding_id)
            output.append(item)
        return output

    def save_local_ai_result(
        self,
        product_status: dict[str, Any] | None,
        summary: dict[str, Any] | None,
        risk_warnings: list[dict[str, Any]] | None,
        command_result: dict[str, Any] | None,
        *,
        full_analysis: dict[str, Any] | None = None,
        explanation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._state_lock:
            return self._save_local_ai_result_locked(
                product_status,
                summary,
                risk_warnings,
                command_result,
                full_analysis=full_analysis,
                explanation=explanation,
            )

    def _save_local_ai_result_locked(
        self,
        product_status: dict[str, Any] | None,
        summary: dict[str, Any] | None,
        risk_warnings: list[dict[str, Any]] | None,
        command_result: dict[str, Any] | None,
        *,
        full_analysis: dict[str, Any] | None = None,
        explanation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.load_state()
        state["local_ai_product_status"] = (
            dict(product_status) if isinstance(product_status, dict) else None
        )
        state["local_ai_summary"] = dict(summary) if isinstance(summary, dict) else None
        state["local_ai_risk_warnings"] = list(risk_warnings or [])[:20]
        compact_command = self._compact_command_result(command_result)
        state["local_ai_command_result"] = compact_command
        state["local_ai_network_context"] = self._compact_network_context(
            product_status,
            compact_command,
        )
        state["local_ai_action_plan"] = (
            list(compact_command.get("action_plan", []))[:8]
            if isinstance(compact_command, dict)
            else []
        )
        state["local_ai_watch_triggers"] = (
            list(compact_command.get("watch_triggers", []))[:12]
            if isinstance(compact_command, dict)
            else []
        )
        state["local_ai_confidence"] = (
            dict(compact_command.get("confidence_summary", {}))
            if isinstance(compact_command, dict)
            and isinstance(compact_command.get("confidence_summary"), dict)
            else None
        )
        state["local_ai_decision_brief"] = (
            str(compact_command.get("decision_brief") or "")
            if isinstance(compact_command, dict)
            else ""
        )
        state["local_ai_analysis_cache"] = self._compact_analysis_cache(full_analysis)
        discussion = (
            full_analysis.get("external_ai_discussion")
            if isinstance(full_analysis, dict)
            and isinstance(full_analysis.get("external_ai_discussion"), dict)
            else None
        )
        state["local_ai_external_discussion"] = (
            {
                "ok": discussion.get("ok") is True,
                "queued": discussion.get("queued") is True,
                "provider": str(discussion.get("provider") or ""),
                "status": str(discussion.get("status") or ""),
                "content": str(discussion.get("content") or "")[:12000],
                "message": str(discussion.get("message") or ""),
                "response_recipient": str(
                    discussion.get("response_recipient") or "local-ai"
                ),
                "transport": str(discussion.get("transport") or ""),
            }
            if discussion is not None
            else None
        )
        state["local_ai_explanation"] = (
            dict(explanation) if isinstance(explanation, dict) else None
        )
        return self.save_state(state)

    def create_state_version(
        self,
        state: dict[str, Any] | None = None,
        *,
        reason: str,
    ) -> dict[str, Any] | None:
        with self._state_lock:
            return self._create_state_version_locked(
                state,
                reason=reason,
            )

    def _create_state_version_locked(
        self,
        state: dict[str, Any] | None = None,
        *,
        reason: str,
        allow_empty: bool = False,
    ) -> dict[str, Any] | None:
        source = dict(state) if isinstance(state, dict) else self.load_state()
        portfolio = source.get("portfolio") if isinstance(source.get("portfolio"), dict) else {}
        holdings = source.get("holdings") if isinstance(source.get("holdings"), list) else []
        if not allow_empty and not holdings and not portfolio:
            return None
        created_at = utc_now()
        version_id = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8]
        document = {
            "version_id": version_id,
            "created_at": created_at,
            "reason": str(reason or "manual"),
            "holding_count": len(holdings),
            "file_name": str(portfolio.get("file_name") or ""),
            "manual_revision": int(portfolio.get("manual_revision") or 0),
            "state": source,
        }
        path = self.history_root / f"{version_id}.json"
        _atomic_write_bytes(path, encode_json_document(document).encode("utf-8"))
        return {key: value for key, value in document.items() if key != "state"}

    def list_state_versions(self, limit: int = 30) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        paths = sorted(
            self.history_root.glob("*.json"),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
        for path in paths[: max(1, min(100, int(limit)))]:
            try:
                _validated_storage_path(
                    path,
                    label="AI investment state version",
                    boundary=self.history_root,
                    require_exists=True,
                    expected_kind="file",
                )
                payload = decode_json_document(path.read_text(encoding="utf-8"))
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            output.append(
                {
                    "version_id": str(payload.get("version_id") or path.stem),
                    "created_at": str(payload.get("created_at") or ""),
                    "reason": str(payload.get("reason") or ""),
                    "holding_count": int(payload.get("holding_count") or 0),
                    "file_name": str(payload.get("file_name") or ""),
                    "manual_revision": int(payload.get("manual_revision") or 0),
                }
            )
        return output

    def restore_state_version(self, version_id: str) -> dict[str, Any]:
        with self._state_lock:
            return self._restore_state_version_locked(version_id)

    def _restore_state_version_locked(self, version_id: str) -> dict[str, Any]:
        normalized = str(version_id or "").strip()
        if not normalized or not re.fullmatch(r"[A-Za-z0-9_\-]+", normalized):
            raise ValueError("invalid state version id")
        path = self.history_root / f"{normalized}.json"
        if not path.exists():
            raise ValueError("state version not found")
        _validated_storage_path(
            path,
            label="AI investment state version",
            boundary=self.history_root,
            require_exists=True,
            expected_kind="file",
        )
        payload = decode_json_document(path.read_text(encoding="utf-8"))
        restored = payload.get("state") if isinstance(payload, dict) else None
        if not isinstance(restored, dict):
            raise ValueError("state version is invalid")
        current = self.load_state()
        self.create_state_version(current, reason="before_restore")
        restored["restored_from_version"] = normalized
        restored["restored_at"] = utc_now()
        return self.save_state(restored)

    def save_mobile_sync_remote_url(self, remote_base_url: str) -> dict[str, Any]:
        normalized = str(remote_base_url or "").strip()

        def update_remote_url(state: dict[str, Any]) -> None:
            settings = state.setdefault("mobile_sync_settings", {})
            if not isinstance(settings, dict):
                settings = {}
                state["mobile_sync_settings"] = settings
            settings["remote_base_url"] = normalized
            settings["updated_at"] = utc_now()

        return self.update_state(update_remote_url)

    def mobile_sync_remote_url(self) -> str:
        state = self.load_state()
        settings = state.get("mobile_sync_settings")
        if not isinstance(settings, dict):
            return ""
        return str(settings.get("remote_base_url") or "").strip()

    def clear_state(self, *, permanent: bool = False) -> dict[str, Any]:
        with self._state_lock:
            return self._clear_state_locked(permanent=permanent)

    def _clear_state_locked(self, *, permanent: bool = False) -> dict[str, Any]:
        remote_url = "" if permanent else self.mobile_sync_remote_url()
        current = self.load_state()
        if permanent:
            for current_root, directory_names, file_names in os.walk(
                self.state_root,
                topdown=False,
                followlinks=False,
            ):
                current_path = Path(current_root)
                for name in file_names:
                    (current_path / name).unlink()
                for name in directory_names:
                    child = current_path / name
                    if child.is_symlink():
                        child.unlink()
                    else:
                        child.rmdir()
        else:
            self.create_state_version(current, reason="before_clear")
        state = self._empty_state_with_memory()
        if remote_url:
            state["mobile_sync_settings"]["remote_base_url"] = remote_url
            state["mobile_sync_settings"]["updated_at"] = utc_now()
        saved = self.save_state(state)
        if permanent:
            for pattern in (
                "local-ai-errors.jsonl",
                "local-ai-errors.*.jsonl",
                "runtime-migration-manifest.json",
            ):
                for path in self.runtime_root.glob(pattern):
                    if path.is_file() or path.is_symlink():
                        path.unlink()
        return saved

    def add_ai_run(
        self,
        role: str,
        provider: str,
        prompt: str,
        status: str,
        content: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        with self._state_lock:
            return self._add_ai_run_locked(
                role,
                provider,
                prompt,
                status,
                content=content,
                error=error,
            )

    def _add_ai_run_locked(
        self,
        role: str,
        provider: str,
        prompt: str,
        status: str,
        content: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        state = self.load_state()
        runs = state.setdefault("ai_runs", [])
        run = {
            "run_id": uuid.uuid4().hex[:16],
            "role": role,
            "provider": provider,
            "prompt": prompt,
            "status": status,
            "content": content,
            "error": error,
            "created_at": utc_now(),
        }
        runs.insert(0, run)
        self.save_state(state)
        return run

    def update_ai_run(
        self,
        run_id: str,
        *,
        status: str,
        content: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        with self._state_lock:
            return self._update_ai_run_locked(
                run_id,
                status=status,
                content=content,
                error=error,
            )

    def _update_ai_run_locked(
        self,
        run_id: str,
        *,
        status: str,
        content: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        state = self.load_state()
        runs = state.setdefault("ai_runs", [])
        for run in runs:
            if not isinstance(run, dict) or str(run.get("run_id") or "") != run_id:
                continue
            run["status"] = status
            run["content"] = content
            run["error"] = error
            run["finished_at"] = utc_now() if status in {"completed", "failed"} else ""
            self.save_state(state)
            return dict(run)
        return self._add_ai_run_locked(
            role="investment_risk_monitor",
            provider="星澄",
            prompt="星澄：經 AI 通道執行背景報價與風險監測",
            status=status,
            content=content,
            error=error,
        )

    def recover_interrupted_ai_runs(self) -> int:
        with self._state_lock:
            return self._recover_interrupted_ai_runs_locked()

    def _recover_interrupted_ai_runs_locked(self) -> int:
        state = self.load_state()
        runs = state.setdefault("ai_runs", [])
        recovered = 0
        finished_at = utc_now()
        for run in runs:
            if not isinstance(run, dict) or run.get("status") != "running":
                continue
            run["status"] = "failed"
            run["content"] = ""
            run["error"] = "背景工作因服務重啟而中斷，系統已自動重新排程。"
            run["finished_at"] = finished_at
            recovered += 1
        if recovered:
            self.save_state(state)
        return recovered

    def latest_ai_content(self, role: str) -> str:
        for run in self.load_state().get("ai_runs", []):
            if run.get("role") == role and run.get("content"):
                return str(run["content"])
        return ""

    def _empty_state(self) -> dict[str, Any]:
        return {
            "version": INVESTMENT_APP_VERSION,
            "schema_version": INVESTMENT_STATE_SCHEMA_VERSION,
            "portfolio": None,
            "holdings": [],
            "workbook_scan": None,
            "workbook_scan_quality": None,
            "excel_import_profile": None,
            "local_ai_product_status": None,
            "local_ai_summary": None,
            "local_ai_risk_warnings": [],
            "local_ai_command_result": None,
            "local_ai_action_plan": [],
            "local_ai_watch_triggers": [],
            "local_ai_confidence": None,
            "local_ai_decision_brief": "",
            "local_ai_network_context": None,
            "local_ai_analysis_cache": None,
            "local_ai_external_discussion": None,
            "local_ai_explanation": None,
            "fund_identity_sync": None,
            "portfolio_memory": "",
            "shared_memory": "",
            "mobile_sync_settings": {
                "remote_base_url": "",
                "updated_at": "",
            },
            "ai_runs": [],
            "updated_at": utc_now(),
        }

    @staticmethod
    def _compact_analysis_cache(
        analysis: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(analysis, dict):
            return None
        holdings = analysis.get("holdings") if isinstance(analysis.get("holdings"), list) else []
        compact_holdings: list[dict[str, Any]] = []
        keep_fields = {
            "symbol",
            "name",
            "market",
            "asset_type",
            "quantity",
            "average_cost",
            "currency",
            "status",
            "quote",
            "market_status",
            "market_value",
            "cost_basis",
            "unrealized_pnl",
            "unrealized_pnl_percent",
            "quote_validation",
            "trusted_quote",
            "data_confidence",
            "analysis_fingerprint",
        }
        for source in holdings:
            if isinstance(source, dict):
                compact_holdings.append(
                    {key: value for key, value in source.items() if key in keep_fields}
                )
        return {
            "generated_at": str(analysis.get("generated_at") or ""),
            "holdings": compact_holdings,
        }

    def _empty_state_with_memory(self) -> dict[str, Any]:
        state = self._empty_state()
        state["shared_memory"] = self._shared_memory(state)
        return state

    @staticmethod
    def _portfolio_memory(holdings: list[dict[str, Any]]) -> str:
        lines = [
            "我的持股狀況：",
            "symbol | name | market | quantity | average_cost | currency",
        ]
        for holding in holdings:
            lines.append(
                " | ".join(
                    [
                        str(holding.get("symbol") or ""),
                        str(holding.get("name") or ""),
                        str(holding.get("market") or ""),
                        str(holding.get("quantity") or 0),
                        str(holding.get("average_cost") or ""),
                        str(holding.get("currency") or ""),
                    ]
                )
            )
        return "\n".join(lines)

    def _shared_memory(self, state: dict[str, Any]) -> str:
        lines = [
            "投資看盤共用記憶",
            f"updated_at: {state.get('updated_at') or utc_now()}",
            "",
            "Portfolio memory:",
            str(state.get("portfolio_memory") or "尚未匯入持股。"),
        ]
        product_status = state.get("local_ai_product_status")
        if isinstance(product_status, dict):
            network_context = (
                product_status.get("network_context")
                if isinstance(product_status.get("network_context"), dict)
                else {}
            )
            lines.extend(
                [
                    "",
                    "Local AI product status:",
                    (
                        f"{product_status.get('state_label', '-')} "
                        f"score={product_status.get('score', '-')} "
                        f"mode={product_status.get('watch_status', '-')} "
                        f"network={product_status.get('network_mode_label', '-')} "
                        f"coverage={product_status.get('coverage_label', '-')} "
                        f"providers={network_context.get('quote_provider_count', 0)}"
                    ),
                    str(product_status.get("recommendation") or ""),
                ]
            )
        network_context = state.get("local_ai_network_context")
        if isinstance(network_context, dict):
            lines.extend(
                [
                    "",
                    "Local AI network context:",
                    (
                        f"{network_context.get('mode_label', '-')} "
                        f"enabled={network_context.get('enabled', False)} "
                        f"coverage={network_context.get('coverage_label', '-')} "
                        f"providers={network_context.get('quote_providers', [])}"
                    ),
                ]
            )
        decision_brief = str(state.get("local_ai_decision_brief") or "").strip()
        if decision_brief:
            lines.extend(["", "Local AI decision brief:", decision_brief])
        action_plan = state.get("local_ai_action_plan")
        if isinstance(action_plan, list) and action_plan:
            lines.extend(["", "Local AI action plan:"])
            for item in action_plan[:6]:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    (
                        f"{item.get('priority', '-')} {item.get('symbol', '-')} "
                        f"{item.get('title', '-')} -> {item.get('action', '-')}"
                    )
                )
        confidence = state.get("local_ai_confidence")
        if isinstance(confidence, dict):
            lines.extend(
                [
                    "",
                    "Local AI confidence:",
                    (
                        f"{confidence.get('label', '-')} "
                        f"score={confidence.get('score', '-')} "
                        f"low={confidence.get('low_confidence_symbols', [])}"
                    ),
                ]
            )
        workbook_quality = state.get("workbook_scan_quality")
        if isinstance(workbook_quality, dict):
            lines.extend(
                [
                    "",
                    "Workbook scan quality:",
                    (
                        f"{workbook_quality.get('state_label', '-')} "
                        f"score={workbook_quality.get('score', '-')} "
                        f"sheet={workbook_quality.get('selected_sheet_name', '-')}"
                    ),
                    str(workbook_quality.get("recommendation") or ""),
                ]
            )
        runs = state.get("ai_runs", [])
        if isinstance(runs, list) and runs:
            lines.extend(["", "Primary AI results, latest first:"])
            for run in runs[:12]:
                if not isinstance(run, dict):
                    continue
                provider = str(run.get("provider") or "unknown")
                role = str(run.get("role") or "unknown")
                status = str(run.get("status") or "unknown")
                created_at = str(run.get("created_at") or "")
                body = str(run.get("content") or run.get("error") or "").strip()
                lines.extend(
                    [
                        "",
                        f"[{provider}] role={role} status={status} created_at={created_at}",
                        self._shorten(body, 2200),
                    ]
                )
        return self._shorten("\n".join(lines), 40000)

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "\n...truncated..."

    @staticmethod
    def _compact_workbook_scan(workbook_scan: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(workbook_scan, dict):
            return None
        selected_sheet = workbook_scan.get("selected_sheet")
        sheets = workbook_scan.get("sheets")
        return {
            "sheet_count": workbook_scan.get("sheet_count", 0),
            "selected_sheet": selected_sheet if isinstance(selected_sheet, dict) else None,
            "sheets": list(sheets)[:20] if isinstance(sheets, list) else [],
        }

    @staticmethod
    def _workbook_scan_quality(workbook_scan: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(workbook_scan, dict):
            return None
        selected_sheet = workbook_scan.get("selected_sheet")
        if not isinstance(selected_sheet, dict):
            return {
                "state": "critical",
                "state_label": "未辨識",
                "score": 0,
                "selected_sheet_name": "",
                "recommendation": "活頁簿未找到可辨識的持股表，請確認有代號欄位或可推斷的持股資料列。",
            }

        score = InvestmentWatchRepository._int_value(selected_sheet.get("score"))
        valid_rows = InvestmentWatchRepository._int_value(
            selected_sheet.get("valid_data_row_count")
        )
        header_mode = str(selected_sheet.get("header_mode") or "")
        header_depth = InvestmentWatchRepository._int_value(
            selected_sheet.get("header_depth"),
            default=1,
        )
        if score >= 180 and valid_rows > 0:
            state = "ready"
            state_label = "辨識穩定"
        elif score >= 120 and valid_rows > 0:
            state = "attention"
            state_label = "需確認"
        else:
            state = "critical"
            state_label = "低信心"

        if header_mode == "horizontal_matrix":
            recommendation = "已合併橫向持股工作表；ETF、台股、美股與共同基金欄位可個別調整。"
        elif header_mode == "headerless_inferred":
            recommendation = "偵測為無表頭自製表格，已從資料列推斷代號、數量、平均成本等欄位。"
        elif header_mode == "inferred":
            recommendation = "已依資料形態推斷自製表格欄位，建議確認代號、數量、平均成本是否對齊。"
        elif header_depth > 1:
            recommendation = "已合併多列自製表頭，建議確認欄位合併後名稱是否符合原表。"
        else:
            recommendation = "已辨識持股欄位，匯入後會自動交給本地AI建立監測基準。"

        return {
            "state": state,
            "state_label": state_label,
            "score": score,
            "selected_sheet_name": str(selected_sheet.get("sheet_name") or ""),
            "header_row_number": selected_sheet.get("header_row_number"),
            "header_mode": header_mode,
            "header_depth": header_depth,
            "valid_data_row_count": valid_rows,
            "recommendation": recommendation,
        }

    @staticmethod
    def _compact_command_result(
        command_result: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(command_result, dict):
            return None
        return {
            "intent": command_result.get("intent"),
            "sections": command_result.get("sections", []),
            "symbols": command_result.get("symbols", []),
            "matched_symbols": command_result.get("matched_symbols", []),
            "missing_symbols": command_result.get("missing_symbols", []),
            "portfolio_score": command_result.get("portfolio_score"),
            "portfolio_rating": command_result.get("portfolio_rating"),
            "risk_level": command_result.get("risk_level"),
            "network_context": InvestmentWatchRepository._compact_network_context(
                None,
                command_result,
            ),
            "next_actions": command_result.get("next_actions", []),
            "confidence_summary": command_result.get("confidence_summary"),
            "action_plan": list(command_result.get("action_plan", []))[:8]
            if isinstance(command_result.get("action_plan"), list)
            else [],
            "watch_triggers": list(command_result.get("watch_triggers", []))[:12]
            if isinstance(command_result.get("watch_triggers"), list)
            else [],
            "decision_brief": InvestmentWatchRepository._shorten(
                str(command_result.get("decision_brief") or ""),
                1000,
            ),
            "text": InvestmentWatchRepository._shorten(
                str(command_result.get("text") or ""),
                4000,
            ),
        }

    @staticmethod
    def _compact_network_context(
        product_status: dict[str, Any] | None,
        command_result: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        source: dict[str, Any] | None = None
        if isinstance(product_status, dict) and isinstance(
            product_status.get("network_context"),
            dict,
        ):
            source = product_status["network_context"]
        elif isinstance(command_result, dict) and isinstance(
            command_result.get("network_context"),
            dict,
        ):
            source = command_result["network_context"]
        if not isinstance(source, dict):
            return None
        return {
            "enabled": bool(source.get("enabled")),
            "mode": str(source.get("mode") or ""),
            "mode_label": str(source.get("mode_label") or ""),
            "health": str(source.get("health") or ""),
            "health_label": str(source.get("health_label") or ""),
            "policy": InvestmentWatchRepository._shorten(str(source.get("policy") or ""), 500),
            "quote_provider_count": InvestmentWatchRepository._int_value(
                source.get("quote_provider_count")
            ),
            "quote_providers": InvestmentWatchRepository._compact_string_list(
                source.get("quote_providers"),
                8,
            ),
            "successful_providers": InvestmentWatchRepository._compact_string_list(
                source.get("successful_providers"),
                8,
            ),
            "failed_providers": InvestmentWatchRepository._compact_string_list(
                source.get("failed_providers"),
                8,
            ),
            "attempt_count": InvestmentWatchRepository._int_value(source.get("attempt_count")),
            "holding_count": InvestmentWatchRepository._int_value(source.get("holding_count")),
            "quoted_count": InvestmentWatchRepository._int_value(source.get("quoted_count")),
            "verified_quote_count": InvestmentWatchRepository._int_value(
                source.get("verified_quote_count")
            ),
            "cross_checked_count": InvestmentWatchRepository._int_value(
                source.get("cross_checked_count")
            ),
            "single_source_count": InvestmentWatchRepository._int_value(
                source.get("single_source_count")
            ),
            "untrusted_quote_count": InvestmentWatchRepository._int_value(
                source.get("untrusted_quote_count")
            ),
            "failed_quote_count": InvestmentWatchRepository._int_value(
                source.get("failed_quote_count")
            ),
            "validation_issue_count": InvestmentWatchRepository._int_value(
                source.get("validation_issue_count")
            ),
            "divergence_count": InvestmentWatchRepository._int_value(
                source.get("divergence_count")
            ),
            "stale_quote_count": InvestmentWatchRepository._int_value(
                source.get("stale_quote_count")
            ),
            "symbol_mismatch_count": InvestmentWatchRepository._int_value(
                source.get("symbol_mismatch_count")
            ),
            "currency_mismatch_count": InvestmentWatchRepository._int_value(
                source.get("currency_mismatch_count")
            ),
            "validation_issues": InvestmentWatchRepository._compact_validation_issues(
                source.get("validation_issues"),
                12,
            ),
            "quote_gap_count": InvestmentWatchRepository._int_value(
                source.get("quote_gap_count")
            ),
            "quote_gaps": InvestmentWatchRepository._compact_quote_gaps(
                source.get("quote_gaps"),
                20,
            ),
            "coverage_percent": source.get("coverage_percent"),
            "coverage_label": str(source.get("coverage_label") or ""),
        }

    @staticmethod
    def _compact_quote_gaps(value: Any, limit: int) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        output: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            output.append(
                {
                    "symbol": str(item.get("symbol") or ""),
                    "name": InvestmentWatchRepository._shorten(
                        str(item.get("name") or ""),
                        120,
                    ),
                    "market": str(item.get("market") or ""),
                    "status": str(item.get("status") or ""),
                    "reason": InvestmentWatchRepository._shorten(
                        str(item.get("reason") or ""),
                        160,
                    ),
                    "detail": InvestmentWatchRepository._shorten(
                        str(item.get("detail") or ""),
                        260,
                    ),
                    "failed_providers": InvestmentWatchRepository._compact_string_list(
                        item.get("failed_providers"),
                        8,
                    ),
                    "attempt_count": InvestmentWatchRepository._int_value(
                        item.get("attempt_count")
                    ),
                    "action": InvestmentWatchRepository._shorten(
                        str(item.get("action") or ""),
                        240,
                    ),
                }
            )
            if len(output) >= limit:
                break
        return output

    @staticmethod
    def _compact_validation_issues(value: Any, limit: int) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        output: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            output.append(
                {
                    "symbol": str(item.get("symbol") or ""),
                    "severity": str(item.get("severity") or ""),
                    "code": str(item.get("code") or ""),
                    "title": InvestmentWatchRepository._shorten(
                        str(item.get("title") or ""),
                        120,
                    ),
                    "detail": InvestmentWatchRepository._shorten(
                        str(item.get("detail") or ""),
                        240,
                    ),
                }
            )
            if len(output) >= limit:
                break
        return output

    @staticmethod
    def _compact_string_list(value: Any, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item or "").strip()][:limit]

    @staticmethod
    def _int_value(value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default
