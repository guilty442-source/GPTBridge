"""Self-learning driver — governed scheduling for xingcheng training cycles.

§1.1 自動化集中 + A554：本 driver 只經 ``AutomationCore.register_flow``
（flow id ``self-learning``，manifest ``kind=periodic``）掛到共享
PeriodicScheduler；被拒絕（kill switch／未列清單）時**不回落私有迴圈**。

每個 tick 只做兩件事：

1. 廉價前置檢查（讀工具端 policy/state JSON）——只為了**避免喚醒一個
   停機中的工具去跑一個不可能執行的循環**；所有政策閘（enabled、
   min_new_examples、min_interval、quiet hours、GPU 退避、熔斷、每日
   上限、inference 互斥）的權威判定仍在工具行程內的 ``run_cycle``。
2. 經 ``ToolboxService.request_tool_execution`` 提交一個受管
   system-channel 請求 ``xingcheng_self_learning_cycle``（queue-and-
   return；訓練可能跑數十分鐘，絕不能用 run_tool 阻塞共享排程迴圈）。

循環必須在工具行程內執行：``inference_exclusion``（§2.7-4）檢查的是
行程本地 engine cache（Python＋C++ 兩側），外掛 watcher 行程看不到，
會靜默繞過互斥閘。冷停時經 ``ToolboxService.start_tool``（governed
path：能力閘、隔離登錄、稽核、節流）喚醒後再提交；請求列在
``tool_request`` 中本身即為 drain 可見的在飛工作，sleep policy 不會在
訓練中 cold-sleep 工具。

使用者明示關閉（broker ``explicit_stop_at``）後 suppression 窗內不喚醒；
``worker_admission_hold``／``regulation_active`` 期間不喚醒（§10.64 ⑤）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, time as clock_time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tasks.resource_governor_signal import (
    regulation_active,
    worker_admission_hold,
)

_logger = logging.getLogger("gptbridge.self_learning_driver")

FLOW_ID = "self-learning"
OWNER_TOOL_ID = "local-model"
CHANNEL_TOOL_ID = "xingcheng"
CYCLE_COMMAND = "xingcheng_self_learning_cycle"

_STATE_FILE = (
    Path(__file__).resolve().parents[2]
    / "runtime" / "state" / "self-learning-driver.json"
)
_POLICY_RELATIVE = Path("runtime") / "settings" / "self-learning.json"
_TOOL_STATE_RELATIVE = (
    Path("xingcheng") / "runtime" / "state" / "self-learning.json"
)

# 使用者明示關閉 local-model 後，排程驅動的喚醒在此窗內被抑制——
# 排程工作不應立即復活使用者剛關掉的行程。
_EXPLICIT_STOP_SUPPRESSION_S = 3600.0
# 喚醒失敗指數退避（對齊工具端 gpu_busy_backoff 的量級）。
_WAKE_BACKOFF_BASE_S = 900.0
_WAKE_BACKOFF_CAP_S = 7200.0
# 未消訖回應的請求上限——有未結請求時不再疊加新請求。
_MAX_OUTSTANDING = 4
_MAX_RESULT_LEDGER = 16


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _training_window_allowed(policy: dict[str, Any]) -> bool:
    """Mirror of ``self_learning.training_window_status`` (quiet hours).

    Pre-check only — parse failures defer to the tool's authoritative
    fail-closed gate instead of suppressing the wake here."""
    try:
        zone = ZoneInfo(str(policy.get("training_timezone") or "Asia/Taipei"))
        start = clock_time(
            *(int(p) for p in str(
                policy.get("quiet_hours_start") or "22:00"
            ).split(":", 1))
        )
        end = clock_time(
            *(int(p) for p in str(
                policy.get("quiet_hours_end") or "07:00"
            ).split(":", 1))
        )
        local = datetime.now(timezone.utc).astimezone(zone).time()
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return True
    quiet = (local >= start or local < end) if start > end else (
        start <= local < end
    )
    return not quiet


class SelfLearningDriver:
    """Drives one governed self-learning cycle request per scheduler tick."""

    def __init__(
        self,
        app: Any,
        toolbox_service: Any,
        *,
        project_root: Path | None = None,
    ) -> None:
        self.app = app
        self.toolbox = toolbox_service
        root = Path(
            project_root or getattr(app, "project_root", "")
        ).resolve()
        self._tool_root = root / "Standalone tools" / "local-model"
        self._registered = False
        self._outstanding: list[str] = []
        self._wake_streak = 0
        self._next_wake_at = 0.0
        self._last_decision = ""
        self._last_error = ""
        self._results: list[dict[str, Any]] = []
        self._last_write_fingerprint: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # lifecycle — the AutomationCore owns the schedule (§1.1)
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        core = getattr(self.app, "automation_core", None)
        if core is None:
            _logger.warning(
                "self-learning driver: no automation core — not starting "
                "(no private loop fallback)"
            )
            return {"status": "no-automation-core"}
        # Interval comes from the governed manifest (automation-flows.json);
        # a denial means kill switch / unlisted — never fall back.
        self._registered = core.register_flow(FLOW_ID, self.run_once)
        return {
            "status": "registered" if self._registered else "denied",
            "flow": FLOW_ID,
        }

    async def stop(self) -> None:
        core = getattr(self.app, "automation_core", None)
        if core is not None and self._registered:
            try:
                core.unregister(FLOW_ID)
            except Exception:
                pass
        self._registered = False

    # ------------------------------------------------------------------
    # tick
    # ------------------------------------------------------------------

    async def run_once(self) -> None:
        """One scheduler tick — never raises into the shared loop."""
        try:
            decision = await self._tick_inner()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — audit then isolate
            decision = "error"
            self._last_error = f"{type(error).__name__}: {error}"
            _logger.warning("self-learning driver tick failed: %s", error)
        self._last_decision = decision
        self._write_state()

    async def _tick_inner(self) -> str:
        await self._drain_results()

        policy = _read_json(self._tool_root / _POLICY_RELATIVE)
        if policy is None:
            # fail-closed: an unreadable policy must never trigger training.
            return "policy-unreadable"
        if policy.get("enabled") is not True:
            return "policy-disabled"

        not_due = self._not_due_reason(policy)
        if not_due is not None:
            return f"not-due:{not_due}"

        if self._outstanding:
            return "request-outstanding"

        if not await self._owner_active():
            wake = await self._wake_owner()
            if wake is not True:
                return wake

        return await self._submit_cycle()

    # ------------------------------------------------------------------
    # due pre-checks (mirror of the tool-side gates; authoritative copy
    # stays inside run_cycle — these only avoid pointless wakes)
    # ------------------------------------------------------------------

    def _not_due_reason(self, policy: dict[str, Any]) -> str | None:
        if not _training_window_allowed(policy):
            return "quiet-hours"
        state = _read_json(self._tool_root / _TOOL_STATE_RELATIVE) or {}
        now = datetime.now(timezone.utc)

        gap = int(policy.get("min_interval_s") or 0)
        last_run = _parse_iso(state.get("last_run_at"))
        if gap > 0 and last_run is not None:
            if (now - last_run).total_seconds() < gap:
                return "min-interval"

        cap = int(policy.get("max_cycles_per_day") or 0)
        counter = state.get("cycles_today")
        if (
            cap > 0
            and isinstance(counter, dict)
            and str(counter.get("date") or "") == now.strftime("%Y-%m-%d")
            and int(counter.get("count") or 0) >= cap
        ):
            return "daily-budget"

        until = _parse_iso(state.get("gpu_busy_until"))
        if until is not None and until > now:
            return "gpu-busy-backoff"

        limit = int(policy.get("max_consecutive_failures") or 0)
        if limit > 0 and int(
            state.get("consecutive_failures") or 0
        ) >= limit:
            return "failure-breaker"

        return None

    # ------------------------------------------------------------------
    # wake path (governed start; suppression gates)
    # ------------------------------------------------------------------

    async def _owner_active(self) -> bool:
        try:
            return bool(
                await self.toolbox.tool_process_active(OWNER_TOOL_ID)
            )
        except Exception as error:  # noqa: BLE001 — unknown = do not wake
            _logger.warning("owner liveness check failed: %s", error)
            return False

    async def _wake_owner(self) -> str | bool:
        if worker_admission_hold() or regulation_active():
            return "resource-hold"
        broker = getattr(self.app, "model_service_activation", None)
        stopped_at = float(
            getattr(broker, "_explicit_stop_at", 0.0) or 0.0
        )
        if (
            stopped_at > 0.0
            and time.time() - stopped_at < _EXPLICIT_STOP_SUPPRESSION_S
        ):
            return "explicit-stop-suppressed"
        now = time.monotonic()
        if now < self._next_wake_at:
            return "wake-backoff"
        try:
            result = await self.toolbox.start_tool(
                {
                    "tool_id": OWNER_TOOL_ID,
                    "background": True,
                    "request_id": f"self-learning-wake-{time.time_ns()}",
                }
            )
        except Exception as error:  # noqa: BLE001
            result = {"ok": False, "message": f"{type(error).__name__}: {error}"}
        if isinstance(result, dict) and result.get("ok") is True:
            self._wake_streak = 0
            return True
        self._wake_streak += 1
        delay = min(
            _WAKE_BACKOFF_CAP_S,
            _WAKE_BACKOFF_BASE_S * (2 ** (self._wake_streak - 1)),
        )
        self._next_wake_at = now + delay
        self._last_error = str(
            (result or {}).get("message")
            or (result or {}).get("error_code")
            or "start_tool failed"
        )
        _logger.warning(
            "self-learning owner wake failed (retry in %.0fs): %s",
            delay,
            self._last_error,
        )
        return "wake-failed"

    # ------------------------------------------------------------------
    # request submission + response draining
    # ------------------------------------------------------------------

    async def _submit_cycle(self) -> str:
        request_id = f"self-learning-{time.time_ns()}"
        try:
            result = await self.toolbox.request_tool_execution(
                {
                    "tool_id": OWNER_TOOL_ID,
                    "request_id": request_id,
                    "_governed_command": CYCLE_COMMAND,
                }
            )
        except Exception as error:  # noqa: BLE001
            self._last_error = f"{type(error).__name__}: {error}"
            return "queue-error"
        if not isinstance(result, dict) or result.get("ok") is not True:
            self._last_error = str(
                (result or {}).get("message")
                or (result or {}).get("error_code")
                or "queue rejected"
            )
            return "queue-failed"
        self._outstanding.append(request_id)
        return "queued"

    async def _drain_results(self) -> None:
        """Consume responses for outstanding requests (bounded ledger)."""
        if not self._outstanding:
            return
        sovereign = getattr(self.toolbox, "permission_sovereign", None)
        if sovereign is None:
            return
        remaining: list[str] = []
        for request_id in self._outstanding[-_MAX_OUTSTANDING:]:
            try:
                response = await asyncio.to_thread(
                    sovereign.tool_execution_response,
                    CHANNEL_TOOL_ID,
                    request_id,
                )
            except Exception:
                response = None
            status = (
                str(response.get("status") or "")
                if isinstance(response, dict)
                else ""
            )
            if status in ("completed", "failed", "cancelled"):
                body = response.get("response")
                entry: dict[str, Any] = {
                    "request_id": request_id,
                    "status": status,
                    "at": _iso_now(),
                }
                if isinstance(body, dict):
                    entry["ok"] = body.get("ok")
                    entry["action"] = body.get("action")
                    entry["reason"] = body.get("reason") or body.get(
                        "error"
                    )
                self._results.append(entry)
            else:
                remaining.append(request_id)
        self._outstanding = remaining
        self._results = self._results[-_MAX_RESULT_LEDGER:]

    # ------------------------------------------------------------------
    # observability
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "registered": self._registered,
            "last_decision": self._last_decision,
            "last_error": self._last_error or None,
            "outstanding": list(self._outstanding),
            "wake_streak": self._wake_streak,
            "next_wake_in_s": max(
                0.0, round(self._next_wake_at - time.monotonic(), 1)
            ),
            "results": list(self._results),
        }

    def _write_state(self) -> None:
        payload = {"updated_at": _iso_now()}
        payload.update(self.status())
        fingerprint = {
            k: payload[k]
            for k in payload
            if k not in {"updated_at", "next_wake_in_s"}
        }
        if fingerprint == self._last_write_fingerprint:
            return
        self._last_write_fingerprint = fingerprint
        temporary = _STATE_FILE.with_name(
            _STATE_FILE.name + f".{os.getpid()}.tmp"
        )
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, _STATE_FILE)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass


__all__ = ["SelfLearningDriver", "FLOW_ID", "CYCLE_COMMAND", "OWNER_TOOL_ID"]
