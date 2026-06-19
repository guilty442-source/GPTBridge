from __future__ import annotations

import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

from core.paths import ensure_sandbox_layout, main_backup_root, sandbox_root
from managers.subsystem_backup import ScopedBackupStore, directory_size_bytes
from settings.config import load_config, save_config
from settings.global_update_coordinator import GlobalUpdateCoordinator


class SharedSettingsManager:
    """Shared settings and maintenance commands for developer-mode cards."""

    def __init__(self, app: Any, project_root: Path) -> None:
        self.app = app
        self.project_root = project_root.resolve()
        self.logs_root = self.project_root / "runtime" / "logs"
        self.global_update_coordinator = GlobalUpdateCoordinator(self.project_root)
        self._non_hot_update_baseline = self._build_non_hot_update_snapshot()

    async def handle(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if command == "load_config":
            return "load_config_result", {"ok": True, "config": load_config()}

        if command == "save_config":
            new_config = payload.get("config", {})
            save_config(new_config)
            return "save_config_result", {"ok": True, "message": "config saved"}

        if command == "settings_health_refresh":
            source = str(payload.get("source", "")).strip().lower()
            return "settings_health_refresh_result", self._health_refresh(source=source)

        if command == "settings_mark_updates_applied":
            return "settings_mark_updates_applied_result", self.global_update_coordinator.mark_applied()

        if command == "settings_maintain_sandbox":
            return "settings_maintain_sandbox_result", self._maintain_sandbox()

        if command == "settings_backup_records":
            return "settings_backup_records_result", self._backup_records()

        if command == "settings_delete_backup":
            return "settings_delete_backup_result", self._delete_backup(payload)

        if command == "settings_export_logs":
            return "settings_export_logs_result", self._export_logs()

        if command == "settings_export_error_logs":
            return "settings_export_error_logs_result", self._export_error_logs()

        if command == "settings_reset_provider_profile":
            return "settings_reset_provider_profile_result", await self._reset_provider_profile(payload)

        if command == "settings_open_system_browser":
            return "settings_open_system_browser_result", self._open_system_browser(payload)

        if command == "settings_factory_reset":
            return "settings_factory_reset_result", self._factory_reset()

        return "unhandled_settings_command", {
            "ok": False,
            "message": f"Unknown command: {command}",
        }

    def _open_system_browser(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(payload.get("provider", "chatgpt")).strip().lower() or "chatgpt"
        profile_name = str(payload.get("profile", "main")).strip() or "main"
        if provider not in {"chatgpt", "gemini", "claude", "perplexity", "deepseek"}:
            return {"ok": False, "message": f"invalid provider: {provider}"}

        config = load_config()
        provider_url_map = {
            "chatgpt": ("chatgpt_main_url", "https://chatgpt.com/"),
            "gemini": ("gemini_main_url", "https://gemini.google.com/"),
            "claude": ("claude_main_url", "https://claude.ai/"),
            "perplexity": ("perplexity_main_url", "https://www.perplexity.ai/"),
            "deepseek": ("deepseek_main_url", "https://chat.deepseek.com/"),
        }
        config_key, default_url = provider_url_map[provider]
        url = str(config.get(config_key, default_url) or default_url)

        profiles_root = (self.project_root / "edge-profile").resolve()
        shared_profile_dir = (profiles_root / profile_name / "shared").resolve()
        try:
            shared_profile_dir.relative_to(profiles_root)
        except ValueError:
            return {"ok": False, "message": "invalid profile path target"}
        shared_profile_dir.mkdir(parents=True, exist_ok=True)

        try:
            edge_executable = self._find_edge_executable()
            if not edge_executable:
                return {"ok": False, "message": "system browser open failed (edge not found)"}

            creation_flags = 0
            if os.name == "nt":
                creation_flags = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                )
            subprocess.Popen(
                [
                    edge_executable,
                    f"--user-data-dir={shared_profile_dir}",
                    url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
            return {
                "ok": True,
                "provider": provider,
                "profile": profile_name,
                "profile_dir": str(shared_profile_dir),
                "url": url,
                "message": "system browser opened (edge)",
            }
        except Exception as exc:
            return {"ok": False, "message": f"system browser open failed: {exc}"}

    async def _reset_provider_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(payload.get("provider", "chatgpt")).strip().lower() or "chatgpt"
        profile_name = str(payload.get("profile", "main")).strip() or "main"
        launch_manual_auth = bool(payload.get("launch_manual_auth", True))

        if provider not in {"chatgpt", "gemini"}:
            return {"ok": False, "message": f"invalid provider: {provider}"}

        profiles_root = (self.project_root / "edge-profile").resolve()
        profile_dir = (profiles_root / profile_name / "shared").resolve()
        legacy_current_chatgpt = (profiles_root / profile_name / "chatgpt").resolve()
        legacy_current_gemini = (profiles_root / profile_name / "gemini").resolve()
        legacy_profiles_root = (self.project_root / "browser-profile").resolve()
        legacy_profile_dir = (legacy_profiles_root / profile_name / provider).resolve()
        legacy_profile_chatgpt = (legacy_profiles_root / profile_name / "chatgpt").resolve()
        legacy_profile_gemini = (legacy_profiles_root / profile_name / "gemini").resolve()
        legacy_runtime_profiles_root = (self.project_root / "runtime" / "profiles").resolve()
        legacy_runtime_profile_dir = (legacy_runtime_profiles_root / profile_name / provider).resolve()
        legacy_runtime_chatgpt = (legacy_runtime_profiles_root / profile_name / "chatgpt").resolve()
        legacy_runtime_gemini = (legacy_runtime_profiles_root / profile_name / "gemini").resolve()

        try:
            profile_dir.relative_to(profiles_root)
            legacy_current_chatgpt.relative_to(profiles_root)
            legacy_current_gemini.relative_to(profiles_root)
            legacy_profile_dir.relative_to(legacy_profiles_root)
            legacy_profile_chatgpt.relative_to(legacy_profiles_root)
            legacy_profile_gemini.relative_to(legacy_profiles_root)
            legacy_runtime_profile_dir.relative_to(legacy_runtime_profiles_root)
            legacy_runtime_chatgpt.relative_to(legacy_runtime_profiles_root)
            legacy_runtime_gemini.relative_to(legacy_runtime_profiles_root)
        except ValueError:
            return {"ok": False, "message": "invalid profile path target"}

        removed_bytes = 0
        cleanup_targets = [profile_dir]
        for legacy_target in (
            legacy_current_chatgpt,
            legacy_current_gemini,
            legacy_profile_chatgpt,
            legacy_profile_gemini,
            legacy_runtime_chatgpt,
            legacy_runtime_gemini,
        ):
            if legacy_target not in cleanup_targets:
                cleanup_targets.append(legacy_target)
        if legacy_profile_dir not in cleanup_targets:
            cleanup_targets.append(legacy_profile_dir)
        if legacy_runtime_profile_dir not in cleanup_targets:
            cleanup_targets.append(legacy_runtime_profile_dir)
        for target in cleanup_targets:
            if target.exists():
                removed_bytes += directory_size_bytes([target], use_exclusions=False)

        session = getattr(self.app, "session", None)
        if session is not None:
            context = getattr(session, "context", None)
            if context is None:
                context = getattr(session, "contexts", {}).get(provider)
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            if hasattr(session, "context"):
                session.context = None
            if hasattr(session, "contexts"):
                session.contexts = {}
            for key in ("chatgpt", "gemini"):
                try:
                    session._set_tracked_page(key, None)
                except Exception:
                    pass
                if hasattr(session, "health_state"):
                    session.health_state[key] = "UNOPENED"
                if hasattr(session, "page_targets"):
                    session.page_targets[key] = ""

        removed = False
        try:
            for target in cleanup_targets:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=False)
                    removed = True
            profile_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {
                "ok": False,
                "provider": provider,
                "profile": profile_name,
                "message": (
                    f"profile reset failed (path may be locked). "
                    f"Close all browser windows and retry. detail={exc}"
                ),
            }

        manual_auth_launched = False
        manual_auth_message = ""
        manual_auth_executable = ""
        if launch_manual_auth:
            manual_auth_launched, manual_auth_executable, manual_auth_message = (
                self._launch_manual_auth_browser(provider, profile_dir)
            )

        return {
            "ok": True,
            "provider": provider,
            "profile": profile_name,
            "profile_scope": "shared",
            "removed": removed,
            "removed_bytes": removed_bytes,
            "profile_dir": str(profile_dir),
            "manual_auth_launched": manual_auth_launched,
            "manual_auth_executable": manual_auth_executable,
            "message": (
                f"{provider} verification profile reset completed (shared profile); {manual_auth_message}"
                if manual_auth_message
                else f"{provider} verification profile reset completed (shared profile)"
            ),
        }

    def _launch_manual_auth_browser(self, provider: str, profile_dir: Path) -> tuple[bool, str, str]:
        config = load_config()
        if provider == "chatgpt":
            url = str(config.get("chatgpt_main_url", "https://chatgpt.com/") or "https://chatgpt.com/")
        else:
            url = str(config.get("gemini_main_url", "https://gemini.google.com/") or "https://gemini.google.com/")

        executable = self._find_edge_executable()
        if not executable:
            return False, "", "manual auth browser not found (edge)"

        launch_args = [
            executable,
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ]

        creation_flags = 0
        if os.name == "nt":
            creation_flags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )

        try:
            subprocess.Popen(
                launch_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
            return True, executable, "manual auth browser opened"
        except OSError as exc:
            return False, executable, f"manual auth browser launch failed: {exc}"

    @staticmethod
    def _find_edge_executable() -> str:
        env_roots = [
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
            os.environ.get("LocalAppData", ""),
        ]
        for root in env_roots:
            if not root:
                continue
            candidate = Path(root).joinpath("Microsoft", "Edge", "Application", "msedge.exe")
            if candidate.exists():
                return str(candidate)
        for binary in ("msedge", "msedge.exe"):
            resolved = shutil.which(binary)
            if resolved:
                return resolved
        return ""

    def _health_refresh(self, source: str = "") -> dict[str, Any]:
        ensure_sandbox_layout(self.project_root)
        config = load_config()
        sandbox = sandbox_root(self.project_root)

        required_urls = (
            "chatgpt_main_url",
            "gemini_main_url",
            "claude_main_url",
            "perplexity_main_url",
            "deepseek_main_url",
        )
        missing_urls = [key for key in required_urls if not str(config.get(key, "")).strip()]

        runtime_size = directory_size_bytes([self.project_root / "runtime"])
        sandbox_size = directory_size_bytes([sandbox])
        backup_size = directory_size_bytes([main_backup_root(self.project_root)])

        non_hot_update_changes: list[str] = []
        non_hot_update_count = 0
        global_update_plan = self.global_update_coordinator.inspect()
        if source == "update_tool":
            non_hot_update_changes = [
                str(change.get("path", ""))
                for change in global_update_plan.get("changes", [])
                if str(change.get("strategy", "")) not in {"renderer_hmr", "data_reload"}
            ]
            non_hot_update_count = len(non_hot_update_changes)
            update_message = (
                str(global_update_plan.get("message", "更新狀態檢查完成"))
                if global_update_plan.get("changed")
                else "目前沒有待套用的全域更新"
            )
        else:
            update_message = ""

        return {
            "ok": len(missing_urls) == 0,
            "source": source,
            "missing_urls": missing_urls,
            "sizes": {
                "runtime_bytes": runtime_size,
                "sandbox_bytes": sandbox_size,
                "backup_bytes": backup_size,
            },
            "non_hot_update_count": non_hot_update_count,
            "non_hot_update_changes": non_hot_update_changes[:30],
            "global_update_plan": global_update_plan,
            "message": (
                update_message
                if source == "update_tool"
                else (
                    "settings health refresh completed"
                    if len(missing_urls) == 0
                    else f"missing url settings: {', '.join(missing_urls)}"
                )
            ),
        }

    def _build_non_hot_update_snapshot(self) -> dict[str, int]:
        snapshot: dict[str, int] = {}
        for path in self._iter_non_hot_update_files():
            try:
                relative = path.relative_to(self.project_root).as_posix()
                snapshot[relative] = path.stat().st_mtime_ns
            except (OSError, ValueError):
                continue
        return snapshot

    def _iter_non_hot_update_files(self) -> list[Path]:
        candidates: list[Path] = []
        for relative in (
            "src-core",
            "src-ui/main",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "tsconfig.json",
            "vite.config.ts",
            "vite.config.js",
            "electron.vite.config.ts",
            "electron.vite.config.js",
        ):
            target = (self.project_root / relative).resolve()
            try:
                target.relative_to(self.project_root)
            except ValueError:
                continue
            if not target.exists():
                continue

            if target.is_file():
                candidates.append(target)
                continue

            for file in target.rglob("*"):
                if file.is_file():
                    candidates.append(file)
        return candidates

    def _collect_non_hot_update_changes(self) -> list[str]:
        current_snapshot = self._build_non_hot_update_snapshot()
        baseline = self._non_hot_update_baseline
        changed: set[str] = set()

        for relative, current_mtime in current_snapshot.items():
            previous_mtime = baseline.get(relative)
            if previous_mtime is None or previous_mtime != current_mtime:
                changed.add(relative)

        for relative in baseline:
            if relative not in current_snapshot:
                changed.add(relative)

        return sorted(changed)

    def _maintain_sandbox(self) -> dict[str, Any]:
        ensure_sandbox_layout(self.project_root)
        s_root = sandbox_root(self.project_root)
        cleaned_files = 0
        cleaned_dirs = 0
        cleaned_bytes = 0

        targets = [
            s_root / "temp",
            s_root / "cache",
            s_root / "artifacts",
            s_root / "logs",
        ]

        for base in targets:
            if not base.exists():
                continue
            for path in sorted(base.rglob("*"), reverse=True):
                try:
                    if path.is_file():
                        size = path.stat().st_size
                        path.unlink(missing_ok=True)
                        cleaned_files += 1
                        cleaned_bytes += size
                    elif path.is_dir():
                        path.rmdir()
                        cleaned_dirs += 1
                except OSError:
                    continue

        ensure_sandbox_layout(self.project_root)
        return {
            "ok": True,
            "cleaned_files": cleaned_files,
            "cleaned_dirs": cleaned_dirs,
            "cleaned_bytes": cleaned_bytes,
            "message": "sandbox maintenance completed",
        }

    def _backup_records(self) -> dict[str, Any]:
        config = load_config()
        max_records = int(config.get("max_backup_count", 3) or 3)
        store = ScopedBackupStore(
            self.project_root,
            main_backup_root(self.project_root),
            max_records=max_records,
        )
        result = store.create("settings-record")
        result["message"] = "backup record created"
        return result

    def _delete_backup(self, payload: dict[str, Any]) -> dict[str, Any]:
        target_raw = str(payload.get("target", "")).strip()
        if not target_raw:
            return {"ok": False, "message": "backup target is required"}

        backup_root = main_backup_root(self.project_root).resolve()
        backup_root.mkdir(parents=True, exist_ok=True)

        candidate = Path(target_raw)
        if candidate.is_absolute():
            target = candidate.resolve()
        else:
            target = (backup_root / candidate).resolve()

        try:
            target.relative_to(backup_root)
        except ValueError:
            return {"ok": False, "message": "backup target must stay inside backup root"}

        if target.suffix.lower() != ".zip":
            return {"ok": False, "message": "backup target must be a .zip file"}
        if not target.exists() or not target.is_file():
            return {"ok": False, "message": "backup file not found"}

        deleted_bytes = 0
        try:
            deleted_bytes = target.stat().st_size
        except OSError:
            deleted_bytes = 0

        try:
            target.unlink()
        except OSError as exc:
            return {"ok": False, "message": f"delete backup failed: {exc}"}

        config = load_config()
        max_records = int(config.get("max_backup_count", 3) or 3)
        store = ScopedBackupStore(self.project_root, backup_root, max_records=max_records)
        return {
            "ok": True,
            "deleted_file": str(target),
            "deleted_bytes": deleted_bytes,
            "backup_root": str(backup_root),
            "records": store.records(),
            "message": "backup record deleted",
        }

    def _export_logs(self) -> dict[str, Any]:
        self.logs_root.mkdir(parents=True, exist_ok=True)
        export_dir = self.project_root / "runtime" / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)

        ts = time.strftime("%Y%m%d_%H%M%S")
        archive_path = export_dir / f"operation-logs-{ts}.zip"
        file_count = 0

        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self.logs_root.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(self.logs_root)
                archive.write(path, arcname=rel.as_posix())
                file_count += 1

        return {
            "ok": True,
            "archive": str(archive_path),
            "file_count": file_count,
            "message": "operation logs exported",
        }

    def _export_error_logs(self) -> dict[str, Any]:
        self.logs_root.mkdir(parents=True, exist_ok=True)
        export_dir = self.project_root / "runtime" / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)

        ts = time.strftime("%Y%m%d_%H%M%S")
        summary_path = export_dir / f"error-lines-{ts}.log"
        archive_path = export_dir / f"error-logs-{ts}.zip"

        markers = (
            "error",
            "exception",
            "traceback",
            "failed",
            "failure",
            "critical",
        )
        matched_files: list[Path] = []
        matched_line_count = 0

        with summary_path.open("w", encoding="utf-8", newline="\n") as summary:
            summary.write(f"# Error Log Export ({ts})\n")

            for path in sorted(self.logs_root.rglob("*")):
                if not path.is_file():
                    continue

                try:
                    content = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue

                matches: list[str] = []
                for line in content.splitlines():
                    lowered = line.lower()
                    if any(marker in lowered for marker in markers):
                        matches.append(line)

                if not matches:
                    continue

                rel = path.relative_to(self.logs_root).as_posix()
                matched_files.append(path)
                matched_line_count += len(matches)
                summary.write(f"\n## {rel}\n")
                for line in matches:
                    summary.write(f"{line}\n")

            if matched_line_count == 0:
                summary.write("\nNo error lines found in current runtime logs.\n")

        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(summary_path, arcname=summary_path.name)
            for path in matched_files:
                rel = path.relative_to(self.logs_root).as_posix()
                archive.write(path, arcname=f"source/{rel}")

        return {
            "ok": True,
            "archive": str(archive_path),
            "summary": str(summary_path),
            "matched_files": len(matched_files),
            "matched_lines": matched_line_count,
            "message": "error logs exported",
        }

    def _factory_reset(self) -> dict[str, Any]:
        cleaned_files = 0
        cleaned_dirs = 0
        cleaned_bytes = 0
        targets = [
            self.project_root / "runtime",
            self.project_root / ".GPTBridge_RuntimeSandbox",
        ]

        for base in targets:
            if not base.exists():
                continue

            for path in sorted(base.rglob("*"), reverse=True):
                try:
                    if path.is_file():
                        size = path.stat().st_size
                        path.unlink(missing_ok=True)
                        cleaned_files += 1
                        cleaned_bytes += size
                    elif path.is_dir():
                        path.rmdir()
                        cleaned_dirs += 1
                except OSError:
                    continue

        ensure_sandbox_layout(self.project_root)
        self.logs_root.mkdir(parents=True, exist_ok=True)
        return {
            "ok": True,
            "cleaned_files": cleaned_files,
            "cleaned_dirs": cleaned_dirs,
            "cleaned_bytes": cleaned_bytes,
            "message": "factory reset completed",
        }
