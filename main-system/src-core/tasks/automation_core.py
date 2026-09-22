"""Automation Core — 自動化核心：單一流程／排程註冊點（§1.1 自動化集中；P0-4）。

法典依據:
- A10/A11: explicit allowlist authorization; deny by default; fail closed.
- A63/A64: decision-only sovereigns; execution delegated to governed executors.
- A334: registry-driven dispatch.

職責（總督核定 2026-09-22）:
1. 自動化流程清單與 owner — ``config/automation-flows.json``
   （``star-automation-flows/v1``）是唯一清單；清單外流程不得註冊排程。
2. 集中排程／觸發註冊 — kind=periodic 的流程一律經
   :meth:`register_flow` 註冊到共享 ``PeriodicScheduler``；event／
   on-demand 流程只登錄清單（觸發註冊），無自有排程。
3. 統一審計與 kill switch — 每次流程執行、每次註冊／拒絕／停權皆寫
   ``runtime/logs/automation-flows.jsonl``；manifest ``enabled=false``
   或 runtime override 即拒絕註冊並解除既有排程（fail-closed）。

本核心只做流程與排程管理，**不執行業務邏輯**——tick 本體仍屬各流程
owner 服務；執行下發模組的語義不變。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from ipc.command_router._audit_writer import append_audit_record

_logger = logging.getLogger("gptbridge.automation_core")

Tick = Callable[[], Awaitable[None]]

_MAIN_SYSTEM_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_CONFIG = _MAIN_SYSTEM_ROOT / "config" / "automation-flows.json"
_DEFAULT_STATE = (
    _MAIN_SYSTEM_ROOT / "runtime" / "state" / "automation-flows-state.json"
)
_DEFAULT_AUDIT = (
    _MAIN_SYSTEM_ROOT / "runtime" / "logs" / "automation-flows.jsonl"
)


class AutomationCore:
    """單一自動化註冊點：allowlist 清單＋集中排程＋統一審計＋kill switch。"""

    VERSION = "star-automation-core/v1"

    def __init__(
        self,
        scheduler: Any | None,
        *,
        config_path: Path | None = None,
        state_path: Path | None = None,
        audit_ledger: Path | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._config_path = config_path or _DEFAULT_CONFIG
        self._state_path = state_path or _DEFAULT_STATE
        self._audit_ledger = audit_ledger or _DEFAULT_AUDIT
        self._manifest = self._load_manifest()
        self._overrides: dict[str, dict[str, Any]] = self._load_overrides()
        # flow_id -> (tick, register kwargs) captured at registration so a
        # runtime ``enable`` can restore the schedule without a restart.
        self._registered: dict[str, tuple[Tick, dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Manifest / override loading (fail-closed)
    # ------------------------------------------------------------------

    def _load_manifest(self) -> dict[str, dict[str, Any]]:
        """Load the governed flow inventory.

        Fail-closed (A10/A11): a missing or corrupt manifest means *no*
        flow is allowed to register — the core denies every schedule
        rather than silently permitting unlisted automation.
        """
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            _logger.error(
                "automation-flows manifest unreadable at %s — denying all "
                "flow registrations (fail-closed)",
                self._config_path,
            )
            return {}
        flows = raw.get("flows")
        if not isinstance(flows, dict):
            return {}
        return {k: v for k, v in flows.items() if isinstance(v, dict)}

    def _load_overrides(self) -> dict[str, dict[str, Any]]:
        """Runtime kill/enable overrides.

        Corrupt state is treated as *no overrides* — the governed manifest
        stays authoritative (a broken state file can never silently keep a
        flow killed nor silently enable an unlisted one).
        """
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        overrides = raw.get("overrides")
        if not isinstance(overrides, dict):
            return {}
        return {
            k: v
            for k, v in overrides.items()
            if isinstance(v, dict) and isinstance(v.get("enabled"), bool)
        }

    def _save_overrides(self) -> None:
        payload = {
            "schema": "star-automation-flows-state/v1",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "overrides": self._overrides,
        }
        temporary = self._state_path.with_name(
            self._state_path.name + f".{os.getpid()}.tmp"
        )
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self._state_path)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass

    def _audit(self, record: dict[str, Any]) -> None:
        record.setdefault("at", time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        record.setdefault("core", self.VERSION)
        append_audit_record(self._audit_ledger, record)

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def flow_entry(self, flow_id: str) -> dict[str, Any] | None:
        return self._manifest.get(flow_id)

    def is_enabled(self, flow_id: str) -> bool:
        """Manifest-enabled AND not runtime-killed. Unknown → denied."""
        entry = self._manifest.get(flow_id)
        if entry is None:
            return False
        override = self._overrides.get(flow_id)
        if override is not None:
            return bool(override["enabled"])
        return bool(entry.get("enabled", True))

    def disable(self, flow_id: str, *, reason: str = "") -> bool:
        """Runtime kill switch: refuse further runs and unregister."""
        if flow_id not in self._manifest:
            self._audit({"action": "disable-denied", "flow": flow_id,
                         "reason": "unlisted-flow"})
            return False
        self._overrides[flow_id] = {
            "enabled": False,
            "reason": reason,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._save_overrides()
        self._unregister(flow_id)
        self._audit({"action": "disabled", "flow": flow_id,
                     "reason": reason})
        return True

    def enable(self, flow_id: str) -> bool:
        """Clear a runtime kill; manifest ``enabled`` still governs."""
        if flow_id not in self._manifest:
            self._audit({"action": "enable-denied", "flow": flow_id,
                         "reason": "unlisted-flow"})
            return False
        self._overrides.pop(flow_id, None)
        self._save_overrides()
        self._audit({"action": "enabled", "flow": flow_id})
        # Re-register if we still hold the tick and the flow is now
        # enabled by the manifest.
        if self.is_enabled(flow_id) and flow_id in self._registered:
            tick, kwargs = self._registered[flow_id]
            self._register(flow_id, tick, **kwargs)
        return True

    def reload_manifest(self) -> None:
        """Re-read the governed inventory; newly disabled flows are
        unregistered (kill switch), newly listed flows wait for their
        owner service to register."""
        self._manifest = self._load_manifest()
        for flow_id in list(self._registered):
            if not self.is_enabled(flow_id):
                self._unregister(flow_id)
                self._audit({"action": "killed-by-manifest",
                             "flow": flow_id})

    # ------------------------------------------------------------------
    # Centralized scheduling (PeriodicScheduler-compatible surface)
    # ------------------------------------------------------------------

    def register_flow(
        self,
        flow_id: str,
        tick: Tick,
        *,
        interval_s: float | None = None,
        run_immediately: bool = False,
        timeout_s: float | None = None,
        pausable: bool | None = None,
    ) -> bool:
        """Register one flow onto the shared scheduler.

        Returns ``False`` (and audits) when the flow is unlisted or
        disabled — callers must NOT fall back to a private loop on denial
        (that would bypass the kill switch); fallback is only legitimate
        when no automation core exists at all.
        """
        entry = self._manifest.get(flow_id)
        if entry is None:
            self._audit({"action": "register-denied", "flow": flow_id,
                         "reason": "unlisted-flow"})
            _logger.warning(
                "automation flow %s denied: not in automation-flows.json",
                flow_id,
            )
            return False
        if not self.is_enabled(flow_id):
            self._audit({"action": "register-denied", "flow": flow_id,
                         "reason": "kill-switch"})
            _logger.info("automation flow %s disabled (kill switch)",
                         flow_id)
            return False

        effective_interval = (
            float(interval_s)
            if interval_s is not None
            else float(entry.get("interval_s") or 0)
        )
        if effective_interval <= 0:
            self._audit({"action": "register-denied", "flow": flow_id,
                         "reason": "no-interval"})
            return False
        manifest_interval = entry.get("interval_s")
        if (manifest_interval and interval_s is not None
                and float(manifest_interval) != float(interval_s)):
            self._audit({"action": "interval-override", "flow": flow_id,
                         "manifest_interval_s": manifest_interval,
                         "effective_interval_s": effective_interval})

        kwargs: dict[str, Any] = {
            "interval_s": effective_interval,
            "run_immediately": run_immediately,
            "timeout_s": timeout_s,
            "pausable": (
                bool(entry.get("pausable", False))
                if pausable is None
                else bool(pausable)
            ),
        }
        self._registered[flow_id] = (tick, kwargs)
        return self._register(flow_id, tick, **kwargs)

    def _register(
        self, flow_id: str, tick: Tick, *, interval_s: float, **kwargs: Any
    ) -> bool:
        if self._scheduler is None:
            self._audit({"action": "register-denied", "flow": flow_id,
                         "reason": "scheduler-unavailable"})
            return False

        async def audited_tick() -> None:
            started = time.monotonic()
            outcome = "ok"
            detail = ""
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                outcome = "error"
                detail = f"{type(error).__name__}: {error}"
                raise
            finally:
                self._audit({
                    "action": "flow-run",
                    "flow": flow_id,
                    "owner": self._manifest.get(flow_id, {}).get("owner"),
                    "outcome": outcome,
                    "duration_ms": round(
                        (time.monotonic() - started) * 1000, 1),
                    "error": detail or None,
                })

        self._scheduler.register(
            flow_id, interval_s, audited_tick, **kwargs
        )
        self._audit({"action": "registered", "flow": flow_id,
                     "interval_s": interval_s})
        return True

    def register(
        self,
        name: str,
        interval_s: float,
        tick: Tick,
        *,
        run_immediately: bool = False,
        timeout_s: float | None = None,
        pausable: bool = False,
    ) -> bool:
        """``PeriodicScheduler.register``-compatible alias so existing
        consumers can be handed the core in place of the scheduler."""
        return self.register_flow(
            name,
            tick,
            interval_s=interval_s,
            run_immediately=run_immediately,
            timeout_s=timeout_s,
            pausable=pausable,
        )

    def unregister(self, name: str) -> None:
        self._unregister(name)
        self._registered.pop(name, None)

    def _unregister(self, flow_id: str) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.unregister(flow_id)
            except Exception:
                pass

    async def stop(self) -> None:
        for flow_id in list(self._registered):
            self._unregister(flow_id)
        self._audit({"action": "core-stop",
                     "flows": sorted(self._registered)})
        self._registered.clear()

    # ------------------------------------------------------------------
    # Inventory surface (acceptance evidence)
    # ------------------------------------------------------------------

    def flows(self) -> list[dict[str, Any]]:
        """Merged manifest + runtime + scheduler state per flow."""
        scheduler_jobs = {}
        if self._scheduler is not None:
            try:
                scheduler_jobs = {
                    j["name"]: j for j in self._scheduler.jobs()
                }
            except Exception:
                scheduler_jobs = {}
        out = []
        for flow_id, entry in self._manifest.items():
            job = scheduler_jobs.get(flow_id)
            out.append({
                "flow": flow_id,
                "owner": entry.get("owner"),
                "kind": entry.get("kind"),
                "interval_s": entry.get("interval_s"),
                "enabled": self.is_enabled(flow_id),
                "manifest_enabled": bool(entry.get("enabled", True)),
                "runtime_override": flow_id in self._overrides,
                "registered": job is not None,
                "last_error": job.get("last_error") if job else None,
                "run_count": job.get("run_count") if job else 0,
            })
        return out


__all__ = ["AutomationCore"]
