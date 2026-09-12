from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from .watch_repo_helpers import (
    INVESTMENT_APP_VERSION,
    INVESTMENT_STATE_MIN_SUPPORTED_SCHEMA_VERSION,
    INVESTMENT_STATE_SCHEMA_VERSION,
    InvestmentStateRecoveryRequired,
    InvestmentStateUpgradeRequired,
    _atomic_write_bytes,
    _copy_verified,
    _iter_migration_files,
    _runtime_root,
    _validated_storage_path,
    decode_json_document,
    encode_json_document,
    utc_now,
)
from .watch_repo_lock import _RuntimeOwnerLock


class WatchRepoCoreMixin:
    """Core repository lifecycle: construction, state load/save, schema migration."""

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

    def _empty_state(self) -> dict[str, Any]:
        return {
            "version": INVESTMENT_APP_VERSION,
            "schema_version": INVESTMENT_STATE_SCHEMA_VERSION,
            "portfolio": None,
            "holdings": [],
            "workbook_scan": None,
            "workbook_scan_quality": None,
            "excel_import_profile": None,
            "xingcheng_product_status": None,
            "xingcheng_summary": None,
            "xingcheng_risk_warnings": [],
            "xingcheng_command_result": None,
            "xingcheng_action_plan": [],
            "xingcheng_watch_triggers": [],
            "xingcheng_confidence": None,
            "xingcheng_decision_brief": "",
            "xingcheng_network_context": None,
            "xingcheng_analysis_cache": None,
            "xingcheng_external_discussion": None,
            "xingcheng_explanation": None,
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

    def _empty_state_with_memory(self) -> dict[str, Any]:
        state = self._empty_state()
        state["shared_memory"] = self._shared_memory(state)
        return state
