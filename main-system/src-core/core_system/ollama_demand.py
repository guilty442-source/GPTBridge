"""§10.7 Ollama 按需啟動（有需要才啟動；fail-closed）。

Ollama 服務不常駐：需求偵測（探針未達）→ 受治理本地 spawn → 就緒等待 →
完成後可卸載。啟動失敗如實回報（回傳 ``False``，由呼叫方 fail-closed），
不得靜默改走未授權路徑。關閉後不得預熱或自動復活——只有新的明確需求
（一次 ``ensure_ollama_ready`` 呼叫）才可再啟動。

稽核：每次需求啟動嘗試 append ``runtime/state/ollama-demand.jsonl``；
呼叫方持有 ``ProcessRegistry`` 時一併登錄 pid（所有權界線：只有
GPTBridge 啟動的 Ollama 才可由其停止，見 ``process_registry.is_owned``）。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger("gptbridge.ollama_demand")

OLLAMA_HOST = "127.0.0.1"
OLLAMA_PORT = 11434
DEFAULT_IDLE_UNLOAD_S = 900.0

_AUDIT_PATH = (
    Path(__file__).resolve().parents[2]
    / "runtime" / "state" / "ollama-demand.jsonl"
)
_STATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "runtime" / "state" / "ollama-demand-state.json"
)
_STATE_LOCK = threading.Lock()


def probe_ollama(*, timeout: float = 0.75) -> bool:
    """經受管資訊邊界探針（registered service，5s 快取）。"""
    try:
        from shared_layer.service_probe import probe_registered_local_service

        return bool(
            probe_registered_local_service("ollama", timeout=timeout).reachable
        )
    except Exception:
        # 探針不可用 → fail-closed 視為未就緒
        return False


def _probe_tcp(timeout: float = 0.75) -> bool:
    """無快取直探——僅供就緒等待迴圈使用（邊界檢查由 probe_ollama 做過）。"""
    try:
        with socket.create_connection(
            (OLLAMA_HOST, OLLAMA_PORT), timeout=max(0.05, timeout)
        ):
            return True
    except OSError:
        return False


def ollama_installed() -> bool:
    """安裝探測：本機存在 Ollama 可執行檔（或 PATH 可解析）。

    與 ``probe_ollama`` 分工：reachable = 服務活著；installed = 能力可被
    按需拉起。就緒語義以 installed 判定能力，reachable 判定當前狀態。"""
    appdata = os.environ.get("LOCALAPPDATA", "")
    if appdata:
        root = Path(appdata) / "Programs" / "Ollama"
        if (root / "ollama app.exe").is_file() or (
            root / "ollama.exe"
        ).is_file():
            return True
    return shutil.which("ollama") is not None


def _demand_state() -> dict[str, Any]:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _write_demand_state(state: dict[str, Any]) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_STATE_PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, _STATE_PATH)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _touch_use(pid: Optional[int] = None) -> None:
    """記錄一次需求使用（每次 ensure 呼叫即為需求）；spawn 成功連帶記 pid。"""
    with _STATE_LOCK:
        state = _demand_state()
        state["last_use"] = time.time()
        if pid:
            state["spawned_pid"] = int(pid)
        _write_demand_state(state)


def _spawn_ollama() -> tuple[int | None, str]:
    """受治理本地 spawn（僅供 ensure_ollama_ready 需求啟動使用）。

    回傳 ``(pid, cmdline)``；找不到可執行檔或 spawn 失敗回 ``(None, "")``。
    """
    appdata = os.environ.get("LOCALAPPDATA", "")
    ollama_root = Path(appdata) / "Programs" / "Ollama"
    app = ollama_root / "ollama app.exe"
    server = ollama_root / "ollama.exe"
    cmd = (
        [str(app)]
        if app.is_file()
        else [str(server), "serve"] if server.is_file() else None
    )
    if cmd is None:
        return None, ""
    creationflags = (
        int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
        | int(getattr(subprocess, "DETACHED_PROCESS", 0) or 0)
    )
    try:
        proc = subprocess.Popen(  # noqa: S603 — governed local tool spawn
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as error:
        _logger.warning("ollama spawn failed: %s", error)
        return None, ""
    return proc.pid, " ".join(cmd)


def _audit(event: str, **fields: Any) -> None:
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        **fields,
    }
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def ensure_ollama_ready(
    timeout_s: float = 15.0,
    *,
    process_registry: Any = None,
) -> bool:
    """Ollama 有需要才啟動。

    已可達 → 直接 ``True``；未達 → spawn → 輪詢至就緒或逾時。
    ``process_registry`` 為可選——持有受管 ProcessRegistry 的呼叫方
    傳入後會把 spawn 的 pid 登錄（module_id=``ollama``）。
    """
    started = time.monotonic()
    _touch_use()
    if probe_ollama():
        return True
    pid, cmd = _spawn_ollama()
    if pid is None:
        _audit("spawn-unavailable")
        _logger.warning("ollama demand-start: executable not found or spawn failed")
        return False
    _audit("spawn", pid=pid, cmd=cmd)
    _touch_use(pid)
    registry = process_registry
    if registry is None:
        try:
            from core_system.process_registry import get_process_registry

            registry = get_process_registry()
        except Exception:
            registry = None
    if registry is not None:
        try:
            registry.register(
                pid,
                module_id="ollama",
                executable=cmd,
                request_id="ollama-demand",
            )
        except Exception:
            pass
    deadline = started + max(0.5, float(timeout_s))
    while time.monotonic() < deadline:
        if _probe_tcp(timeout=0.5):
            _audit(
                "ready",
                pid=pid,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            return True
        time.sleep(0.5)
    _audit(
        "timeout",
        pid=pid,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    return False


def stop_ollama_if_owned(idle_s: float = DEFAULT_IDLE_UNLOAD_S) -> bool:
    """完成後卸載：僅終止『本系統 spawn 且仍 owned』且已閒置 ``idle_s`` 的 Ollama。

    所有權界線由 ProcessRegistry 判定（``is_owned``）；外加 exe 影像名稱
    複核以防 pid 回收誤殺。外部自行啟動的 Ollama 永不觸碰。
    回傳是否實際執行停止。"""
    state = _demand_state()
    pid = state.get("spawned_pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        from core_system.process_registry import get_process_registry

        registry = get_process_registry()
    except Exception:
        return False
    if not registry.is_owned(pid):
        return False
    last_use = float(state.get("last_use") or 0.0)
    if time.time() - last_use < max(0.0, float(idle_s)):
        return False
    try:
        from shared_layer.performance.process_metrics import (
            process_exe,
            process_terminate,
        )
    except Exception:
        return False
    exe = (process_exe(pid) or "").lower()
    if "ollama" not in os.path.basename(exe):
        _audit("unload-refused", pid=pid, reason="image-mismatch")
        return False
    if not process_terminate(pid):
        _audit("unload-failed", pid=pid)
        return False
    registry.mark_shutdown(pid, "exited")
    _audit("unload", pid=pid, idle_s=round(time.time() - last_use, 1))
    return True


__all__ = [
    "DEFAULT_IDLE_UNLOAD_S",
    "OLLAMA_HOST",
    "OLLAMA_PORT",
    "ensure_ollama_ready",
    "ollama_installed",
    "probe_ollama",
    "stop_ollama_if_owned",
]
