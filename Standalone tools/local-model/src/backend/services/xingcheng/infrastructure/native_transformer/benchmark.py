"""星澄原生推論基準：可重現的延遲／吞吐／量化困惑度量測。

固定 seed 與 prompt，輸出確定性 JSON 報告（不觸網）。供 Phase 4
驗收「量化前後困惑度差異在門檻內、延遲基準可重現」使用。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import torch

from .checkpoint import load_checkpoint
from .execution.backend import describe_sdpa_backends
from .inference.sampler import Sampler, SamplingConfig


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _perplexity(model: Any, token_ids: list[int], block: int = 64) -> float:
    """在 token 序列上以固定區塊量測困惑度（確定性）。"""
    block = min(int(block), len(token_ids) - 1)
    total = 0.0
    batches = 0
    model.eval()
    with torch.no_grad():
        for start in range(0, len(token_ids) - block, block):
            ids = torch.tensor(
                [token_ids[start : start + block]], dtype=torch.long
            )
            out = model(ids, labels=ids)
            total += float(out["loss"].item())
            batches += 1
    if not batches:
        return float("nan")
    return float(math.exp(min(20.0, total / batches)))


def run_benchmark(
    checkpoint_path: str | Path,
    *,
    prompt: str = "The model",
    max_new_tokens: int = 32,
    repeat: int = 3,
    seed: int = 42,
    quantize: int | None = None,
    eval_text: str = "",
) -> dict[str, Any]:
    """量測單一 checkpoint 的推論延遲／吞吐；``eval_text`` 提供時附
    困惑度（供量化前後對比）。回傳可重現的 JSON 相容 dict。"""
    loaded = load_checkpoint(checkpoint_path)
    model = loaded["model"]
    config = loaded["config"]
    tokenizer = loaded["tokenizer"]
    quantization = "none"
    if quantize in (4, 8):
        from .quantization import quantize_model

        model = quantize_model(model, n_bits=int(quantize))
        quantization = f"int{int(quantize)}"

    prompt_ids = tokenizer.encode(prompt, add_bos=True, add_eos=False)
    ids = torch.tensor([prompt_ids], dtype=torch.long)
    sampler = Sampler(
        SamplingConfig(
            do_sample=False,
            eos_token_id=tokenizer.eos_id,
            pad_token_id=tokenizer.pad_id,
        )
    )

    from .inference.generate import Generator

    generator = Generator(model, device=torch.device("cpu"))
    latencies: list[float] = []
    generated_tokens = 0
    for index in range(max(1, int(repeat))):
        torch.manual_seed(int(seed))
        started = time.perf_counter()
        generated = generator.generate(
            ids, max_new_tokens=int(max_new_tokens), sampling=sampler.config
        )
        elapsed = time.perf_counter() - started
        latencies.append(round(elapsed * 1000.0, 3))
        generated_tokens = int(generated.numel())

    report: dict[str, Any] = {
        "format_version": "star-native-benchmark/v1",
        "checkpoint": str(checkpoint_path),
        "checkpoint_sha256": _file_sha256(Path(checkpoint_path)),
        "model_parameters": int(model.num_parameters())
        if quantization == "none"
        else None,
        "quantization": quantization,
        "prompt_tokens": len(prompt_ids),
        "max_new_tokens": int(max_new_tokens),
        "generated_tokens": generated_tokens,
        "repeat": int(repeat),
        "seed": int(seed),
        "latency_ms": latencies,
        "latency_ms_mean": round(sum(latencies) / len(latencies), 3),
        "latency_ms_min": min(latencies),
        "tokens_per_second": round(
            generated_tokens / max(1e-6, min(latencies) / 1000.0), 2
        ),
        "context_window": int(config.max_position_embeddings),
        "attention_backend": describe_sdpa_backends(),
        "remote_network_used": False,
    }
    if eval_text:
        eval_ids = tokenizer.encode(eval_text, add_bos=False, add_eos=True)
        report["eval_perplexity"] = _perplexity(model, eval_ids)
    return report


def compare_perplexity(
    checkpoint_path: str | Path,
    eval_text: str,
    *,
    block: int = 64,
) -> dict[str, Any]:
    """量化前後困惑度對比（INT8 gate：差異需在門檻內）。"""
    base = run_benchmark(
        checkpoint_path, max_new_tokens=1, repeat=1, eval_text=eval_text
    )
    int8 = run_benchmark(
        checkpoint_path,
        max_new_tokens=1,
        repeat=1,
        quantize=8,
        eval_text=eval_text,
    )
    base_ppl = float(base["eval_perplexity"])
    int8_ppl = float(int8["eval_perplexity"])
    delta = (
        abs(int8_ppl - base_ppl) / base_ppl if math.isfinite(base_ppl) else None
    )
    return {
        "baseline_perplexity": base_ppl,
        "int8_perplexity": int8_ppl,
        "relative_delta": delta,
        "within_5_percent": bool(delta is not None and delta <= 0.05),
    }


@torch.no_grad()
def cached_perplexity(
    model: Any,
    token_ids: list[int],
    *,
    device: torch.device,
    kv_dtype: str | None = None,
    kv_quant: str | None = None,
) -> float:
    """以 KV cache 逐 token 推進的困惑度（真正走快取路徑，含量化）。"""
    from .inference.kv_cache import KVCache, resolve_kv_dtype

    if len(token_ids) < 2:
        return float("nan")
    config = model.config
    activation_dtype = next(model.parameters()).dtype
    dtype = resolve_kv_dtype(kv_dtype, activation_dtype)
    cache = KVCache(
        config, 1, len(token_ids), device, dtype, quant=kv_quant
    )
    total = 0.0
    count = 0
    model.eval()
    for index in range(len(token_ids) - 1):
        current = torch.tensor([[token_ids[index]]], dtype=torch.long, device=device)
        position = torch.tensor([[index]], dtype=torch.long, device=device)
        mask = torch.ones((1, 1), dtype=torch.long, device=device)
        out = model(
            current,
            position_ids=position,
            attention_mask=mask,
            kv_caches=cache.slice(index),
            use_cache=True,
        )
        logits = out["logits"][0, 0].to(torch.float32)
        target = int(token_ids[index + 1])
        nll = float(
            -torch.log_softmax(logits, dim=-1)[target].item()
        )
        total += nll
        count += 1
        for layer_idx, (k, v) in enumerate(out["kv_caches"]):
            cache.update(layer_idx, k, v, index)
    if count == 0:
        return float("nan")
    return float(math.exp(min(20.0, total / count)))


def run_kv_variant(
    checkpoint_path: str | Path,
    *,
    eval_text: str,
    prompt: str,
    max_new_tokens: int,
    device: str,
    kv_dtype: str | None,
    kv_quant: str | None,
) -> dict[str, Any]:
    """單一 KV 變體量測（獨立行程執行，VRAM 量測乾淨）。"""
    resolved = resolve_device_for_benchmark(device)
    loaded = load_checkpoint(checkpoint_path, map_location="cpu")
    tokenizer = loaded["tokenizer"]
    prompt_ids = tokenizer.encode(prompt, add_bos=True, add_eos=False)
    eval_ids = tokenizer.encode(eval_text, add_bos=False, add_eos=True)[:384]
    sampler = Sampler(
        SamplingConfig(
            do_sample=False, eos_token_id=-1, pad_token_id=tokenizer.pad_id
        )
    )

    from .inference.generate import Generator

    model = loaded["model"].to(resolved)
    model.eval()
    if resolved.type == "cuda":
        torch.cuda.reset_peak_memory_stats()
    generator = Generator(
        model,
        sampler=sampler,
        device=resolved,
        kv_cache_dtype=kv_dtype,
        kv_cache_quant=kv_quant,
    )
    ids = torch.tensor([prompt_ids], dtype=torch.long, device=resolved)
    started = time.perf_counter()
    generated = generator.generate(ids, max_new_tokens=max_new_tokens)
    elapsed = time.perf_counter() - started
    tokens = [int(token) for token in generated[0].tolist()]
    vram_peak_mb = (
        round(torch.cuda.max_memory_allocated() / (1024 * 1024), 1)
        if resolved.type == "cuda"
        else None
    )
    cache = generator.last_cache
    # 以「快取解碼步」量測 logits：餵最後一個 prompt token，past 為其之前的 K/V。
    with torch.no_grad():
        if cache is not None:
            past = cache.slice(max(0, len(prompt_ids) - 1))
            step = model(
                ids[:, -1:],
                position_ids=torch.tensor(
                    [[len(prompt_ids) - 1]], dtype=torch.long, device=resolved
                ),
                attention_mask=torch.ones((1, 1), dtype=torch.long, device=resolved),
                kv_caches=past,
                use_cache=True,
            )
            step_logits = step["logits"][0, -1].to(torch.float32)
        else:
            step_logits = model(
                ids, attention_mask=torch.ones_like(ids)
            )["logits"][0, -1].to(torch.float32)
    return {
        "kv_dtype": kv_dtype,
        "kv_quant": kv_quant or "none",
        "generated_tokens": tokens,
        "logits": [round(float(value), 6) for value in step_logits[:64].tolist()],
        "decode_ms_per_token": round(elapsed * 1000.0 / max(1, len(tokens)), 3),
        "vram_peak_mb": vram_peak_mb,
        "kv_cache_bytes": int(cache.memory_bytes()) if cache else 0,
        "kv_cache": cache.describe() if cache else {},
        "cached_perplexity": round(
            cached_perplexity(
                model, eval_ids, device=resolved, kv_dtype=kv_dtype, kv_quant=kv_quant
            ),
            4,
        ),
        "prompt_tokens": len(prompt_ids),
        "eval_tokens": len(eval_ids),
    }


def _kv_variant_subprocess(
    checkpoint_path: str | Path,
    *,
    eval_text: str,
    prompt: str,
    max_new_tokens: int,
    device: str,
    kv_dtype: str | None,
    kv_quant: str | None,
) -> dict[str, Any]:
    """以子行程執行單一變體，避免 C 擴充在多次載卸模型時崩潰。"""
    import subprocess

    command = [
        sys.executable, "-m", "native_transformer.benchmark",
        "--checkpoint", str(checkpoint_path),
        "--kv-variant-dtype", str(kv_dtype or ""),
        "--kv-variant-quant", str(kv_quant or ""),
        "--kv-eval-text", eval_text,
        "--prompt", prompt,
        "--max-new-tokens", str(int(max_new_tokens)),
        "--device", device,
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        cwd=str(Path(__file__).resolve().parents[1]), timeout=3_600,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"KV_VARIANT_FAILED:{kv_dtype}/{kv_quant}:"
            f"{(completed.stderr or completed.stdout)[-400:]}"
        )
    payload = completed.stdout.strip().splitlines()
    for line in reversed(payload):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError("KV_VARIANT_NO_JSON")


def compare_kv_cache(
    checkpoint_path: str | Path,
    *,
    eval_text: str,
    prompt: str = "星澄是",
    max_new_tokens: int = 24,
    device: str = "cuda",
    variants: tuple[tuple[str, str | None, str | None], ...] = (
        ("fp32", "float32", None),
        ("bf16", "bfloat16", None),
        ("int8", "float32", "int8"),
    ),
    isolate: bool = True,
) -> dict[str, Any]:
    """KV Cache 變體對比：logits／生成／VRAM／解碼延遲／長上下文困惑度。"""
    results: dict[str, Any] = {}
    baseline: dict[str, Any] | None = None
    baseline_logits: torch.Tensor | None = None
    for label, kv_dtype, kv_quant in variants:
        if isolate:
            entry = _kv_variant_subprocess(
                checkpoint_path,
                eval_text=eval_text,
                prompt=prompt,
                max_new_tokens=max_new_tokens,
                device=device,
                kv_dtype=kv_dtype,
                kv_quant=kv_quant,
            )
        else:
            entry = run_kv_variant(
                checkpoint_path,
                eval_text=eval_text,
                prompt=prompt,
                max_new_tokens=max_new_tokens,
                device=device,
                kv_dtype=kv_dtype,
                kv_quant=kv_quant,
            )
        if baseline is None:
            baseline = entry
        entry["matches_baseline"] = entry["generated_tokens"] == baseline["generated_tokens"]
        raw_logits = entry.get("logits") or []
        logits = torch.tensor(raw_logits, dtype=torch.float32)
        if baseline_logits is None:
            baseline_logits = torch.tensor(raw_logits, dtype=torch.float32)
        entry.pop("logits", None)
        entry["logits_max_abs_diff"] = (
            round(float((logits - baseline_logits).abs().max().item()), 6)
            if logits.numel() and logits.numel() == baseline_logits.numel()
            else None
        )
        results[label] = entry

    baseline_ppl = results.get("fp32", {}).get("cached_perplexity")
    for label, entry in results.items():
        ppl = entry.get("cached_perplexity")
        entry["perplexity_delta_pct"] = (
            round((ppl - baseline_ppl) / baseline_ppl * 100.0, 3)
            if baseline_ppl and ppl
            else None
        )
    return {
        "format_version": "star-native-kv-cache-comparison/v1",
        "checkpoint": str(checkpoint_path),
        "checkpoint_sha256": _file_sha256(Path(checkpoint_path)),
        "device": str(resolve_device_for_benchmark(device)),
        "prompt_tokens": results.get("fp32", {}).get("prompt_tokens"),
        "max_new_tokens": int(max_new_tokens),
        "eval_tokens": results.get("fp32", {}).get("eval_tokens"),
        "variants": results,
        "remote_network_used": False,
    }


def resolve_device_for_benchmark(device: str) -> torch.device:
    from .execution.backend import resolve_device

    return resolve_device(device)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="星澄原生推論基準")
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--prompt", default="The model")
    parser.add_argument("--eval-text", default="")
    parser.add_argument("--max-new-tokens", type=int, default=32)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--quantize", type=int, default=None, choices=[4, 8])
    parser.add_argument("--compare-int8", action="store_true")
    parser.add_argument(
        "--compare-kv-cache",
        action="store_true",
        help="FP32/BF16 與 INT8 KV cache 對比（logits/生成/VRAM/延遲/困惑度）",
    )
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--kv-eval-text", default="")
    parser.add_argument("--kv-variant-dtype", default="")
    parser.add_argument("--kv-variant-quant", default="")
    args = parser.parse_args(argv)

    if args.kv_variant_dtype or args.kv_variant_quant:
        entry = run_kv_variant(
            args.checkpoint,
            eval_text=args.kv_eval_text,
            prompt=args.prompt,
            max_new_tokens=args.max_new_tokens,
            device=args.device,
            kv_dtype=args.kv_variant_dtype or None,
            kv_quant=args.kv_variant_quant or None,
        )
        print(json.dumps(entry, ensure_ascii=False))
        return 0

    if args.compare_kv_cache:
        print(
            json.dumps(
                compare_kv_cache(
                    args.checkpoint,
                    eval_text=args.kv_eval_text or args.eval_text or args.prompt,
                    prompt=args.prompt,
                    max_new_tokens=args.max_new_tokens,
                    device=args.device,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.compare_int8:
        print(
            json.dumps(
                compare_perplexity(args.checkpoint, args.eval_text or args.prompt),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    report = run_benchmark(
        args.checkpoint,
        prompt=args.prompt,
        max_new_tokens=args.max_new_tokens,
        repeat=args.repeat,
        seed=args.seed,
        quantize=args.quantize,
        eval_text=args.eval_text,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
