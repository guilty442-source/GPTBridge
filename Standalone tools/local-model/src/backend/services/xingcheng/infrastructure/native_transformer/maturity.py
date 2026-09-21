"""星澄統一模型成熟度（``star-model-maturity/v1``）。

成熟度**只由實際執行的測試決定**——參數量、preset 名稱、checkpoint
大小都不計入分級依據。認證從 LEVEL 0 起逐級向上執行，
第一個失敗／缺件的等級即為天花板（等級必須連續，
不允許「L5 過但 L3 跳過」）。

等級定義：

    LEVEL 0  structure_init        模型結構可初始化
    LEVEL 1  forward_backward      Forward / Backward 正常
    LEVEL 2  overfit_small         小資料集可過度擬合
    LEVEL 3  effective_pretrain    完成有效語料預訓練（困惑度閘門）
    LEVEL 4  generation            獨立語言生成
    LEVEL 5  dialogue_instruction  多輪對話與指令遵循
    LEVEL 6  reasoning_tools       可驗證推理與工具使用
    LEVEL 7  controlled_evolution  受控持續學習與版本演進

閘門原則：
- 每個探針都有**程式化可驗證**的判準（數值閘門、格式解析、
  精確比對），不靠人工判讀；
- ``skipped`` 不等於通過——缺少必要 artefact（checkpoint /
  tokenizer / tool_root）時該級標記 skipped 並終止認證；
- 認證報告寫入 ``xingcheng/runtime/logs/maturity-*.json``，
  最新狀態在 ``xingcheng/runtime/state/model-maturity.json``。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import torch

from .config import XingChengConfig
from .modules.model import XingChengForCausalLM

MATURITY_FORMAT = "star-model-maturity/v1"
STATE_RELATIVE = "xingcheng/runtime/state/model-maturity.json"
LOG_RELATIVE = "xingcheng/runtime/logs"
LIFECYCLE_RELATIVE = "xingcheng/runtime/models/lifecycle/xingcheng-native"

LEVELS: tuple[dict[str, Any], ...] = (
    {"level": 0, "code": "structure_init",
     "name": "模型結構可初始化"},
    {"level": 1, "code": "forward_backward",
     "name": "Forward / Backward 正常"},
    {"level": 2, "code": "overfit_small",
     "name": "模型能在小資料集成功過度擬合"},
    {"level": 3, "code": "effective_pretrain",
     "name": "完成有效語料預訓練"},
    {"level": 4, "code": "generation",
     "name": "具備獨立語言生成能力"},
    {"level": 5, "code": "dialogue_instruction",
     "name": "具備多輪對話與指令遵循能力"},
    {"level": 6, "code": "reasoning_tools",
     "name": "具備可驗證的推理及工具使用能力"},
    {"level": 7, "code": "controlled_evolution",
     "name": "具備受控模型持續學習與版本演進能力"},
)
MAX_LEVEL = len(LEVELS) - 1

# L4 生成探針：覆蓋中英文與結構化輸出
_GENERATION_PROMPTS: tuple[str, ...] = (
    "今天的天氣",
    "星澄模型是",
    "請寫出 1 2 3",
    "def add(",
)

# L5 對話／指令探針：每個附程式化 checker（不用人工判讀）
#   checker(reply_text, reply) -> bool
_L5_PROBES: tuple[dict[str, Any], ...] = (
    {
        "id": "turn_boundary",
        "turns": ["你好"],
        "check": "stopped_or_eot",
        "desc": "回覆在回合邊界停止（eos 或 <|eot|>）",
    },
    {
        "id": "echo",
        "turns": ["請只輸出這四個字：星火測試"],
        "check": "contains:星火測試",
        "desc": "指令遵循：逐字複誦指定字串",
    },
    {
        "id": "choice",
        "turns": ["只能回答「是」或「否」。地球是圓的嗎？"],
        "check": "choice:是|否",
        "desc": "指令遵循：限定回答集合",
    },
    {
        "id": "multiturn_memory",
        "turns": ["請記住這個代號：QZ-88", "我剛才給你的代號是什麼？"],
        "check": "contains:QZ-88",
        "desc": "多輪上下文記憶",
    },
)

# L6 可驗證推理探針：答案可程式化比對
_L6_REASONING_PROBES: tuple[dict[str, Any], ...] = (
    {"id": "arith_add", "prompt": "計算 13 + 29，只輸出數字。", "expect": "42"},
    {"id": "arith_mul", "prompt": "計算 6 × 7，只輸出數字。", "expect": "42"},
    {"id": "compare", "prompt": "9 和 4 哪個大？只輸出較大的數字。", "expect": "9"},
)


@dataclass
class LevelResult:
    """單一等級的測試結果（結構化證據）。"""

    level: int
    code: str
    name: str
    status: str = "pending"          # pass | fail | skipped
    metrics: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "name": self.name,
            "status": self.status,
            "metrics": self.metrics,
            "evidence": self.evidence,
            "error": self.error,
            "duration_s": round(self.duration_s, 3),
        }


@dataclass
class MaturityContext:
    """認證過程共享的資源（模型／tokenizer／artefacts）。"""

    model: Any = None
    config: Any = None
    tokenizer: Any = None
    device: torch.device = torch.device("cpu")
    tool_root: Path | None = None
    checkpoint_path: Path | None = None
    checkpoint_extra: dict[str, Any] = field(default_factory=dict)
    eval_text: str = ""
    run_live_cycle: bool = False
    gates: dict[str, Any] = field(default_factory=dict)


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _isfinite_model(model: Any) -> tuple[bool, int]:
    """回傳 (all_finite, non_finite_count)。"""
    bad = 0
    with torch.no_grad():
        for param in model.parameters():
            bad += int((~torch.isfinite(param)).sum().item())
    return bad == 0, bad


def _run(level_index: int, ctx: MaturityContext,
         fn: Callable[[MaturityContext, LevelResult], None]) -> LevelResult:
    spec = LEVELS[level_index]
    result = LevelResult(level=spec["level"], code=spec["code"],
                         name=spec["name"])
    start = time.monotonic()
    try:
        fn(ctx, result)
    except Exception as exc:  # 測試本身異常 = fail（附錯誤證據）
        result.status = "fail"
        result.error = f"{type(exc).__name__}:{exc}"
    result.duration_s = time.monotonic() - start
    if result.status == "pending":
        result.status = "fail"
        result.error = result.error or "test did not produce a verdict"
    return result


def _skip(result: LevelResult, reason: str) -> None:
    result.status = "skipped"
    result.error = reason


# ── LEVEL 0：結構初始化 ──────────────────────────────────────

def _test_l0(ctx: MaturityContext, result: LevelResult) -> None:
    model = ctx.model
    params = int(model.num_parameters())
    finite, bad = _isfinite_model(model)
    modules = dict(model.named_modules())
    evidence = {
        "parameters": params,
        "non_finite_params": bad,
        "num_layers": int(ctx.config.num_hidden_layers),
        "hidden_size": int(ctx.config.hidden_size),
        "vocab_size": int(ctx.config.vocab_size),
        "use_moe": bool(getattr(ctx.config, "use_moe", False)),
    }
    result.metrics = evidence
    has_blocks = any("block" in name or "layer" in name for name in modules)
    ok = params > 0 and finite and has_blocks
    result.evidence = {
        "parameters_positive": params > 0,
        "all_params_finite": finite,
        "transformer_blocks_present": has_blocks,
    }
    result.status = "pass" if ok else "fail"
    if not ok:
        result.error = "structure check failed"


# ── LEVEL 1：Forward / Backward ─────────────────────────────

def _test_l1(ctx: MaturityContext, result: LevelResult) -> None:
    model = ctx.model
    model.train()
    vocab = int(ctx.config.vocab_size)
    seq = min(64, int(ctx.config.max_position_embeddings))
    ids = torch.randint(0, vocab, (2, seq), device=ctx.device)
    out = model(ids, labels=ids)
    loss = out["loss"] if isinstance(out, Mapping) else out.loss
    loss_value = float(loss.item())
    if not math.isfinite(loss_value):
        result.metrics = {"loss": loss_value}
        result.error = "forward loss is not finite"
        return
    model.zero_grad(set_to_none=True)
    loss.backward()
    total_norm = 0.0
    missing = 0
    nonfinite = 0
    for param in model.parameters():
        if param.grad is None:
            missing += 1
            continue
        norm = float(param.grad.norm().item())
        if not math.isfinite(norm):
            nonfinite += 1
        total_norm += norm * norm
    total_norm = math.sqrt(total_norm)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-5)
    optimizer.step()
    model.eval()
    with torch.no_grad():
        out2 = model(ids)
        logits = out2["logits"] if isinstance(out2, Mapping) else out2.logits
        logits_finite = bool(torch.isfinite(logits).all().item())
    result.metrics = {
        "loss": loss_value,
        "grad_norm": round(total_norm, 4),
        "params_missing_grad": missing,
        "params_nonfinite_grad": nonfinite,
        "post_step_logits_finite": logits_finite,
        "logits_shape": list(logits.shape),
    }
    ok = (
        missing == 0 and nonfinite == 0 and total_norm > 0.0
        and logits_finite
    )
    result.status = "pass" if ok else "fail"
    if not ok:
        result.error = "backward/step check failed"


# ── LEVEL 2：小資料集過度擬合 ────────────────────────────────

_OVERFIT_SAMPLES: tuple[str, ...] = (
    "星澄模型成熟度測試樣本一。",
    "模型需要能夠記住這句話。",
    "GPTBridge 本地原生架構。",
    "過度擬合是學習能力的下限證明。",
    "1 + 1 = 2",
    "今天日期不重要，記住這串文字。",
    "Transformer attention is all you need.",
    "最後一個樣本：完。",
)


def _overfit_batch(ctx: MaturityContext, seq_len: int = 48) -> torch.Tensor:
    """固定樣本 → 確定性 batch（pad 到等長）。"""
    pad = int(getattr(ctx.tokenizer, "pad_id", 0) or 0)
    rows: list[list[int]] = []
    for text in _OVERFIT_SAMPLES:
        ids = list(ctx.tokenizer.encode(text, add_bos=True, add_eos=True))
        ids = ids[:seq_len]
        ids = ids + [pad] * (seq_len - len(ids))
        rows.append(ids)
    return torch.tensor(rows, dtype=torch.long, device=ctx.device)


def _test_l2(ctx: MaturityContext, result: LevelResult) -> None:
    gates = ctx.gates
    max_steps = int(gates.get("overfit_max_steps", 400))
    loss_gate = float(gates.get("overfit_loss_threshold", 0.5))
    ratio_gate = float(gates.get("overfit_ratio_threshold", 0.20))
    lr = float(gates.get("overfit_lr", 5e-4))

    # 備份權重——過擬合只證明「可學習」，認證不得污染受測權重
    backup = {
        key: value.detach().to("cpu", copy=True)
        for key, value in ctx.model.state_dict().items()
    }
    try:
        model = ctx.model
        model.train()
        batch = _overfit_batch(ctx)
        optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
        initial_loss = None
        final_loss = None
        steps_used = 0
        for step in range(max_steps):
            out = model(batch, labels=batch)
            loss = out["loss"] if isinstance(out, Mapping) else out.loss
            value = float(loss.item())
            if initial_loss is None:
                initial_loss = value
            final_loss = value
            if not math.isfinite(value):
                break
            if value <= loss_gate:
                steps_used = step + 1
                break
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()
            steps_used = step + 1
        ratio = (
            final_loss / initial_loss
            if initial_loss and initial_loss > 0
            else float("inf")
        )
        result.metrics = {
            "initial_loss": round(initial_loss or 0.0, 4),
            "final_loss": round(final_loss or 0.0, 4),
            "loss_ratio": round(ratio, 4),
            "steps_used": steps_used,
            "loss_gate": loss_gate,
            "ratio_gate": ratio_gate,
        }
        ok = (
            final_loss is not None
            and math.isfinite(final_loss)
            and (final_loss <= loss_gate or ratio <= ratio_gate)
        )
        result.status = "pass" if ok else "fail"
        if not ok:
            result.error = "model could not overfit the fixed 8-sample set"
    finally:
        ctx.model.load_state_dict(backup)
        ctx.model.to(ctx.device)
        ctx.model.eval()


# ── LEVEL 3：有效語料預訓練 ─────────────────────────────────

def _eval_perplexity(ctx: MaturityContext, text: str, block: int = 64) -> float:
    ids = list(ctx.tokenizer.encode(text, add_bos=False, add_eos=False))
    if len(ids) < block + 1:
        block = max(8, len(ids) - 1)
    if len(ids) < block + 1:
        return float("nan")
    total = 0.0
    batches = 0
    ctx.model.eval()
    with torch.no_grad():
        for start in range(0, len(ids) - block, block):
            chunk = ids[start : start + block]
            batch = torch.tensor([chunk], dtype=torch.long, device=ctx.device)
            out = ctx.model(batch, labels=batch)
            loss = out["loss"] if isinstance(out, Mapping) else out.loss
            total += float(loss.item())
            batches += 1
    if not batches:
        return float("nan")
    return float(math.exp(min(20.0, total / batches)))


_DEFAULT_EVAL_TEXT = (
    "星澄是 GPTBridge 的本地原生語言模型，採用自研 Transformer 架構，"
    "在受治理的訓練管線中完成預訓練與微調。模型核心與網路功能完全分離，"
    "所有外部存取都必須經過治理工具路徑。評估模型的成熟度時，"
    "必須以實際測試結果為依據，而不是以參數規模直接認定能力等級。"
    "The quick brown fox jumps over the lazy dog. " * 4
)


def _test_l3(ctx: MaturityContext, result: LevelResult) -> None:
    if ctx.checkpoint_path is None or ctx.tokenizer is None:
        return _skip(result, "requires checkpoint + tokenizer")
    text = ctx.eval_text or _DEFAULT_EVAL_TEXT
    ppl = _eval_perplexity(ctx, text)
    vocab = int(ctx.config.vocab_size)
    ratio_gate = float(ctx.gates.get("max_ppl_ratio", 0.25))
    ratio = ppl / vocab if vocab > 0 else float("inf")
    extra = ctx.checkpoint_extra
    result.metrics = {
        "eval_perplexity": round(ppl, 4),
        "vocab_baseline_ppl": vocab,
        "ppl_ratio": round(ratio, 4),
        "ppl_ratio_gate": ratio_gate,
        "trained_steps": extra.get("step"),
        "tokens_seen": extra.get("tokens_seen"),
    }
    result.evidence = {
        "checkpoint": str(ctx.checkpoint_path),
        "baseline": "random-uniform ppl == vocab_size",
    }
    ok = math.isfinite(ppl) and ratio <= ratio_gate
    result.status = "pass" if ok else "fail"
    if not ok:
        result.error = "perplexity above gate (or not finite)"


# ── LEVEL 4：獨立語言生成 ───────────────────────────────────

def _build_generator(ctx: MaturityContext):
    from .inference.generate import Generator
    from .inference.sampler import Sampler, SamplingConfig

    eos = int(getattr(ctx.tokenizer, "eos_id", 2) or 2)
    pad = int(getattr(ctx.tokenizer, "pad_id", 0) or 0)
    sampler = Sampler(SamplingConfig(
        do_sample=False, eos_token_id=eos, pad_token_id=pad,
    ))
    return Generator(ctx.model, sampler=sampler, device=ctx.device)


def _generate_text(ctx: MaturityContext, prompt: str,
                   max_new: int) -> tuple[str, torch.Tensor]:
    generator = _build_generator(ctx)
    ids = list(ctx.tokenizer.encode(prompt, add_bos=True, add_eos=False))
    if not ids:
        ids = [int(getattr(ctx.tokenizer, "bos_id", 1) or 1)]
    batch = torch.tensor([ids], dtype=torch.long, device=ctx.device)
    out = generator.generate(batch, max_new_tokens=max_new)
    new_ids = out[0, len(ids):].tolist()
    return ctx.tokenizer.decode(new_ids, skip_special=True), out


def _test_l4(ctx: MaturityContext, result: LevelResult) -> None:
    if ctx.tokenizer is None:
        return _skip(result, "requires tokenizer")
    min_tokens = int(ctx.gates.get("gen_min_tokens", 4))
    min_unique = float(ctx.gates.get("gen_min_unique_ratio", 0.25))
    max_top_fraction = float(ctx.gates.get("gen_max_top_token_fraction", 0.9))
    pass_ratio_gate = float(ctx.gates.get("gen_pass_ratio", 0.75))
    per_prompt: list[dict[str, Any]] = []
    passed = 0
    for prompt in _GENERATION_PROMPTS:
        text, out = _generate_text(ctx, prompt, max_new=32)
        new = out[0, -32:].tolist()
        produced = len(new)
        unique_ratio = len(set(new)) / produced if produced else 0.0
        top_fraction = (
            max(new.count(t) for t in set(new)) / produced if produced else 1.0
        )
        ok = (
            produced >= min_tokens
            and bool(text.strip())
            and unique_ratio >= min_unique
            and top_fraction <= max_top_fraction
        )
        passed += int(ok)
        per_prompt.append({
            "prompt": prompt,
            "produced_tokens": produced,
            "unique_ratio": round(unique_ratio, 3),
            "top_token_fraction": round(top_fraction, 3),
            "output_excerpt": text.strip()[:80],
            "passed": ok,
        })
    total = len(_GENERATION_PROMPTS)
    ratio = passed / total if total else 0.0
    result.metrics = {
        "prompts_passed": passed,
        "prompts_total": total,
        "pass_ratio": round(ratio, 3),
        "pass_ratio_gate": pass_ratio_gate,
    }
    result.evidence = {"probes": per_prompt}
    ok = ratio >= pass_ratio_gate
    result.status = "pass" if ok else "fail"
    if not ok:
        result.error = f"generation pass ratio {ratio:.2f} < {pass_ratio_gate}"


# ── LEVEL 5：多輪對話與指令遵循 ─────────────────────────────

def _check_l5(spec: Mapping[str, Any], reply: Any) -> bool:
    check = str(spec["check"])
    text = str(reply.text or "")
    raw = str(reply.raw_text or "")
    if check == "stopped_or_eot":
        return bool(reply.stopped_by_eos) or "<|eot|>" in raw
    if check.startswith("contains:"):
        needle = check.split(":", 1)[1]
        return needle in text or needle in raw
    if check.startswith("choice:"):
        options = check.split(":", 1)[1].split("|")
        normalized = text.strip().rstrip("。．.!！?？")
        return normalized in options
    return False


def _test_l5(ctx: MaturityContext, result: LevelResult) -> None:
    if ctx.tokenizer is None:
        return _skip(result, "requires tokenizer")
    from .inference.chat_session import ChatSession

    pass_ratio_gate = float(ctx.gates.get("dialogue_pass_ratio", 0.75))
    max_new = int(ctx.gates.get("dialogue_max_new_tokens", 48))
    session = ChatSession(
        generator=_build_generator(ctx),
        tokenizer=ctx.tokenizer,
        system_prompt="你是星澄，一個本地模型。簡短回答。",
    )
    probe_results: list[dict[str, Any]] = []
    passed = 0
    for spec in _L5_PROBES:
        reply = None
        for turn in spec["turns"]:
            reply = session.step(turn, max_new_tokens=max_new)
        ok = reply is not None and _check_l5(spec, reply)
        passed += int(ok)
        probe_results.append({
            "id": spec["id"],
            "desc": spec["desc"],
            "reply_excerpt": str(reply.text if reply else "")[:80],
            "stopped_by_eos": bool(reply.stopped_by_eos) if reply else False,
            "passed": ok,
        })
    total = len(_L5_PROBES)
    ratio = passed / total if total else 0.0
    result.metrics = {
        "probes_passed": passed,
        "probes_total": total,
        "pass_ratio": round(ratio, 3),
        "pass_ratio_gate": pass_ratio_gate,
    }
    result.evidence = {"probes": probe_results}
    ok = ratio >= pass_ratio_gate
    result.status = "pass" if ok else "fail"
    if not ok:
        result.error = f"dialogue pass ratio {ratio:.2f} < {pass_ratio_gate}"


# ── LEVEL 6：可驗證推理與工具使用 ───────────────────────────

_TOOL_CALL_INSTRUCTION = (
    "你有一個工具 calculator。當需要計算時，只輸出："
    '<tool_call>{"name": "calculator", "arguments": {"expression": "算式"}}'
    "</tool_call>，不要輸出其他內容。"
)


def _extract_first_int(text: str) -> int | None:
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def _test_l6(ctx: MaturityContext, result: LevelResult) -> None:
    if ctx.tokenizer is None:
        return _skip(result, "requires tokenizer")
    from .chat_format import split_tool_call
    from .inference.chat_session import ChatSession

    pass_ratio_gate = float(ctx.gates.get("reasoning_pass_ratio", 0.5))
    max_new = int(ctx.gates.get("reasoning_max_new_tokens", 48))
    generator = _build_generator(ctx)

    reasoning: list[dict[str, Any]] = []
    reasoning_passed = 0
    for spec in _L6_REASONING_PROBES:
        session = ChatSession(generator=generator, tokenizer=ctx.tokenizer)
        reply = session.step(spec["prompt"], max_new_tokens=max_new)
        answer = _extract_first_int(reply.text or reply.raw_text)
        ok = answer is not None and str(answer) == str(spec["expect"])
        reasoning_passed += int(ok)
        reasoning.append({
            "id": spec["id"], "expected": spec["expect"],
            "answer": answer, "passed": ok,
            "reply_excerpt": str(reply.text)[:80],
        })

    tool_ok = False
    tool_detail: dict[str, Any] = {}
    session = ChatSession(
        generator=generator,
        tokenizer=ctx.tokenizer,
        system_prompt=_TOOL_CALL_INSTRUCTION,
    )
    reply = session.step("請用工具計算 128 + 256。", max_new_tokens=max_new)
    calls = list(reply.tool_calls or ())
    parsed = None
    if calls:
        parsed = calls[0]
    else:
        # 後備：直接對 raw text 切 tool_call（防 history 清理影響）
        _, fallback = split_tool_call(reply.raw_text or "")
        parsed = fallback[0] if fallback else None
    if isinstance(parsed, Mapping):
        name = str(parsed.get("name") or "")
        arguments = parsed.get("arguments")
        tool_ok = name == "calculator" and isinstance(arguments, Mapping)
        tool_detail = {"emitted": True, "name": name,
                       "arguments_valid": isinstance(arguments, Mapping)}
    else:
        tool_detail = {"emitted": False}

    total = len(_L6_REASONING_PROBES) + 1
    passed = reasoning_passed + int(tool_ok)
    ratio = passed / total
    result.metrics = {
        "reasoning_passed": reasoning_passed,
        "reasoning_total": len(_L6_REASONING_PROBES),
        "tool_call_valid": tool_ok,
        "pass_ratio": round(ratio, 3),
        "pass_ratio_gate": pass_ratio_gate,
    }
    result.evidence = {"reasoning": reasoning, "tool_call": tool_detail}
    ok = ratio >= pass_ratio_gate and tool_ok
    result.status = "pass" if ok else "fail"
    if not ok:
        result.error = "reasoning/tool-use below gate"


# ── LEVEL 7：受控持續學習與版本演進 ─────────────────────────

def _test_l7(ctx: MaturityContext, result: LevelResult) -> None:
    if ctx.tool_root is None:
        return _skip(result, "requires tool_root")
    from .lifecycle import ModelLifecycle
    from .self_learning import SelfLearningPolicy, run_cycle

    evidence: dict[str, Any] = {}

    # (1) kill switch 實測：disabled policy 必須 fail-closed 不訓練
    disabled = run_cycle(ctx.tool_root, policy=SelfLearningPolicy(enabled=False))
    kill_switch_ok = (
        isinstance(disabled, Mapping) and disabled.get("action") == "disabled"
    )
    evidence["kill_switch"] = {"ok": kill_switch_ok, "result_action":
                               disabled.get("action") if isinstance(disabled, Mapping) else None}

    # (2) 生命週期版本演進機制：在記憶體副本上實際走
    #     register → activate → rollback（不寫回真實 lifecycle.json）
    lifecycle_dir = ctx.tool_root / LIFECYCLE_RELATIVE
    lifecycle = (
        ModelLifecycle.load(lifecycle_dir)
        if (lifecycle_dir / "lifecycle.json").is_file()
        else ModelLifecycle(model_id="maturity-probe")
    )
    # 先記錄真實版本數——下方 mechanics 探針會在記憶體副本上 +1
    weights_on_disk = len(
        lifecycle.artifacts.get("weights", {}).get("versions", [])
    )
    evals_on_disk = len(
        lifecycle.artifacts.get("evaluation_report", {}).get("versions", [])
    )
    mechanics_ok = False
    try:
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"maturity-probe-weights")
            temp_weights = handle.name
        try:
            entry = lifecycle.register_artifact(
                "weights", temp_weights, activate=True
            )
            rolled = lifecycle.rollback_weights(int(entry["version"]) - 1) \
                if int(entry["version"]) > 1 else entry
            mechanics_ok = (
                int(entry["version"]) >= 1
                and lifecycle.active_weights_version
                == int(rolled["version"])
            )
        finally:
            os.unlink(temp_weights)
    except Exception as exc:
        evidence["lifecycle_error"] = f"{type(exc).__name__}:{exc}"
    evidence["lifecycle_mechanics"] = {"ok": mechanics_ok}

    # (3) 受控循環證據：跑過一次完整 governed cycle（live）或
    #     存在已通過評估閘門的循環報告
    cycle_ok = False
    if ctx.run_live_cycle:
        cycle = run_cycle(ctx.tool_root, force=True)
        cycle_ok = bool(
            isinstance(cycle, Mapping)
            and cycle.get("ok")
            and cycle.get("action") not in {"disabled", "idle", "blocked"}
        )
        evidence["live_cycle"] = {"ok": cycle_ok,
                                  "action": cycle.get("action")
                                  if isinstance(cycle, Mapping) else None}
    else:
        logs_dir = ctx.tool_root / LOG_RELATIVE
        reports = sorted(logs_dir.glob("self-learning-*.json")) \
            if logs_dir.is_dir() else []
        passing_report = None
        for path in reversed(reports):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            evaluation = report.get("evaluation") or {}
            if report.get("ok") and (
                report.get("action") == "upgraded"
                or evaluation.get("passed") is True
            ):
                passing_report = str(path)
                break
        # 版本演進證據：self-learning 循環報告，或生命週期本身已有
        # ≥2 代權重 + ≥1 份評估報告（受管升級的 artefact 鏈）
        evolved = weights_on_disk >= 2 and evals_on_disk >= 1
        cycle_ok = passing_report is not None or evolved
        evidence["recorded_cycle"] = {
            "ok": cycle_ok,
            "passing_report": passing_report,
            "lifecycle_weight_versions": weights_on_disk,
            "lifecycle_eval_reports": evals_on_disk,
            "evolution_evidence": "self_learning_report" if passing_report
                else ("lifecycle_versions" if evolved else None),
        }

    result.metrics = {
        "kill_switch_ok": kill_switch_ok,
        "lifecycle_mechanics_ok": mechanics_ok,
        "governed_cycle_ok": cycle_ok,
    }
    result.evidence = evidence
    ok = kill_switch_ok and mechanics_ok and cycle_ok
    result.status = "pass" if ok else "fail"
    if not ok:
        result.error = "controlled-evolution checks incomplete"


_TESTS: tuple[Callable[[MaturityContext, LevelResult], None], ...] = (
    _test_l0, _test_l1, _test_l2, _test_l3,
    _test_l4, _test_l5, _test_l6, _test_l7,
)


# ── 認證入口 ─────────────────────────────────────────────────

def certify(
    *,
    checkpoint: str | Path | None = None,
    config: XingChengConfig | None = None,
    tool_root: str | Path | None = None,
    device: str | None = None,
    max_level: int = MAX_LEVEL,
    eval_text: str = "",
    run_live_cycle: bool = False,
    gates: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """執行成熟度認證：LEVEL 0 起逐級實測，首個 fail/skip 即終止。

    ``checkpoint``：認證已訓練權重（L3+ 需要）。
    ``config``：僅驗證架構時使用（L0–L2 可跑，L3+ 會 skipped）。
    參數量僅作為證據紀錄，不參與任何等級判定。
    """
    ctx = MaturityContext(
        tool_root=Path(tool_root).resolve() if tool_root else None,
        eval_text=eval_text,
        run_live_cycle=run_live_cycle,
        gates=dict(gates or {}),
    )
    ctx.device = torch.device(
        device or ("cuda" if torch.cuda.is_available() else "cpu")
    )

    if checkpoint is not None:
        from .checkpoint import load_checkpoint

        ctx.checkpoint_path = Path(checkpoint)
        bundle = load_checkpoint(ctx.checkpoint_path)
        ctx.model = bundle["model"]
        ctx.config = bundle["config"]
        ctx.tokenizer = bundle.get("tokenizer")
        try:
            ctx.checkpoint_extra = json.loads(
                bundle.get("extra_json") or "{}"
            ) if isinstance(bundle.get("extra_json"), str) else dict(
                bundle.get("extra") or {}
            )
        except (TypeError, json.JSONDecodeError):
            ctx.checkpoint_extra = {}
    elif config is not None:
        ctx.config = config
        ctx.model = XingChengForCausalLM(config)
        try:
            from .tokenizer import XingChengTokenizer
            ctx.tokenizer = XingChengTokenizer.from_config(config)
        except Exception:
            ctx.tokenizer = None
    else:
        raise ValueError("certify requires checkpoint= or config=")

    ctx.model.to(ctx.device)
    ctx.model.eval()

    results: list[LevelResult] = []
    certified = -1
    for index in range(0, min(int(max_level), MAX_LEVEL) + 1):
        result = _run(index, ctx, _TESTS[index])
        results.append(result)
        if result.status != "pass":
            break
        certified = result.level

    report = {
        "format": MATURITY_FORMAT,
        "certified_at": _utcnow(),
        "certified_level": certified,
        "certified_level_name": (
            LEVELS[certified]["name"] if certified >= 0 else None
        ),
        "checkpoint": str(ctx.checkpoint_path) if ctx.checkpoint_path else None,
        "device": str(ctx.device),
        "parameters": (
            int(ctx.model.num_parameters()) if ctx.model is not None else None
        ),
        "note": "level is decided by executed tests only; "
                "parameter count is evidence, not a criterion",
        "levels": [item.to_dict() for item in results],
    }
    return report


def _report_paths(tool_root: Path) -> tuple[Path, Path]:
    logs = tool_root / LOG_RELATIVE
    logs.mkdir(parents=True, exist_ok=True)
    report_path = logs / f"maturity-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.json"
    state_path = tool_root / STATE_RELATIVE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    return report_path, state_path


def persist_report(tool_root: str | Path, report: Mapping[str, Any]) -> Path:
    """寫入認證報告並更新最新狀態檔（原子寫入）。"""
    root = Path(tool_root).resolve()
    report_path, state_path = _report_paths(root)
    report_path.write_text(
        json.dumps(dict(report), ensure_ascii=False, indent=2, default=str)
        + "\n",
        encoding="utf-8",
    )
    temp = state_path.with_name(state_path.name + ".tmp")
    temp.write_text(
        json.dumps(
            {
                "format": MATURITY_FORMAT,
                "certified_level": report.get("certified_level"),
                "certified_at": report.get("certified_at"),
                "checkpoint": report.get("checkpoint"),
                "report": str(report_path),
            },
            ensure_ascii=False, indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temp, state_path)
    return report_path


def current_maturity(tool_root: str | Path) -> dict[str, Any]:
    """讀取最近一次認證的成熟度（供治理／自學習閘門查詢）。"""
    path = Path(tool_root).resolve() / STATE_RELATIVE
    if not path.is_file():
        return {"format": MATURITY_FORMAT, "certified_level": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"format": MATURITY_FORMAT, "certified_level": None}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="星澄模型成熟度認證")
    parser.add_argument("--checkpoint", default=None,
                        help="已訓練 checkpoint 路徑（L3+ 需要）")
    parser.add_argument("--preset", default=None,
                        help="僅驗證架構（如 small / medium_moe；L0-L2）")
    parser.add_argument("--tool-root", default=None,
                        help="local-model 工具根目錄（L7 / 報告落盤需要）")
    parser.add_argument("--device", default=None)
    parser.add_argument("--max-level", type=int, default=MAX_LEVEL)
    parser.add_argument("--eval-file", default=None,
                        help="L3 held-out 評估文本檔案")
    parser.add_argument("--live-cycle", action="store_true",
                        help="L7 實際執行一次 governed self-learning cycle")
    parser.add_argument("--status", action="store_true",
                        help="讀取 tool_root 最近一次認證結果")
    parser.add_argument("--save", action="store_true",
                        help="報告寫入 runtime logs/state（需 --tool-root）")
    args = parser.parse_args(argv)

    if args.status:
        if not args.tool_root:
            parser.error("--status requires --tool-root")
        print(json.dumps(current_maturity(args.tool_root),
                         ensure_ascii=False, indent=2))
        return 0

    config = None
    if args.preset:
        presets = {
            name: getattr(XingChengConfig, name)
            for name in (
                "small", "medium", "base", "xlarge", "large",
                "small_moe", "medium_moe", "base_moe",
                "xlarge_moe", "large_moe",
            )
        }
        if args.preset not in presets:
            parser.error(f"unknown preset: {args.preset}")
        config = presets[args.preset]()

    eval_text = ""
    if args.eval_file:
        eval_text = Path(args.eval_file).read_text(encoding="utf-8")

    report = certify(
        checkpoint=args.checkpoint,
        config=config,
        tool_root=args.tool_root,
        device=args.device,
        max_level=args.max_level,
        eval_text=eval_text,
        run_live_cycle=args.live_cycle,
    )
    if args.save and args.tool_root:
        path = persist_report(args.tool_root, report)
        report = dict(report)
        report["report_path"] = str(path)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if int(report.get("certified_level") or -1) >= 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "LEVELS",
    "MATURITY_FORMAT",
    "MAX_LEVEL",
    "LevelResult",
    "MaturityContext",
    "certify",
    "current_maturity",
    "persist_report",
]
