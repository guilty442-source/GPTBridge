from __future__ import annotations

from typing import Any, Dict, TYPE_CHECKING

from providers.base_provider import SessionStatusEnum

if TYPE_CHECKING:
    from main import GPTBridgeApp


GOVERNANCE_RULE_COMMANDS = {
    "app:get-governance-rules",
    "app:set-governance-rules",
    "app:add-governance-rule",
    "app:delete-governance-rule",
    "app:update-governance-rule",
}

APP_CODE_COMMANDS = {
    "app:save-code",
    "app:delete-code",
    "app:move-code",
    "app:update-config",
    "app:agent-intervention",
    "app:agent-instruct",
    "app:agent-execute-tool",
    "app:run-unit-tests",
}

TOOLBOX_COMMAND_HANDLERS = {
    "toolbox_add_tool": "add_tool",
    "toolbox_list_tools": "list_tools",
    "toolbox_start_tool": "start_tool",
    "toolbox_stop_tool": "stop_tool",
    "toolbox_run_tool": "run_tool",
    "toolbox_cancel_tool_run": "cancel_tool_run",
    "toolbox_open_tool_code": "open_tool_code",
    "toolbox_save_tool_code": "save_tool_code",
}

SETTINGS_COMMANDS = {
    "load_config",
    "save_config",
    "settings_health_refresh",
    "settings_mark_updates_applied",
    "settings_maintain_sandbox",
    "settings_backup_records",
    "settings_delete_backup",
    "settings_export_logs",
    "settings_export_error_logs",
    "settings_reset_provider_profile",
    "settings_open_system_browser",
    "settings_factory_reset",
}


