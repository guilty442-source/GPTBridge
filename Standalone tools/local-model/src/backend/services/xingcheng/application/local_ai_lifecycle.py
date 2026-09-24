from __future__ import annotations

import asyncio
import threading
from typing import Any

from ..integration.channel_client import build_star_ai_channel_client
from .xingcheng_commands import get_xingcheng_registry, resolve_command
from .command_parser import validate_parameters


class LocalAiLifecycleMixin:
    def owns(self, command: str) -> bool:
        registry = get_xingcheng_registry()
        return resolve_command(command) is not None

    def bind_channel(self, channel: Any) -> None:
        self._ai_channel_client = build_star_ai_channel_client(channel)
        self.external_research.bind_channel(channel)

    def begin_request(self, request_id: str) -> threading.Event:
        event = threading.Event()
        self._request_cancel_events[str(request_id)] = event
        return event

    def finish_request(self, request_id: str) -> None:
        self._request_cancel_events.pop(str(request_id), None)

    async def cancel_request(self, request_id: str) -> bool:
        event = self._request_cancel_events.get(str(request_id))
        if event is None:
            return False
        event.set()
        return True

    async def _handle_self_learning(
        self, command: str, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """``xingcheng_self_learning_cycle``：受管排程觸發的一輪自我學習。

        §1.1/A554：main-system ``self_learning_driver`` 經 AutomationCore
        排程、由 governed system channel 送達本行程；循環必須在工具
        行程內執行——``inference_exclusion``（§2.7-4）檢查的是行程本地
        engine cache，外掛行程無法判定。
        重入鎖保證 lease 重排／連續 tick 不會並行兩輪訓練；所有政策閘
        （enabled、min_new_examples、min_interval、quiet hours、GPU
        退避、熔斷、每日上限）由 ``run_cycle`` 權威判定，本 handler 不
        複製任何閘門。
        """
        if command != "xingcheng_self_learning_cycle":
            return "error", {
                "ok": False,
                "error_code": "UNKNOWN_COMMAND",
                "message": f"未知命令: {command}",
            }
        lock = self._self_learning_cycle_lock
        if not lock.acquire(blocking=False):
            return "xingcheng_self_learning_cycle_result", {
                "ok": True,
                "action": "already-running",
                "reason": "another self-learning cycle is in flight",
            }
        # 鎖的釋放在執行緒函式內的 finally——若 governed worker 取消本
        # coroutine（request_cancelled），to_thread 的訓練執行緒仍在跑；
        # 在 coroutine 層釋放會讓下一輪請求誤判空閒而並行第二輪訓練。
        result = await asyncio.to_thread(
            self._run_self_learning_cycle, payload
        )
        return "xingcheng_self_learning_cycle_result", result

    def _run_self_learning_cycle(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            from ..infrastructure.native_transformer.self_learning import (
                run_cycle,
            )

            return run_cycle(
                self.tool_root,
                force=bool(payload.get("force")),
            )
        except Exception as exc:  # noqa: BLE001 — 循環結果必須回到請求方
            return {
                "ok": False,
                "action": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            self._self_learning_cycle_lock.release()

    async def _handle_retention_sweep(
        self, command: str, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """``xingcheng_retention_sweep``：受管排程觸發的一輪保留清理。

        §10.67：self-learning 停用時 retention 不能跟著死亡——main-system
        的 ``retention`` periodic flow 經 governed channel 送達本行程
        執行（檔案操作需在工具行程內判定 lifecycle/native-engine 受保護
        路徑）。獨立輕鎖防止重入；retention 政策（``retention.json`` 的
        ``enabled``）由 ``apply_retention`` 權威判定。
        """
        if command != "xingcheng_retention_sweep":
            return "error", {
                "ok": False,
                "error_code": "UNKNOWN_COMMAND",
                "message": f"未知命令: {command}",
            }
        lock = self._retention_sweep_lock
        if not lock.acquire(blocking=False):
            return "xingcheng_retention_sweep_result", {
                "ok": True,
                "action": "already-running",
                "reason": "another retention sweep is in flight",
            }
        # 同 self-learning：釋放由執行緒完成時做，coroutine 取消不提早放鎖。
        result = await asyncio.to_thread(self._run_retention_sweep)
        return "xingcheng_retention_sweep_result", result

    def _run_retention_sweep(self) -> dict[str, Any]:
        try:
            from ..infrastructure.native_transformer.retention import (
                apply_retention,
            )

            return apply_retention(self.tool_root)
        except Exception as exc:  # noqa: BLE001 — 結果必須回到請求方
            return {
                "ok": False,
                "action": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            self._retention_sweep_lock.release()

    async def start(self) -> None:
        # Local-model bound with Ollama (§10.7 on-demand): when local-model
        # is opened, ensure Ollama is also ready as needed (fail-closed if
        # unavailable, but never block model startup).
        try:
            import sys
            from pathlib import Path as _P
            # local-model's TOOL_ROOT is two levels above this file's parent
            _sys_root = _P(__file__).resolve().parents[5]  # -> Standalone tools/local-model
            # main-system is sibling of Standalone tools
            _main_root = _sys_root.parents[1] / "main-system" / "src-core"
            if str(_main_root) not in sys.path:
                sys.path.insert(0, str(_main_root))
            from core_system.ollama_demand import ensure_ollama_ready, ollama_installed, probe_ollama
            if ollama_installed() and not probe_ollama(timeout=0.5):
                # Fire-and-forget with bounded wait; model startup must not hang.
                await asyncio.to_thread(ensure_ollama_ready, timeout_s=8.0)
        except Exception:
            pass
        await asyncio.gather(
            asyncio.to_thread(self._run_self_maintenance),
            asyncio.to_thread(self.transformer_runtime.probe),
        )
        if (
            self.transformer_runtime.enabled
            and self._internal_maintenance_loop_task is None
        ):
            self._default_model_preload_task = asyncio.create_task(
                asyncio.to_thread(
                    self.transformer_runtime.resource_manager.preload_model,
                    self.transformer_runtime.MODEL,
                    keep_alive=-1,
                )
            )
            self._internal_maintenance_loop_task = asyncio.create_task(
                self._internal_maintenance_loop()
            )

    async def shutdown(self) -> None:
        preload = self._default_model_preload_task
        self._default_model_preload_task = None
        if preload is not None:
            await asyncio.gather(preload, return_exceptions=True)
        task = self._internal_maintenance_loop_task
        self._internal_maintenance_loop_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        training = self._internal_training_task
        self._internal_training_task = None
        if training is not None and not training.done():
            training.cancel()
            await asyncio.gather(training, return_exceptions=True)

    async def _internal_maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            if self._request_cancel_events or not self._internal_training_due():
                continue
            self._internal_training_task = asyncio.create_task(
                self._run_internal_ollama_training()
            )
            await asyncio.gather(
                self._internal_training_task,
                return_exceptions=True,
            )

    async def handle(self, command: str, payload: dict[str, Any], _latest: Any = None
    ) -> tuple[str, dict[str, Any]]:
        """Handle incoming command using the new command registry."""
        # Resolve command with fuzzy matching
        spec = resolve_command(command)

        if spec is None:
            from .xingcheng_commands import get_xingcheng_registry
            registry = get_xingcheng_registry()
            # The registry may have been empty on first use; resolve again
            # once it has been populated.
            spec = resolve_command(command)
        if spec is None:
            suggestions = registry.get_suggestions(command)
            if suggestions:
                return "error", {
                    "ok": False,
                    "error_code": "UNKNOWN_COMMAND",
                    "message": f"未知命令: {command}",
                    "suggestions": suggestions,
                }
            return "error", {
                "ok": False,
                "error_code": "UNKNOWN_COMMAND",
                "message": f"未知命令: {command}",
            }

        # Dispatch to appropriate handler based on command name
        handler_name = spec.handler
        handler = getattr(self, handler_name, None)

        if handler is None:
            return "error", {
                "ok": False,
                "error_code": "HANDLER_NOT_FOUND",
                "message": f"命令處理器未找到: {spec.handler}",
            }

        # Validate parameters if spec has parameters
        if spec.parameters:
            from .command_parser import validate_parameters
            try:
                validated_payload = validate_parameters(payload, spec)
                payload = validated_payload
            except Exception as e:
                return "error", {
                    "ok": False,
                    "error_code": "PARAM_VALIDATION_ERROR",
                    "message": str(e),
                }

        # Call the handler
        return await handler(command, payload)
