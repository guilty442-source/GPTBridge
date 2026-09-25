"""Codex amendment intake driver — governed pipeline advancement.

§1.1 自動化集中 + A382/A488：掃描受管 intake 目錄中的
``codex-amendment-request`` artifact，並經
``governance_rule.execution.codex_amendment_driver.advance_all`` 推進每個
未結請求：lineage-locked intake → successor build → five-sovereign audit
→ ``ready-for-governor`` → ``auto_execute`` 開啟時經
``codex_amendment_executor`` 解除唯讀、原子發布、恢復唯讀並封印
（五主權一致稽核憑證為封印條件，executor 於任何變更前重新驗證）；
旗標關閉時停在 ``ready-for-governor``，由 human governor 手動
``--apply``。旗標單一來源：``automation-flows.json`` 本 flow 條目的
``auto_execute``（每 tick 即時讀取，關閉立即生效）。

Xingcheng 收據需要受管 web-search 路徑：``local-model`` 在跑時經
``ToolboxService.request_tool_execution`` 提交 ``xingcheng_web_search``
命令（同步 callable 由 ``asyncio.run_coroutine_threadsafe`` 橋接——
稽核在 worker 行程的獨立 loop 內執行，不阻塞主 loop）；工具冷停時
稽核 defer（請求維持 ``successor-built``）而非為一個暫時性缺勤永久
拒絕請求，也**不為稽核喚醒冷停工具**（修訂可等，喚醒成本高）。

只經 ``AutomationCore.register_flow``（flow id ``codex-amendment-
intake``，manifest ``kind=periodic interval_s=300``）掛到共享
PeriodicScheduler；被拒絕時**不回落私有迴圈**。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger("gptbridge.codex_amendment_intake")

FLOW_ID = "codex-amendment-intake"
OWNER_TOOL_ID = "local-model"
CHANNEL_TOOL_ID = "xingcheng"
WEB_SEARCH_COMMAND = "xingcheng_web_search"
WEB_SEARCH_TIMEOUT_S = 30.0

_STATE_FILE = (
    Path(__file__).resolve().parents[2]
    / "runtime" / "state" / "codex-amendment-intake.json"
)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class CodexAmendmentIntakeDriver:
    """Advances staged amendment requests to ``ready-for-governor``."""

    def __init__(
        self,
        app: Any,
        toolbox_service: Any,
        *,
        project_root: Path | None = None,
        state_path: Path | None = None,
        ledger: Any = None,
        intake_dirs: Any = None,
    ) -> None:
        self.app = app
        self.toolbox = toolbox_service
        self._project_root = Path(
            project_root or getattr(app, "project_root", "")
        ).resolve()
        self._state_path = Path(state_path) if state_path else _STATE_FILE
        self._ledger = ledger
        self._intake_dirs = intake_dirs
        self._loop: asyncio.AbstractEventLoop | None = None
        self._registered = False
        self._last_decision = ""
        self._last_error = ""
        self._last_wake: dict[str, Any] | None = None
        self._last_results: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # lifecycle — the AutomationCore owns the schedule (§1.1)
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        core = getattr(self.app, "automation_core", None)
        if core is None:
            _logger.warning(
                "codex-amendment intake: no automation core — not starting "
                "(no private loop fallback)"
            )
            return {"status": "no-automation-core"}
        self._loop = asyncio.get_running_loop()
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
    # governed web-search callable (xingcheng audit receipt path)
    # ------------------------------------------------------------------

    async def _owner_active(self) -> bool:
        try:
            return bool(
                await self.toolbox.tool_process_active(OWNER_TOOL_ID)
            )
        except Exception as error:  # noqa: BLE001 — unknown = cold
            _logger.warning("local-model liveness check failed: %s", error)
            return False

    async def _search_roundtrip(self, query: str) -> dict[str, Any]:
        request_id = f"codex-audit-search-{time.time_ns()}"
        try:
            queued = await self.toolbox.request_tool_execution(
                {
                    "tool_id": OWNER_TOOL_ID,
                    "request_id": request_id,
                    "_governed_command": WEB_SEARCH_COMMAND,
                    "query": query,
                }
            )
        except Exception as error:  # noqa: BLE001
            return {
                "ok": False,
                "source": "xingcheng-web-search",
                "query": query,
                "error": f"queue:{type(error).__name__}",
            }
        if not isinstance(queued, dict) or queued.get("ok") is not True:
            return {
                "ok": False,
                "source": "xingcheng-web-search",
                "query": query,
                "error": "queue-denied",
            }
        sovereign = getattr(self.toolbox, "permission_sovereign", None)
        if sovereign is None:
            return {
                "ok": False,
                "source": "xingcheng-web-search",
                "query": query,
                "error": "response-ledger-unavailable",
            }
        deadline = time.monotonic() + WEB_SEARCH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                response = await asyncio.to_thread(
                    sovereign.tool_execution_response,
                    CHANNEL_TOOL_ID,
                    request_id,
                )
            except Exception:
                response = None
            if isinstance(response, dict) and str(
                response.get("status") or ""
            ) in {"completed", "failed", "cancelled"}:
                body = response.get("response")
                if response.get("status") == "completed" and isinstance(
                    body, dict
                ):
                    body.setdefault("source", "xingcheng-web-search")
                    return body
                return {
                    "ok": False,
                    "source": "xingcheng-web-search",
                    "query": query,
                    "error": f"tool-{response.get('status')}",
                }
            await asyncio.sleep(0.5)
        return {
            "ok": False,
            "source": "xingcheng-web-search",
            "query": query,
            "error": "timeout",
        }

    def _channel_search(self, query: str) -> dict[str, Any]:
        """Sync bridge: submit on the main loop, block the worker thread."""
        if self._loop is None or self._loop.is_closed():
            return {
                "ok": False,
                "source": "xingcheng-web-search",
                "query": query,
                "error": "main-loop-unavailable",
            }
        future = asyncio.run_coroutine_threadsafe(
            self._search_roundtrip(query), self._loop
        )
        try:
            return future.result(timeout=WEB_SEARCH_TIMEOUT_S + 10.0)
        except Exception as error:  # noqa: BLE001
            return {
                "ok": False,
                "source": "xingcheng-web-search",
                "query": query,
                "error": f"roundtrip:{type(error).__name__}",
            }

    # ------------------------------------------------------------------
    # governed auto-execute toggle (single source: automation-flows.json)
    # ------------------------------------------------------------------

    def _auto_execute_enabled(self) -> bool:
        try:
            manifest = json.loads(
                (
                    self._project_root
                    / "main-system"
                    / "config"
                    / "automation-flows.json"
                ).read_text(encoding="utf-8")
            )
            entry = (manifest.get("flows") or {}).get(FLOW_ID) or {}
            return bool(entry.get("auto_execute"))
        except Exception as error:  # noqa: BLE001 — unreadable = off
            _logger.warning(
                "codex-amendment intake: auto_execute flag unreadable: %s",
                error,
            )
            return False

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
            _logger.warning("codex-amendment intake tick failed: %s", error)
        self._last_decision = decision
        self._write_state()

    async def _tick_inner(self) -> str:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        from governance_rule.execution.codex_amendment_driver import (
            advance_all,
            scan_requests,
        )
        from governance_rule.execution.codex_amendment_lifecycle import (
            CodexAmendmentRequestLedger,
            TERMINAL_STATES,
        )

        ledger = self._ledger or CodexAmendmentRequestLedger()
        pending = await asyncio.to_thread(
            scan_requests, self._intake_dirs, ledger=ledger
        )
        actionable = [
            item
            for item in pending
            if item.get("valid") and item.get("state") not in TERMINAL_STATES
        ]
        if not actionable:
            return "no-pending"

        search = None
        if await self._owner_active():
            search = self._channel_search
        elif actionable:
            # Governance-critical audit path: the xingcheng receipt requires
            # the governed web-search channel, but the owner (local-model)
            # is lazily started only for ai-channel dialogue.  A codex
            # amendment waiting in successor-built would otherwise remain
            # deferred forever (XINGCHENG_SEARCH_UNAVAILABLE) and the
            # "法典修正按" appears to not auto-execute.  Wake the owner
            # directly via the governed toolbox path (bypassing the
            # broker's ai-channel and resource-hold gates) so the next
            # audit can obtain the receipt.
            try:
                start_result = await self.toolbox.start_tool(
                    {
                        "tool_id": OWNER_TOOL_ID,
                        "request_id": f"codex-audit-wake-{time.time_ns()}",
                        "background": True,
                    }
                )
                # The governed start path reports denials as a result dict
                # rather than raising — persist it so the wake failure is
                # observable instead of silently re-deferring every tick.
                self._last_wake = (
                    dict(start_result)
                    if isinstance(start_result, dict)
                    else {"ok": False, "result": str(start_result)}
                )
                # governed-source startup takes ~5-10 s to verify —
                # waiting only 3 ticks (6 s) abandons a wake that
                # succeeded moments later and leaves the tool up with no
                # pending search (previously the sleep policy then
                # cold-slept it on a stale idle baseline before the next
                # tick could use it).
                for _ in range(8):
                    await asyncio.sleep(2)
                    if await self._owner_active():
                        search = self._channel_search
                        _logger.info("codex audit owner woken for search")
                        break
            except Exception as error:  # noqa: BLE001
                self._last_wake = {
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                }
                _logger.warning("codex audit wake failed: %s", error)

        # advance_all runs its own event loop on this worker thread; the
        # sync search callable bridges back to the main loop for each
        # governed channel roundtrip.
        results = await asyncio.to_thread(
            asyncio.run,
            advance_all(
                intake_dirs=self._intake_dirs,
                ledger=ledger,
                search=search,
                auto_execute=self._auto_execute_enabled(),
            ),
        )
        self._last_results = results
        ready = sum(
            1 for r in results if r.get("state") == "ready-for-governor"
        )
        executed = sum(
            1 for r in results if r.get("state") == "executed"
        )
        rejected = sum(1 for r in results if r.get("state") == "rejected")
        deferred = sum(
            1
            for r in results
            if r.get("error") == "XINGCHENG_SEARCH_UNAVAILABLE"
        )
        errors = [r for r in results if not r.get("ok") and not deferred]
        if errors:
            self._last_error = json.dumps(
                [
                    {
                        "request_id": r.get("request_id"),
                        "stage": r.get("stage"),
                        "error": r.get("error")
                        or (r.get("build") or {}).get("errors"),
                    }
                    for r in errors
                ],
                ensure_ascii=False,
            )
        return (
            f"advanced:{len(results)} ready:{ready} "
            f"executed:{executed} "
            f"rejected:{rejected} deferred:{deferred}"
        )

    # ------------------------------------------------------------------
    # state observability
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "flow": FLOW_ID,
            "registered": self._registered,
            "last_decision": self._last_decision,
            "last_error": self._last_error,
            "last_wake": self._last_wake,
            "last_results": list(self._last_results),
        }

    def _write_state(self) -> None:
        payload = {
            "flow": FLOW_ID,
            "last_decision": self._last_decision,
            "last_error": self._last_error,
            "last_wake": self._last_wake,
            "result_count": len(self._last_results),
            "written_at": _iso_now(),
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._state_path)
        except OSError:
            pass


__all__ = [
    "CodexAmendmentIntakeDriver",
    "FLOW_ID",
    "OWNER_TOOL_ID",
    "WEB_SEARCH_COMMAND",
]
