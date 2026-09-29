"""星澄原生引擎控制面與 C++ 推論路由。

B167/B38/E180：PyTorch（Python 推論棧）已全數退役——零 source、
dependency、artifact、execution 與 fallback 角色，無過渡期。模型執行
只由正式 C++ 推論引擎（``native_transformer.cpp_runtime`` →
``_xingcheng_inference``）服務，權重來源為已驗證的
``star-native-inference-bundle/v1`` 匯出物；任何載入或生成失敗皆
fail-closed，不回退任何 Python/retired 路徑。

Feature flag（預設關閉、fail-closed）：

- ``XINGCHENG_NATIVE_ENGINE=1`` 啟用；未啟用時 ``generate`` 路徑完全不變。
- ``XINGCHENG_NATIVE_CHECKPOINT`` 可指定服務物（bundle 目錄）；未指定時
  解析 ``cpp-bundles/`` 下最新、manifest 完整的 bundle。

本模組不進行任何網路 I/O——模型核心與網路功能保持分離。
輸出稽核旗標：``star_native_model_used=True``、``third_party_weights_used=False``。

CLI（控制面與單次生成）::

    python -m xingcheng.infrastructure.native_engine --status
    python -m xingcheng.infrastructure.native_engine --prompt "星澄"
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .native_transformer import cpp_runtime

NATIVE_ENGINE_ENV = "XINGCHENG_NATIVE_ENGINE"
NATIVE_CHECKPOINT_ENV = "XINGCHENG_NATIVE_CHECKPOINT"
NATIVE_QUANTIZATION_ENV = "XINGCHENG_NATIVE_QUANTIZATION"
NATIVE_MODEL_ID = "xingcheng-native-transformer"
NATIVE_MODEL_FAMILY = "xingcheng-native"
NATIVE_FOUNDATION_LICENSE = "first-party-self-trained"
NATIVE_ENGINE_SETTINGS = "runtime/settings/native-engine.json"
NATIVE_EXECUTION_LEDGER = "xingcheng/runtime/logs/native-engine-executions.jsonl"

# Perf/bounded-growth: per-generation telemetry with no readers — cap to
# the newest 2000 lines once past ~1MB. One stat per append; trim runs
# only past the threshold. Fail-open: errors leave the file untouched.
_EXECUTION_LEDGER_TRIM_BYTES = 1_048_576
_EXECUTION_LEDGER_KEEP_LINES = 2000


def _maybe_trim_execution_ledger(ledger: Path) -> None:
    try:
        if ledger.stat().st_size <= _EXECUTION_LEDGER_TRIM_BYTES:
            return
        lines = ledger.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) <= _EXECUTION_LEDGER_KEEP_LINES:
            return
        ledger.write_text("".join(lines[-_EXECUTION_LEDGER_KEEP_LINES:]), encoding="utf-8")
    except OSError:
        pass

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


#: 超短問候／道謝的第一方模板回覆（品質閘門要求輸入 ≥4 字）。
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


def _resolve_serving_path(raw: str) -> Path:
    """Bundle 目錄直釘，或 torch-lineage ``.pt`` 對應的 deterministic bundle。

    ``.pt`` 來源只換算 bundle 目錄名；執行期不需要、也不允許
    checkpoint 檔本身存在（A35/A610 服務契約）。
    """
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = tool_root() / candidate
    if candidate.is_dir():
        return candidate
    if candidate.suffix == ".pt":
        return cpp_runtime.bundle_dir_for(candidate)
    return candidate


def _newest_bundle_dir() -> Path | None:
    root = tool_root() / cpp_runtime.CPP_BUNDLES_DIR
    if not root.is_dir():
        return None
    candidates = sorted(
        (
            item
            for item in root.iterdir()
            if item.is_dir() and cpp_runtime.is_bundle_dir(item)
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def configured_checkpoint_path() -> Path:
    """服務物來源：env 覆寫 → settings 指定 → ``cpp-bundles/`` 最新 bundle。

    回傳值為 C++ 引擎可直接服務的 bundle 目錄（或 ``.pt`` 對應的
    deterministic bundle 目錄）；找不到任何 bundle 時回傳預定位置，
    由載入端 fail-closed。
    """
    override = str(os.environ.get(NATIVE_CHECKPOINT_ENV) or "").strip()
    if override:
        return _resolve_serving_path(override)
    settings = load_settings()
    configured = str(
        settings.get("cpp_bundle") or settings.get("checkpoint") or ""
    ).strip()
    if configured:
        return _resolve_serving_path(configured)
    newest = _newest_bundle_dir()
    if newest is not None:
        return newest
    return tool_root() / cpp_runtime.CPP_BUNDLES_DIR / "native-model-bundle"


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
    """CPU 執行緒上限：settings 指定，否則取核心數的 1/4（最多 4）。

    一律再經 §10.30 統一入口收斂至核心預算（5 核硬頂，僅可下調）。
    """
    from shared_layer.performance.thread_budget import bounded_threads

    settings = load_settings()
    try:
        configured = int(settings.get("cpu_threads") or 0)
    except (TypeError, ValueError):
        configured = 0
    if configured > 0:
        return bounded_threads(configured, 1, workload="model")
    cores = os.cpu_count() or 8
    return bounded_threads(max(1, min(4, cores // 4)), 1, workload="model")


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


def quality_guard(
    text: str, *, min_chars: int, prompt: str = ""
) -> tuple[bool, str]:
    """回應品質護欄：過短、亂碼比例過高、高度重複或已知退化模式時觸發。"""
    value = str(text or "").strip()
    if len(value) < max(1, int(min_chars)):
        return True, "answer-too-short"
    # 記憶句式 parroting：SFT 記憶訓練曾佔比過高使模型對任何問題
    # 都回「好的，我記住了：X」。prompt 非記憶指令時命中即退化。
    if "我記住了" in value.replace(" ", ""):
        memory_cues = ("記住", "記得", "記一下", "幫我記", "remember")
        lowered = str(prompt or "").casefold()
        if not any(cue in lowered for cue in memory_cues):
            return True, "memorization-parroting"
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


def native_engine_available() -> bool:
    """flag 開啟且服務 bundle 完整才算可用（fail-closed）。"""
    if not flag_enabled():
        return False
    path = configured_checkpoint_path()
    return path.is_dir() and cpp_runtime.is_bundle_dir(path)


def native_engine_for(
    checkpoint_path: str | Path | None = None,
) -> Any:
    """解析（並快取）服務 bundle 對應的 C++ 引擎；不可用時 fail-closed。"""
    path = (
        _resolve_serving_path(str(checkpoint_path))
        if checkpoint_path
        else configured_checkpoint_path()
    )
    return cpp_runtime.cpp_engine_for(cpp_runtime.assert_inside_xingcheng(path))


_DIALOGUE_SYSTEM_PROMPT = "你是星澄，一個本地模型。簡短回答。"


def _dialogue_templated_request(request: Mapping[str, Any]) -> Mapping[str, Any]:
    """Interactive dialogue turns must reach the serving weights in
    ``star-chat-format/v1``. Non-dialogue requests (automatic workflows,
    batch, pre-templated prompts) pass through."""
    if request.get("dialogue_interactive") is not True:
        return request
    prompt_text = str(request.get("prompt") or "")
    if not prompt_text.strip() or "<|user|>" in prompt_text:
        return request
    from .native_transformer.chat_format import (
        ChatMessage,
        render_conversation,
    )

    turns = [ChatMessage("system", _DIALOGUE_SYSTEM_PROMPT)]
    # 有界歷史回合（呼叫端已裁切；此處再限 8 輪防 prompt 膨脹）。
    raw_history = request.get("history")
    if isinstance(raw_history, list):
        for item in raw_history[-8:]:
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("role") or "").strip().casefold()
            if role not in {"user", "assistant"}:
                continue
            content = str(item.get("content") or "").strip()[:1_000]
            if content:
                turns.append(ChatMessage(role, content))
    turns.append(ChatMessage("user", prompt_text))
    rendered = dict(request)
    rendered["prompt"] = render_conversation(turns, add_generation_prompt=True)
    return rendered


def generate_via_native_engine(request: Mapping[str, Any]) -> dict[str, Any]:
    """governed generate() 的原生短路入口（flag 開啟時由 runtime 呼叫）。

    B167/B38：Python/PyTorch 推論面已退役——唯一執行路徑是正式 C++
    引擎；引擎或 bundle 不可用時回傳型別化錯誤，無 fallback。
    """
    if not flag_enabled():
        return {
            "ok": False,
            "error_code": "NATIVE_ENGINE_DISABLED",
            "message": "原生引擎未啟用（XINGCHENG_NATIVE_ENGINE）",
            "fallback_required": False,
        }
    request = _dialogue_templated_request(request)
    return cpp_runtime.generate_via_cpp_engine(request)


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
    """控制面狀態：開關、服務 bundle、生成預設。"""
    serving = configured_checkpoint_path()
    return {
        "enabled": flag_enabled(),
        "serving_bundle": str(serving),
        "serving_bundle_ready": serving.is_dir() and cpp_runtime.is_bundle_dir(serving),
        "cpp_engine_available": cpp_runtime.available(),
        "settings_path": str(settings_path()),
        "settings": load_settings(),
        "defaults": generation_defaults(),
    }


def _cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="xingcheng-native-engine",
        description="星澄原生引擎 CLI：控制面（status/enable/disable/bundle）與生成",
    )
    parser.add_argument("--status", action="store_true", help="顯示控制面狀態")
    parser.add_argument("--enable", action="store_true", help="開啟原生引擎")
    parser.add_argument("--disable", action="store_true", help="關閉原生引擎（fail-closed）")
    parser.add_argument("--set-bundle", default=None, help="指定服務 bundle 目錄並持久化")
    parser.add_argument("--bundle", default=None, help="本次生成使用的 bundle 目錄")
    parser.add_argument("--prompt", default=None, help="輸入文字（未提供時僅執行控制指令）")
    parser.add_argument("--max-new-tokens", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument("--repetition-penalty", type=float, default=None)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    if args.enable or args.disable or args.set_bundle:
        write_settings(
            enabled=True if args.enable else (False if args.disable else None),
            cpp_bundle=args.set_bundle,
        )
        print(json.dumps(control_status(), ensure_ascii=False, indent=2))
        if not args.prompt:
            return 0
    if args.status or not args.prompt:
        print(json.dumps(control_status(), ensure_ascii=False, indent=2))
        return 0
    request: dict[str, Any] = {
        "prompt": args.prompt,
        "max_tokens": args.max_new_tokens,
        "temperature": args.temperature,
        "top_k": args.top_k,
        "top_p": args.top_p,
        "repetition_penalty": args.repetition_penalty,
        "seed": args.seed,
    }
    if args.bundle:
        try:
            engine = cpp_runtime.cpp_engine_for(
                cpp_runtime.assert_inside_xingcheng(
                    _resolve_serving_path(args.bundle)
                )
            )
        except Exception as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
            return 2
        result = engine.generate(prompt=args.prompt)
    else:
        result = generate_via_native_engine(request)
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
    "configured_checkpoint_path",
    "control_status",
    "cpu_generation_cap",
    "cpu_thread_budget",
    "flag_enabled",
    "generate_via_native_engine",
    "generation_defaults",
    "load_settings",
    "native_engine_available",
    "native_engine_for",
    "quality_guard",
    "scope_check",
    "scope_refusal_message",
    "settings_path",
    "small_talk_reply",
    "tool_root",
    "write_settings",
]
