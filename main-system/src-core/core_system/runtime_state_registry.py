"""§10.3 全專案統一 Runtime State——單一事實來源（`star-runtime-state/v1`）。

每個模組一筆記錄，雙軸狀態分離：

- 運行軸（runtime_state）：STARTING／READY／DEGRADED／RECOVERING／FAILED／
  STOPPING／STOPPED
- 能力軸（capability_state）：AVAILABLE／UNAVAILABLE／DISABLED／NOT_INSTALLED

原則：**局部故障不擴散**——單一模組 FAILED 不影響其他模組記錄；
聚合檢視（`aggregate`）只彙整計數，不把整體標為 FAILED。
法典／正式權限／資料完整性等必要檢查的 fail-closed 由呼叫方維持，
本登錄簿只記錄事實，不遮蔽必要失敗。

供 UI／Updater／Maintenance／Resource Management 共用查詢；
持久化 JSON atomic write（與 process_registry 同一慣例）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger("gptbridge.runtime_state_registry")

REGISTRY_VERSION = "star-runtime-state/v1"

RUNTIME_STATES = frozenset(
    {
        "STARTING",
        "READY",
        "DEGRADED",
        "RECOVERING",
        "FAILED",
        "STOPPING",
        "STOPPED",
    }
)
CAPABILITY_STATES = frozenset(
    {"AVAILABLE", "UNAVAILABLE", "DISABLED", "NOT_INSTALLED"}
)


@dataclass
class ModuleRuntimeRecord:
    module_id: str
    runtime_state: str = "STOPPED"
    capability_state: str = "AVAILABLE"
    health: str = "unknown"
    release_id: str = ""
    last_heartbeat: str = ""
    last_error: str = ""
    recovery_attempts: int = 0
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class RuntimeStateRegistry:
    """模組級雙軸狀態登錄簿；變更即寫盤（atomic）。"""

    def __init__(
        self, state_path: str | Path, *, project_root: Path | None = None
    ) -> None:
        self._path = Path(state_path)
        self._records: dict[str, ModuleRuntimeRecord] = {}
        # §10.65 act-2: policy mode "primary" -> the C registry owns the
        # state machine (transitions, recovery counting, error clearing,
        # staleness); Python keeps persistence, ``metadata`` and reads.
        # mode "shadow" -> parallel comparison (Python authoritative);
        # anything else -> Python-only.  Primary and shadow are mutually
        # exclusive — never both.
        self._native_primary = None
        self._native_shadow = None
        if project_root is not None:
            try:
                from .runtime_state_native_shadow import (
                    RuntimeStateNativeShadow,
                    load_primary,
                )

                self._native_primary = load_primary(Path(project_root))
                if self._native_primary is None:
                    self._native_shadow = (
                        RuntimeStateNativeShadow.from_policy(
                            Path(project_root)
                        )
                    )
            except Exception:
                self._native_primary = None
                self._native_shadow = None
        self._load()
        if self._native_primary is not None:
            for record in self._records.values():
                try:
                    restored = self._native_primary.restore(asdict(record))
                except Exception:
                    restored = False
                if not restored:
                    _logger.warning(
                        "runtime-state native restore refused %s",
                        record.module_id,
                    )

    # ---- persistence ----------------------------------------------------

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for entry in data.get("modules", []):
                record = ModuleRuntimeRecord(**{
                    k: v for k, v in entry.items()
                    if k in ModuleRuntimeRecord.__dataclass_fields__
                })
                self._records[record.module_id] = record
        except Exception as error:  # corrupt state must not crash startup
            _logger.warning("runtime-state registry load failed: %s", error)

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": REGISTRY_VERSION,
            "updated_at": _now(),
            "modules": [asdict(r) for r in self._records.values()],
        }
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), suffix=".tmp", prefix="runtime-state-"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- mutation -------------------------------------------------------

    def _record(self, module_id: str) -> ModuleRuntimeRecord:
        record = self._records.get(module_id)
        if record is None:
            record = ModuleRuntimeRecord(module_id=module_id)
            self._records[module_id] = record
        return record

    def _sync_from_native(self, module_id: str) -> ModuleRuntimeRecord:
        """Mirror the authoritative C record back into the Python mirror
        (metadata stays Python-owned)."""
        record = self._record(module_id)
        native = self._native_primary
        if native is None:
            return record
        state = native.get(module_id)
        if state is None:
            return record
        for field_name in (
            "runtime_state",
            "capability_state",
            "health",
            "release_id",
            "last_heartbeat",
            "last_error",
            "recovery_attempts",
            "updated_at",
        ):
            if field_name in state:
                setattr(record, field_name, state[field_name])
        return record

    def set_runtime_state(
        self,
        module_id: str,
        state: str,
        *,
        health: Optional[str] = None,
        release_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> ModuleRuntimeRecord:
        if state not in RUNTIME_STATES:
            raise ValueError(f"unknown runtime state: {state}")
        native = self._native_primary
        if native is not None:
            now_str = _now()
            if native.set_runtime_state(
                module_id, state, health, release_id, error, now_str
            ):
                record = self._sync_from_native(module_id)
                self._persist()
                return record
            _logger.warning(
                "native runtime-state refused %s -> %s; Python fallback",
                module_id,
                state,
            )
        record = self._record(module_id)
        record.runtime_state = state
        if state == "RECOVERING":
            record.recovery_attempts += 1
        if health is not None:
            record.health = health
        if release_id is not None:
            record.release_id = release_id
        if error is not None:
            record.last_error = error
        elif state in ("READY", "STARTING"):
            record.last_error = ""
        record.updated_at = _now()
        self._persist()
        if self._native_shadow is not None:
            try:
                self._native_shadow.observe_set_runtime(
                    module_id,
                    state,
                    health=health,
                    release_id=release_id,
                    error=error,
                    now_str=record.updated_at,
                    py_record=record,
                )
            except Exception:
                pass
        return record

    def set_capability_state(
        self, module_id: str, state: str
    ) -> ModuleRuntimeRecord:
        if state not in CAPABILITY_STATES:
            raise ValueError(f"unknown capability state: {state}")
        native = self._native_primary
        if native is not None:
            if native.set_capability_state(module_id, state, _now()):
                record = self._sync_from_native(module_id)
                self._persist()
                return record
            _logger.warning(
                "native capability-state refused %s -> %s; Python fallback",
                module_id,
                state,
            )
        record = self._record(module_id)
        record.capability_state = state
        record.updated_at = _now()
        self._persist()
        if self._native_shadow is not None:
            try:
                self._native_shadow.observe_set_capability(
                    module_id, state, now_str=record.updated_at,
                    py_record=record,
                )
            except Exception:
                pass
        return record

    def heartbeat(self, module_id: str) -> ModuleRuntimeRecord:
        native = self._native_primary
        if native is not None:
            if native.heartbeat(module_id, _now(), _now_ms()):
                record = self._sync_from_native(module_id)
                self._persist()
                return record
            _logger.warning(
                "native heartbeat refused %s; Python fallback", module_id
            )
        record = self._record(module_id)
        record.last_heartbeat = _now()
        record.updated_at = record.last_heartbeat
        self._persist()
        if self._native_shadow is not None:
            try:
                self._native_shadow.observe_heartbeat(
                    module_id,
                    now_str=record.last_heartbeat,
                    now_ms=_now_ms(),
                )
            except Exception:
                pass
        return record

    def is_stale(
        self, module_id: str, stale_after_s: float
    ) -> bool:
        """Heartbeat staleness predicate (P4 parity dimension).

        A module with no heartbeat or a heartbeat older than
        ``stale_after_s`` is stale — fail-closed so an absent record can
        never read as healthy.  The verdict is mirrored into the native
        shadow for divergence detection.
        """
        native = self._native_primary
        if native is not None:
            # C verdict: 1 stale / 0 fresh / -1 unknown -> fail-closed stale
            return native.is_stale(
                module_id, _now_ms(), int(stale_after_s * 1000.0)
            ) != 0
        record = self._records.get(module_id)
        if record is None or not record.last_heartbeat:
            stale = True
        else:
            try:
                beat = datetime.strptime(
                    record.last_heartbeat, "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc).timestamp()
                stale = (time.time() - beat) > stale_after_s
            except (ValueError, OverflowError):
                stale = True
        if self._native_shadow is not None:
            try:
                self._native_shadow.observe_staleness(
                    module_id,
                    py_stale=stale,
                    now_ms=_now_ms(),
                    stale_after_ms=int(stale_after_s * 1000.0),
                )
            except Exception:
                pass
        return stale

    def record_error(self, module_id: str, error: str) -> ModuleRuntimeRecord:
        native = self._native_primary
        if native is not None:
            if native.record_error(module_id, error, _now()):
                record = self._sync_from_native(module_id)
                self._persist()
                return record
            _logger.warning(
                "native record-error refused %s; Python fallback", module_id
            )
        record = self._record(module_id)
        record.last_error = error[:500]
        record.updated_at = _now()
        self._persist()
        if self._native_shadow is not None:
            try:
                self._native_shadow.observe_record_error(
                    module_id, error, now_str=record.updated_at,
                    py_record=record,
                )
            except Exception:
                pass
        return record

    # ---- queries --------------------------------------------------------

    def get(self, module_id: str) -> Optional[ModuleRuntimeRecord]:
        return self._records.get(module_id)

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": REGISTRY_VERSION,
            "modules": {
                mid: asdict(r) for mid, r in sorted(self._records.items())
            },
        }

    def aggregate(self) -> dict[str, Any]:
        """Local-failure-isolation view: counts per axis, never a global FAILED."""
        native = self._native_primary
        if native is not None:
            aggregate = dict(native.aggregate())
            aggregate["generated_at"] = _now()
            return aggregate
        by_runtime: dict[str, int] = {}
        by_capability: dict[str, int] = {}
        failed: list[str] = []
        for record in self._records.values():
            by_runtime[record.runtime_state] = (
                by_runtime.get(record.runtime_state, 0) + 1
            )
            by_capability[record.capability_state] = (
                by_capability.get(record.capability_state, 0) + 1
            )
            if record.runtime_state == "FAILED":
                failed.append(record.module_id)
        aggregate = {
            "schema": REGISTRY_VERSION,
            "module_count": len(self._records),
            "by_runtime_state": by_runtime,
            "by_capability_state": by_capability,
            "failed_modules": sorted(failed),
            "generated_at": _now(),
        }
        if self._native_shadow is not None:
            try:
                self._native_shadow.observe_aggregate(aggregate)
            except Exception:
                pass
        return aggregate


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _now_ms() -> int:
    return int(time.time() * 1000.0)
