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
        # tool_id -> marker identifying the currently observed
        # process/session (started request id, else active request id).
        # A changed or newly appearing marker means the process (re)started
        # since the previous scan — its idle baseline cannot predate that.
        self._running_marks: dict[str, str] = {}
        # tool_id -> current tier ("hot"|"warm"|"cold")
        self._tiers: dict[str, str] = {}
        self._last_decisions: dict[str, str] = {}
        # tool_id -> epoch this manager cold-slept it; the wake fallback
        # only reacts to requests queued AFTER this mark (same semantics
        # as the activation broker's explicit-stop window) and only ever
        # fires for units this manager itself slept.
        self._cold_since: dict[str, float] = {}
        # tool_id -> monotonic time before which no new wake attempt
        self._wake_not_before: dict[str, float] = {}
        # §1.1: True when the automation core drives the scan cadence;
        # False when the flow was denied (kill switch — no private
        # fallback); None when no core exists and the private loop runs.
        self._core_driven: bool | None = None

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            return {"status": "already_running"}
        self._stop_event.clear()
        self._restore_cold_state()
        # §1.1 自動化集中：when the automation core is present it owns the
        # cadence — a denied registration (unlisted/kill-switched) must
        # NOT fall back to the private loop.
        core = getattr(self.app, "automation_core", None)
        if core is not None:
            self._core_driven = bool(
                core.register_flow("sleep-policy", self._tick)
            )
            if not self._core_driven:
                return {"status": "denied", "loop": "disabled"}
            _logger.info("sleep policy manager started (automation-core)")
            return {"status": "started", "loop": "automation-core"}
        try:
            self._task = asyncio.create_task(
                self._loop(), name="sleep-policy-manager"
            )
        except RuntimeError:
            self._task = None
            return {"status": "no_event_loop"}
        _logger.info("sleep policy manager started")
        return {"status": "started", "loop": "private"}

    async def _tick(self) -> None:
        """Single scan — the automation-core flow entry point."""
        policy = _load_policy()
        try:
            await self._tick_body(policy)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # never propagate into the scheduler
            _logger.warning("sleep policy cycle error: %s", error)

    async def _tick_body(self, policy: dict[str, Any]) -> None:
        """Wake + scan work shared by the private loop and the core flow."""
        await self._wake_slept_units(policy)
        await self._scan(policy)
        # §10.7 on-demand 對稱卸載：本系統 spawn 的 Ollama 閒置逾
        # warm_after_s 即終止（僅 owned pid；外部啟動的永不觸碰）。
        # 同一 kill switch（enabled=false）閘住——卸載亦屬 sleep 動作。
        if policy["enabled"] is not True:
            return
        try:
            from core_system.ollama_demand import (  # noqa: PLC0415
                DEFAULT_IDLE_UNLOAD_S,
                stop_ollama_if_owned,
            )

            await asyncio.to_thread(
                stop_ollama_if_owned,
                float(policy.get("warm_after_s") or DEFAULT_IDLE_UNLOAD_S),
            )
        except Exception:
            pass

    async def stop(self) -> None:
        core = getattr(self.app, "automation_core", None)
        if core is not None and self._core_driven:
            try:
                core.unregister("sleep-policy")
            except Exception:
                pass
        self._core_driven = None
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
            # Per-tick deadline (P7): a hung drain-state await or governed
            # stop_tool call must not freeze the private loop forever.
            # The automation-core path gets the same bound via the
            # scheduler's wait_for tick wrapper.
            tick_deadline = max(
                30.0, min(600.0, float(policy["scan_interval_s"]) * 5)
            )
            try:
                await asyncio.wait_for(
                    self._tick_body(policy), timeout=tick_deadline
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                _logger.warning(
                    "sleep policy tick exceeded %.0fs deadline", tick_deadline
                )
                _audit({
                    "event": "tick-deadline-exceeded",
                    "deadline_s": round(tick_deadline, 1),
                })
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

        tools = await self._running_marks()
        now = time.time()
        # Marks for vanished tools are dropped so a later re-appearance
        # counts as a fresh process even if its marker repeats.
        for gone in set(self._running_marks) - set(tools):
            self._running_marks.pop(gone, None)
        for tool_id in sorted(tools):
            if self._running_marks.get(tool_id) != tools[tool_id]:
                # Freshly (re)started process/session — reset the idle
                # baseline.  Without this a stale ``_last_active`` epoch
                # survives restarts and the very next scan cold-sleeps a
                # just-woken unit before it can serve the request that
                # demanded it (observed: every governed/local-model wake
                # killed ~35 s after start).
                self._running_marks[tool_id] = tools[tool_id]
                self._last_active[tool_id] = now
            if tool_id in never_sleep:
                self._tiers[tool_id] = "hot"
                self._last_decisions[tool_id] = "exempt"
                self._cold_since.pop(tool_id, None)
                continue
            # A unit that reappeared running (manual start, wake, broker)
            # is no longer cold — drop the wake-fallback mark.
            self._cold_since.pop(tool_id, None)
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
            self._cold_since[tool_id] = time.time()
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

    # -- wake fallback (回退路徑) ----------------------------------------

    _WAKE_COOLDOWN_S = 30.0

    async def _wake_slept_units(self, policy: dict[str, Any]) -> None:
        """Demand-driven wake for units this manager cold-slept.

        Only requests queued AFTER the cold-sleep mark count — same
        semantics as the activation broker's explicit-stop window: a
        fresh demand restarts the unit through the governed
        ``start_tool`` path; stale backlog predating the sleep does not.
        Runs even when ``enabled`` is false: the kill switch stops sleep
        transitions, not service restoration for already-slept units.
        """
        if not self._cold_since:
            return
        try:
            from tasks.resource_governor_signal import worker_admission_hold
        except Exception:
            worker_admission_hold = None  # type: ignore[assignment]
        never_sleep = set(policy["never_sleep"])
        running = await self._running_tools()
        now_mono = time.monotonic()
        for tool_id in sorted(self._cold_since):
            if tool_id in never_sleep or tool_id in running:
                self._cold_since.pop(tool_id, None)
                continue
            if now_mono < self._wake_not_before.get(tool_id, 0.0):
                continue
            if not await self._has_fresh_demand(tool_id):
                continue
            if worker_admission_hold is not None and worker_admission_hold():
                self._last_decisions[tool_id] = "wake-hold:resource-governor"
                _audit({
                    "event": "cold-wake-hold", "tool_id": tool_id,
                    "reason": "resource-governor",
                })
                continue
            self._wake_not_before[tool_id] = now_mono + self._WAKE_COOLDOWN_S
            try:
                result = await self.toolbox.start_tool(
                    {
                        "tool_id": tool_id,
                        "request_id": (
                            f"sleep-wake-{tool_id}-{time.time_ns()}"
                        ),
                        "background": True,
                    }
                )
            except Exception as error:
                result = {
                    "ok": False,
                    "message": f"{type(error).__name__}: {error}",
                }
            ok = isinstance(result, dict) and result.get("ok") is True
            _audit({
                "event": "cold-wake", "tool_id": tool_id, "ok": ok,
                "detail": (result or {}).get("message")
                or (result or {}).get("error_code"),
            })
            if ok:
                self._tiers[tool_id] = "hot"
                self._cold_since.pop(tool_id, None)
                self._last_active[tool_id] = time.time()
                self._last_decisions[tool_id] = "cold-woke"
            else:
                self._last_decisions[tool_id] = "cold-wake-failed"

    async def _has_fresh_demand(self, tool_id: str) -> bool:
        return await asyncio.to_thread(self._fresh_demand_sync, tool_id)

    def _fresh_demand_sync(self, tool_id: str) -> bool:
        """A queued, unexpired request created after the cold-sleep mark."""
        since = self._cold_since.get(tool_id)
        if since is None:
            return False
        try:
            from shared_layer.database.workload_lanes import (
                WorkloadClass,
                get_lane_pool,
            )

            with get_lane_pool().connection(WorkloadClass.BACKGROUND) as conn:
                row = conn.execute(
                    """
                    SELECT 1 FROM gptbridge_transport.tool_request
                    WHERE target_tool_id = %s
                      AND status = 'queued'
                      AND created_at > to_timestamp(%s)
                      AND (deadline_at IS NULL OR deadline_at > now())
                    LIMIT 1
                    """,
                    (tool_id, since),
                ).fetchone()
            return row is not None
        except Exception as error:
            _logger.debug("wake demand probe unavailable for %s: %s",
                          tool_id, error)
            # fail-closed: unknown demand = do not wake
            return False

    def _restore_cold_state(self) -> None:
        """Resume the wake fallback across a main-system restart: units
        this manager cold-slept before the restart must still be
        wakeable — otherwise their queued requests would be stranded."""
        try:
            record = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        cold = record.get("cold_since")
        if isinstance(cold, dict):
            for tool_id, epoch in cold.items():
                try:
                    self._cold_since[str(tool_id)] = float(epoch)
                except (TypeError, ValueError):
                    continue
                self._tiers.setdefault(str(tool_id), "cold")

    # -- data sources ---------------------------------------------------

    async def _running_tools(self) -> set[str]:
        """Tool ids with a live governed process or started session."""
        return set(await self._running_marks())

    async def _running_marks(self) -> dict[str, str]:
        """tool_id -> marker for the currently observed process/session.

        The started-session request id is stable for the life of the
        process; the active request id identifies one-shot executions.
        A marker change therefore signals a (re)start or fresh work.
        """
        active = getattr(self.toolbox, "_active_request_by_tool", None)
        started = getattr(self.toolbox, "_started_request_by_tool", None)
        marks: dict[str, str] = {}
        for label, mapping in (("started", started), ("active", active)):
            if isinstance(mapping, dict):
                for tool_id, request_id in mapping.items():
                    if tool_id:
                        marks[str(tool_id)] = f"{label}:{request_id}"
        return marks

    def _drain_state_sync(self, tool_id: str) -> tuple[bool, float | None]:
        """(drained, last_activity_epoch) from the transport outbox."""
        try:
            from shared_layer.database.workload_lanes import (
                WorkloadClass,
                get_lane_pool,
            )

            # §10.5: periodic drain probe — background lane.
            with get_lane_pool().connection(WorkloadClass.BACKGROUND) as conn:
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
                    SELECT max(extract(epoch from updated_at)) AS last_activity
                    FROM gptbridge_transport.tool_request
                    WHERE target_tool_id = %s
                    """,
                    (tool_id,),
                ).fetchone()
            drained = row is None
            # dict_row: fetchone() returns {"last_activity": ...} — indexing
            # a dict with 0 raises KeyError and wrongly fails closed.
            last_value = (last or {}).get("last_activity") if isinstance(
                last, dict
            ) else (last[0] if last else None)
            last_activity = float(last_value) if last_value else None
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
            "cold_since": dict(self._cold_since),
            "last_decisions": dict(self._last_decisions),
            "never_sleep": list(policy["never_sleep"]),
        }

    def _write_state(self) -> None:
        payload = {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cold_since": dict(self._cold_since),
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
