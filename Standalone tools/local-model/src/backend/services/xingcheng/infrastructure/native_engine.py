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
from .native_transformer.execution.backend import default_dtype, resolve_device
from .native_transformer.inference import (
    Generator,
    PrefixKVStore,
    Sampler,
    SamplingConfig,
)
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


class _NativeGenerationCancelled(Exception):
    """原生生成於串流期間被取消。"""


#: 超短問候／道謝的第一方模板回覆（品質閘門要求輸入 ≥4 字，這類輸入不進訓練）。
SMALL_TALK_REPLIES: tuple[tuple[str, str], ...] = (
    ("你好", "您好，我是星澄，本機執行的原生生成式語言模型。"),
    ("哈囉", "您好，我是星澄，本機執行的原生生成式語言模型。"),
    ("嗨", "您好，我是星澄，本機執行的原生生成式語言模型。"),
    ("早安", "早安，我是星澄，本機執行的原生生成式語言模型。"),
    ("午安", "午安，我是星澄，本機執行的原生生成式語言模型。"),
    ("晚安", "晚安，我是星澄，本機執行的原生生成式語言模型。"),
    ("謝謝", "不客氣，我是星澄，原生模型能獨立完成基本語言模型推理。"),
    ("感謝", "不客氣，我是星澄，神經網路核心為第一方原生實作。"),
    ("再見", "再見，我是星澄，隨時可以為您服務。"),
    ("掰掰", "再見，我是星澄，隨時可以為您服務。"),
)

_SMALL_TALK_STRIP = " 　\t\r\n！!？?。.，,、；;：:~～"


def small_talk_reply(prompt: str) -> str | None:
    """精確比對超短問候；命中才回模板，其餘一律走模型生成。"""
    normalized = str(prompt or "").strip().strip(_SMALL_TALK_STRIP)
    for question, answer in SMALL_TALK_REPLIES:
        if normalized == question:
            return answer
    return None


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


