"""Python facade for the formal C++ inference extension.

Python remains the development/training layer. This module only locates,
caches and wraps the compiled C++ runtime; it does not move model execution
back into Python or bypass the public C ABI used by the C++ engine.

Runtime selection is governed by ``XINGCHENG_CPP_RUNTIME`` (or the
``cpp_runtime`` key in ``native-engine.json``):

- ``off`` (default): Python/PyTorch path only.
- ``required``: C++ engine must serve the request; any failure is a
  fail-closed ``CPP_RUNTIME_*`` error with no silent fallback.
- ``fallback``: try C++ first; on failure record a ``cpp-runtime-fallback``
  ledger entry and use the Python engine.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .cpp_export import export_checkpoint_for_cpp

CPP_RUNTIME_ENV = "XINGCHENG_CPP_RUNTIME"
CPP_BUNDLES_DIR = "xingcheng/runtime/models/cpp-bundles"
EXECUTION_LEDGER = "xingcheng/runtime/logs/native-engine-executions.jsonl"
_VALID_MODES = {"off", "required", "fallback"}
_DEFAULT_KV_LIMIT_MB = 512


def tool_root() -> Path:
    return Path(__file__).resolve().parents[6]


def _extension_dir() -> Path:
    return tool_root() / "dist-native"


def _cuda_bin_dirs() -> list[str]:
    """CUDA bin dirs for dependent-DLL resolution (Windows ignores PATH for
    extension-module dependencies since Python 3.8)."""
    dirs: list[str] = []
    for raw in (
        os.environ.get("CUDA_PATH"),
        os.environ.get("CUDA_HOME"),
        r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.0",
    ):
        if not raw:
            continue
        bin_dir = Path(raw) / "bin"
        if bin_dir.is_dir():
            dirs.append(str(bin_dir))
    return dirs


def load_extension() -> Any:
    """Import ``_xingcheng_inference`` from dist-native or site-packages."""
    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        for cuda_bin in _cuda_bin_dirs():
            try:
                os.add_dll_directory(cuda_bin)
            except OSError:
                pass
    dist = str(_extension_dir())
    if dist not in sys.path:
        sys.path.insert(0, dist)
    return importlib.import_module("_xingcheng_inference")


def available() -> bool:
    try:
        load_extension()
    except ImportError:
        return False
    return True


def cpp_runtime_mode() -> str:
    """Governed runtime selector: env → settings → ``off``.

    A non-empty value outside ``{off, required, fallback}`` resolves to
    ``invalid`` so the router can fail closed instead of guessing.
    """
    raw = str(os.environ.get(CPP_RUNTIME_ENV) or "").strip().casefold()
    if not raw:
        from ..native_engine import load_settings

        raw = str(load_settings().get("cpp_runtime") or "").strip().casefold()
    if not raw:
        return "off"
    return raw if raw in _VALID_MODES else "invalid"


def _cpp_cuda_requested() -> bool:
    """P3f GPU 協調：CUDA opt-in 由 env → settings 決定（預設關閉）。

    啟用後仍須通過 GpuCoordinator 預算閘（`CppInferenceEngine`
    載入時判定）；任何環節拒絕皆降級 CPU——互動路徑 fail-soft，
    與 native engine `_gate_cuda_device` 同一語義。"""
    from ..native_engine import load_settings

    raw = (
        os.environ.get("XINGCHENG_CPP_CUDA")
        or load_settings().get("cpp_cuda")
        or ""
    )
    return str(raw).strip().casefold() in {"1", "true", "on", "yes"}


def _kv_memory_limit_bytes() -> int:
    from ..native_engine import load_settings

    raw = (
        os.environ.get("XINGCHENG_CPP_KV_LIMIT_MB")
        or load_settings().get("cpp_kv_memory_limit_mb")
        or _DEFAULT_KV_LIMIT_MB
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = _DEFAULT_KV_LIMIT_MB
    return max(0, value) * 1024 * 1024


def bundle_dir_for(checkpoint_path: str | Path) -> Path:
    """Deterministic bundle directory derived from the checkpoint identity."""
    path = Path(checkpoint_path)
    try:
        stat = path.stat()
        identity = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    except OSError:
        identity = str(path.resolve())
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return tool_root() / CPP_BUNDLES_DIR / f"{path.stem}-{digest}"


def _bundle_matches_source(bundle_dir: Path, checkpoint_path: Path) -> bool:
    manifest_path = bundle_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stat = checkpoint_path.stat()
        return (
            manifest.get("schema_version") == "star-native-inference-bundle/v1"
            and str(manifest.get("source_checkpoint") or "")
            == str(checkpoint_path.resolve())
            and int(manifest.get("source_size") or -1) == stat.st_size
            and int(manifest.get("source_mtime_ns") or -1) == stat.st_mtime_ns
            and (bundle_dir / str(manifest.get("weights_file") or "")).is_file()
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def ensure_bundle(checkpoint_path: str | Path) -> dict[str, Any]:
    """Return a valid C++ bundle for ``checkpoint_path``, exporting on demand.

    Reuse requires manifest schema, recorded source path/size/mtime and the
    weights blob to match; anything else re-exports through a staging
    directory so a partial export is never visible to readers.
    """
    checkpoint = Path(checkpoint_path)
    target = bundle_dir_for(checkpoint)
    if _bundle_matches_source(target, checkpoint):
        manifest = json.loads(
            (target / "manifest.json").read_text(encoding="utf-8")
        )
        return {
            "output_dir": str(target),
            "manifest": str(target / "manifest.json"),
            "weights_sha256": str(manifest.get("weights_sha256") or ""),
            "reused": True,
        }
    staging = target.with_name(target.name + ".staging")
    info = export_checkpoint_for_cpp(checkpoint, staging)
    manifest = json.loads(
        (staging / "manifest.json").read_text(encoding="utf-8")
    )
    try:
        stat = checkpoint.stat()
        manifest["source_checkpoint"] = str(checkpoint.resolve())
        manifest["source_size"] = stat.st_size
        manifest["source_mtime_ns"] = stat.st_mtime_ns
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    try:
        if target.exists():
            import shutil

            shutil.rmtree(target)
        staging.rename(target)
    except OSError:
        if not _bundle_matches_source(target, checkpoint):
            raise
    return {
        "output_dir": str(target),
        "manifest": str(target / "manifest.json"),
        "weights_sha256": str(manifest.get("weights_sha256") or info["weights_sha256"]),
        "reused": False,
    }


def sampling_config(**kwargs: Any) -> Any:
    module = load_extension()
    config = module.SamplingConfig()
    for key, value in kwargs.items():
        if not hasattr(config, key):
            raise ValueError(f"SAMPLING_OPTION_UNSUPPORTED:{key}")
        setattr(config, key, value)
    return config


def load_engine(bundle_dir: str | Path, *, kv_memory_limit: int = 0) -> Any:
    module = load_extension()
    engine = module.NativeInferenceEngine()
    if kv_memory_limit:
        engine.set_kv_memory_limit(int(kv_memory_limit))
    engine.load(str(bundle_dir))
    return engine


def _ledger_append(entry: dict[str, Any]) -> None:
    from ..native_engine import tool_root as _root

    try:
        ledger = _root() / EXECUTION_LEDGER
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


class CppInferenceEngine:
    """Governed C++ runtime engine; CPU-only, batch=1, dense FP64.

    The wrapper keeps the same generate() result contract as
    ``NativeTransformerEngine`` so governed callers do not need to know
    which layer served the request. Unsupported model features were already
    rejected at export/load time (fail-closed).
    """

    def __init__(
        self,
        checkpoint_path: str | Path,
        *,
        kv_memory_limit: int | None = None,
    ) -> None:
        self.checkpoint_path = Path(checkpoint_path)
        if not self.checkpoint_path.is_file():
            raise FileNotFoundError(
                f"CPP_RUNTIME_CHECKPOINT_MISSING:{self.checkpoint_path}"
            )
        bundle_info = ensure_bundle(self.checkpoint_path)
        self.bundle_dir = Path(bundle_info["output_dir"])
        self.weights_sha256 = str(bundle_info["weights_sha256"])
        limit = (
            kv_memory_limit
            if kv_memory_limit is not None
            else _kv_memory_limit_bytes()
        )
        self._engine = self._load_gated_engine(limit)
        self._lock = threading.Lock()

    # -- P3f GPU coordination -------------------------------------------

    def _cuda_required_mb(self) -> float:
        """Estimate device-side footprint: one resident weight copy plus
        cuBLAS/activation headroom (1.5x), mirroring the native engine's
        admission estimate."""
        try:
            manifest = json.loads(
                (self.bundle_dir / "manifest.json").read_text(encoding="utf-8")
            )
            weights_bytes = int(manifest.get("weights_bytes") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            weights_bytes = 0
        return max(256.0, weights_bytes * 1.5 / (1024**2))

    def _load_gated_engine(self, limit: int) -> Any:
        """Load with the CUDA opt-in env scoped to engine.load().

        The C++ engine reads XINGCHENG_CPP_CUDA at load time; the env is
        set only inside the coordinator admission window and restored
        afterwards so later engine loads are unaffected. Budget denial or
        coordinator failure degrades to CPU and is ledger-audited
        (fail-soft, same contract as `_gate_cuda_device`)."""
        if not _cpp_cuda_requested():
            return load_engine(self.bundle_dir, kv_memory_limit=limit)
        required_mb = self._cuda_required_mb()
        timeout = float(
            os.environ.get("XINGCHENG_GPU_ACQUIRE_TIMEOUT_S", "15")
        )
        try:
            from shared_layer.adaptive.gpu_coordinator import GpuCoordinator

            coordinator = GpuCoordinator()
            with coordinator.acquire(
                required_mb, priority="inference", timeout=timeout
            ):
                os.environ["XINGCHENG_CPP_CUDA"] = "1"
                try:
                    return load_engine(
                        self.bundle_dir, kv_memory_limit=limit
                    )
                finally:
                    os.environ.pop("XINGCHENG_CPP_CUDA", None)
        except Exception as error:
            os.environ.pop("XINGCHENG_CPP_CUDA", None)
            self._record_cuda_downgrade(required_mb, error)
            return load_engine(self.bundle_dir, kv_memory_limit=limit)

    def _record_cuda_downgrade(
        self, required_mb: float, error: Exception
    ) -> None:
        _ledger_append(
            {
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "engine": "cpp",
                "event": "gpu-budget-downgrade",
                "checkpoint_path": str(self.checkpoint_path),
                "weights_sha256": self.weights_sha256,
                "device_requested": "cuda",
                "device_used": "cpu",
                "required_mb": round(required_mb, 1),
                "reason": f"{type(error).__name__}: {error}",
            }
        )

    # -- introspection ---------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return json.loads(self._engine.describe())

    def memory_bytes(self) -> int:
        return int(self._engine.memory_bytes())

    def unload(self) -> None:
        with self._lock:
            self._engine.unload()

    # -- generation ------------------------------------------------------

    def generate(
        self,
        *,
        prompt: str,
        intent: str = "",
        max_tokens: Any = None,
        temperature: Any = None,
        top_k: Any = None,
        top_p: Any = None,
        repetition_penalty: Any = None,
        seed: Any = None,
        cancel_event: Any = None,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        from ..native_engine import generation_defaults

        started = time.perf_counter()
        defaults = generation_defaults()
        configured_cap = int(defaults["max_new_tokens"])
        try:
            max_new = int(max_tokens) if max_tokens else configured_cap
        except (TypeError, ValueError):
            max_new = configured_cap
        max_new = max(1, min(max_new, configured_cap))

        try:
            temperature_value = (
                float(temperature)
                if temperature is not None
                else float(defaults["temperature"])
            )
        except (TypeError, ValueError):
            temperature_value = float(defaults["temperature"])
        temperature_value = max(0.0, min(2.0, temperature_value))
        try:
            top_k_value = max(0, int(top_k)) if top_k else int(defaults["top_k"])
        except (TypeError, ValueError):
            top_k_value = int(defaults["top_k"])
        top_k_value = max(0, min(200, top_k_value))
        try:
            top_p_value = (
                float(top_p) if top_p is not None else float(defaults["top_p"])
            )
        except (TypeError, ValueError):
            top_p_value = float(defaults["top_p"])
        top_p_value = max(0.0, min(1.0, top_p_value))
        try:
            rep_value = (
                float(repetition_penalty)
                if repetition_penalty is not None
                else float(defaults["repetition_penalty"])
            )
        except (TypeError, ValueError):
            rep_value = float(defaults["repetition_penalty"])
        rep_value = max(0.01, min(16.0, rep_value))
        do_sample = (
            temperature_value > 0 or top_k_value > 0 or top_p_value < 1.0
        )
        try:
            seed_value = int(seed) if seed is not None else int(defaults["seed"] or 0)
        except (TypeError, ValueError):
            seed_value = 0

        if cancel_event is not None and cancel_event.is_set():
            return {
                "ok": False,
                "error_code": "TRANSFORMER_REQUEST_CANCELLED",
                "message": "Model generation was cancelled",
                "fallback_required": False,
            }

        with self._lock:
            try:
                prompt_ids = self._engine.encode(str(prompt or ""), True, False, 0)
                if not prompt_ids:
                    return {
                        "ok": False,
                        "error_code": "NATIVE_ENGINE_EMPTY_PROMPT",
                        "message": "prompt 編碼後為空",
                        "fallback_required": False,
                    }
                config = sampling_config(
                    do_sample=do_sample,
                    temperature=temperature_value,
                    top_k=top_k_value,
                    top_p=top_p_value,
                    repetition_penalty=rep_value,
                    seed=seed_value,
                )
                out_ids = list(
                    self._engine.generate(prompt_ids, max_new, config)
                )
            except Exception as error:
                return {
                    "ok": False,
                    "error_code": "CPP_RUNTIME_GENERATION_FAILED",
                    "message": str(error),
                    "fallback_required": False,
                }
            text = self._engine.decode(out_ids, True)

        latency_ms = round((time.perf_counter() - started) * 1_000, 3)

        def _digest(value: str) -> str:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()

        describe = self.describe()
        _ledger_append(
            {
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "engine": "cpp",
                "event": "cpp-runtime-execution",
                "checkpoint_path": str(self.checkpoint_path),
                "bundle_dir": str(self.bundle_dir),
                "weights_sha256": self.weights_sha256,
                "prompt_sha256": _digest(str(prompt or "")),
                "output_sha256": _digest(text),
                "eval_count": len(out_ids),
                "latency_ms": latency_ms,
                "device": "cpu",
                "kv_memory_bytes": describe.get("kv_memory_bytes", 0),
                "third_party_foundation_weights": False,
                "loopback_runtime_used": False,
            }
        )
        if progress_callback is not None:
            try:
                progress_callback({"stage": "cpp-runtime", "done": True})
            except Exception:
                pass
        return {
            "ok": True,
            "text": text,
            "decoder": "xingcheng-cpp-inference-engine",
            "model": "xingcheng-native-transformer",
            "model_family": "xingcheng-native",
            "parameter_class": "native-self-trained",
            "quantization": "none",
            "device": "cpu",
            "cpp_runtime": True,
            "sampling": {
                "do_sample": bool(do_sample),
                "temperature": temperature_value,
                "top_k": top_k_value,
                "top_p": top_p_value,
                "repetition_penalty": rep_value,
                "seed": seed_value or None,
            },
            "architecture": "xingcheng-native-decoder-transformer",
            "context_window": int(
                self._engine_config_value("max_position_embeddings")
            ),
            "prompt_eval_count": len(prompt_ids),
            "eval_count": len(out_ids),
            "total_duration_ns": int(latency_ms * 1_000_000),
            "latency_ms": latency_ms,
            "facts_supported": True,
            "remote_network_used": False,
            "loopback_runtime_used": False,
            "third_party_foundation_weights": False,
            "star_native_model_used": True,
            "native_engine": True,
            "foundation_model_license": "first-party-self-trained",
            "model_selected_by_user": False,
            "checkpoint_path": str(self.checkpoint_path),
            "bundle_dir": str(self.bundle_dir),
            "weights_sha256": self.weights_sha256,
            "intent": str(intent or ""),
        }

    def _engine_config_value(self, name: str) -> int:
        try:
            manifest = json.loads(
                (self.bundle_dir / "manifest.json").read_text(encoding="utf-8")
            )
            return int(manifest["config"][name])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return 0


_engine_cache: dict[str, CppInferenceEngine] = {}
_engine_lock = threading.Lock()


def cpp_engine_for(checkpoint_path: str | Path) -> CppInferenceEngine:
    """Resolve (and cache) the C++ engine for ``checkpoint_path``."""
    path = Path(checkpoint_path)
    key = str(path.resolve())
    with _engine_lock:
        engine = _engine_cache.get(key)
        if engine is None:
            engine = CppInferenceEngine(path)
            _engine_cache[key] = engine
            try:
                from ..native_engine import load_settings
                from .execution.auto_release import get_manager

                mgr = get_manager()
                mgr.idle = int(
                    load_settings().get("auto_release_idle_seconds") or 300
                )
                mgr.register(key, engine, _release_engine)
            except Exception:
                pass
        else:
            try:
                from .execution.auto_release import get_manager

                get_manager().touch(key)
            except Exception:
                pass
        return engine


def _release_engine(engine: CppInferenceEngine) -> None:
    with _engine_lock:
        for cache_key, cached in list(_engine_cache.items()):
            if cached is engine:
                _engine_cache.pop(cache_key, None)
    try:
        engine.unload()
    except Exception:
        pass


def generate_via_cpp_engine(request: dict[str, Any]) -> dict[str, Any]:
    """C++ counterpart of ``generate_via_native_engine``; fail-closed."""
    from ..native_engine import configured_checkpoint_path

    try:
        engine = cpp_engine_for(configured_checkpoint_path())
    except FileNotFoundError as error:
        return {
            "ok": False,
            "error_code": "CPP_RUNTIME_BUNDLE_UNAVAILABLE",
            "message": str(error),
            "fallback_required": False,
        }
    except Exception as error:
        return {
            "ok": False,
            "error_code": "CPP_RUNTIME_LOAD_FAILED",
            "message": str(error),
            "fallback_required": False,
        }
    return engine.generate(
        prompt=str(request.get("prompt") or ""),
        intent=str(request.get("intent") or ""),
        max_tokens=request.get("max_tokens"),
        temperature=request.get("temperature"),
        top_k=request.get("top_k"),
        top_p=request.get("top_p"),
        repetition_penalty=request.get("repetition_penalty"),
        seed=request.get("seed"),
        cancel_event=request.get("cancel_event"),
        progress_callback=request.get("progress_callback"),
    )


def record_cpp_fallback(reason: str) -> None:
    """Ledger evidence that a request fell back to the Python engine."""
    _ledger_append(
        {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "engine": "cpp",
            "event": "cpp-runtime-fallback",
            "reason": reason,
        }
    )


__all__ = [
    "CPP_RUNTIME_ENV",
    "CppInferenceEngine",
    "available",
    "bundle_dir_for",
    "cpp_engine_for",
    "cpp_runtime_mode",
    "ensure_bundle",
    "generate_via_cpp_engine",
    "load_engine",
    "load_extension",
    "record_cpp_fallback",
    "sampling_config",
    "tool_root",
]
