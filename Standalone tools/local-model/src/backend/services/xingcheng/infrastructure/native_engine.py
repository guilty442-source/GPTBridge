"""星澄原生引擎（建置藍圖 Phase 1）：以自訓 Transformer 權重推論。

Feature flag（預設關閉、fail-closed）：

- ``XINGCHENG_NATIVE_ENGINE=1`` 啟用；未啟用時 ``generate`` 路徑完全不變。
- 啟用後若 checkpoint 不存在或載入失敗，回傳明確錯誤，不靜默回退第三方權重。
- ``XINGCHENG_NATIVE_CHECKPOINT`` 可指定 checkpoint；預設為
  :func:`native_transformer.checkpoint.default_checkpoint_dir` 下最新的 ``*.pt``。

本模組不進行任何網路 I/O——模型核心與網路功能保持分離。
輸出稽核旗標：``star_native_model_used=True``、``third_party_weights_used=False``。

CLI（Phase 1 驗收）::

    python -m xingcheng.infrastructure.native_engine \
        --checkpoint <phase0.pt> --prompt "星澄"
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .native_transformer.checkpoint import (
    default_checkpoint_dir,
    load_checkpoint,
)
from .native_transformer.execution.backend import resolve_device
from .native_transformer.inference import Generator, Sampler, SamplingConfig
from .native_transformer.tokenizer import XingChengTokenizer

NATIVE_ENGINE_ENV = "XINGCHENG_NATIVE_ENGINE"
NATIVE_CHECKPOINT_ENV = "XINGCHENG_NATIVE_CHECKPOINT"
NATIVE_QUANTIZATION_ENV = "XINGCHENG_NATIVE_QUANTIZATION"
NATIVE_MODEL_ID = "xingcheng-native-transformer"
NATIVE_MODEL_FAMILY = "xingcheng-native"
NATIVE_FOUNDATION_LICENSE = "first-party-self-trained"
NATIVE_ENGINE_SETTINGS = "runtime/settings/native-engine.json"
NATIVE_EXECUTION_LEDGER = "xingcheng/runtime/logs/native-engine-executions.jsonl"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def tool_root() -> Path:
    """local-model 工具根目錄（``Standalone tools/local-model``）。"""
    return Path(__file__).resolve().parents[5]


def settings_path() -> Path:
    return tool_root() / NATIVE_ENGINE_SETTINGS


def load_settings() -> dict[str, Any]:
    """工具自有設定（環境變數受 allowlist 限制，settings 為正式開關路徑）。"""
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def flag_enabled() -> bool:
    """原生引擎 feature flag；環境變數優先，其次工具 settings；預設關閉。"""
    raw = str(os.environ.get(NATIVE_ENGINE_ENV) or "").strip().casefold()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return load_settings().get("enabled") is True


def configured_checkpoint_path() -> Path:
    """checkpoint 來源：env 覆寫 → settings 指定 → 預設目錄最新 ``*.pt``。"""
    override = str(os.environ.get(NATIVE_CHECKPOINT_ENV) or "").strip()
    if override:
        return Path(override)
    configured = str(load_settings().get("checkpoint") or "").strip()
    if configured:
        candidate = Path(configured)
        return candidate if candidate.is_absolute() else tool_root() / candidate
    directory = default_checkpoint_dir()
    candidates = sorted(
        directory.rglob("*.pt"), key=lambda item: item.stat().st_mtime, reverse=True
    ) if directory.is_dir() else []
    if candidates:
        return candidates[0]
    return directory / "native-model.pt"


class NativeTransformerEngine:
    """自訓權重推論引擎；模型核心不觸網。"""

    def __init__(
        self,
        checkpoint_path: str | Path,
        *,
        quantize: int | None = None,
    ) -> None:
        loaded = load_checkpoint(checkpoint_path)
        self.checkpoint_path = Path(checkpoint_path)
        self.model = loaded["model"]
        self.config = loaded["config"]
        self.metadata = dict(loaded.get("metadata") or {})
        self.state_sha256 = str(loaded.get("state_sha256") or "")
        self._parameter_count = int(self.model.num_parameters())
        self.quantization = "none"
        if quantize in (4, 8):
            from .native_transformer.quantization import quantize_model

            self.model = quantize_model(self.model, n_bits=int(quantize))
            self.quantization = f"int{int(quantize)}"
        tokenizer = loaded.get("tokenizer")
        self.tokenizer = tokenizer or XingChengTokenizer.from_config(self.config)
        # 推論裝置：有 CUDA 用 CUDA（GPU 加速），否則 CPU。
        self.device = resolve_device(None)
        self.model = self.model.to(self.device)
        self._generator = Generator(self.model, device=self.device)
        self._lock = threading.Lock()

    @classmethod
    def available(cls) -> bool:
        """flag 開啟且 checkpoint 可讀才算可用（fail-closed）。"""
        if not flag_enabled():
            return False
        path = configured_checkpoint_path()
        return path.is_file()

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
        sliding_window: bool = False,
        cancel_event: Any = None,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        """以原生權重生成；回傳與 governed generate() 相容的結果。"""
        import torch

        started = time.perf_counter()
        try:
            configured_cap = int(load_settings().get("max_new_tokens") or 512)
        except (TypeError, ValueError):
            configured_cap = 512
        configured_cap = max(1, min(configured_cap, 512))
        try:
            max_new = int(max_tokens) if max_tokens else configured_cap
        except (TypeError, ValueError):
            max_new = configured_cap
        max_new = max(1, min(max_new, configured_cap))
        prompt_ids = self.tokenizer.encode(
            str(prompt or ""), add_bos=True, add_eos=False
        )
        if not prompt_ids:
            return {
                "ok": False,
                "error_code": "NATIVE_ENGINE_EMPTY_PROMPT",
                "message": "prompt 編碼後為空",
                "fallback_required": False,
            }
        capacity = int(self.config.max_position_embeddings)
        prompt_truncated = False
        if len(prompt_ids) >= capacity and sliding_window:
            keep = max(1, capacity - 1)
            prompt_ids = prompt_ids[-keep:]
            prompt_truncated = True
        remaining = capacity - len(prompt_ids)
        if remaining < 1:
            return {
                "ok": False,
                "error_code": "NATIVE_ENGINE_PROMPT_TOO_LONG",
                "message": "prompt 超過 max_position_embeddings",
                "fallback_required": False,
            }
        max_new = min(max_new, remaining)
        try:
            temperature_value = float(temperature) if temperature is not None else 1.0
        except (TypeError, ValueError):
            temperature_value = 1.0
        try:
            top_k_value = max(0, int(top_k)) if top_k else 0
        except (TypeError, ValueError):
            top_k_value = 0
        try:
            top_p_value = float(top_p) if top_p is not None else 1.0
        except (TypeError, ValueError):
            top_p_value = 1.0
        top_p_value = max(0.0, min(1.0, top_p_value))
        try:
            rep_value = (
                float(repetition_penalty) if repetition_penalty is not None else 1.0
            )
        except (TypeError, ValueError):
            rep_value = 1.0
        rep_value = max(0.01, min(16.0, rep_value))
        do_sample = (
            (temperature is not None and temperature_value > 0)
            or top_k_value > 0
            or top_p_value < 1.0
        )
        sampler = Sampler(
            SamplingConfig(
                do_sample=do_sample,
                temperature=temperature_value,
                top_k=top_k_value,
                top_p=top_p_value,
                repetition_penalty=rep_value,
                eos_token_id=self.tokenizer.eos_id,
                pad_token_id=self.tokenizer.pad_id,
            )
        )
        ids = torch.tensor([prompt_ids], dtype=torch.long)
        if cancel_event is not None and cancel_event.is_set():
            return {
                "ok": False,
                "error_code": "TRANSFORMER_REQUEST_CANCELLED",
                "message": "Model generation was cancelled",
                "fallback_required": False,
            }
        try:
            with self._lock:
                if seed is not None:
                    try:
                        torch.manual_seed(int(seed))
                    except (TypeError, ValueError):
                        pass
                generated = self._generator.generate(
                    ids, max_new_tokens=max_new, sampling=sampler.config
                )
        except ValueError as error:
            return {
                "ok": False,
                "error_code": "NATIVE_ENGINE_GENERATION_INVALID",
                "message": str(error),
                "fallback_required": False,
            }
        except Exception as error:  # pragma: no cover - 防禦性
            return {
                "ok": False,
                "error_code": "NATIVE_ENGINE_GENERATION_FAILED",
                "message": str(error),
                "fallback_required": False,
            }
        out_ids = [int(token) for token in generated[0].tolist()]
        text = self.tokenizer.decode(out_ids, skip_special=True)
        latency_ms = round((time.perf_counter() - started) * 1_000, 3)

        def _digest(value: str) -> str:
            import hashlib

            return hashlib.sha256(value.encode("utf-8")).hexdigest()

        try:
            ledger = tool_root() / NATIVE_EXECUTION_LEDGER
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with ledger.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "engine": "native",
                            "checkpoint_path": str(self.checkpoint_path),
                            "state_sha256": self.state_sha256,
                            "parameter_count": self._parameter_count,
                            "quantization": self.quantization,
                            "prompt_sha256": _digest(str(prompt or "")),
                            "output_sha256": _digest(text),
                            "eval_count": len(out_ids),
                            "latency_ms": latency_ms,
                            "device": str(self.device),
                            "third_party_foundation_weights": False,
                            "loopback_runtime_used": False,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:  # pragma: no cover - 帳本寫入為 best-effort 證據
            pass
        if progress_callback is not None:
            try:
                progress_callback({"stage": "native-engine", "done": True})
            except Exception:
                pass
        return {
            "ok": True,
            "text": text,
            "decoder": "native-transformer-autoregressive-decoder",
            "model": NATIVE_MODEL_ID,
            "model_family": NATIVE_MODEL_FAMILY,
            "parameter_class": "native-self-trained",
            "parameter_count": self._parameter_count,
            "quantization": self.quantization,
            "device": str(self.device),
            "sampling": {
                "do_sample": bool(do_sample),
                "temperature": temperature_value,
                "top_k": top_k_value,
                "top_p": top_p_value,
                "repetition_penalty": rep_value,
                "seed": int(seed) if isinstance(seed, (int, float)) else None,
            },
            "prompt_truncated": prompt_truncated,
            "architecture": "xingcheng-native-decoder-transformer",
            "context_window": int(self.config.max_position_embeddings),
            "prompt_eval_count": len(prompt_ids),
            "eval_count": len(out_ids),
            "load_duration_ns": 0,
            "total_duration_ns": int(latency_ms * 1_000_000),
            "latency_ms": latency_ms,
            "facts_supported": True,
            "unsupported_facts": {},
            "remote_network_used": False,
            "loopback_runtime_used": False,
            "third_party_foundation_weights": False,
            "star_native_model_used": True,
            "native_engine": True,
            "foundation_model_license": NATIVE_FOUNDATION_LICENSE,
            "model_selected_by_user": False,
            "checkpoint_path": str(self.checkpoint_path),
            "state_sha256": self.state_sha256,
            "intent": str(intent or ""),
        }


_engine_cache: dict[str, NativeTransformerEngine] = {}
_engine_lock = threading.Lock()


def native_engine_for(
    checkpoint_path: str | Path | None = None,
) -> NativeTransformerEngine:
    """解析（並快取）flag 對應的引擎實例；不可用時 fail-closed。"""
    path = Path(checkpoint_path) if checkpoint_path else configured_checkpoint_path()
    quantize_raw = str(
        os.environ.get(NATIVE_QUANTIZATION_ENV)
        or load_settings().get("quantization")
        or ""
    ).strip().casefold()
    quantize = {"int8": 8, "int4": 4}.get(quantize_raw)
    key = f"{path.resolve()}|{quantize or 0}"
    with _engine_lock:
        engine = _engine_cache.get(key)
        if engine is None:
            if not path.is_file():
                raise FileNotFoundError(f"NATIVE_CHECKPOINT_MISSING:{path}")
            engine = NativeTransformerEngine(path, quantize=quantize)
            _engine_cache[key] = engine
        return engine


def generate_via_native_engine(request: Mapping[str, Any]) -> dict[str, Any]:
    """governed generate() 的原生短路入口（flag 開啟時由 runtime 呼叫）。"""
    if not flag_enabled():
        return {
            "ok": False,
            "error_code": "NATIVE_ENGINE_DISABLED",
            "message": "原生引擎未啟用（XINGCHENG_NATIVE_ENGINE）",
            "fallback_required": False,
        }
    try:
        engine = native_engine_for()
    except FileNotFoundError as error:
        return {
            "ok": False,
            "error_code": "NATIVE_CHECKPOINT_MISSING",
            "message": str(error),
            "fallback_required": False,
        }
    except Exception as error:  # pragma: no cover - 防禦性
        return {
            "ok": False,
            "error_code": "NATIVE_ENGINE_LOAD_FAILED",
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
        sliding_window=bool(request.get("sliding_window")),
        cancel_event=request.get("cancel_event"),
        progress_callback=request.get("progress_callback"),
    )


def _cli(argv: list[str]) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="xingcheng-native-engine",
        description="星澄原生引擎 CLI：以自訓 checkpoint 生成（不觸網）",
    )
    parser.add_argument("--checkpoint", default=None, help="checkpoint 路徑")
    parser.add_argument("--prompt", required=True, help="輸入文字")
    parser.add_argument("--max-new-tokens", type=int, default=64)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-k", type=int, default=0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--repetition-penalty", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--sliding-window", action="store_true")
    parser.add_argument("--quantize", type=int, default=None, choices=[4, 8])
    args = parser.parse_args(argv)
    try:
        path = Path(args.checkpoint) if args.checkpoint else configured_checkpoint_path()
        engine = NativeTransformerEngine(path, quantize=args.quantize)
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    result = engine.generate(
        prompt=args.prompt,
        max_tokens=args.max_new_tokens,
        temperature=args.temperature,
        top_k=args.top_k,
        top_p=args.top_p,
        repetition_penalty=args.repetition_penalty,
        seed=args.seed,
        sliding_window=args.sliding_window,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover - CLI 進入點
    sys.exit(_cli(sys.argv[1:]))


__all__ = [
    "NATIVE_CHECKPOINT_ENV",
    "NATIVE_ENGINE_ENV",
    "NATIVE_ENGINE_SETTINGS",
    "NATIVE_EXECUTION_LEDGER",
    "NATIVE_FOUNDATION_LICENSE",
    "NATIVE_MODEL_FAMILY",
    "NATIVE_MODEL_ID",
    "NATIVE_QUANTIZATION_ENV",
    "NativeTransformerEngine",
    "configured_checkpoint_path",
    "flag_enabled",
    "generate_via_native_engine",
    "load_settings",
    "native_engine_for",
    "settings_path",
    "tool_root",
]