def _bounded_setting(
    settings: Mapping[str, Any],
    key: str,
    default: Any,
    minimum: Any,
    maximum: Any,
    *,
    cast: Any = float,
) -> Any:
    try:
        value = cast(settings.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def generation_defaults() -> dict[str, Any]:
    """settings 驅動的生成預設；請求值優先，任何值都夾在安全範圍內。"""
    settings = load_settings()
    return {
        "temperature": _bounded_setting(settings, "temperature", 0.4, 0.0, 2.0),
        "top_k": int(_bounded_setting(settings, "top_k", 10, 0, 200, cast=int)),
        "top_p": _bounded_setting(settings, "top_p", 0.9, 0.0, 1.0),
        "repetition_penalty": _bounded_setting(
            settings, "repetition_penalty", 1.05, 0.01, 16.0
        ),
        "max_new_tokens": int(
            _bounded_setting(settings, "max_new_tokens", 192, 1, 512, cast=int)
        ),
        "seed": settings.get("seed"),
        "fallback_message": str(settings.get("fallback_message") or "").strip(),
        "min_answer_chars": int(
            _bounded_setting(settings, "min_answer_chars", 4, 0, 200, cast=int)
        ),
    }


def cpu_thread_budget() -> int:
    """CPU 執行緒上限：settings 指定，否則取核心數的 1/4（最多 4）。"""
    settings = load_settings()
    try:
        configured = int(settings.get("cpu_threads") or 0)
    except (TypeError, ValueError):
        configured = 0
    if configured > 0:
        return max(1, min(16, configured))
    cores = os.cpu_count() or 8
    return max(1, min(4, cores // 4))


def cpu_generation_cap() -> int:
    """CPU 生成上限（token）：避免 CPU 路徑長時間滿載。"""
    settings = load_settings()
    try:
        cap = int(settings.get("cpu_max_new_tokens") or 64)
    except (TypeError, ValueError):
        cap = 64
    return max(1, min(128, cap))


def scope_check(prompt: str) -> tuple[bool, str]:
    """範圍閘門：settings 啟用時，prompt 必須命中允許主題才放行。"""
    settings = load_settings()
    if settings.get("scope_enabled") is not True:
        return False, ""
    keywords = [
        str(keyword).strip().casefold()
        for keyword in (settings.get("scope_keywords") or [])
        if str(keyword).strip()
    ]
    if not keywords:
        return False, ""
    text = str(prompt or "")
    # 對話組合提示會前置人格與歷史；範圍判定只看使用者最新訊息，
    # 否則人格文字中的關鍵詞會讓所有問題都通過閘門。
    for marker in ("使用者最新訊息：", "使用者最新訊息:", "使用者："):
        if marker in text:
            text = text.rsplit(marker, 1)[-1]
            break
    lowered = text.casefold()
    if any(keyword in lowered for keyword in keywords):
        return False, ""
    return True, "prompt-out-of-scope"


def scope_refusal_message() -> str:
    settings = load_settings()
    return str(
        settings.get("scope_fallback_message")
        or settings.get("fallback_message")
        or "這個問題超出我目前可回答的範圍。"
    )


_ALLOWED_PUNCTUATION = set(
    "，。！？；：、（）「」『』《》〈〉…—－·,.!?;:()[]<>\"'%+-=*/@#$&_|~`^\\"
)


def quality_guard(text: str, *, min_chars: int) -> tuple[bool, str]:
    """回應品質護欄：過短、亂碼比例過高或高度重複時觸發。"""
    value = str(text or "").strip()
    if len(value) < max(1, int(min_chars)):
        return True, "answer-too-short"
    allowed = 0
    total = 0
    for char in value:
        if char.isspace():
            continue
        total += 1
        if char.isalnum() or "\u3400" <= char <= "\u9fff" or char in _ALLOWED_PUNCTUATION:
            allowed += 1
    if total and allowed / total < 0.85:
        return True, "answer-garbled"
    compact = "".join(value.split())
    if len(compact) >= 24:
        counts: dict[str, int] = {}
        for index in range(len(compact) - 5):
            gram = compact[index : index + 6]
            counts[gram] = counts.get(gram, 0) + 1
        if counts and max(counts.values()) >= 5:
            return True, "answer-repetitive"
    return False, ""


class NativeTransformerEngine:
    """自訓權重推論引擎；模型核心不觸網。"""

    def __init__(
        self,
        checkpoint_path: str | Path,
        *,
        quantize: int | None = None,
        device: str | torch.device | None = None,
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
        self.device = resolve_device(device)
        self.gpu_budget_downgraded = False
        if self.device.type == "cuda":
            # MS3：CUDA 推論先過 GpuCoordinator VRAM 預算，與訓練共用
            # 同一協調器；預算不足時降級 CPU 而非硬塞進 VRAM 造成 OOM。
            self.device, self.gpu_budget_downgraded = self._gate_cuda_device(
                self.device
            )
        if self.device.type == "cpu":
            # CPU 路徑限制執行緒數，避免與主系統爭用全部核心（R8 統一入口）。
            from .native_transformer.execution.backend import apply_cpu_thread_budget

            try:
                apply_cpu_thread_budget("inference", configured=cpu_thread_budget())
            except Exception:
                pass
        # CUDA / MPS 走 bf16（Tensor Core GEMM + mem-efficient attention）；
        # CPU 維持 fp32。int8/uint8 量化 buffer 不受浮點 dtype cast 影響。
        self.model = self.model.to(device=self.device, dtype=default_dtype(self.device))
        self.prefix_store = PrefixKVStore(
            max_entries=8, tag=f"xingcheng-native:{self.state_sha256[:12]}"
        )
        self._generator = Generator(
            self.model, device=self.device, prefix_store=self.prefix_store
        )
        self._lock = threading.Lock()

    def _gate_cuda_device(self, device: torch.device) -> tuple[torch.device, bool]:
        """MS3 GPU 協調：估計所需 VRAM，經 GpuCoordinator 取得預算後才上卡。

        預算不足／協調器無法判定時降級 CPU（推論屬互動路徑，fail-soft
        降級而非拒絕服務），降級事實寫入執行帳本可稽核。
        """
        import torch as _torch

        bytes_per_param = 4 if default_dtype(device) == _torch.float32 else 2
        required_mb = max(
            256.0, (self._parameter_count * bytes_per_param * 1.5) / (1024**2)
        )
        timeout = float(os.environ.get("XINGCHENG_GPU_ACQUIRE_TIMEOUT_S", "15"))
        try:
            from shared_layer.adaptive.gpu_coordinator import GpuCoordinator

            coordinator = GpuCoordinator()
            with coordinator.acquire(
                required_mb, priority="inference", timeout=timeout
            ):
                return device, False
        except Exception as error:
            self._record_gpu_downgrade(device, required_mb, error)
            return _torch.device("cpu"), True

    def _record_gpu_downgrade(
        self, device: torch.device, required_mb: float, error: Exception
    ) -> None:
        try:
            ledger = tool_root() / NATIVE_EXECUTION_LEDGER
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with ledger.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "at": time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                            ),
                            "engine": "native",
                            "event": "gpu-budget-downgrade",
                            "checkpoint_path": str(self.checkpoint_path),
                            "state_sha256": self.state_sha256,
                            "parameter_count": self._parameter_count,
                            "device_requested": str(device),
                            "device_used": "cpu",
                            "required_mb": round(required_mb, 1),
                            "reason": f"{type(error).__name__}: {error}",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass

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
        defaults = generation_defaults()
        configured_cap = int(defaults["max_new_tokens"])
        if self.device.type == "cpu":
            configured_cap = min(configured_cap, cpu_generation_cap())
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
        scope_triggered, scope_reason = scope_check(prompt)
        if scope_triggered:
            message = scope_refusal_message()
            latency_ms = round((time.perf_counter() - started) * 1_000, 3)
            return {
                "ok": True,
                "text": message,
                "decoder": "native-transformer-autoregressive-decoder",
                "model": NATIVE_MODEL_ID,
                "model_family": NATIVE_MODEL_FAMILY,
                "parameter_class": "native-self-trained",
                "parameter_count": self._parameter_count,
                "quantization": self.quantization,
                "device": str(self.device),
                "sampling": {},
                "quality_guard": {"triggered": False, "reason": "", "min_answer_chars": 0},
                "scope_guard": {"triggered": True, "reason": scope_reason},
                "architecture": "xingcheng-native-decoder-transformer",
                "context_window": int(self.config.max_position_embeddings),
                "prompt_eval_count": 0,
                "eval_count": 0,
                "latency_ms": latency_ms,
                "facts_supported": False,
                "remote_network_used": False,
                "loopback_runtime_used": False,
                "third_party_foundation_weights": False,
                "star_native_model_used": True,
                "native_engine": True,
                "foundation_model_license": NATIVE_FOUNDATION_LICENSE,
                "checkpoint_path": str(self.checkpoint_path),
                "state_sha256": self.state_sha256,
                "intent": str(intent or ""),
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
        if seed is None:
            seed = defaults["seed"]
        do_sample = temperature_value > 0 or top_k_value > 0 or top_p_value < 1.0
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
        streamed: list[int] = []

        def _on_token(token_id: int) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise _NativeGenerationCancelled()
            streamed.append(int(token_id))
            if progress_callback is not None:
                try:
                    progress_callback(
                        {
                            "sequence": len(streamed),
                            "text": self.tokenizer.decode(streamed, skip_special=True),
                            "model": NATIVE_MODEL_ID,
                        }
                    )
                except Exception:
                    pass

        try:
            with self._lock:
                if seed is not None:
                    try:
                        torch.manual_seed(int(seed))
                    except (TypeError, ValueError):
                        pass
                generated = self._generator.generate(
                    ids,
                    max_new_tokens=max_new,
                    sampling=sampler.config,
                    on_token=_on_token if progress_callback is not None or cancel_event is not None else None,
                )
        except _NativeGenerationCancelled:
            return {
                "ok": False,
                "error_code": "TRANSFORMER_REQUEST_CANCELLED",
                "message": "Model generation was cancelled",
                "fallback_required": False,
            }
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

        guard_triggered, guard_reason = quality_guard(
            text, min_chars=int(defaults["min_answer_chars"])
        )
        if guard_triggered:
            text = (
                str(defaults["fallback_message"])
                or "我目前無法可靠回答這個問題。"
            )
            if progress_callback is not None:
                try:
                    progress_callback(
                        {
                            "sequence": len(streamed),
                            "text": text,
                            "guard": guard_reason,
                            "model": NATIVE_MODEL_ID,
                        }
                    )
                except Exception:
                    pass

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
            "prefix_cache": {
                "reused_tokens": int(getattr(self._generator, "last_prefix_reuse", 0)),
                "entries": len(self.prefix_store),
                "hits": int(self.prefix_store.hits),
                "misses": int(self.prefix_store.misses),
            },
            "sampling": {
                "do_sample": bool(do_sample),
                "temperature": temperature_value,
                "top_k": top_k_value,
                "top_p": top_p_value,
                "repetition_penalty": rep_value,
                "seed": int(seed) if isinstance(seed, (int, float)) else None,
            },
            "prompt_truncated": prompt_truncated,
            "quality_guard": {
                "triggered": bool(guard_triggered),
                "reason": guard_reason,
                "min_answer_chars": int(defaults["min_answer_chars"]),
            },
            "scope_guard": {"triggered": False, "reason": ""},
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
    settings = load_settings()
    quantize_raw = str(
        os.environ.get(NATIVE_QUANTIZATION_ENV)
        or settings.get("quantization")
        or ""
    ).strip().casefold()
    quantize = {"int8": 8, "int4": 4}.get(quantize_raw)
    device = settings.get("device") or None
    key = f"{path.resolve()}|{quantize or 0}|{device or 'auto'}"
    with _engine_lock:
        engine = _engine_cache.get(key)
        if engine is None:
            if not path.is_file():
                raise FileNotFoundError(f"NATIVE_CHECKPOINT_MISSING:{path}")
            engine = NativeTransformerEngine(
                path, quantize=quantize, device=device
            )
            _engine_cache[key] = engine
            # P4：註冊自動釋放——閒置逾時或記憶體壓力時從快取卸載。
            # release_fn 只移除快取項；進行中的 generate 持有強參照不受影響，
            # 結束後 refcount 歸零由 GC 回收權重，下次請求再重載 checkpoint。
            try:
                from .native_transformer.execution.auto_release import get_manager

                idle_s = int(settings.get("auto_release_idle_seconds") or 300)
                mgr = get_manager()
                mgr.idle = idle_s
                mgr.register(key, engine, _release_engine)
            except Exception:
                pass  # auto-release 失效不影響引擎可用性
        else:
            try:
                from .native_transformer.execution.auto_release import get_manager

                get_manager().touch(key)
            except Exception:
                pass
        return engine


def _release_engine(engine: NativeTransformerEngine) -> None:
    """把 engine 從快取移除（auto_release 回調）；權重釋放交由 GC。"""
    with _engine_lock:
        for cache_key, cached in list(_engine_cache.items()):
            if cached is engine:
                _engine_cache.pop(cache_key, None)


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


def write_settings(**updates: Any) -> dict[str, Any]:
    """更新工具 settings（治理控制面）；未知鍵保留，寫入為原子替換。"""
    current = load_settings()
    current.update({key: value for key, value in updates.items() if value is not None})
    target = settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, target)
    return current


def control_status() -> dict[str, Any]:
    """控制面狀態：開關、checkpoint、生成預設。"""
    return {
        "enabled": flag_enabled(),
        "checkpoint": str(configured_checkpoint_path()),
        "settings_path": str(settings_path()),
        "settings": load_settings(),
        "defaults": generation_defaults(),
    }


def _cli(argv: list[str]) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="xingcheng-native-engine",
        description="星澄原生引擎 CLI：控制面（status/enable/disable/checkpoint）與生成",
    )
    parser.add_argument("--status", action="store_true", help="顯示控制面狀態")
    parser.add_argument("--enable", action="store_true", help="開啟原生引擎")
    parser.add_argument("--disable", action="store_true", help="關閉原生引擎（fail-closed）")
    parser.add_argument("--set-checkpoint", default=None, help="指定 checkpoint 並持久化")
    parser.add_argument("--checkpoint", default=None, help="本次生成使用的 checkpoint")
    parser.add_argument("--prompt", default=None, help="輸入文字（未提供時僅執行控制指令）")
    parser.add_argument("--max-new-tokens", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument("--repetition-penalty", type=float, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--sliding-window", action="store_true")
    parser.add_argument("--quantize", type=int, default=None, choices=[4, 8])
    args = parser.parse_args(argv)

    if args.enable or args.disable or args.set_checkpoint:
        write_settings(
            enabled=True if args.enable else (False if args.disable else None),
            checkpoint=args.set_checkpoint,
        )
        print(json.dumps(control_status(), ensure_ascii=False, indent=2))
        if not args.prompt:
            return 0
    if args.status or not args.prompt:
        print(json.dumps(control_status(), ensure_ascii=False, indent=2))
        return 0
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
    "control_status",
    "cpu_generation_cap",
    "cpu_thread_budget",
    "flag_enabled",
    "generate_via_native_engine",
    "generation_defaults",
    "load_settings",
    "native_engine_for",
    "quality_guard",
    "settings_path",
    "tool_root",
    "write_settings",
]
