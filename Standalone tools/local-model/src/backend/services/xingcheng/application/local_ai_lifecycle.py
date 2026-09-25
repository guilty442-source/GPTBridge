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

    async def _handle_web_search(
        self, command: str, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """``xingcheng_web_search``：受管 SearXNG loopback 搜尋。

        A177：對外查詢只允許資訊層治理通道；此命令是五核心稽核
        ``xingcheng`` 收據的外部證據來源（audit gate 的
        ``build_xingcheng_network_check`` 以此 callable 為準）。只回傳
        metadata（標題/URL/網域/截斷摘要），不攜帶完整頁面內容。
        """
        if command != "xingcheng_web_search":
            return "error", {
                "ok": False,
                "error_code": "UNKNOWN_COMMAND",
                "message": f"未知命令: {command}",
            }
        query = str(payload.get("query") or "").strip()
        if not query:
            return "xingcheng_web_search_result", {
                "ok": False,
                "error": "QUERY_REQUIRED",
            }
        result = await asyncio.to_thread(
            self._run_web_search,
            query,
            int(payload.get("max_results") or 5),
        )
        return "xingcheng_web_search_result", result

    def _web_search_settings(self) -> dict[str, Any]:
        """``runtime/settings/web-search.json``（provider 選擇政策檔）。"""
        import json
        from pathlib import Path

        try:
            data = json.loads(
                (Path(self.tool_root) / "runtime/settings/web-search.json")
                .read_text(encoding="utf-8-sig")
            )
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _ensure_searchd(self, url: str, auto_start: bool) -> bool:
        """確認 searchd loopback 服務可用；必要時惰性啟動受管 binary。"""
        import json
        import subprocess
        import time
        import urllib.request
        from urllib.parse import urlsplit
        from pathlib import Path

        def healthy() -> bool:
            try:
                req = urllib.request.Request(
                    f"{url.rstrip('/')}/healthz",
                    headers={"User-Agent": "XingCheng/1.0"},
                )
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    return json.loads(resp.read().decode("utf-8")).get("ok") is True
            except Exception:
                return False

        if healthy():
            return True
        if not auto_start:
            return False
        binary = Path(self.tool_root).parent / "searchd-go" / "bin" / "searchd.exe"
        if not binary.is_file() and not self._build_searchd(binary):
            return False
        # 啟動時帶上 url 指定的 host:port——與 healthz 探測的端點一致；
        # 非 loopback 位址由 searchd 自身拒絕（fail-closed），這裡不再重複判斷。
        listen = ""
        try:
            parts = urlsplit(url)
            if parts.hostname and parts.port:
                listen = f"{parts.hostname}:{parts.port}"
        except ValueError:
            listen = ""
        argv = [str(binary)] + (["--listen", listen] if listen else [])
        try:
            subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0),
            )
        except OSError:
            return False
        for _ in range(30):  # 最長等 3 秒
            if healthy():
                return True
            time.sleep(0.1)
        return False

    def _build_searchd(self, binary: "Path") -> bool:
        """bin/ 是 gitignore 產物——fresh checkout 缺 binary 時用 vendored
        Go 工具鏈就地建置（GOPROXY=off：go.mod 無外部依賴）。失敗 fail-closed。"""
        import os
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[7]
        module_dir = repo_root / "Standalone tools" / "searchd-go"
        go_exe = repo_root / ".tools" / "go" / "go" / "bin" / "go.exe"
        if not go_exe.is_file() or not (module_dir / "go.mod").is_file():
            return False
        env = os.environ.copy()
        env.update(
            {
                "GOCACHE": str(repo_root / ".tools" / "gocache"),
                "GOFLAGS": "-buildvcs=false",
                "GOPROXY": "off",
                "GOSUMDB": "off",
                "GOTOOLCHAIN": "local",
                "GOWORK": "off",
            }
        )
        try:
            proc = subprocess.run(
                [str(go_exe), "build", "-o", str(binary), "./cmd/searchd"],
                cwd=str(module_dir),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0 and binary.is_file()

    def _web_search_providers(self) -> list[Any]:
        """依 settings/env 組出有序 provider 鏈（auto = searchd→searxng）。"""
        import os

        from ..infrastructure.xingcheng_tools.search.searxng import (
            SearXNGProvider,
        )
        from ..infrastructure.xingcheng_tools.search.searchd import (
            SearchdProvider,
        )

        settings = self._web_search_settings()
        mode = str(
            os.environ.get("XINGCHENG_SEARCH_PROVIDER")
            or settings.get("provider")
            or "auto"
        ).strip().lower()
        searchd_url = str(
            os.environ.get("XINGCHENG_SEARCHD_URL")
            or settings.get("searchd_url")
            or "http://127.0.0.1:8091"
        )
        searxng_url = str(
            os.environ.get("XINGCHENG_SEARXNG_URL")
            or settings.get("searxng_url")
            or "http://127.0.0.1:8080"
        )
        auto_start = settings.get("auto_start") is not False

        providers: list[Any] = []
        if mode in {"auto", "searchd"} and self._ensure_searchd(searchd_url, auto_start):
            providers.append(SearchdProvider(searchd_url))
        if mode in {"auto", "searxng"}:
            providers.append(SearXNGProvider(searxng_url))
        return providers

    def _run_web_search(self, query: str, max_results: int) -> dict[str, Any]:
        try:
            from ..infrastructure.xingcheng_tools.search.types import (
                SearchRequest,
            )

            providers = self._web_search_providers()
            if not providers:
                raise RuntimeError("no governed search provider available")

            request = SearchRequest(
                original_question=query,
                queries=[query],
                max_results=max_results,
            )
            last_error: Exception | None = None
            used_provider: Any | None = None
            used_results: list[Any] = []
            for provider in providers:
                try:
                    results = provider.search(request)
                except Exception as exc:  # noqa: BLE001 — 降級到下一 provider
                    last_error = exc
                    continue
                used_provider, used_results = provider, results
                if results:
                    break
                # 空結果可能是上游全掛而非真空無結果——鏈上還有
                # provider 時繼續嘗試，全部空才回空集合。
            if used_provider is None:
                raise last_error or RuntimeError("all search providers failed")
            return {
                "ok": True,
                "source": "xingcheng-web-search",
                "provider": used_provider.name,
                "query": query,
                "result_count": len(used_results),
                "adapters": getattr(used_provider, "last_adapter_status", []),
                "results": [
                    {
                        "title": item.title,
                        "url": item.url,
                        "domain": item.domain,
                        "snippet": str(item.snippet or "")[:300],
                        "provider": item.provider,
                        "rank": item.rank,
                    }
                    for item in used_results
                ],
            }
        except Exception as exc:  # noqa: BLE001 — fail closed, record type
            return {
                "ok": False,
                "source": "xingcheng-web-search",
                "query": query,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def start(self) -> None:
        await asyncio.gather(
            asyncio.to_thread(self._run_self_maintenance),
            asyncio.to_thread(self.native_runtime.probe),
        )
        if (
            self.native_runtime.enabled
            and self._internal_maintenance_loop_task is None
        ):
            self._default_model_preload_task = asyncio.create_task(
                asyncio.to_thread(self.native_runtime.preload)
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
                self._run_internal_native_training()
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
