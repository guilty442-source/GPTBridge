"""§10.66 Sleep policy manager — hot/warm/cold tiering for sleepable units.

Generalises the model-owner on-demand pattern
(:class:`tasks.model_service_activation.ModelServiceActivationBroker`) to
every sleepable unit: tools idle past their thresholds are tiered
``hot -> warm -> cold`` and cold-slept through the governed
``ToolboxService.stop_tool`` path (expected-stop marking, tracked-process
termination, status update, audit).  Waking is demand-driven — queued
transport requests activate the owner through the existing broker /
toolbox on-demand start paths; this manager never pre-warms (A540).

Boundaries (blueprint §10.66 B/C):

- drain first: a unit only sleeps when it has no in-flight transport
  requests (``queued``/``claimed``/``pushed``) — unfinished work is never
  dropped;
- governance cores and units listed ``never_sleep`` are exempt;
- every transition appends to ``runtime/logs/sleep-transitions.jsonl``;
- kill switch: ``sleep-policy.json`` ``enabled=false`` disables all
  transitions (default off — activation is opt-in per governor);
- warm tier (weights/caches evicted, process alive) is delegated to the
  component's own release machinery (``AutoReleaseManager`` for the model
  engine); the manager records the tier but only executes cold sleeps.

State: ``runtime/state/sleep-policy.json``; audit:
``runtime/logs/sleep-transitions.jsonl``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger("gptbridge.sleep_policy")

_ROOT = Path(__file__).resolve().parents[2]
_POLICY_FILE = _ROOT / "config" / "sleep-policy.json"
_STATE_FILE = _ROOT / "runtime" / "state" / "sleep-policy.json"
_AUDIT_FILE = _ROOT / "runtime" / "logs" / "sleep-transitions.jsonl"

_IN_FLIGHT_STATUSES = ("queued", "claimed", "pushed")

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "scan_interval_s": 60.0,
    # idle seconds before a unit tiers warm (weights may be evicted)
    "warm_after_s": 300.0,
    # idle seconds before a unit is cold-slept (process stopped)
    "cold_after_s": 1800.0,
    # units that must never be slept (governance cores, always-on)
    "never_sleep": [
        "main-system",
        "model-dialogue",
    ],
    # per-unit overrides: {"local-model": {"cold_after_s": 900}}
    "units": {},
}


def _load_policy() -> dict[str, Any]:
    try:
        raw = json.loads(_POLICY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_DEFAULTS)
    merged = dict(_DEFAULTS)
    if isinstance(raw, dict):
        merged.update({k: v for k, v in raw.items() if k in _DEFAULTS})
    return merged


def _audit(entry: dict[str, Any]) -> None:
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}
    try:
        _AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        _logger.warning("sleep audit append failed: %s", _AUDIT_FILE)


class SleepPolicyManager:
    """Periodic idle scan; cold-sleeps drained units via the toolbox."""

    def __init__(self, app: Any, toolbox_service: Any) -> None:
        self.app = app
        self.toolbox = toolbox_service
        self._task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()
        # tool_id -> last observed activity epoch (first seen = now)
        self._last_active: dict[str, float] = {}
        # tool_id -> current tier ("hot"|"warm"|"cold")
        self._tiers: dict[str, str] = {}
        self._last_decisions: dict[str, str] = {}

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            return {"status": "already_running"}
        self._stop_event.clear()
        try:
            self._task = asyncio.create_task(
                self._loop(), name="sleep-policy-manager"
            )
        except RuntimeError:
            self._task = None
            return {"status": "no_event_loop"}
        _logger.info("sleep policy manager started")
        return {"status": "started"}

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # -- loop -----------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            policy = _load_policy()
            try:
                await self._scan(policy)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # never kill the loop
                _logger.warning("sleep policy cycle error: %s", error)
            interval = max(5.0, float(policy["scan_interval_s"]))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _scan(self, policy: dict[str, Any]) -> None:
        if policy["enabled"] is not True:
            return
        if getattr(self.app, "_shutting_down", False):
            return
        never_sleep = set(policy["never_sleep"])
        overrides = policy["units"] if isinstance(policy["units"], dict) else {}

        tools = await self._running_tools()
        now = time.time()
        for tool_id in sorted(tools):
            if tool_id in never_sleep:
                self._tiers[tool_id] = "hot"
                self._last_decisions[tool_id] = "exempt"
                continue
            unit = overrides.get(tool_id) or {}
            warm_after = float(unit.get("warm_after_s", policy["warm_after_s"]))
            cold_after = float(unit.get("cold_after_s", policy["cold_after_s"]))

            drained, last_activity = await self._drain_state(tool_id)
            if not drained:
                self._last_active[tool_id] = last_activity or now
                self._tiers[tool_id] = "hot"
                self._last_decisions[tool_id] = "in-flight"
                continue

            idle_since = self._last_active.setdefault(tool_id, last_activity or now)
            if last_activity and last_activity > idle_since:
                idle_since = last_activity
                self._last_active[tool_id] = last_activity
            idle_for = now - idle_since

            if idle_for >= cold_after:
                await self._cold_sleep(tool_id, idle_for)
            elif idle_for >= warm_after:
                if self._tiers.get(tool_id) != "warm":
                    _audit({
                        "event": "tier-transition", "tool_id": tool_id,
                        "from": self._tiers.get(tool_id, "hot"), "to": "warm",
                        "idle_for_s": round(idle_for, 1),
                    })
                self._tiers[tool_id] = "warm"
                # warm sleep = weight/cache eviction; the component's own
                # release machinery (AutoReleaseManager) owns the mechanics.
                self._last_decisions[tool_id] = "warm-idle"
            else:
                self._tiers[tool_id] = "hot"
                self._last_decisions[tool_id] = "active"
        self._write_state()

    async def _cold_sleep(self, tool_id: str, idle_for: float) -> None:
        """Stop a fully drained unit through the governed toolbox path."""
        # Re-check drain immediately before acting — a request that
        # arrived during the scan aborts the sleep (no lost work).
        drained, _ = await self._drain_state(tool_id)
        if not drained:
            self._tiers[tool_id] = "hot"
            self._last_decisions[tool_id] = "drain-abort"
            return
        payload = {
            "tool_id": tool_id,
            "request_id": f"sleep-cold-{tool_id}-{time.time_ns()}",
        }
        try:
            result = await self.toolbox.stop_tool(payload)
        except Exception as error:
            result = {"ok": False, "message": f"{type(error).__name__}: {error}"}
        ok = isinstance(result, dict) and result.get("ok") is True
        _audit({
            "event": "cold-sleep", "tool_id": tool_id,
            "idle_for_s": round(idle_for, 1), "ok": ok,
            "detail": (result or {}).get("message") or (result or {}).get("error_code"),
        })
        if ok:
            self._tiers[tool_id] = "cold"
            self._last_decisions[tool_id] = "cold-slept"
            _logger.info(
                "unit %s cold-slept after %.0fs idle", tool_id, idle_for
            )
        else:
            self._last_decisions[tool_id] = "cold-sleep-failed"
            _logger.warning(
                "cold sleep failed for %s: %s",
                tool_id,
                (result or {}).get("message") or (result or {}).get("error_code"),
            )

    # -- data sources ---------------------------------------------------

    async def _running_tools(self) -> set[str]:
        """Tool ids with a live governed process or started session."""
        active = getattr(self.toolbox, "_active_request_by_tool", None)
        started = getattr(self.toolbox, "_started_request_by_tool", None)
        tools: set[str] = set()
        for mapping in (active, started):
            if isinstance(mapping, dict):
                tools.update(str(t) for t in mapping if t)
        return tools

    def _drain_state_sync(self, tool_id: str) -> tuple[bool, float | None]:
        """(drained, last_activity_epoch) from the transport outbox."""
        try:
            from shared_layer.database.connection import get_connection_manager

            with get_connection_manager().connection() as conn:
                row = conn.execute(
                    """
                    SELECT 1 FROM gptbridge_transport.tool_request
                    WHERE target_tool_id = %s AND status = ANY(%s)
                    LIMIT 1
                    """,
                    (tool_id, list(_IN_FLIGHT_STATUSES)),
                ).fetchone()
                last = conn.execute(
                    """
                    SELECT max(extract(epoch from updated_at))
                    FROM gptbridge_transport.tool_request
                    WHERE target_tool_id = %s
                    """,
                    (tool_id,),
                ).fetchone()
            drained = row is None
            last_activity = float(last[0]) if last and last[0] else None
            return drained, last_activity
        except Exception as error:
            _logger.debug("drain probe unavailable for %s: %s", tool_id, error)
            # fail-closed: unknown drain state = do not sleep
            return False, None

    async def _drain_state(self, tool_id: str) -> tuple[bool, float | None]:
        return await asyncio.to_thread(self._drain_state_sync, tool_id)

    # -- observability --------------------------------------------------

    def status(self) -> dict[str, Any]:
        policy = _load_policy()
        return {
            "enabled": policy["enabled"],
            "running": bool(self._task is not None and not self._task.done()),
            "tiers": dict(self._tiers),
            "last_decisions": dict(self._last_decisions),
            "never_sleep": list(policy["never_sleep"]),
        }

    def _write_state(self) -> None:
        payload = {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **self.status(),
        }
        temporary = _STATE_FILE.with_name(
            _STATE_FILE.name + f".{os.getpid()}.tmp"
        )
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, _STATE_FILE)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass


__all__ = ["SleepPolicyManager"]
