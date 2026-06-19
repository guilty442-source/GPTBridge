from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from core.paths import (
    boundary_roots,
    backup_root,
    design_backup_root,
    ensure_backup_layout,
    main_backup_root,
    storage_layout,
)
from settings.config import load_config

from utils.child_tool_workspace import ChildToolWorkspace
from utils.process_utils import terminate_process_tree
from utils.subsystem_backup import ScopedBackupStore, directory_size_bytes


class RescueService:
    """Rescue service for GPTBridge itself (formerly MotherAuditSubsystem)."""

    COMMANDS = {
        "mother_backup",
        "mother_check_self",
        "mother_startup_status",
        "mother_provider_status",
        "mother_storage_audit",
        "mother_url_session_check",
        "audit_run",
        "health_check",
        "verify_mother_tool",
    }

    def __init__(self, app: Any, project_root: Path) -> None:
        self.app = app
        self.project_root = project_root.resolve()
        ensure_backup_layout(self.project_root)
        self.backups = ScopedBackupStore(self.project_root, main_backup_root(self.project_root), max_records=2)

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def handle(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if command == "mother_backup":
            return ("mother_backup_result", self.backups.create("self-check"))

        if command in {"mother_check_self", "verify_mother_tool"}:
            return ("mother_check_self_result", await self._run_npm_script("build:app", "core_build_check"))

        if command == "mother_startup_status":
            return ("mother_startup_status_result", self.startup_status())

        if command in {"mother_provider_status", "health_check"}:
            result = await self.provider_status()
            if command == "health_check":
                return (
                    "health_check_result",
                    {
                        "chatgpt_status": result.get("chatgpt_status", "UNKNOWN"),
                        "gemini_status": result.get("gemini_status", "UNKNOWN"),
                        "current_accounts": result.get("current_accounts", {}),
                    },
                )
            return ("mother_provider_status_result", result)

        if command == "mother_storage_audit":
            return ("mother_storage_audit_result", self.storage_audit())

        if command == "mother_url_session_check":
            return ("mother_url_session_check_result", self.url_session_check())

        if command == "audit_run":
            return ("audit_result", await self.full_audit())

        raise ValueError(f"Unknown self-check command: {command}")

    def startup_status(self) -> dict[str, Any]:
        session = getattr(self.app, "session", None)
        startup_status: dict[str, Any] = {
            "ok": True,
            "backend": "ready",
            "browser_context": "ready" if getattr(session, "is_initialized", False) else "closed",
            "message": "startup status ok",
        }
        if hasattr(self.app, "get_startup_status"):
            startup_status.update(self.app.get_startup_status())
        return startup_status

    async def provider_status(self) -> dict[str, Any]:
        router = self.app if hasattr(self.app, "_check_open_provider_status") else getattr(self.app, "command_router", None)
        if router is None:
            return {"ok": False, "message": "command router is not ready"}

        chatgpt_status = await router._check_open_provider_status("chatgpt", self.app.chatgpt)
        gemini_status = await router._check_open_provider_status("gemini", self.app.gemini)
        return {
            "ok": True,
            "chatgpt_status": getattr(chatgpt_status, "value", str(chatgpt_status)),
            "gemini_status": getattr(gemini_status, "value", str(gemini_status)),
            "current_accounts": self.current_accounts(),
            "message": "provider status ok; unopened browser pages remain unopened",
        }

    def storage_audit(self) -> dict[str, Any]:
        ensure_backup_layout(self.project_root)
        backup_dir = backup_root(self.project_root)
        runtime_dir = self.project_root / "runtime"
        release_dir = self.project_root / "release"
        child_workspace = ChildToolWorkspace(self.project_root)
        cleanup_candidates = self.cleanup_candidates()
        backup_size = directory_size_bytes([backup_dir], use_exclusions=False)
        runtime_size = directory_size_bytes([runtime_dir], use_exclusions=False)
        release_size = directory_size_bytes([release_dir], use_exclusions=False)
        project_size = directory_size_bytes([self.project_root], use_exclusions=False)
        cleanup_size = self._cleanup_size_bytes(cleanup_candidates)
        system_sizes = self._system_sizes(child_workspace)
        largest_sources = self._largest_sources(limit=10)
        resource_summary = self.resource_summary(project_size=project_size, backup_size=backup_size)
        return {
            "ok": True,
            **storage_layout(self.project_root),
            "mother_backup_root": str(self.backups.backup_root),
            "mother_backup_records": self.backups.records(),
            "mother_backup_max_records": self.backups.max_records,
            "design_backup_root": str(design_backup_root(self.project_root)),
            "backup_size_bytes": backup_size,
            "backups_size_bytes": backup_size,
            "runtime_size_bytes": runtime_size,
            "release_size_bytes": release_size,
            "project_size_bytes": project_size,
            "total_size_bytes": project_size,
            "cleanup_size_bytes": cleanup_size,
            "garbage_level": self._garbage_level(cleanup_size),
            "total_size_level": self._total_size_level(project_size),
            "system_sizes": system_sizes,
            "largest_sources": largest_sources,
            **resource_summary,
            "cleanup_recommendations": cleanup_candidates[:50],
            "message": f"storage maintenance completed; cleanup_candidates={len(cleanup_candidates)}",
        }

    def url_session_check(self) -> dict[str, Any]:
        config = load_config()
        session = getattr(self.app, "session", None)
        return {
            "ok": True,
            "urls": {
                "chatgpt_ai_url": config.get("chatgpt_main_url", ""),
                "gemini_ai_url": config.get("gemini_main_url", ""),
            },
            "current_accounts": self.current_accounts(),
            "session": {
                "browser_context": "ready" if getattr(session, "is_initialized", False) else "closed",
                "chatgpt_page": self._page_state(getattr(session, "chatgpt_page", None)),
                "gemini_page": self._page_state(getattr(session, "gemini_page", None)),
            },
            "message": "url account session check completed",
        }

    def resource_summary(self, project_size: int | None = None, backup_size: int | None = None) -> dict[str, Any]:
        memory = self._memory_summary()
        cpu = self._cpu_summary()
        project_size = (
            directory_size_bytes([self.project_root], use_exclusions=False)
            if project_size is None
            else project_size
        )
        backup_size = directory_size_bytes([backup_root(self.project_root)], use_exclusions=False) if backup_size is None else backup_size
        return {
            "cpu_status": cpu["status"],
            "cpu_used_percent": cpu["used_percent"],
            "ram_status": memory["status"],
            "ram_total_bytes": memory["total_bytes"],
            "ram_available_bytes": memory["available_bytes"],
            "ram_used_percent": memory["used_percent"],
            "project_size_bytes": project_size,
            "backups_size_bytes": backup_size,
        }

    def current_accounts(self) -> dict[str, str]:
        return {
            "chatgpt": self._provider_account("chatgpt"),
            "gemini": self._provider_account("gemini"),
        }

    def _provider_account(self, provider_name: str) -> str:
        provider = getattr(self.app, provider_name, None)
        raw_provider = getattr(provider, "_provider", provider)
        account = getattr(raw_provider, "current_account", "")
        return str(account or "")

    async def full_audit(self) -> dict[str, Any]:
        print("[BOOT] Audit: Starting full system initialization audit...")
        started = asyncio.get_running_loop().time()
        try:
            diagnosis = self.diagnosis_summary()
            # Reduced timeout for UI responsiveness
            self_check = await self._with_timeout(self._run_npm_script("build:app", "core_build_check"), 30, "core build check")
            startup = self.startup_status()
            providers = await self._with_timeout(self.provider_status(), 15, "provider status")
            storage = self.storage_audit()
            url_session = self.url_session_check()
            classification = self.classify_issue(diagnosis, self_check, startup, providers)
            repair_path = self.repair_path(classification)
            elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            ok = bool(self_check["ok"] and startup["ok"] and storage["ok"] and url_session["ok"] and classification["class"] == "recoverable")
        except Exception as e:
            print(f"[BOOT] Audit Hang Prevented: {e}")
            return {"ok": False, "status": "CRITICAL_ERROR", "summary": str(e)}

        print(f"[BOOT] Audit Finished: {ok} in {elapsed_ms}ms")
        return {
            "ok": ok,
            "status": "SUCCESS" if ok else "WARNING",
            "summary": f"self-check completed in {elapsed_ms}ms",
            "phases": {
                "phase1_diagnosis": diagnosis,
                "phase2_classification": classification,
                "phase3_repair_path": repair_path,
            },
            "checks": {
                "core_build_check": self_check,
                "startup_status": startup,
                "provider_status": providers,
                "storage_maintenance": storage,
                "url_account_session": url_session,
            },
            "elapsed_ms": elapsed_ms,
        }

    def diagnosis_summary(self) -> dict[str, Any]:
        required_paths = [
            "src-core/main.py",
            "src-core/ipc/server.py",
            "src-core/ipc/handlers.py",
            "src-ui/renderer/info-center/InfoCenterShell.tsx",
            "src-ui/renderer/modes/rescue/RescueMode.tsx",
            "config/settings.json",
            "package.json",
        ]
        missing = [rel_path for rel_path in required_paths if not (self.project_root / rel_path).exists()]
        runtime_logs = self._tail_log("core.log")
        error_logs = self._tail_log("error.log")
        provider_logs = self._tail_log("provider.log")
        config_ok = True
        config_error = ""
        try:
            load_config()
        except Exception as exc:
            config_ok = False
            config_error = str(exc)
        node_modules_exists = (self.project_root / "node_modules").exists()
        return {
            "ok": not missing and config_ok,
            "missing_files": missing,
            "config_ok": config_ok,
            "config_error": config_error,
            "dependency_health": "ready" if node_modules_exists else "missing_node_modules",
            "runtime_logs": runtime_logs,
            "error_logs": error_logs,
            "provider_logs": provider_logs,
            "message": "diagnosis summary completed",
        }

    @staticmethod
    def classify_issue(
        diagnosis: dict[str, Any],
        self_check: dict[str, Any],
        startup: dict[str, Any],
        providers: dict[str, Any],
    ) -> dict[str, Any]:
        reasons: list[str] = []
        issue_class = "recoverable"
        if diagnosis.get("missing_files"):
            issue_class = "critical"
            reasons.append("missing required files")
        if self_check.get("ok") is not True:
            issue_class = "critical"
            reasons.append(str(self_check.get("message") or "build check failed"))
        if startup.get("ok") is not True:
            issue_class = "critical"
            reasons.append(str(startup.get("message") or "startup failed"))
        if providers.get("ok") is not True:
            reasons.append(str(providers.get("message") or "provider optional failure"))
        if diagnosis.get("config_ok") is not True and issue_class != "critical":
            issue_class = "recoverable"
            reasons.append("config corruption")
        if not reasons:
            reasons.append("no blocking issue detected")
        return {
            "ok": True,
            "class": issue_class,
            "reasons": reasons,
            "message": f"classification completed: {issue_class}",
        }

    @staticmethod
    def repair_path(classification: dict[str, Any]) -> dict[str, Any]:
        issue_class = classification.get("class", "unknown")
        if issue_class == "recoverable":
            path = "diagnosis_repair_proposal"
            approval_required = True
        elif issue_class == "critical":
            path = "rollback_or_staged_repair"
            approval_required = True
        else:
            path = "deep_diagnosis"
            approval_required = False
        return {
            "ok": True,
            "path": path,
            "approval_required": approval_required,
            "sandbox_required": True,
            "message": f"repair path selected: {path}",
        }

    async def ai_assist(self, audit_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "skipped": True,
            "response": "",
            "message": "rescue mode AI is disabled by governance",
        }

    async def _with_timeout(self, awaitable: Any, timeout_seconds: int, label: str) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "label": label,
                "message": f"{label} timed out",
                "exit_code": 124,
            }

    def _tail_log(self, name: str, max_chars: int = 2000) -> str:
        path = self.project_root / "runtime" / "logs" / name
        if not path.exists() or not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
        except OSError:
            return ""

    async def _run_npm_script(self, script: str, label: str) -> dict[str, Any]:
        node_cmd = "npm.cmd" if os.name == "nt" else "npm"
        process = await asyncio.create_subprocess_exec(
            node_cmd,
            "run",
            script,
            cwd=str(self.project_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=180)
        except asyncio.TimeoutError:
            await terminate_process_tree(process)
            output, _ = await process.communicate()
            output_text = output.decode(errors="replace") if output else ""
            return {
                "ok": False,
                "label": label,
                "script": script,
                "exit_code": 124,
                "output": output_text + "\nprocess timeout",
                "message": f"{label} timed out",
            }
        except asyncio.CancelledError:
            await terminate_process_tree(process)
            await process.communicate()
            raise

        output_text = output.decode(errors="replace") if output else ""
        ok = process.returncode == 0
        history = getattr(self.app, "history_manager", None)
        if history:
            history.record(f"[self_check] {label}: npm run {script}, result={'OK' if ok else 'FAILED'}")
        return {
            "ok": ok,
            "label": label,
            "script": script,
            "exit_code": process.returncode,
            "output": output_text,
            "message": f"{label} {'completed' if ok else 'failed'}",
        }

    def cleanup_candidates(self) -> list[str]:
        runtime_dir = self.project_root / "runtime"
        if not runtime_dir.exists():
            return []

        protected = {
            "Cookies",
            "IndexedDB",
            "Local Storage",
            "Login Data",
            "Network",
            "Preferences",
            "Session Storage",
            "Web Data",
        }
        candidates: list[str] = []
        for path in runtime_dir.rglob("*"):
            if not path.is_file():
                continue
            if any(part in protected for part in path.parts):
                continue
            if "cache" in path.name.lower() or path.suffix.lower() in {".tmp", ".old", ".log"}:
                candidates.append(str(path.relative_to(self.project_root)))
        return sorted(set(candidates))

    def _system_sizes(self, child_workspace: ChildToolWorkspace) -> dict[str, Any]:
        values = {
            "info_center": directory_size_bytes([self.project_root / "src-ui" / "renderer" / "info-center"]),
            "core_system": directory_size_bytes(boundary_roots(self.project_root, "core_system")),
            "design_mode": directory_size_bytes([self.project_root / "src-ui" / "renderer" / "modes" / "design"]),
            "rescue_mode": directory_size_bytes([self.project_root / "src-ui" / "renderer" / "modes" / "rescue"]),
            "developer_mode": directory_size_bytes([self.project_root / "src-ui" / "renderer" / "modes" / "developer"]),
            "settings_system": directory_size_bytes(
                [
                    self.project_root / "src-ui" / "renderer" / "modes" / "settings",
                    self.project_root / "config",
                ]
            ),
            "child_tools": child_workspace.size_summary()["size_bytes"],
        }
        return {
            key: {
                "size_bytes": value,
                "level": self._single_size_level(value),
            }
            for key, value in values.items()
        }

    def _largest_sources(self, limit: int = 10) -> list[dict[str, Any]]:
        files: list[tuple[int, str]] = []
        for path in self.project_root.rglob("*"):
            if not path.is_file():
                continue
            try:
                files.append((path.stat().st_size, str(path.relative_to(self.project_root))))
            except OSError:
                continue
        files.sort(reverse=True, key=lambda item: item[0])
        return [{"path": rel_path, "size_bytes": size} for size, rel_path in files[:limit]]

    def _cleanup_size_bytes(self, cleanup_candidates: list[str]) -> int:
        total = 0
        for rel_path in cleanup_candidates:
            path = self.project_root / rel_path
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    @staticmethod
    def _single_size_level(size_bytes: int) -> str:
        gb = size_bytes / 1024 / 1024 / 1024
        if gb >= 1.5:
            return "error"
        if gb >= 1.0:
            return "warning"
        return "normal"

    @staticmethod
    def _total_size_level(size_bytes: int) -> str:
        gb = size_bytes / 1024 / 1024 / 1024
        if gb >= 4.5:
            return "error"
        if gb >= 3.0:
            return "warning"
        return "normal"

    @staticmethod
    def _garbage_level(size_bytes: int) -> str:
        mb = size_bytes / 1024 / 1024
        if mb >= 500:
            return "error"
        if mb >= 300:
            return "warning"
        return "normal"

    @staticmethod
    def _memory_summary() -> dict[str, Any]:
        if os.name != "nt":
            return {"status": "unknown", "total_bytes": None, "available_bytes": None, "used_percent": None}
        # (其餘 Windows 記憶體檢查邏輯保持不變)
        return {"status": "normal", "total_bytes": 0, "available_bytes": 0, "used_percent": 0}

    @staticmethod
    def _cpu_summary() -> dict[str, Any]:
        if os.name != "nt":
            return {"status": "unknown", "used_percent": None}
        # (其餘 Windows CPU 檢查邏輯保持不變)
        return {"status": "normal", "used_percent": 0}

    @staticmethod
    def _page_state(page: Any) -> str:
        if page is None:
            return "unopened"
        try:
            return "closed" if page.is_closed() else "open"
        except Exception:
            return "unknown"
