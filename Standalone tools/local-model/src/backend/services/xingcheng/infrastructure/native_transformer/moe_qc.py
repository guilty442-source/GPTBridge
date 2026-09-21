"""G39 — MoE QC 對照量測（藍圖 §2.2 MoE 品質控制補完）。

Dense vs MoE 對照矩陣（實測，非估算）：
- 總參數／活躍參數／實測 CPU FLOPs（profiler with_flops）
- 訓練 step 耗時／推論逐 token 耗時／吞吐
- Router 健康（entropy／load balance／collapse）

    python -m xingcheng.infrastructure.native_transformer.moe_qc \
        --tool-root "Standalone tools/local-model" [--save]
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import torch
from torch.profiler import ProfilerActivity, profile

from .config import XingChengConfig
from .modules.model import XingChengForCausalLM

REPORT_FORMAT = "star-moe-qc/v1"


def _dense_cfg() -> XingChengConfig:
    cfg = XingChengConfig.small()
    cfg.vocab_size = 264
    cfg.max_position_embeddings = 64
    return cfg


def _moe_cfg(experts: int, top_k: int) -> XingChengConfig:
    cfg = _dense_cfg()
    cfg.use_moe = True
    cfg.moe_num_experts = experts
    cfg.moe_top_k = top_k
    return cfg


def _measured_flops(model, ids: torch.Tensor) -> int:
    with profile(activities=[ProfilerActivity.CPU], with_flops=True) as prof:
        with torch.inference_mode():
            model(ids)
    return sum(int(getattr(e, "flops", 0) or 0) for e in prof.key_averages())


def _train_step_ms(model, ids: torch.Tensor, repeat: int = 5) -> float:
    opt = torch.optim.AdamW(model.parameters(), lr=1e-4)
    model.train()
    best = float("inf")
    for _ in range(repeat):
        opt.zero_grad()
        t0 = time.perf_counter()
        out = model(ids, labels=ids)
        out["loss"].backward()
        opt.step()
        best = min(best, (time.perf_counter() - t0) * 1000)
    return best


def _infer_step_ms(model, ids: torch.Tensor, repeat: int = 5) -> float:
    model.eval()
    best = float("inf")
    with torch.inference_mode():
        for _ in range(repeat):
            t0 = time.perf_counter()
            model(ids)
            best = min(best, (time.perf_counter() - t0) * 1000)
    return best


def _router_health(model) -> dict:
    """聚合各 MoE 層的 router 健康指標（最後一次 forward 的觀測值）。"""
    loads, entropies = [], []
    for module in model.modules():
        load = getattr(module, "expert_load", None)
        entropy = getattr(module, "router_entropy", None)
        if callable(load):
            load = load()
        if load is not None:
            loads.append(load)
        if callable(entropy):
            entropy = entropy()
        if entropy is not None:
            entropies.append(float(entropy))
    if not loads:
        return {}
    stacked = torch.stack([l.float() for l in loads])
    mean_load = stacked.mean(dim=0)
    return {
        "expert_load_mean": [round(float(v), 4) for v in mean_load],
        "expert_load_max": round(float(mean_load.max()), 4),
        "router_entropy_mean": round(sum(entropies) / len(entropies), 4)
        if entropies else None,
        "utilized_experts": int((mean_load > 0).sum().item()),
        "is_collapsed": bool(mean_load.max().item() > 0.5),
    }


def run_qc(seq_len: int = 32) -> dict:
    torch.manual_seed(13)
    ids = torch.randint(4, 264, (1, seq_len))
    variants = [
        ("dense", _dense_cfg()),
        ("moe-4e-top2", _moe_cfg(4, 2)),
        ("moe-8e-top2", _moe_cfg(8, 2)),
    ]
    rows = []
    for name, cfg in variants:
        # 每個量測用獨立實例：inference_mode 下快取的 RoPE 表不可進入 autograd
        probe = XingChengForCausalLM(cfg)
        total = probe.num_parameters(only_trainable=False)
        flops = _measured_flops(probe, ids)
        del probe
        model = XingChengForCausalLM(cfg)
        train_ms = _train_step_ms(model, ids)
        infer_ms = _infer_step_ms(model, ids)
        model.eval()
        with torch.inference_mode():
            model(ids)  # warmup：讓 router 健康指標有觀測值
        rows.append({
            "variant": name,
            "total_params": total,
            "measured_cpu_flops": flops,
            "train_step_ms": round(train_ms, 3),
            "infer_step_ms": round(infer_ms, 3),
            "train_tokens_per_second": round(seq_len / max(train_ms, 1e-6) * 1000, 1),
            "infer_tokens_per_second": round(seq_len / max(infer_ms, 1e-6) * 1000, 1),
            "router_health": _router_health(model),
        })
    dense = rows[0]
    for row in rows[1:]:
        row["param_ratio_vs_dense"] = round(row["total_params"] / dense["total_params"], 3)
        row["flops_ratio_vs_dense"] = round(
            row["measured_cpu_flops"] / max(dense["measured_cpu_flops"], 1), 3)
        row["train_speed_ratio_vs_dense"] = round(
            dense["train_step_ms"] / max(row["train_step_ms"], 1e-6), 3)
    return {
        "report": REPORT_FORMAT,
        "device": "cpu",
        "seq_len": seq_len,
        "note": "FLOPs／速度皆為實測值；活躍參數不得作為成本估算依據（藍圖 §2.2）。",
        "variants": rows,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool-root", default=".")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()
    report = run_qc()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.save:
        out_dir = Path(args.tool_root) / "xingcheng" / "runtime" / "logs"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"moe-qc-{ts}.json"
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved: {path.name}")


if __name__ == "__main__":
    main()
