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
- Only user-driven ``ai``-channel dialogue requests activate the owner.
  Queued ``system``-channel internal commands (e.g. the repair teaching
  bridge) never auto-start the model: the model only runs when a user
  actually talks to it or starts it directly.
- An explicit toolbox force-close of the owner suppresses re-activation for
  the rest of the pending window, so pre-existing queued requests stop
  being enough to resurrect the model after the user turned it off.
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
from tasks.resource_governor_signal import (
    regulation_active,
    worker_admission_hold,
)


def _worker_admission_hold() -> bool:
    """§10.64 control-law ⑤: deny new worker starts while the resource
    governor is regulating (aggregate worker budget exceeded)."""
    return worker_admission_hold()

TARGET_TOOL_ID = "xingcheng"
OWNER_TOOL_ID = "local-model"

_DEFAULT_IDLE_INTERVAL = 5.0
_DEFAULT_PENDING_INTERVAL = 1.0
_DEFAULT_COOLDOWN_SECONDS = 20.0
_DEFAULT_MIN_BACKOFF_SECONDS = 15.0
_DEFAULT_MAX_BACKOFF_SECONDS = 180.0
_PENDING_WINDOW_MINUTES = 10

_ACTIVE_BROKER: "ModelServiceActivationBroker | None" = None
_RESOURCE_GATE_UNSET: Any = object()  # sentinel: None 明確停用資源閘門


