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
    args = parser.parse_args(argv)

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
