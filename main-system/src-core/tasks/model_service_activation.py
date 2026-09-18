"""On-demand activation of the model-owner runtime (lazy start).

Governor directive 2026-09-18: the model dialogue opens without the local
model, and a conversation request transparently activates it.  This broker
watches the governed AI transport for queued dialogue requests addressed to
the model owner (``xingcheng``) and starts the owning tool (``local-model``)
through the existing governed toolbox start path — the same path already
used for on-demand tool execution (``toolbox_execution``).

Boundaries:
- It never claims or answers transport requests; the xingcheng runtime owns
  that.  It only ensures a consumer exists.
- Every activation goes through ``ToolboxService.start_tool`` (capability
  gate, isolation registry, audit, quarantine/backoff) — no side channel.
- Stale queued rows and requests past their deadline are ignored; attempts
  are throttled and backed off, so a failing owner is never restart-looped.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger("gptbridge.model_service_activation")

_STATE_FILE = (
    Path(__file__).resolve().parents[2]
    / "runtime" / "state" / "model-service-activation.json"
)

TARGET_TOOL_ID = "xingcheng"
OWNER_TOOL_ID = "local-model"

_DEFAULT_IDLE_INTERVAL = 5.0
_DEFAULT_PENDING_INTERVAL = 1.0
_DEFAULT_COOLDOWN_SECONDS = 20.0
_DEFAULT_MIN_BACKOFF_SECONDS = 15.0
_DEFAULT_MAX_BACKOFF_SECONDS = 180.0
_PENDING_WINDOW_MINUTES = 10


class ModelServiceActivationBroker:
    """Starts the model-owner tool when a dialogue request is waiting."""

    def __init__(
        self,
        app: Any,
        toolbox_service: Any,
        *,
        idle_interval: float = _DEFAULT_IDLE_INTERVAL,
        pending_interval: float = _DEFAULT_PENDING_INTERVAL,
        cooldown: float = _DEFAULT_COOLDOWN_SECONDS,
        min_backoff: float = _DEFAULT_MIN_BACKOFF_SECONDS,
        max_backoff: float = _DEFAULT_MAX_BACKOFF_SECONDS,
    ) -> None:
        self.app = app
        self.toolbox = toolbox_service
        self.idle_interval = max(0.5, float(idle_interval))
        self.pending_interval = max(0.25, float(pending_interval))
        self.cooldown = max(0.0, float(cooldown))
        self.min_backoff = max(1.0, float(min_backoff))
        self.max_backoff = max(self.min_backoff, float(max_backoff))

        self._task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()
        self._next_attempt_at = 0.0
        self._backoff = self.min_backoff
        self._pending = False
        self._attempts = 0
        self._last_result: dict[str, Any] = {}
        self._last_decision = ""

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            return {"status": "already_running"}
        self._stop_event.clear()
        try:
            self._task = asyncio.create_task(
                self._loop(), name="model-service-activation-broker"
            )
        except RuntimeError:
            self._task = None
            return {"status": "no_event_loop"}
        _logger.info(
            "model service activation broker started (idle=%.1fs pending=%.1fs)",
            self.idle_interval,
            self.pending_interval,
        )
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
            try:
                await self._ensure()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # never kill the loop
                _logger.warning("model activation broker cycle error: %s", error)
            interval = self.pending_interval if self._pending else self.idle_interval
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _ensure(self) -> str:
        """One activation check; returns the decision for observability."""
        decision = await self._ensure_inner()
        self._last_decision = decision
        self._write_state()
        return decision

    async def _ensure_inner(self) -> str:
        self._pending = await asyncio.to_thread(self._has_pending_dialogue_request)
        if not self._pending:
            self._backoff = self.min_backoff
            return "idle"
        if getattr(self.app, "maintenance_ready", True) is not True:
            return "maintenance-pending"
        if getattr(self.app, "_shutting_down", False):
            return "shutting-down"
        try:
            if await self.toolbox.tool_process_active(OWNER_TOOL_ID):
                self._backoff = self.min_backoff
                return "owner-running"
        except Exception as error:
            _logger.warning("model owner liveness check failed: %s", error)
            return "liveness-unknown"

        now = time.monotonic()
        if now < self._next_attempt_at:
            return "throttled"

        payload = {
            "tool_id": OWNER_TOOL_ID,
            "background": True,
            "request_id": f"model-on-demand-{time.time_ns()}",
        }
        self._attempts += 1
        try:
            result = await self.toolbox.start_tool(payload)
        except Exception as error:
            result = {"ok": False, "message": f"{type(error).__name__}: {error}"}
        self._last_result = result if isinstance(result, dict) else {}

        if self._last_result.get("ok") is True:
            self._next_attempt_at = now + self.cooldown
            self._backoff = self.min_backoff
            _logger.info(
                "model service activated on demand: pid=%s",
                self._last_result.get("pid"),
            )
            return "started"

        delay = self._backoff
        self._backoff = min(self.max_backoff, self._backoff * 2.0)
        self._next_attempt_at = now + delay
        _logger.warning(
            "model service activation failed (retry in %.0fs): %s",
            delay,
            self._last_result.get("message") or self._last_result.get("error_code"),
        )
        return "start-failed"

    # -- detection ------------------------------------------------------

    def _has_pending_dialogue_request(self) -> bool:
        """True when a fresh queued governed request targets the model owner.

        On-demand activation is demand-driven, not channel-specific: a
        queued ``ai`` inference request needs the model runtime, and so
        does a queued ``system`` governed command (e.g. the repair
        teaching bridge's ``xingcheng_submit_teaching``).  Either proves
        the owner is required and must be started.
        """
        try:
            from shared_layer.database.connection import get_connection_manager

            with get_connection_manager().connection() as conn:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM gptbridge_transport.tool_request
                    WHERE channel_id IN ('ai', 'system')
                      AND target_tool_id = %s
                      AND status = 'queued'
                      AND created_at > now() - (%s || ' minutes')::interval
                      AND (deadline_at IS NULL OR deadline_at > now())
                    LIMIT 1
                    """,
                    (TARGET_TOOL_ID, str(_PENDING_WINDOW_MINUTES)),
                ).fetchone()
                return row is not None
        except Exception as error:
            _logger.debug("pending dialogue probe unavailable: %s", error)
            return False

    # -- observability --------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._task is not None and not self._task.done()),
            "target_tool_id": TARGET_TOOL_ID,
            "owner_tool_id": OWNER_TOOL_ID,
            "pending_request": self._pending,
            "last_decision": self._last_decision,
            "attempts": self._attempts,
            "backoff_seconds": round(self._backoff, 1),
            "next_attempt_in": max(0.0, round(self._next_attempt_at - time.monotonic(), 1)),
            "last_result_ok": self._last_result.get("ok"),
            "last_pid": self._last_result.get("pid"),
            "last_error": self._last_result.get("message")
            or self._last_result.get("error_code"),
        }

    def _write_state(self) -> None:
        """Best-effort runtime state surface for operators and audits."""
        payload = {"updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        payload.update(self.status())
        temporary = _STATE_FILE.with_name(_STATE_FILE.name + f".{os.getpid()}.tmp")
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


__all__ = [
    "ModelServiceActivationBroker",
    "OWNER_TOOL_ID",
    "TARGET_TOOL_ID",
]
