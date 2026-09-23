"""Audit scheduler — periodic governance audit execution.

負責：
1. 定期自動執行治理審計
2. 審計結果記錄與告警
3. 審計歷史管理
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger("gptbridge.permission_automation")


class AuditScheduler:
    """審計排程器。

    負責：
    1. 定期自動執行治理審計
    2. 審計結果記錄與告警
    3. 審計歷史管理
    """

    def __init__(self, project_root: Path, interval: float = 3600.0) -> None:
        self.project_root = project_root
        self.interval = interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._audit_history: list[dict] = []

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="audit-scheduler")
        _logger.info("AuditScheduler started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def run_once(self) -> None:
        """單次治理審計——供 automation core 外部驅動（§1.1 自動化集中）。"""
        await self._run_audit()

    async def _run_loop(self) -> None:
        while self._running:
            # Fallback private loop only runs with no automation core;
            # bound the tick so a hung check cannot freeze it silently
            # (same contract the core's wait_for wrapper gives).
            tick_deadline = max(30.0, min(600.0, float(self.interval) * 5))
            try:
                await asyncio.wait_for(self._run_audit(), timeout=tick_deadline)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                _logger.warning(
                    "AuditScheduler tick exceeded %.0fs deadline", tick_deadline
                )
            except Exception as e:
                _logger.error(f"Scheduled audit failed: {e}")
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break

    async def _run_audit(self) -> dict[str, Any]:
        """執行治理審計。"""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "governance_rule.execution.audit"],
                cwd=str(Path(__file__).resolve().parents[3]),
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            output = (result.stdout or "") + (result.stderr or "")
            passed = "[PASS]" in output and result.returncode == 0

            audit_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "passed": passed,
                "output": output[:2000],
                "return_code": result.returncode,
            }
            self._audit_history.append(audit_record)

            if not passed:
                _logger.error(f"Scheduled governance audit FAILED: {output[:500]}")
            else:
                _logger.info("Scheduled governance audit PASSED")

            return audit_record

        except subprocess.TimeoutExpired:
            _logger.error("Scheduled audit timed out")
            return {"passed": False, "error": "timeout"}
        except Exception as e:
            _logger.error(f"Scheduled audit error: {e}")
            return {"passed": False, "error": str(e)}

    def get_audit_history(self, count: int = 50) -> list[dict]:
        return self._audit_history[-count:]


__all__ = ["AuditScheduler"]
