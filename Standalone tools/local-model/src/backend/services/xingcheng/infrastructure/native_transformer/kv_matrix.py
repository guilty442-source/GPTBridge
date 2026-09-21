"""Phase 5F — KV Cache 效能矩陣量測（藍圖 §2.3）。

四情境（短／中／長／最大）量測：Full Forward vs Prefill＋Decode 的
Prefill 耗時、Decode 耗時、Tokens per Second、KV Cache RAM 佔用。
CPU 可跑；CUDA 可用時附 VRAM。報告落 `runtime/logs/kv-matrix-*.json`。

    python -m xingcheng.infrastructure.native_transformer.kv_matrix \
        --tool-root "Standalone tools/local-model" [--device cpu] [--save]
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import torch

from .config import XingChengConfig
from .modules.model import XingChengForCausalLM
from .inference.generate import Generator
from .inference.kv_cache import KVCache

SCENARIOS = [
    ("short", 8, 16),
    ("medium", 32, 32),
    ("long", 48, 16),
    ("max", 59, 5),  # 59+5 <= max_position 64
]


def _cfg() -> XingChengConfig:
    cfg = XingChengConfig.small()
    cfg.vocab_size = 264
    cfg.max_position_embeddings = 64
    cfg.pad_token_id = 0
    return cfg


def _measure(fn, repeat: int = 3) -> float:
    """回傳 best-of-N 毫秒。"""
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        best = min(best, (time.perf_counter() - t0) * 1000)
    return best


def run_matrix(device: str = "cpu") -> dict:
    cfg = _cfg()
    torch.manual_seed(11)
    model = XingChengForCausalLM(cfg).to(device).eval()
    rows = []
    for name, prefix_len, gen_len in SCENARIOS:
        ids = torch.randint(1, cfg.vocab_size, (1, prefix_len), device=device)
        gen = Generator(model, device=torch.device(device))

        full_ms = _measure(lambda: gen.generate(
            ids, max_new_tokens=gen_len, use_cache=False))
        cached_ms = _measure(lambda: gen.generate(
            ids, max_new_tokens=gen_len, use_cache=True))

        # 分離 prefill / decode：prefill = 第一個 token 前段
        def _prefill():
            with torch.no_grad():
                model(ids, use_cache=True)

        prefill_ms = _measure(_prefill)
        cache = KVCache(cfg, 1, prefix_len + gen_len,
                        torch.device(device), torch.float32)
        cache_bytes = sum(
            k.numel() * k.element_size() + v.numel() * v.element_size()
            for k, v in cache.layers
        )
        decode_ms = max(cached_ms - prefill_ms, 0.0)
        rows.append({
            "scenario": name,
            "prefix_len": prefix_len,
            "gen_len": gen_len,
            "full_forward_ms": round(full_ms, 3),
            "prefill_decode_ms": round(cached_ms, 3),
            "prefill_ms": round(prefill_ms, 3),
            "decode_ms": round(decode_ms, 3),
            "speedup_x": round(full_ms / max(cached_ms, 1e-6), 2),
            "tokens_per_second_cached": round(gen_len / max(cached_ms, 1e-6) * 1000, 1),
            "tokens_per_second_full": round(gen_len / max(full_ms, 1e-6) * 1000, 1),
            "kv_cache_ram_bytes": cache_bytes,
        })
        if device == "cuda":
            rows[-1]["vram_peak_mb"] = round(
                torch.cuda.max_memory_allocated() / 1e6, 1)
            torch.cuda.reset_peak_memory_stats()
    return {
        "report": "star-kv-matrix/v1",
        "device": device,
        "config": {
            "hidden_size": cfg.hidden_size,
            "num_hidden_layers": cfg.num_hidden_layers,
            "num_key_value_heads": cfg.num_key_value_heads,
            "max_position_embeddings": cfg.max_position_embeddings,
        },
        "scenarios": rows,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool-root", default=".")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()
    report = run_matrix(args.device)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.save:
        out_dir = Path(args.tool_root) / "xingcheng" / "runtime" / "logs"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        (out_dir / f"kv-matrix-{ts}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"saved: kv-matrix-{ts}.json")


if __name__ == "__main__":
    main()