class CommandRouter:
    def __init__(self, app: "GPTBridgeApp", **kwargs: Any) -> None:
        self.app = app
        self.mode_manager = app.mode_manager
        self._log_reporter: Any = None

        self.toolbox_service = kwargs.get("toolbox_service")
        self.developer_service = kwargs.get("developer_service")
        self.rescue_service = kwargs.get("rescue_service")
        self.settings_service = kwargs.get("settings_service")
        self.mode_services = kwargs.get("mode_services", {})

        self.session = kwargs.get("session")
        self.chatgpt = kwargs.get("chatgpt")
        self.gemini = kwargs.get("gemini")
        self.backup_manager = kwargs.get("backup_manager")
        self.history_manager = kwargs.get("history_manager")
        self.orchestrator = kwargs.get("orchestrator")
        self.autonomous_agent = kwargs.get("autonomous_agent")

    async def handle(self, command: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        if self.mode_manager and not self.mode_manager.can_execute_command(command):
            return "command_blocked_result", {
                "ok": False,
                "command": command,
                "message": f"Command '{command}' is blocked in {self.mode_manager.active_mode} mode.",
            }

        if command in GOVERNANCE_RULE_COMMANDS:
            # Governance management is strictly reserved for manual user intervention
            return await self._handle_governance_rules(command, payload)

        app_code_result = await self._handle_app_code_command(command, payload)
        if app_code_result is not None:
            return app_code_result

        toolbox_result = await self._handle_toolbox_command(command, payload)
        if toolbox_result is not None:
            return toolbox_result

        settings_result = await self._handle_settings_command(command, payload)
        if settings_result is not None:
            return settings_result

        if command == "app:list-mode-services":
            return "app:list-mode-services_result", {"ok": True, "services": list(self.mode_services.keys())}

        if command == "app:get-mode-services-status":
            services_info: dict[str, dict[str, str]] = {}
            for name, svc in self.mode_services.items():
                info: dict[str, str] = {
                    "class": svc.__class__.__name__,
                    "module": svc.__class__.__module__,
                }
                ver = getattr(svc, "VERSION", None)
                if ver:
                    info["version"] = str(ver)
                ws_root = getattr(getattr(svc, "workspace", None), "workspace_root", None)
                if ws_root is not None:
                    try:
                        info["path"] = str(ws_root)
                    except Exception:
                        pass
                services_info[name] = info
            return "app:get-mode-services-status_result", {"ok": True, "services": services_info}

        # Mode-specific subsystem dispatch
        for service in self.mode_services.values():
            if hasattr(service, "owns") and service.owns(command):
                latest = None
                if isinstance(payload, dict):
                    latest = payload.get("latest_ai_answer")
                return await service.handle(command, payload, latest)

        # Provider focus / URL change
        if command == "focus_chatgpt":
            return await self._focus_provider("chatgpt", payload)

        if command == "focus_gemini":
            return await self._focus_provider("gemini", payload)

        if command == "change_provider_url":
            return await self._change_provider_url(payload)

        if command == "discussion_query":
            return await self._discussion_query(payload)

        # Rescue / audit
        if self.rescue_service and hasattr(self.rescue_service, "owns") and self.rescue_service.owns(command):
            return await self.rescue_service.handle(command, payload)

        # Developer service (best effort if implemented)
        if (
            self.developer_service
            and hasattr(self.developer_service, "owns")
            and self.developer_service.owns(command)
            and hasattr(self.developer_service, "handle")
        ):
            return await self.developer_service.handle(command, payload)

        return "unhandled_command_result", {
            "ok": False,
            "message": f"Command '{command}' not handled by router.",
        }

    async def _handle_app_code_command(
        self, command: str, payload: Dict[str, Any]
    ) -> tuple[str, Dict[str, Any]] | None:
        if command not in APP_CODE_COMMANDS:
            return None

        if command == "app:save-code":
            rel_path = payload.get("path", "")
            content = payload.get("content", "")
            result = await self.app.save_code_to_disk(rel_path, content)
            return "app:save-code_result", result

        if command == "app:delete-code":
            rel_path = payload.get("path", "")
            result = await self.app.delete_code_from_disk(rel_path)
            return "app:delete-code_result", result

        if command == "app:move-code":
            src = payload.get("from", "")
            dst = payload.get("to", "")
            result = await self.app.move_code_on_disk(src, dst)
            return "app:move-code_result", result

        if command == "app:update-config":
            key = payload.get("key", "")
            value = payload.get("value")
            await self.app.update_config_value(key, value)
            return "app:update-config_result", {"ok": True, "key": key}

        if command == "app:agent-intervention":
            rel_path = payload.get("path", "")
            content = payload.get("content", "")
            result = await self.app.request_agent_intervention(rel_path, content)
            return "app:agent-intervention_result", result

        if command == "app:agent-instruct":
            rel_path = payload.get("path", "")
            content = payload.get("content", "")
            instruction = payload.get("instruction", "")
            auto_test = payload.get("auto_test", True)
            result = await self.app.instruct_agent_on_code(
                rel_path, content, instruction, auto_test
            )
            return "app:agent-instruct_result", result

        if command == "app:agent-execute-tool":
            service = payload.get("service", "")
            cmd = payload.get("tool_command", "")
            args = payload.get("payload", {})
            result = await self.app.execute_agent_tool_operation(service, cmd, args)
            return "app:agent-execute-tool_result", result

        target_path = payload.get("path", "")
        result = await self.app.run_unit_tests(target_path)
        return "app:run-unit-tests_result", result

    async def _handle_toolbox_command(
        self, command: str, payload: Dict[str, Any]
    ) -> tuple[str, Dict[str, Any]] | None:
        handler_name = TOOLBOX_COMMAND_HANDLERS.get(command)
        if handler_name is None:
            return None

        if not self.toolbox_service:
            return f"{command}_result", {
                "ok": False,
                "message": "Toolbox service not available",
            }

        handler = getattr(self.toolbox_service, handler_name, None)
        if not callable(handler):
            return f"{command}_result", {
                "ok": False,
                "message": f"Toolbox handler '{handler_name}' is not available",
            }

        if command == "toolbox_list_tools":
            result = await handler()
        else:
            result = await handler(payload)
        return f"{command}_result", result

    async def _handle_settings_command(
        self, command: str, payload: Dict[str, Any]
    ) -> tuple[str, Dict[str, Any]] | None:
        if command not in SETTINGS_COMMANDS:
            return None

        if not self.settings_service:
            return f"{command}_result", {
                "ok": False,
                "message": "Settings service not available",
            }
        return await self.settings_service.handle(command, payload)

    async def _handle_governance_rules(
        self, command: str, payload: Dict[str, Any]
    ) -> tuple[str, Dict[str, Any]]:
        rules_raw = getattr(self.app, "governance_rules", [])
        rules = [str(item).strip() for item in rules_raw if str(item).strip()]
        available_raw = getattr(self.app, "AVAILABLE_GOVERNANCE_RULES", [])
        available_rules = [
            str(item).strip() for item in available_raw if str(item).strip()
        ]
        all_rules = list(dict.fromkeys([*available_rules, *rules]))
        active_rules = all_rules

        if command == "app:get-governance-rules":
            persist = self._persist_governance_rules(active_rules)
            if persist is not None:
                return "app:get-governance-rules_result", persist
            return "app:get-governance-rules_result", {
                "ok": True,
                "rules": all_rules,
                "active_rules": active_rules,
                "available_rules": all_rules,
                "scope": "global",
            }

        if command == "app:set-governance-rules":
            new_rules = payload.get("rules", [])
            if not isinstance(new_rules, list):
                return "app:set-governance-rules_result", {"ok": False, "message": "rules must be a list"}

            requested_rules = [
                str(item).strip() for item in new_rules if str(item).strip()
            ]
            next_rules = list(dict.fromkeys([*all_rules, *requested_rules]))

            persist = self._persist_governance_rules(next_rules)
            if persist is not None:
                return "app:set-governance-rules_result", persist
            return "app:set-governance-rules_result", {
                "ok": True,
                "rules": next_rules,
                "active_rules": next_rules,
                "available_rules": next_rules,
                "scope": "global",
            }

        rule = str(payload.get("rule", "")).strip()
        if not rule and command != "app:update-governance-rule":
            return f"{command}_result", {"ok": False, "message": "rule is required"}

        if command == "app:add-governance-rule":
            next_rules = list(dict.fromkeys([*all_rules, rule]))
            persist = self._persist_governance_rules(next_rules)
            if persist is not None:
                return "app:add-governance-rule_result", persist
            return "app:add-governance-rule_result", {
                "ok": True,
                "rules": next_rules,
                "active_rules": next_rules,
                "available_rules": next_rules,
                "scope": "global",
                "message": "rule added globally",
            }

        if command == "app:delete-governance-rule":
            if rule in available_rules:
                return "app:delete-governance-rule_result", {
                    "ok": True,
                    "rules": all_rules,
                    "active_rules": active_rules,
                    "available_rules": all_rules,
                    "scope": "global",
                    "message": "內建治理規則必須全域啟動，不能停用。",
                }
            next_rules = [item for item in all_rules if item != rule]
            persist = self._persist_governance_rules(next_rules)
            if persist is not None:
                return "app:delete-governance-rule_result", persist
            return "app:delete-governance-rule_result", {
                "ok": True,
                "rules": next_rules,
                "active_rules": next_rules,
                "available_rules": next_rules,
                "scope": "global",
                "message": "自訂治理規則已移除；所有內建規則仍全域啟動。",
            }

        if command == "app:update-governance-rule":
            old_rule = str(payload.get("old_rule", "")).strip()
            new_rule = str(payload.get("rule", "")).strip()
            if not old_rule or not new_rule:
                return "app:update-governance-rule_result", {
                    "ok": False,
                    "message": "old_rule and rule are required",
                }

            if old_rule in available_rules:
                next_rules = list(dict.fromkeys([*all_rules, new_rule]))
            else:
                replaced = [new_rule if item == old_rule else item for item in all_rules]
                next_rules = list(dict.fromkeys(replaced))

            persist = self._persist_governance_rules(next_rules)
            if persist is not None:
                return "app:update-governance-rule_result", persist
            return "app:update-governance-rule_result", {
                "ok": True,
                "rules": next_rules,
                "active_rules": next_rules,
                "available_rules": next_rules,
                "scope": "global",
                "message": "rule updated globally",
            }

        return f"{command}_result", {
            "ok": False,
            "message": f"unsupported governance command: {command}",
        }

    def _persist_governance_rules(self, rules: list[str]) -> Dict[str, Any] | None:
        try:
            available_raw = getattr(self.app, "AVAILABLE_GOVERNANCE_RULES", [])
            available_rules = [
                str(item).strip() for item in available_raw if str(item).strip()
            ]
            normalized_rules = [
                str(item).strip() for item in rules if str(item).strip()
            ]
            active_rules = list(dict.fromkeys([*available_rules, *normalized_rules]))
            setattr(self.app, "governance_rules", active_rules)
            saver = getattr(self.app, "_save_governance_rules", None)
            if callable(saver):
                saver()
            return None
        except Exception as exc:
            return {"ok": False, "message": f"save governance rules failed: {exc}"}

    async def _focus_provider(self, provider: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        if not self.session:
            return f"focus_{provider}_result", {"ok": False, "message": "Browser session is not available"}

        target = str(payload.get("target", "main") or "main")
        try:
            if provider == "chatgpt":
                await self.session.focus_chatgpt(target)
                provider_status = str(getattr(self.session, "health_state", {}).get(provider, "UNKNOWN"))
                verification_required = provider_status == "UNAUTHENTICATED"
                return "focus_chatgpt_result", {
                    "ok": True,
                    "provider": provider,
                    "target": target,
                    "provider_status": provider_status,
                    "verification_required": verification_required,
                    "message": (
                        "請改用系統瀏覽器完成登入/驗證，完成後返回工具。"
                        if verification_required
                        else "browser focused"
                    ),
                }
            await self.session.focus_gemini(target)
            provider_status = str(getattr(self.session, "health_state", {}).get(provider, "UNKNOWN"))
            verification_required = provider_status == "UNAUTHENTICATED"
            return "focus_gemini_result", {
                "ok": True,
                "provider": provider,
                "target": target,
                "provider_status": provider_status,
                "verification_required": verification_required,
                "message": (
                    "請改用系統瀏覽器完成登入/驗證，完成後返回工具。"
                    if verification_required
                    else "browser focused"
                ),
            }
        except Exception as exc:
            return f"focus_{provider}_result", {"ok": False, "message": str(exc)}

    async def _change_provider_url(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        if not self.session:
            return "change_provider_url_result", {"ok": False, "message": "Browser session is not available"}

        provider = str(payload.get("provider", "")).strip().lower()
        target = str(payload.get("target", "main")).strip().lower() or "main"
        url = str(payload.get("url", "")).strip()

        if provider not in {"chatgpt", "gemini"}:
            return "change_provider_url_result", {"ok": False, "message": "Invalid provider"}
        if target not in {"main", "developer", "audit"}:
            return "change_provider_url_result", {"ok": False, "message": "Invalid target"}
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            return "change_provider_url_result", {"ok": False, "message": "Invalid URL"}

        try:
            await self.session.set_provider_url(provider, target, url)
            return "change_provider_url_result", {
                "ok": True,
                "provider": provider,
                "target": target,
                "url": url,
            }
        except Exception as exc:
            return "change_provider_url_result", {"ok": False, "message": str(exc)}

    @staticmethod
    def _normalize_discussion_mode(mode: str) -> str:
        value = str(mode or "").strip().lower()
        mapping = {
            "gpt_first": "chatgpt_first",
            "chatgpt_first": "chatgpt_first",
            "gemini_first": "gemini_first",
            "ask_both": "ask_both",
            "mutual_review": "mutual_review",
            "gpt_only": "gpt_only",
            "gemini_only": "gemini_only",
        }
        return mapping.get(value, "chatgpt_first")

    async def _discussion_query(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        if self.orchestrator is None:
            return "discussion_result", {"ok": False, "message": "Orchestrator is not available"}

        prompt = str(payload.get("text") or payload.get("prompt") or "").strip()
        if not prompt:
            return "discussion_result", {"ok": False, "message": "Prompt is empty"}

        mode = self._normalize_discussion_mode(str(payload.get("mode", "chatgpt_first")))
        try:
            result = await self.orchestrator.discussion_query(prompt, mode)
            return "discussion_result", result
        except Exception as exc:
            return "discussion_result", {"ok": False, "message": str(exc)}

    async def _check_open_provider_status(self, provider_name: str, provider: Any) -> SessionStatusEnum:
        """Check provider health without forcing browser tabs to open."""
        session = self.session
        if session is None or provider is None:
            return SessionStatusEnum.UNOPENED

        raw_provider = getattr(provider, "_provider", provider)
        page = getattr(raw_provider, "page", None)
        session_page = getattr(session, f"{provider_name}_page", None)

        def page_open(target: Any) -> bool:
            try:
                return target is not None and not target.is_closed()
            except Exception:
                return False

        if not page_open(page) and page_open(session_page):
            raw_provider.page = session_page
            page = session_page

        if not page_open(page):
            return SessionStatusEnum.UNOPENED

        try:
            status = await raw_provider.check_session_health()
            if isinstance(status, SessionStatusEnum):
                return status
            value = str(getattr(status, "value", status))
            return SessionStatusEnum(value) if value in SessionStatusEnum._value2member_map_ else SessionStatusEnum.WARNING
        except Exception:
            return SessionStatusEnum.ERROR
