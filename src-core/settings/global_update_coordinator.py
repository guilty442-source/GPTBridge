from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GlobalUpdateRule:
    prefix: str
    strategy: str
    scope: str
    label: str
    reason: str


class GlobalUpdateCoordinator:
    """Classify project changes into the safest global update action."""

    ACTION_PRIORITY: dict[str, int] = {
        "none": 0,
        "renderer_hmr": 1,
        "data_reload": 2,
        "window_reload": 3,
        "backend_restart": 4,
        "app_restart": 5,
    }

    RULES: tuple[GlobalUpdateRule, ...] = (
        GlobalUpdateRule(
            "src-ui/renderer/",
            "renderer_hmr",
            "renderer",
            "介面熱更新",
            "React 介面、CSS 或語系可由 Vite HMR 即時套用。",
        ),
        GlobalUpdateRule(
            "runtime/governance/",
            "data_reload",
            "data",
            "資料重新載入",
            "治理規則資料變更需透過 IPC 重新讀取，全域立即生效。",
        ),
        GlobalUpdateRule(
            "config.json",
            "data_reload",
            "data",
            "設定重新載入",
            "設定資料變更可重新讀取，不需要重啟整個應用程式。",
        ),
        GlobalUpdateRule(
            "src-core/",
            "backend_restart",
            "backend",
            "後端重啟",
            "Python 後端程式碼變更需重啟後端程序。",
        ),
        GlobalUpdateRule(
            "src-ui/main/",
            "app_restart",
            "electron",
            "應用程式重啟",
            "Electron main/preload 變更需重啟應用程式。",
        ),
        GlobalUpdateRule(
            "package.json",
            "app_restart",
            "dependency",
            "應用程式重啟",
            "套件或啟動腳本變更需重啟應用程式。",
        ),
        GlobalUpdateRule(
            "package-lock.json",
            "app_restart",
            "dependency",
            "應用程式重啟",
            "依賴鎖定檔變更需重啟應用程式。",
        ),
        GlobalUpdateRule(
            "tsconfig.json",
            "app_restart",
            "build",
            "應用程式重啟",
            "編譯設定變更需重啟相關流程。",
        ),
        GlobalUpdateRule(
            "vite.config.ts",
            "app_restart",
            "build",
            "應用程式重啟",
            "Vite 設定變更需重啟開發伺服器或應用程式。",
        ),
        GlobalUpdateRule(
            "vite.main.config.ts",
            "app_restart",
            "build",
            "應用程式重啟",
            "Electron main 建置設定變更需重啟應用程式。",
        ),
    )

    WATCH_TARGETS: tuple[str, ...] = (
        "src-ui/renderer",
        "src-ui/main",
        "src-core",
        "runtime/governance",
        "config.json",
        "package.json",
        "package-lock.json",
        "tsconfig.json",
        "vite.config.ts",
        "vite.main.config.ts",
    )

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self._baseline = self.build_snapshot()

    def build_snapshot(self) -> dict[str, int]:
        snapshot: dict[str, int] = {}
        for path in self._iter_watch_files():
            try:
                relative = path.relative_to(self.project_root).as_posix()
                snapshot[relative] = path.stat().st_mtime_ns
            except (OSError, ValueError):
                continue
        return snapshot

    def inspect(self) -> dict[str, Any]:
        current_snapshot = self.build_snapshot()
        changed_paths = self._collect_changed_paths(current_snapshot)
        changes = [self.classify(path) for path in changed_paths]
        highest_strategy = self._highest_strategy(changes)
        counts = self._count_by_strategy(changes)
        message = self._message(highest_strategy, len(changes))

        return {
            "changed": len(changes) > 0,
            "changed_count": len(changes),
            "highest_strategy": highest_strategy,
            "action_label": self._action_label(highest_strategy),
            "message": message,
            "counts": counts,
            "changes": changes[:40],
            "generated_at": self._now_marker(current_snapshot),
        }

    def mark_applied(self) -> dict[str, Any]:
        self._baseline = self.build_snapshot()
        return {
            "ok": True,
            "message": "全域更新基準已刷新。",
            "global_update_plan": self.inspect(),
        }

    def classify(self, relative_path: str) -> dict[str, str]:
        normalized = relative_path.replace("\\", "/")
        for rule in self.RULES:
            if normalized == rule.prefix.rstrip("/") or normalized.startswith(rule.prefix):
                return {
                    "path": normalized,
                    "strategy": rule.strategy,
                    "scope": rule.scope,
                    "label": rule.label,
                    "reason": rule.reason,
                }

        return {
            "path": normalized,
            "strategy": "window_reload",
            "scope": "unknown",
            "label": "視窗重載",
            "reason": "未知變更類型，採用保守視窗重載。",
        }

    def _iter_watch_files(self) -> list[Path]:
        files: list[Path] = []
        for relative in self.WATCH_TARGETS:
            target = (self.project_root / relative).resolve()
            try:
                target.relative_to(self.project_root)
            except ValueError:
                continue
            if not target.exists():
                continue
            if target.is_file():
                files.append(target)
                continue
            for file in target.rglob("*"):
                if file.is_file() and not self._is_ignored(file):
                    files.append(file)
        return files

    def _collect_changed_paths(self, current_snapshot: dict[str, int]) -> list[str]:
        changed: set[str] = set()
        for relative, current_mtime in current_snapshot.items():
            previous_mtime = self._baseline.get(relative)
            if previous_mtime is None or previous_mtime != current_mtime:
                changed.add(relative)

        for relative in self._baseline:
            if relative not in current_snapshot:
                changed.add(relative)

        return sorted(changed)

    @staticmethod
    def _is_ignored(path: Path) -> bool:
        ignored_parts = {
            "__pycache__",
            ".pytest_cache",
            "node_modules",
            "dist-ui",
            "release",
            ".git",
        }
        return any(part in ignored_parts for part in path.parts)

    def _highest_strategy(self, changes: list[dict[str, str]]) -> str:
        highest = "none"
        highest_score = 0
        for change in changes:
            strategy = change.get("strategy", "none")
            score = self.ACTION_PRIORITY.get(strategy, 0)
            if score > highest_score:
                highest = strategy
                highest_score = score
        return highest

    @staticmethod
    def _count_by_strategy(changes: list[dict[str, str]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for change in changes:
            strategy = change.get("strategy", "none")
            counts[strategy] = counts.get(strategy, 0) + 1
        return counts

    @staticmethod
    def _action_label(strategy: str) -> str:
        return {
            "none": "無需套用",
            "renderer_hmr": "介面熱更新",
            "data_reload": "資料重新載入",
            "window_reload": "視窗重載",
            "backend_restart": "後端重啟",
            "app_restart": "應用程式重啟",
        }.get(strategy, "視窗重載")

    def _message(self, strategy: str, change_count: int) -> str:
        if change_count == 0:
            return "目前沒有待套用的全域更新。"
        return f"偵測到 {change_count} 項變更，建議套用：{self._action_label(strategy)}。"

    @staticmethod
    def _now_marker(snapshot: dict[str, int]) -> int:
        if not snapshot:
            return 0
        return max(snapshot.values())