def note_explicit_owner_stop(tool_id: str) -> None:
    """Notify the broker that the user explicitly force-closed a tool.

    Only ``OWNER_TOOL_ID`` is relevant; other tools are silently ignored.
    When the owner is stopped explicitly, pending pre-existing queued
    requests must no longer trigger a fresh on-demand activation for the
    remainder of their pending window.
    """
    if tool_id != OWNER_TOOL_ID:
        return
    broker = _ACTIVE_BROKER
    if broker is not None:
        broker.note_explicit_owner_stop()


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
        project_root: Path | None = None,
        resource_manager: Any = _RESOURCE_GATE_UNSET,
    ) -> None:
        global _ACTIVE_BROKER
        self.app = app
        self.toolbox = toolbox_service
        self._project_root = Path(project_root) if project_root is not None else None
        self.idle_interval = max(0.5, float(idle_interval))
        self.pending_interval = max(0.25, float(pending_interval))
        self.cooldown = max(0.0, float(cooldown))
        self.min_backoff = max(1.0, float(min_backoff))
        self.max_backoff = max(self.min_backoff, float(max_backoff))

        self._task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._loop_obj: asyncio.AbstractEventLoop | None = None
        self._next_attempt_at = 0.0
        self._backoff = self.min_backoff
        self._pending = False
        self._attempts = 0
        self._last_result: dict[str, Any] = {}
        self._last_decision = ""
        self._explicit_stop_at = 0.0
        self._broker_started_owner = False
        self._next_release_at = 0.0
        self._last_release_result: dict[str, Any] = {}
        self._last_written_fingerprint: dict[str, Any] | None = None
        self._last_write_at = 0.0
        # §10.7: governed model-resource admission gate.  Auto-attaches when
        # project_root is given (production always passes it); pass
        # ``resource_manager=None`` explicitly to disable in tests.
        if resource_manager is _RESOURCE_GATE_UNSET:
            resource_manager = None
            if project_root is not None:
                try:
                    from core_system.model_resource_manager import (
                        get_model_resource_manager,
                    )

                    resource_manager = get_model_resource_manager()
                except Exception:
                    resource_manager = None
        self._resource_manager = resource_manager
        # §10.65 act-1: native shadow attaches only when project_root is
        # given; policy mode != "shadow" or a missing extension yields None.
        self._native_shadow = None
        if project_root is not None:
            try:
                from tasks.model_service_activation_native_shadow import (
                    ActivationBrokerNativeShadow,
                )

                self._native_shadow = ActivationBrokerNativeShadow.from_policy(
                    Path(project_root),
                    cooldown_s=self.cooldown,
                    min_backoff_s=self.min_backoff,
                    max_backoff_s=self.max_backoff,
                )
            except Exception:
                self._native_shadow = None
        _ACTIVE_BROKER = self

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            return {"status": "already_running"}
        self._stop_event.clear()
        try:
            self._loop_obj = asyncio.get_running_loop()
            self._task = asyncio.create_task(
                self._loop(), name="model-service-activation-broker"
            )
        except RuntimeError:
            self._task = None
            return {"status": "no_event_loop"}
        # §10.63/G26: LISTEN/NOTIFY 消費端——新 ai 請求入列即喚醒（事件驅動），
        # 週期輪詢保留為 fail-closed 備援。
        try:
            from shared_layer.transport_notify import (
                get_transport_notify_listener,
            )

            listener = get_transport_notify_listener()
            listener.subscribe("ai", self._on_transport_notify)
            listener.start()
        except Exception as error:
            _logger.debug("transport notify listener unavailable: %s", error)
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
        try:
            from shared_layer.transport_notify import (
                peek_transport_notify_listener,
            )

            listener = peek_transport_notify_listener()
            if listener is not None:
                listener.unsubscribe("ai", self._on_transport_notify)
        except Exception:
            pass
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def _on_transport_notify(self, channel: str, request_id: str) -> None:
        """LISTEN/NOTIFY callback（監聽執行緒）——轉入自身事件迴圈喚醒。"""
        loop = self._loop_obj
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._wake_event.set)
        except Exception:
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
            shadow = self._native_shadow
            if shadow is not None:
                try:
                    shadow.observe_poll_interval(
                        bool(self._pending),
                        self.idle_interval,
                        self.pending_interval,
                        py_interval=interval,
                    )
                except Exception:
                    pass
            try:
                await self._wait_stop_or_wake(interval)
            except asyncio.CancelledError:
                raise

    async def _wait_stop_or_wake(self, interval: float) -> None:
        """等到 stop、notify 喚醒或輪詢逾時（先到者為準）。"""
        self._wake_event.clear()
        stopper = asyncio.ensure_future(self._stop_event.wait())
        waker = asyncio.ensure_future(self._wake_event.wait())
        try:
            await asyncio.wait(
                {stopper, waker},
                timeout=interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for pending in (stopper, waker):
                if not pending.done():
                    pending.cancel()

    async def _ensure(self) -> str:
        """One activation check; returns the decision for observability."""
        decision = await self._ensure_inner()
        self._last_decision = decision
        self._write_state()
        shadow = self._native_shadow
        if shadow is not None:
            try:
                shadow.observe_status(
                    {
                        "attempts": self._attempts,
                        "backoff_seconds": self._backoff,
                        "next_attempt_at": self._next_attempt_at,
                        "next_release_at": self._next_release_at,
                        "broker_started_owner": self._broker_started_owner,
                        "explicit_stop_at": self._explicit_stop_at,
                    }
                )
            except Exception:
                pass
        return decision

    _STATE_HEARTBEAT_SECONDS = 60.0

    async def _ensure_inner(self) -> str:
        facts: dict[str, Any] = {"pending": False}
        self._pending = await asyncio.to_thread(self._has_pending_dialogue_request)
        facts["pending"] = bool(self._pending)
        if not self._pending:
            self._backoff = self.min_backoff
            return await self._maybe_release_owner()
        facts["maintenance_ready"] = (
            getattr(self.app, "maintenance_ready", True) is True
        )
        if not facts["maintenance_ready"]:
            return self._shadow_ensure(facts, "maintenance-pending")
        facts["shutting_down"] = bool(getattr(self.app, "_shutting_down", False))
        if facts["shutting_down"]:
            return self._shadow_ensure(facts, "shutting-down")
        # §10.64 ⑤: while the governor is regulating, hold new worker
        # starts (fail-closed load shedding).  User-explicit tool starts
        # via the command surface are unaffected — this only gates the
        # broker's automatic activation.
        facts["admission_hold"] = bool(_worker_admission_hold())
        if facts["admission_hold"]:
            return self._shadow_ensure(facts, "resource-hold")
        try:
            owner_active = await self.toolbox.tool_process_active(OWNER_TOOL_ID)
            facts["liveness_known"] = True
            facts["owner_active"] = bool(owner_active)
            if owner_active:
                self._backoff = self.min_backoff
                return self._shadow_ensure(facts, "owner-running")
        except Exception as error:
            _logger.warning("model owner liveness check failed: %s", error)
            return self._shadow_ensure(facts, "liveness-unknown")

        now = time.monotonic()
        facts["now_monotonic"] = now
        if now < self._next_attempt_at:
            return self._shadow_ensure(facts, "throttled")

        # §10.7: resource admission gate — the model owner only starts when
        # the governed ModelResourceManager admits the load; denials are
        # fail-closed (no start, backoff, ledgered).
        denied = self._resource_gate(facts)
        if denied is not None:
            return self._shadow_ensure(facts, denied)

        # Gate passed — the native ladder must agree before the toolbox
        # call; the post-call result is mirrored separately.
        self._shadow_ensure(facts, "should-start")

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

        if self._last_result.get("ok") is not True:
            # Admission never materialised — free the reservation.
            self._release_resource_admission()

        if self._last_result.get("ok") is True:
            self._next_attempt_at = now + self.cooldown
            self._backoff = self.min_backoff
            self._broker_started_owner = True
            _logger.info(
                "model service activated on demand: pid=%s",
                self._last_result.get("pid"),
            )
            return self._shadow_start(True, now, "started")

        delay = self._backoff
        self._backoff = min(self.max_backoff, self._backoff * 2.0)
        self._next_attempt_at = now + delay
        _logger.warning(
            "model service activation failed (retry in %.0fs): %s",
            delay,
            self._last_result.get("message") or self._last_result.get("error_code"),
        )
        return self._shadow_start(False, now, "start-failed")

    async def _maybe_release_owner(self) -> str:
        """§10.64 ⑥: governed auto-release of the on-demand owner.

        While the governor's regulation is active and the pending window
        is empty, release the owner through the same governed
        ``ToolboxService.stop_tool`` path a user stop uses (expected-stop
        marking, tracked-process termination, status update, audit).
        Only owners this broker activated are released — an explicitly
        user-started model is never force-closed by regulation.
        """
        facts: dict[str, Any] = {"pending": False}
        if not self._broker_started_owner:
            return self._shadow_ensure(facts, "idle")
        facts["regulation_active"] = bool(regulation_active())
        if not facts["regulation_active"]:
            return self._shadow_ensure(facts, "idle")
        try:
            owner_active = await self.toolbox.tool_process_active(OWNER_TOOL_ID)
            facts["liveness_known"] = True
            facts["owner_active"] = bool(owner_active)
            if not owner_active:
                self._broker_started_owner = False
                return self._shadow_ensure(facts, "idle")
        except Exception:
            return self._shadow_ensure(facts, "idle")
        now = time.monotonic()
        facts["now_monotonic"] = now
        if now < self._next_release_at:
            return self._shadow_ensure(facts, "release-cooldown")

        self._shadow_ensure(facts, "should-release")
        payload = {
            "tool_id": OWNER_TOOL_ID,
            "request_id": f"model-release-{time.time_ns()}",
        }
        try:
            result = await self.toolbox.stop_tool(payload)
        except Exception as error:
            result = {"ok": False, "message": f"{type(error).__name__}: {error}"}
        self._last_release_result = result if isinstance(result, dict) else {}
        self._next_release_at = now + self.cooldown
        if self._last_release_result.get("ok") is True:
            self._broker_started_owner = False
            self._release_resource_admission()
            _logger.info(
                "on-demand model owner released under resource regulation"
            )
            return self._shadow_release(True, now, "released")
        _logger.warning(
            "model owner auto-release failed: %s",
            self._last_release_result.get("message")
            or self._last_release_result.get("error_code"),
        )
        return self._shadow_release(False, now, "release-failed")

    # -- resource admission gate (§10.7; fail-closed) ----------------------

    _RAM_HEADROOM = 1.5  # 權重→RAM 執行期 overhead，對齊引擎載入估算慣例

    def _resource_gate(self, facts: dict[str, Any]) -> str | None:
        """§10.7：載入前經 ``ModelResourceManager.request_load`` 資源閘門。

        否決／估測不可用／閘門錯誤一律 fail-closed（不啟動、退避、記帳）。
        僅在 manager 接上時生效（production 必接）。
        """
        mgr = self._resource_manager
        if mgr is None:
            return None
        required_mb = self._estimate_owner_ram_mb()
        facts["ram_required_mb"] = required_mb
        if required_mb is None:
            _logger.warning(
                "model resource gate: cannot estimate owner RAM requirement"
            )
            return "resource-estimate-unavailable"
        try:
            from core_system.model_resource_manager import ModelRole

            decision = mgr.request_load(
                ModelRole.XINGCHENG_NATIVE, TARGET_TOOL_ID, ram_mb=required_mb
            )
        except Exception as error:
            _logger.warning("model resource gate error: %s", error)
            return "resource-gate-error"
        facts["resource_admitted"] = bool(decision.admitted)
        facts["resource_reason"] = decision.reason
        try:
            mgr.record_measurement(
                TARGET_TOOL_ID,
                ModelRole.XINGCHENG_NATIVE,
                {
                    "event": "admission",
                    "admitted": decision.admitted,
                    "reason": decision.reason,
                    "ram_required_mb": required_mb,
                },
            )
        except Exception:
            pass
        if not decision.admitted:
            self._backoff = min(self.max_backoff, self._backoff * 2.0)
            self._next_attempt_at = time.monotonic() + self._backoff
            _logger.warning(
                "model activation denied by resource gate (retry in %.0fs): %s",
                self._backoff,
                decision.reason,
            )
            return "resource-denied"
        return None

    def _estimate_owner_ram_mb(self) -> int | None:
        """由 ``native-engine.json`` 釘定 checkpoint 檔案大小估 RAM 需求。

        權重無論走 CPU 或 GPU 都必須進 RAM；VRAM 不列入硬性閘門
        （引擎逾時會自行降級 CPU）。無法定位權重 → None（fail-closed）。
        """
        root = self._project_root
        if root is None:
            return None
        try:
            tool_root = root / "Standalone tools" / "local-model"
            settings = tool_root / "runtime" / "settings" / "native-engine.json"
            checkpoint = json.loads(settings.read_text(encoding="utf-8"))[
                "checkpoint"
            ]
            size_mb = (tool_root / checkpoint).stat().st_size / (1024 * 1024)
            return int(size_mb * self._RAM_HEADROOM) + 1
        except Exception:
            return None

    def _release_resource_admission(self) -> None:
        mgr = self._resource_manager
        if mgr is None:
            return
        try:
            mgr.release(TARGET_TOOL_ID)
        except Exception:
            pass

    # -- native shadow helpers (§10.65 act-1; fail-closed, Python authoritative)

    def _shadow_ensure(self, facts: dict[str, Any], decision: str) -> str:
        shadow = self._native_shadow
        if shadow is not None:
            try:
                shadow.observe_ensure(facts, py_decision=decision)
            except Exception:
                pass
        return decision

    def _shadow_start(self, ok: bool, now: float, decision: str) -> str:
        shadow = self._native_shadow
        if shadow is not None:
            try:
                shadow.observe_start_result(ok, now, py_decision=decision)
            except Exception:
                pass
        return decision

    def _shadow_release(self, ok: bool, now: float, decision: str) -> str:
        shadow = self._native_shadow
        if shadow is not None:
            try:
                shadow.observe_release_result(ok, now, py_decision=decision)
            except Exception:
                pass
        return decision

    def note_explicit_owner_stop(self) -> None:
        """Remember an explicit close of the owner so it is not resurrected.

        After the user force-closes the model runtime, queued requests that
        already existed before that moment are treated as non-triggering for
        the rest of their pending window.  Requests arriving after the stop
        still activate on demand — that is the intended dialogue behaviour.
        """
        self._explicit_stop_at = time.time()
        self._broker_started_owner = False
        self._release_resource_admission()
        # Cooldown so the next observed request waits before a fresh attempt.
        now = time.monotonic()
        self._next_attempt_at = now + self.cooldown
        shadow = self._native_shadow
        if shadow is not None:
            try:
                shadow.observe_explicit_stop(now, self._explicit_stop_at)
            except Exception:
                pass
        _logger.info(
            "model owner explicit stop recorded at %.0f; pre-stop queued "
            "requests will not auto-start the model",
            self._explicit_stop_at,
        )

    # -- detection ------------------------------------------------------

    def _has_pending_dialogue_request(self) -> bool:
        """True when a fresh queued ``ai`` request needs the model owner.

        Activation is demand-driven, user-facing and ``ai``-channel only:
        a queued ``ai`` inference request proves the owner is required.
        Queued ``system``-channel internal commands (e.g. the repair
        teaching bridge) do NOT auto-start the model; the model only runs
        when a user drives it.

        Requests that were already queued before an explicit force-close of
        the owner (``explicit_stop_at``) are ignored for the rest of their
        pending window, so the user's explicit stop is honoured.
        """
        explicit_stop_at = self._explicit_stop_at
        try:
            from shared_layer.database.workload_lanes import (
                WorkloadClass,
                get_lane_pool,
            )

            # §10.5: periodic broker probe — background lane so it can
            # never starve the interactive lane's small inflight budget.
            # §10.5 殘項：單次唯讀探針走 pool.execute 一次性介面。
            pool = get_lane_pool()
            if explicit_stop_at > 0.0:
                rows = pool.execute(
                    WorkloadClass.BACKGROUND,
                    """
                    SELECT 1
                    FROM gptbridge_transport.tool_request
                    WHERE channel_id = 'ai'
                      AND target_tool_id = %s
                      AND status = 'queued'
                      AND created_at > now() - (%s || ' minutes')::interval
                      AND created_at > to_timestamp(%s)
                      AND (deadline_at IS NULL OR deadline_at > now())
                    LIMIT 1
                    """,
                    (
                        TARGET_TOOL_ID,
                        str(_PENDING_WINDOW_MINUTES),
                        explicit_stop_at,
                    ),
                )
            else:
                rows = pool.execute(
                    WorkloadClass.BACKGROUND,
                    """
                    SELECT 1
                    FROM gptbridge_transport.tool_request
                    WHERE channel_id = 'ai'
                      AND target_tool_id = %s
                      AND status = 'queued'
                      AND created_at > now() - (%s || ' minutes')::interval
                      AND (deadline_at IS NULL OR deadline_at > now())
                    LIMIT 1
                    """,
                    (TARGET_TOOL_ID, str(_PENDING_WINDOW_MINUTES)),
                )
            return len(rows) > 0
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
            "explicit_stop_at": round(self._explicit_stop_at, 1),
            "broker_started_owner": self._broker_started_owner,
            "last_release_ok": self._last_release_result.get("ok"),
            "last_error": self._last_result.get("message")
            or self._last_result.get("error_code"),
        }

    def _write_state(self) -> None:
        """Best-effort runtime state surface for operators and audits."""
        payload = {"updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        payload.update(self.status())
        # §10.63 R2: skip the disk write while nothing changed — an idle
        # broker used to rewrite this file every 5 s.  A 60 s heartbeat
        # keeps updated_at fresh for staleness checks.
        fingerprint = {k: v for k, v in payload.items() if k != "updated_at"}
        now = time.monotonic()
        py_due = not (
            fingerprint == self._last_written_fingerprint
            and now - self._last_write_at < self._STATE_HEARTBEAT_SECONDS
        )
        shadow = self._native_shadow
        if shadow is not None:
            try:
                shadow.observe_state_write_due(
                    fingerprint != self._last_written_fingerprint,
                    now,
                    self._last_write_at,
                    self._STATE_HEARTBEAT_SECONDS,
                    py_due=py_due,
                )
            except Exception:
                pass
        if not py_due:
            return
        self._last_written_fingerprint = fingerprint
        self._last_write_at = now
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
    "note_explicit_owner_stop",
]
