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
RETENTION_FLOW_ID = "retention"
OWNER_TOOL_ID = "local-model"
CHANNEL_TOOL_ID = "xingcheng"
CYCLE_COMMAND = "xingcheng_self_learning_cycle"
RETENTION_COMMAND = "xingcheng_retention_sweep"

_STATE_FILE = (
    Path(__file__).resolve().parents[2]
    / "runtime" / "state" / "self-learning-driver.json"
)
_POLICY_RELATIVE = Path("runtime") / "settings" / "self-learning.json"
_RETENTION_POLICY_RELATIVE = Path("runtime") / "settings" / "retention.json"
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
# 在飛請求的最大年齡：train_time_budget（政策 7200s）＋排隊/喚醒裕度。
# 超齡視為遺失（列被頻外清除、工具消亡）——不永久阻斷後續循環。
_OUTSTANDING_MAX_AGE_S = 6 * 3600.0
# 請求已提交但工具未被喚醒的認領超時：超過則允許再次嘗試喚醒，
# 讓重啟的 runtime 認領仍 queued 的列（lease 回收也依賴工具在跑）。
_QUEUED_WAKE_RETRY_S = 600.0


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
        state_path: Path | None = None,
    ) -> None:
        self.app = app
        self.toolbox = toolbox_service
        root = Path(
            project_root or getattr(app, "project_root", "")
        ).resolve()
        self._tool_root = root / "Standalone tools" / "local-model"
        self._state_path = Path(state_path) if state_path else _STATE_FILE
        self._registered = False
        self._retention_registered = False
        self._last_retention_decision = ""
        # request_id -> monotonic submit time；超齡未消訖視為遺失。
        self._outstanding: dict[str, float] = {}
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
        # §10.67：retention 是獨立 periodic flow——self-learning 停用時
        # 保留清理不得跟著死亡（它唯一的另一觸發點是 run_cycle 結尾）。
        self._retention_registered = core.register_flow(
            RETENTION_FLOW_ID, self.run_retention_once
        )
        return {
            "status": "registered" if self._registered else "denied",
            "flow": FLOW_ID,
            "retention_flow": (
                "registered" if self._retention_registered else "denied"
            ),
        }

    async def stop(self) -> None:
        core = getattr(self.app, "automation_core", None)
        if core is not None:
            for flow_id, flag in (
                (FLOW_ID, "_registered"),
                (RETENTION_FLOW_ID, "_retention_registered"),
            ):
                if getattr(self, flag):
                    try:
                        core.unregister(flow_id)
                    except Exception:
                        pass
                setattr(self, flag, False)

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
            # 請求在飛不重複提交。但列還停在 queued、工具又冷停時，
            # lease/claim 無人執行——允許過認領超時後再次喚醒，讓
            # 重啟的 runtime 消化既有 queued 請求（suppression 閘
            # 仍由 _wake_owner 判定）。
            oldest = min(self._outstanding.values())
            if (
                time.monotonic() - oldest >= _QUEUED_WAKE_RETRY_S
                and not await self._owner_active()
            ):
                await self._wake_owner()
            return "request-outstanding"

        if not await self._owner_active():
            wake = await self._wake_owner()
            if wake is not True:
                return wake

        return await self._submit_cycle(CYCLE_COMMAND)

    # ------------------------------------------------------------------
    # retention tick — §10.67 機會式執行：只在工具已在跑時提交，
    # 絕不為了修剪檔案喚醒冷停的工具（檔案不急迫，喚醒成本高）。
    # ------------------------------------------------------------------

    async def run_retention_once(self) -> None:
        """Retention flow tick — never raises into the shared loop."""
        try:
            decision = await self._retention_tick_inner()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — audit then isolate
            decision = "error"
            self._last_error = f"{type(error).__name__}: {error}"
            _logger.warning("retention driver tick failed: %s", error)
        self._last_retention_decision = decision
        self._write_state()

    async def _retention_tick_inner(self) -> str:
        await self._drain_results()

        policy = _read_json(self._tool_root / _RETENTION_POLICY_RELATIVE)
        if policy is None:
            # fail-closed：政策不可讀時不觸發刪除。
            return "policy-unreadable"
        if policy.get("enabled") is not True:
            return "policy-disabled"

        if self._outstanding:
            return "request-outstanding"

        if not await self._owner_active():
            # 不喚醒——retention 順路執行即可；工具冷停時檔案修剪可等。
            return "owner-cold-deferred"

        return await self._submit_cycle(RETENTION_COMMAND)

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

    async def _submit_cycle(self, command: str) -> str:
        request_id = f"{command}-{time.time_ns()}"
        try:
            result = await self.toolbox.request_tool_execution(
                {
                    "tool_id": OWNER_TOOL_ID,
                    "request_id": request_id,
                    "_governed_command": command,
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
        if len(self._outstanding) >= _MAX_OUTSTANDING:
            return "outstanding-limit"
        self._outstanding[request_id] = time.monotonic()
        return "queued"

    async def _drain_results(self) -> None:
        """Consume responses for outstanding requests (bounded ledger)."""
        if not self._outstanding:
            return
        sovereign = getattr(self.toolbox, "permission_sovereign", None)
        if sovereign is None:
            return
        now = time.monotonic()
        for request_id, submitted_at in list(self._outstanding.items()):
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
                del self._outstanding[request_id]
            elif now - submitted_at >= _OUTSTANDING_MAX_AGE_S:
                # 回應永遠不會到達（列遺失/工具消亡）——記為 lost 並放行
                # 下一循環；否則一個幽靈請求會永久凍結排程。
                self._results.append(
                    {
                        "request_id": request_id,
                        "status": "lost",
                        "at": _iso_now(),
                        "reason": "response never arrived "
                        f"(age>{int(_OUTSTANDING_MAX_AGE_S)}s)",
                    }
                )
                _logger.warning(
                    "self-learning request %s expired without response",
                    request_id,
                )
                del self._outstanding[request_id]
        self._results = self._results[-_MAX_RESULT_LEDGER:]

    # ------------------------------------------------------------------
    # observability
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "registered": self._registered,
            "retention_registered": self._retention_registered,
            "last_decision": self._last_decision,
            "last_retention_decision": self._last_retention_decision,
            "last_error": self._last_error or None,
            "outstanding": [
                {
                    "request_id": rid,
                    "age_s": round(time.monotonic() - submitted, 1),
                }
                for rid, submitted in self._outstanding.items()
            ],
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
        state_path = self._state_path
        temporary = state_path.with_name(
            state_path.name + f".{os.getpid()}.tmp"
        )
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, state_path)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass


__all__ = ["SelfLearningDriver", "FLOW_ID", "CYCLE_COMMAND", "OWNER_TOOL_ID"]
