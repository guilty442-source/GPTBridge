from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from settings.update_repository import UpdateRepository


@dataclass(frozen=True)
class GlobalUpdateRule:
    prefix: str
    strategy: str
    scope: str
    label: str
    reason: str


class GlobalUpdateCoordinator:
    """Classify project changes and restart only the affected boundary."""

    ACTION_PRIORITY = {
        "none": 0,
        "renderer_hmr": 1,
        "data_reload": 2,
        "window_reload": 3,
        "tool_restart": 4,
        "backend_restart": 4,
        "app_restart": 5,
    }

    RULES = (
        GlobalUpdateRule(
            "src-ui/renderer/", "renderer_hmr", "renderer", "前端熱更新",
            "React 與 CSS 變更可透過前端熱更新套用。",
        ),
        GlobalUpdateRule(
            "runtime/governance/", "data_reload", "data", "重新載入治理資料",
            "治理資料變更不需要重啟程序。",
        ),
        GlobalUpdateRule(
            "config.json", "data_reload", "data", "重新載入設定",
            "設定資料可在連線後重新載入。",
        ),
        GlobalUpdateRule(
            "platform_tools/", "tool_restart", "independent_tool",
            "重新啟動受影響工具", "獨立工具變更只重啟該工具，不影響其他程序。",
        ),
        GlobalUpdateRule(
            "src-core/", "backend_restart", "backend", "重新啟動後端",
            "後端程式碼變更需要重新啟動主後端。",
        ),
        GlobalUpdateRule(
            "src-ui/main/", "app_restart", "electron", "重新啟動主程式",
            "Electron 主程序或 preload 變更需要重啟主程式。",
        ),
        GlobalUpdateRule(
            "package.json", "app_restart", "dependency", "重新啟動主程式",
            "產品或相依版本變更需要重新啟動。",
        ),
        GlobalUpdateRule(
            "package-lock.json", "app_restart", "dependency", "重新啟動主程式",
            "相依鎖定資料變更需要重新啟動。",
        ),
        GlobalUpdateRule(
            "tsconfig.json", "app_restart", "build", "重新啟動主程式",
            "建置設定變更需要重新建置與啟動。",
        ),
        GlobalUpdateRule(
            "vite.config.ts", "app_restart", "build", "重新啟動主程式",
            "前端建置設定變更需要重新建置。",
        ),
        GlobalUpdateRule(
            "vite.main.config.ts", "app_restart", "build", "重新啟動主程式",
            "主程序建置設定變更需要重新建置。",
        ),
    )

    WATCH_TARGETS = (
        "src-ui/renderer", "src-ui/main", "src-core", "platform_tools",
        "runtime/governance", "config.json", "package.json", "package-lock.json",
        "tsconfig.json", "vite.config.ts", "vite.main.config.ts",
    )

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.repository = UpdateRepository(self.project_root)
        stored = self.repository.load_snapshot()
        current = self.build_snapshot()
        self._baseline = stored or current
        if not stored:
            self.repository.replace_snapshot(current)

    def build_snapshot(self) -> dict[str, int]:
        snapshot: dict[str, int] = {}
        for path in self._iter_watch_files():
            try:
                snapshot[path.relative_to(self.project_root).as_posix()] = path.stat().st_mtime_ns
            except (OSError, ValueError):
                continue
        return snapshot

    def inspect(self) -> dict[str, Any]:
        current = self.build_snapshot()
        changes = [self.classify(path) for path in self._collect_changed_paths(current)]
        strategy = self._highest_strategy(changes)
        return {
            "changed": bool(changes),
            "changed_count": len(changes),
            "highest_strategy": strategy,
            "action_label": self._action_label(strategy),
            "message": self._message(strategy, len(changes)),
            "counts": self._count_by_strategy(changes),
            "changes": changes[:40],
            "generated_at": self._now_marker(current),
        }

    def mark_applied(self) -> dict[str, Any]:
        self._baseline = self.build_snapshot()
        self.repository.replace_snapshot(self._baseline)
        return {
            "ok": True,
            "message": "更新基準已記錄。",
            "global_update_plan": {
                "changed": False, "changed_count": 0, "highest_strategy": "none",
                "changes": [], "generated_at": self._now_marker(self._baseline),
            },
        }

    def classify(self, relative_path: str) -> dict[str, str]:
        normalized = relative_path.replace("\\", "/")
        for rule in self.RULES:
            if normalized == rule.prefix.rstrip("/") or normalized.startswith(rule.prefix):
                return {
                    "path": normalized, "strategy": rule.strategy, "scope": rule.scope,
                    "label": rule.label, "reason": rule.reason,
                }
        return {
            "path": normalized, "strategy": "window_reload", "scope": "unknown",
            "label": "重新載入視窗", "reason": "未分類變更採用安全的視窗重新載入策略。",
        }

    def _iter_watch_files(self) -> list[Path]:
        files: list[Path] = []
        ignored = self._ignored_directory_names()
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
            for current_root, directories, filenames in os.walk(
                target, topdown=True, followlinks=False
            ):
                current = Path(current_root)
                directories[:] = [
                    name for name in directories
                    if name not in ignored and not (current / name).is_symlink()
                ]
                for filename in filenames:
                    path = current / filename
                    if not path.is_symlink() and not self._is_ignored(path):
                        files.append(path)
        return files

    def _collect_changed_paths(self, current: dict[str, int]) -> list[str]:
        changed = {
            relative for relative, mtime in current.items()
            if self._baseline.get(relative) != mtime
        }
        changed.update(relative for relative in self._baseline if relative not in current)
        return sorted(changed)

    @staticmethod
    def _ignored_directory_names() -> frozenset[str]:
        return frozenset({
            "__pycache__", ".pytest_cache", "node_modules", "dist-ui", "release",
            "runtime", "dist", "build", ".git", "browser-profile",
            "browser-profiles", "edge-profile",
        })

    @classmethod
    def _is_ignored(cls, path: Path) -> bool:
        return any(part in cls._ignored_directory_names() for part in path.parts)

    def _highest_strategy(self, changes: list[dict[str, str]]) -> str:
        return max(
            (change.get("strategy", "none") for change in changes),
            key=lambda strategy: self.ACTION_PRIORITY.get(strategy, 0),
            default="none",
        )

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
            "none": "無需更新", "renderer_hmr": "套用前端熱更新",
            "data_reload": "重新載入資料", "window_reload": "重新載入視窗",
            "tool_restart": "重新啟動受影響工具", "backend_restart": "重新啟動後端",
            "app_restart": "重新啟動主程式",
        }.get(strategy, "重新載入視窗")

    def _message(self, strategy: str, change_count: int) -> str:
        if change_count == 0:
            return "目前沒有待套用的更新。"
        return f"偵測到 {change_count} 項變更；建議動作：{self._action_label(strategy)}。"

    @staticmethod
    def _now_marker(snapshot: dict[str, int]) -> int:
        return max(snapshot.values(), default=0)
