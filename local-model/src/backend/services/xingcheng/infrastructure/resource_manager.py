from __future__ import annotations

import http.client
import gc
import json
import os
import platform
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any


JsonTransport = Callable[[str, str, dict[str, Any] | None, float], dict[str, Any]]


class ResourceManager:
    """Own local CPU/RAM/GPU/VRAM allocation and Ollama model residency."""

    MODES = frozenset({"auto", "cpu", "gpu"})

    def __init__(
        self,
        *,
        endpoint: str = "http://127.0.0.1:11434",
        transport: JsonTransport | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        resident_models: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        self.endpoint = self._validated_endpoint(endpoint)
        self._transport = transport or self._http_json
        self._command_runner = command_runner or subprocess.run
        self._resident_models = frozenset(
            str(model).strip() for model in resident_models if str(model).strip()
        )
        self._mode = "auto"
        self._active_model: str | None = None

    @staticmethod
    def _validated_endpoint(value: str) -> str:
        parsed = urllib.parse.urlparse(str(value or "").strip())
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("RESOURCE_MANAGER_ENDPOINT_MUST_BE_LOOPBACK")
        port = parsed.port or 11434
        host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
        return f"http://{host}:{port}"

    @staticmethod
    def _http_json(method: str, url: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        parsed = urllib.parse.urlparse(str(url))
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 11434
        path = parsed.path or "/"
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            connection.request(
                method, path, body=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)) if body else "0",
                    "Connection": "keep-alive",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError(f"RESOURCE_MANAGER_HTTP_{response.status}")
            raw = response.read(2_000_001)
        except http.client.HTTPException as error:
            raise RuntimeError(f"RESOURCE_MANAGER_HTTP_ERROR:{str(error)[:200]}") from error
        finally:
            try:
                connection.close()
            except Exception:
                pass
        if len(raw) > 2_000_000:
            raise RuntimeError("RESOURCE_MANAGER_RESPONSE_TOO_LARGE")
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise RuntimeError("RESOURCE_MANAGER_RESPONSE_INVALID")
        return decoded

    def _nvidia_rows(self) -> list[dict[str, Any]]:
        executable = shutil.which("nvidia-smi")
        if executable is None:
            return []
        try:
            result = self._command_runner(
                [executable, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []
        rows: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            fields = [field.strip() for field in line.split(",")]
            if len(fields) != 5:
                continue
            try:
                index, name, usage, used, total = fields
                rows.append({
                    "index": int(index), "name": name, "usage_percent": float(usage),
                    "vram_used_mb": float(used), "vram_total_mb": float(total),
                    "vram_percent": round(float(used) * 100 / float(total), 2) if float(total) else 0.0,
                })
            except ValueError:
                continue
        return rows

    def _ollama_command(self, *arguments: str) -> subprocess.CompletedProcess[str] | None:
        executable = shutil.which("ollama")
        if executable is None:
            return None
        try:
            return self._command_runner(
                [executable, *arguments], capture_output=True, text=True,
                timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _running_models(self) -> tuple[str, ...]:
        try:
            running = self._transport("GET", f"{self.endpoint}/api/ps", None, 10.0)
            return tuple(dict.fromkeys(
                str(item.get("name") or item.get("model") or "").strip()
                for item in running.get("models", []) if isinstance(item, Mapping)
                and str(item.get("name") or item.get("model") or "").strip()
            ))
        except (OSError, RuntimeError, ValueError):
            result = self._ollama_command("ps")
            if result is None or result.returncode != 0:
                raise RuntimeError("OLLAMA_RESIDENCY_QUERY_FAILED")
            lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
            return tuple(row[0] for row in lines[1:] if row)

    def detect_hardware(self) -> dict[str, Any]:
        gpus = self._nvidia_rows()
        ram = self.get_ram_usage()
        return {
            "platform": platform.system(),
            "cpu": platform.processor() or platform.machine(),
            "cpu_count": os.cpu_count() or 1,
            "ram_total_bytes": ram["total_bytes"],
            "gpus": gpus,
            "gpu_available": bool(gpus),
            "mode": self._mode,
        }

    def get_cpu_usage(self) -> dict[str, Any]:
        try:
            import psutil  # type: ignore[import-not-found]
            return {"percent": float(psutil.cpu_percent(interval=0.1)), "cpu_count": psutil.cpu_count() or 1}
        except ImportError:
            load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
            count = os.cpu_count() or 1
            return {"percent": round(min(100.0, load * 100 / count), 2), "cpu_count": count}

    def get_ram_usage(self) -> dict[str, Any]:
        try:
            import psutil  # type: ignore[import-not-found]
            memory = psutil.virtual_memory()
            total, available, percent = int(memory.total), int(memory.available), float(memory.percent)
        except ImportError:
            total = available = 0
            percent = 0.0
            if os.name == "nt":
                import ctypes

                class MemoryStatus(ctypes.Structure):
                    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                                ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                                ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong),
                                ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                                ("available_extended", ctypes.c_ulonglong)]
                status = MemoryStatus()
                status.length = ctypes.sizeof(status)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                    total, available, percent = int(status.total), int(status.available), float(status.load)
        return {"total_bytes": total, "used_bytes": max(0, total - available),
                "available_bytes": available, "percent": percent}

    def get_gpu_usage(self) -> dict[str, Any]:
        rows = self._nvidia_rows()
        return {"available": bool(rows), "devices": [
            {"index": row["index"], "name": row["name"], "percent": row["usage_percent"]} for row in rows
        ]}

    def get_vram_usage(self) -> dict[str, Any]:
        rows = self._nvidia_rows()
        return {"available": bool(rows), "devices": [
            {key: row[key] for key in ("index", "name", "vram_used_mb", "vram_total_mb", "vram_percent")}
            for row in rows
        ]}

    def _num_gpu(self, mode: str | None = None) -> int | None:
        selected_mode = mode or self._mode
        if selected_mode == "cpu":
            return 0
        if selected_mode == "gpu":
            if not self._nvidia_rows():
                raise RuntimeError("GPU_NOT_AVAILABLE")
            return -1
        return None

    def preload_model(
        self, model: str, *, keep_alive: int | str = -1, device: str | None = None
    ) -> dict[str, Any]:
        name = str(model or "").strip()
        if not name:
            raise ValueError("MODEL_REQUIRED")
        if self.verify_gpu_residency(name):
            self._active_model = name
            return {"model": name, "mode": device or self._mode, "loaded": True, "response": {}}
        options: dict[str, Any] = {}
        selected_mode = str(device or self._mode).strip().casefold()
        if selected_mode not in self.MODES:
            raise ValueError("INVALID_RESOURCE_MODE")
        num_gpu = self._num_gpu(selected_mode)
        if num_gpu is not None:
            options["num_gpu"] = num_gpu
        result = self._transport("POST", f"{self.endpoint}/api/generate",
                                 {"model": name, "prompt": "", "stream": False,
                                  "keep_alive": keep_alive, "options": options}, 120.0)
        self._active_model = name
        # Bounded exponential backoff instead of fixed 0.05s polling.
        # The /api/generate call above already loads the model; the poll
        # is just a confirmation.  Start at 0.02s, double each iteration.
        deadline = time.monotonic() + 2.0
        delay = 0.02
        loaded = self.verify_gpu_residency(name)
        while not loaded and time.monotonic() < deadline:
            time.sleep(delay)
            delay = min(delay * 1.5, 0.2)
            loaded = self.verify_gpu_residency(name)
        return {"model": name, "mode": selected_mode, "loaded": loaded, "response": result}

    def unload_model(self, model: str) -> bool:
        name = str(model or "").strip()
        if not name:
            raise ValueError("MODEL_REQUIRED")
        if not self.verify_gpu_residency(name):
            return True
        try:
            self._transport("POST", f"{self.endpoint}/api/generate",
                            {"model": name, "prompt": "", "stream": False, "keep_alive": 0}, 30.0)
        except (OSError, RuntimeError, ValueError):
            result = self._ollama_command("stop", name)
            if result is None or result.returncode != 0:
                raise RuntimeError(f"MODEL_UNLOAD_FAILED:{name}")
        if self._active_model == name:
            self._active_model = None
        released = not self.verify_gpu_residency(name)
        deadline = time.monotonic() + 2.0
        while not released and time.monotonic() < deadline:
            time.sleep(0.05)
            released = not self.verify_gpu_residency(name)
        if not released:
            result = self._ollama_command("stop", name)
            if result is not None and result.returncode == 0:
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if not self.verify_gpu_residency(name):
                        released = True
                        break
                    time.sleep(0.05)
        return released

    def unload_all_models(self) -> tuple[str, ...]:
        names = self._running_models()
        for name in filter(None, names):
            self.unload_model(name)
        self._active_model = None
        return tuple(filter(None, names))

    def release_failed_model(self, model: str) -> bool:
        """Best-effort cleanup after an allocation or inference failure."""
        try:
            return self.unload_model(model)
        except (OSError, RuntimeError, ValueError):
            return False

    def keep_alive_for(
        self,
        model: str,
        configured_keep_alive: int | str,
        *,
        release_after_request: bool = False,
    ) -> int | str:
        """Centralize model residency policy for every Ollama request."""
        if str(model).strip() in self._resident_models:
            return configured_keep_alive
        return 0 if release_after_request else configured_keep_alive

    def switch_to_gpu(self) -> str:
        if not self._nvidia_rows():
            raise RuntimeError("GPU_NOT_AVAILABLE")
        self._mode = "gpu"
        return self._mode

    def switch_to_cpu(self) -> str:
        self._mode = "cpu"
        return self._mode

    def switch_to_auto(self) -> str:
        self._mode = "auto"
        return self._mode

    def verify_gpu_residency(self, model: str) -> bool:
        name = str(model or "").strip()
        return name in self._running_models()

    def prepare_model(
        self, model: str, *, keep_alive: int | str = -1, required_bytes: int = 0
    ) -> dict[str, Any]:
        before = {"ram": self.get_ram_usage(), "vram": self.get_vram_usage()}
        running = self._running_models()
        if self.verify_gpu_residency(model):
            self._active_model = model
            return {"model": model, "device": self._mode, "released_models": (),
                    "resources_before": before, "resident": True, "already_loaded": True}
        old_models = tuple(
            name for name in running
            if name != model and name not in self._resident_models
        )
        for old_model in dict.fromkeys(filter(None, old_models)):
            self.unload_model(old_model)
        selected_mode = self._mode
        if selected_mode == "auto":
            gpus = self._nvidia_rows()
            required_mb = max(0, int(required_bytes)) / (1024 * 1024)
            enough_vram = any(
                row["vram_total_mb"] - row["vram_used_mb"] >= required_mb * 1.1
                for row in gpus
            )
            selected_mode = "gpu" if gpus and (required_mb == 0 or enough_vram) else "cpu"
        preload_keep_alive: int | str = "30s" if keep_alive == 0 else keep_alive
        if self.verify_gpu_residency(model):
            self._active_model = model
            return {"model": model, "device": selected_mode, "released_models": old_models,
                    "resources_before": before, "resident": True, "already_loaded": True}
        loaded = self.preload_model(
            model, keep_alive=preload_keep_alive, device=selected_mode
        )
        if not loaded["loaded"]:
            raise RuntimeError("MODEL_RESIDENCY_VERIFICATION_FAILED")
        return {"model": model, "device": selected_mode, "released_models": old_models,
                "resources_before": before, "resident": True}

    def emergency_release(self) -> dict[str, Any]:
        errors: list[str] = []
        released: tuple[str, ...] = ()
        try:
            released = self.unload_all_models()
        except (OSError, RuntimeError, ValueError) as error:
            errors.append(str(error))
        collected = int(gc.collect())
        return {"released_models": released, "collected_objects": collected,
                "errors": tuple(errors), "completed_at": time.time()}


__all__ = ["ResourceManager"]
