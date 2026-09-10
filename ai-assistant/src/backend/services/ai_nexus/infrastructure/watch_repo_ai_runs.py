from __future__ import annotations

import uuid
from typing import Any

from .watch_repo_helpers import utc_now


class WatchRepoAIRunsMixin:
    """AI run lifecycle tracking: creation, updates, and crash recovery."""

    def add_ai_run(
        self,
        role: str,
        provider: str,
        prompt: str,
        status: str,
        content: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        with self._state_lock:
            return self._add_ai_run_locked(
                role,
                provider,
                prompt,
                status,
                content=content,
                error=error,
            )

    def _add_ai_run_locked(
        self,
        role: str,
        provider: str,
        prompt: str,
        status: str,
        content: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        state = self.load_state()
        runs = state.setdefault("ai_runs", [])
        run = {
            "run_id": uuid.uuid4().hex[:16],
            "role": role,
            "provider": provider,
            "prompt": prompt,
            "status": status,
            "content": content,
            "error": error,
            "created_at": utc_now(),
        }
        runs.insert(0, run)
        self.save_state(state)
        return run

    def update_ai_run(
        self,
        run_id: str,
        *,
        status: str,
        content: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        with self._state_lock:
            return self._update_ai_run_locked(
                run_id,
                status=status,
                content=content,
                error=error,
            )

    def _update_ai_run_locked(
        self,
        run_id: str,
        *,
        status: str,
        content: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        state = self.load_state()
        runs = state.setdefault("ai_runs", [])
        for run in runs:
            if not isinstance(run, dict) or str(run.get("run_id") or "") != run_id:
                continue
            run["status"] = status
            run["content"] = content
            run["error"] = error
            run["finished_at"] = utc_now() if status in {"completed", "failed"} else ""
            self.save_state(state)
            return dict(run)
        return self._add_ai_run_locked(
            role="investment_risk_monitor",
            provider="投資管家",
            prompt="投資管家：經 AI 通道執行背景報價與風險監測",
            status=status,
            content=content,
            error=error,
        )

    def recover_interrupted_ai_runs(self) -> int:
        with self._state_lock:
            return self._recover_interrupted_ai_runs_locked()

    def _recover_interrupted_ai_runs_locked(self) -> int:
        state = self.load_state()
        runs = state.setdefault("ai_runs", [])
        recovered = 0
        finished_at = utc_now()
        for run in runs:
            if not isinstance(run, dict) or run.get("status") != "running":
                continue
            run["status"] = "failed"
            run["content"] = ""
            run["error"] = "背景工作因服務重啟而中斷，系統已自動重新排程。"
            run["finished_at"] = finished_at
            recovered += 1
        if recovered:
            self.save_state(state)
        return recovered

    def latest_ai_content(self, role: str) -> str:
        for run in self.load_state().get("ai_runs", []):
            if run.get("role") == role and run.get("content"):
                return str(run["content"])
        return ""
