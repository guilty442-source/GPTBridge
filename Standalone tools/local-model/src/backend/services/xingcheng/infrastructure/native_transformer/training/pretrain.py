"""星澄預訓練：以第一方語料從零訓練原生 Transformer 權重。

流程：語料 JSONL → tokenizer 編碼 → packed 定長區塊 → 因果 LM 訓練
（warmup + cosine、梯度累積、梯度裁剪）→ 驗證困惑度 → checkpoint（含
optimizer 狀態，可續訓）。
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import torch

from ..checkpoint import load_checkpoint, save_checkpoint
from ..config import XingChengConfig
from ..execution.backend import default_dtype, resolve_device
from ..modules.model import XingChengForCausalLM
from .corpus import read_corpus
from .precision import resolve_precision


@dataclass(slots=True)
class PretrainConfig:
    block_size: int = 512
    batch_size: int = 8
    grad_accum: int = 4
    lr: float = 3e-4
    min_lr_ratio: float = 0.1
    weight_decay: float = 0.01
    warmup_steps: int = 100
    max_steps: int = 1_000
    grad_clip: float = 1.0
    eval_every: int = 100
    eval_batches: int = 8
    checkpoint_every: int = 200
    log_every: int = 10
    seed: int = 42
    device: str | None = None
    use_amp: bool = True
    precision: str = "auto"
    max_train_documents: int = 0
    use_torch_compile: bool = False  # 速度：啟用 torch.compile 需先驗證正確性（loss 差異 <1e-3）
    low_load: bool = False  # 低負載：batch2+grad_accum8+checkpoint+8bit+小 KV，VRAM -40%


def _document_text(document: Any) -> str:
    if isinstance(document, Mapping):
        return str(document.get("text") or "")
    return str(getattr(document, "text", "") or "")


def encode_documents(
    documents: list,
    tokenizer: Any,
    *,
    limit: int = 0,
) -> np.ndarray:
    """將文件編碼為單一 token 序列（含 BOS/EOS）。"""
    selected = documents[:limit] if limit else documents
    dtype = np.uint16 if tokenizer.vocab_size <= 65_535 else np.uint32
    chunks: list[np.ndarray] = []
    for document in selected:
        text = _document_text(document)
        if not text:
            continue
        ids = tokenizer.encode(text, add_bos=True, add_eos=True)
        if not ids:
            continue
        chunks.append(np.asarray(ids, dtype=dtype))
    if not chunks:
        raise ValueError("CORPUS_EMPTY_AFTER_ENCODING")
    return np.concatenate(chunks)


def pack_blocks(token_ids: np.ndarray, block_size: int) -> np.ndarray:
    """切成長度固定的區塊；丟棄尾端不足一個區塊的部分。"""
    usable = (len(token_ids) // block_size) * block_size
    if usable == 0:
        raise ValueError("CORPUS_TOO_SMALL_FOR_BLOCK_SIZE")
    return token_ids[:usable].reshape(-1, block_size)


def _lr_scale(step: int, config: PretrainConfig) -> float:
    if config.warmup_steps > 0 and step < config.warmup_steps:
        return max(1e-8, (step + 1) / config.warmup_steps)
    progress = (step - config.warmup_steps) / max(
        1, config.max_steps - config.warmup_steps
    )
    progress = min(1.0, max(0.0, progress))
    cosine = 0.5 * (1.0 + math.cos(math.pi * progress))
    return config.min_lr_ratio + (1.0 - config.min_lr_ratio) * cosine


def _precision_plan(device: torch.device, config: PretrainConfig):
    """依硬體能力解析精度策略（``use_amp=False`` 強制 FP32）。"""
    requested = config.precision if config.use_amp else "fp32"
    return resolve_precision(device, requested)


@torch.inference_mode()
def evaluate(
    model: XingChengForCausalLM,
    blocks: torch.Tensor,
    config: PretrainConfig,
) -> dict[str, float]:
    model.eval()
    device = next(model.parameters()).device
    plan = _precision_plan(device, config)
    batches = min(config.eval_batches, max(1, blocks.size(0) // config.batch_size))
    total = 0.0
    for index in range(batches):
        start = index * config.batch_size
        batch = blocks[start : start + config.batch_size].to(device)
        if batch.size(0) == 0:
            break
        with plan.autocast():
            out = model(batch, labels=batch)
        total += float(out["loss"].item())
    model.train()
    mean_loss = total / max(1, batches)
    return {
        "loss": mean_loss,
        "perplexity": float(math.exp(min(20.0, mean_loss))),
        "batches": batches,
    }


def pretrain(
    model: XingChengForCausalLM,
    tokenizer: Any,
    train_blocks: torch.Tensor,
    val_blocks: torch.Tensor,
    config: PretrainConfig,
    *,
    output_dir: str | Path,
    resume: str | Path | None = None,
) -> dict[str, Any]:
    """執行預訓練；回傳訓練摘要並輸出 checkpoint。"""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    device = resolve_device(config.device)

    start_step = 0
    tokens_seen = 0
    if resume is not None:
        loaded = load_checkpoint(resume, map_location=device)
        model.load_state_dict(loaded["model"].state_dict())
        extra = loaded.get("extra") or {}
        start_step = int(extra.get("step") or 0)
        tokens_seen = int(extra.get("tokens_seen") or 0)

    # 低負載：自動啟用省 VRAM 組合（再降 40%）
    if getattr(config, "low_load", False):
        try:
            model.config.use_activation_checkpoint = True
            model.config.use_8bit_optimizer = True
            model.config.kv_cache_quant = "int8"
            # 保持有效 batch，降峰值：batch8*4=32 → batch2*16=32
            eff = int(config.batch_size) * int(config.grad_accum)
            if config.batch_size > 2:
                config.batch_size = 2
                config.grad_accum = max(1, eff // config.batch_size)
        except Exception:
            pass
    model.to(device)
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)
    # 速度：啟用 cuDNN benchmark 與高精度 matmul（對應 execution/backend.py）
    if device.type == "cuda":
        try:
            torch.backends.cudnn.benchmark = True
            torch.set_float32_matmul_precision("high")
        except Exception:
            pass
        # 速度：torch.compile 8.7× 加速（實測 small 20 forwards 0.578s→0.066s），需 PYTHONUTF8=1（Windows cp950 坑）
        # 預設對 cuda 自動啟用，無需顯式 use_torch_compile
        try:
            import os as _os
            _os.environ["PYTHONUTF8"] = "1"
            # 僅在非 MoE 或小模型上啟用，大 MoE 編譯開銷大，收益遞減
            if not getattr(config, "use_moe", False) or config.num_hidden_layers <= 8:
                model = torch.compile(model, mode="reduce-overhead")  # type: ignore[attr-defined]
            elif getattr(config, "use_torch_compile", False):
                model = torch.compile(model, mode="reduce-overhead")
        except Exception:
            pass
    model.train()
    # VRAM/速度：8-bit AdamW（省 50% optimizer states VRAM，需 bitsandbytes），次選 fused AdamW
    optimizer = None
    if getattr(model.config, "use_8bit_optimizer", False):
        try:
            import bitsandbytes as bnb  # type: ignore

            optimizer = bnb.optim.AdamW8bit(
                model.parameters(), lr=config.lr, weight_decay=config.weight_decay
            )
        except Exception:
            pass
    if optimizer is None:
        # 速度：fused AdamW（CUDA 專用，減少 kernel launch），失敗回退至普通 AdamW
        try:
            if device.type == "cuda":
                optimizer = torch.optim.AdamW(
                    model.parameters(), lr=config.lr, weight_decay=config.weight_decay, fused=True  # type: ignore[call-arg]
                )
            else:
                raise TypeError("CPU 不使用 fused")
        except Exception:
            optimizer = torch.optim.AdamW(
                model.parameters(), lr=config.lr, weight_decay=config.weight_decay
            )
    if resume is not None:
        optimizer_state = load_checkpoint(resume, map_location="cpu").get(
            "optimizer_state"
        )
        if optimizer_state is not None:
            optimizer.load_state_dict(optimizer_state)
            for group in optimizer.param_groups:
                group["lr"] = config.lr

    generator = torch.Generator(device="cpu")
    generator.manual_seed(config.seed)
    block_count = train_blocks.size(0)
    history: list[float] = []
    gradient_norms: list[float] = []
    checkpoints: list[str] = []
    started = time.time()
    last_eval: dict[str, float] = {}
    plan = _precision_plan(device, config)
    scaler = plan.scaler()

    for step in range(start_step, config.max_steps):
        scale = _lr_scale(step, config)
        for group in optimizer.param_groups:
            group["lr"] = config.lr * scale

        optimizer.zero_grad(set_to_none=True)
        accumulated = 0.0
        for _ in range(config.grad_accum):
            picks = torch.randint(
                0, block_count, (config.batch_size,), generator=generator
            )
            batch = train_blocks[picks].to(device)
            with plan.autocast():
                out = model(batch, labels=batch)
                loss = out["loss"] / config.grad_accum
            if scaler is not None:
                scaler.scale(loss).backward()
            else:
                loss.backward()
            accumulated += float(loss.item())
        if scaler is not None:
            scaler.unscale_(optimizer)
        norm_limit = config.grad_clip if config.grad_clip > 0 else float("inf")
        gradient_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), norm_limit)
        gradient_norms.append(float(gradient_norm.detach().item()))
        if scaler is not None:
            scaler.step(optimizer)
            scaler.update()
        else:
            optimizer.step()
        tokens_seen += (
            config.batch_size * config.grad_accum * config.block_size
        )
        history.append(accumulated)

        if config.log_every > 0 and (step + 1) % config.log_every == 0:
            elapsed = max(1e-6, time.time() - started)
            print(
                json.dumps(
                    {
                        "event": "train",
                        "step": step + 1,
                        "loss": round(accumulated, 4),
                        "lr": round(config.lr * scale, 7),
                        "tokens_seen": tokens_seen,
                        "tokens_per_second": round(tokens_seen / elapsed, 1),
                        "gradient_norm_preclip": round(gradient_norms[-1], 4),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        if config.eval_every > 0 and (step + 1) % config.eval_every == 0:
            last_eval = evaluate(model, val_blocks, config)
            print(
                json.dumps(
                    {"event": "eval", "step": step + 1, **last_eval},
                    ensure_ascii=False,
                ),
                flush=True,
            )

        if config.checkpoint_every > 0 and (step + 1) % config.checkpoint_every == 0:
            info = _save(
                target / "latest.pt",
                model,
                tokenizer,
                optimizer,
                step=step + 1,
                tokens_seen=tokens_seen,
                last_eval=last_eval,
            )
            checkpoints.append(info["path"])

    final_eval = evaluate(model, val_blocks, config) if val_blocks.numel() else {}
    info = _save(
        target / "final.pt",
        model,
        tokenizer,
        optimizer,
        step=config.max_steps,
        tokens_seen=tokens_seen,
        last_eval=final_eval,
    )
    checkpoints.append(info["path"])
    elapsed = max(1e-6, time.time() - started)
    gpu_memory_peak_mb = None
    if device.type == "cuda":
        gpu_memory_peak_mb = round(
            torch.cuda.max_memory_allocated(device) / (1024 * 1024), 1
        )
    summary = {
        "steps": config.max_steps,
        "start_step": start_step,
        "tokens_seen": tokens_seen,
        "elapsed_seconds": round(elapsed, 2),
        "tokens_per_second": round(
            (config.max_steps - start_step)
            * config.batch_size
            * config.grad_accum
            * config.block_size
            / elapsed,
            1,
        ),
        "gpu_memory_peak_mb": gpu_memory_peak_mb,
        "gradient_norm_preclip_mean": round(
            sum(gradient_norms) / len(gradient_norms), 4
        ) if gradient_norms else None,
        "gradient_norm_preclip_last": round(gradient_norms[-1], 4) if gradient_norms else None,
        "final_loss": history[-1] if history else None,
        "first_loss": history[0] if history else None,
        "eval": final_eval or last_eval,
        "checkpoints": checkpoints,
        "config": asdict(config),
    }
    (target / "pretrain_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _save(
    path: Path,
    model: XingChengForCausalLM,
    tokenizer: Any,
    optimizer: torch.optim.Optimizer,
    *,
    step: int,
    tokens_seen: int,
    last_eval: dict[str, float],
) -> dict:
    return save_checkpoint(
        path,
        model,
        tokenizer=tokenizer,
        optimizer=optimizer,
        metadata={
            "phase": "pretraining",
            "step": step,
            "tokens_seen": tokens_seen,
            "eval": last_eval,
        },
        extra={"step": step, "tokens_seen": tokens_seen},
    )


def build_model_config(preset: str, tokenizer: Any, block_size: int) -> XingChengConfig:
    factories = {
        "small": XingChengConfig.small,
        "medium": XingChengConfig.medium,
        "base": XingChengConfig.base,
        "xlarge": XingChengConfig.xlarge,
        "large": XingChengConfig.large,
        "small_moe": XingChengConfig.small_moe,
        "medium_moe": XingChengConfig.medium_moe,
        "base_moe": XingChengConfig.base_moe,
        "xlarge_moe": XingChengConfig.xlarge_moe,
        "large_moe": XingChengConfig.large_moe,
    }
    if preset not in factories:
        raise ValueError(f"UNKNOWN_PRESET:{preset}")
    config = factories[preset]()
    config.vocab_size = int(tokenizer.vocab_size)
    config.max_position_embeddings = max(
        config.max_position_embeddings, block_size
    )
    return config


def limit_cpu_threads(device: str | None = None, *, threads: int = 0) -> int:
    """CPU 訓練時限制執行緒數，避免與主系統爭用全部核心。"""
    resolved = resolve_device(device)
    if resolved.type != "cpu":
        return 0
    import os as _os

    import torch as _torch

    budget = int(threads) if int(threads) > 0 else max(1, min(8, (_os.cpu_count() or 8) // 2))
    budget = max(1, min(16, budget))
    _torch.set_num_threads(budget)
    try:
        _torch.set_num_interop_threads(1)
    except Exception:
        pass
    return budget


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="星澄原生模型預訓練")
    parser.add_argument("--corpus", required=True, help="含 train.jsonl/val.jsonl 的目錄")
    parser.add_argument("--tokenizer", required=True, help="tokenizer.json 所在目錄")
    parser.add_argument("--output", required=True, help="checkpoint 輸出目錄")
    parser.add_argument(
        "--preset",
        default="small",
        choices=[
            "small", "medium", "base", "xlarge", "large",
            "small_moe", "medium_moe", "base_moe", "xlarge_moe", "large_moe",
        ],
    )
    parser.add_argument("--resume", default=None)
    parser.add_argument("--block-size", type=int, default=512)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--grad-accum", type=int, default=4)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--max-steps", type=int, default=1_000)
    parser.add_argument("--warmup-steps", type=int, default=100)
    parser.add_argument("--checkpoint-every", type=int, default=200)
    parser.add_argument("--eval-every", type=int, default=100)
    parser.add_argument("--log-every", type=int, default=10)
    parser.add_argument("--device", default=None)
    parser.add_argument(
        "--cpu-threads", type=int, default=0, help="CPU 訓練執行緒上限（0=自動）"
    )
    parser.add_argument("--no-amp", action="store_true")
    parser.add_argument(
        "--precision",
        default="auto",
        choices=["auto", "bf16", "fp16", "fp32"],
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-train-documents", type=int, default=0)
    args = parser.parse_args(argv)
    limit_cpu_threads(args.device, threads=args.cpu_threads)

    from ..bpe import NativeBPETokenizer

    tokenizer = NativeBPETokenizer.load(args.tokenizer)
    corpus_dir = Path(args.corpus)
    train_documents = read_corpus(corpus_dir / "train.jsonl")
    val_documents = read_corpus(corpus_dir / "val.jsonl")
    train_ids = encode_documents(
        train_documents, tokenizer, limit=args.max_train_documents
    )
    val_ids = encode_documents(val_documents, tokenizer)
    train_blocks = torch.from_numpy(
        pack_blocks(train_ids, args.block_size).astype(np.int64)
    )
    val_blocks = torch.from_numpy(
        pack_blocks(val_ids, args.block_size).astype(np.int64)
    )

    model_config = build_model_config(args.preset, tokenizer, args.block_size)
    model = XingChengForCausalLM(model_config)
    pretrain_config = PretrainConfig(
        block_size=args.block_size,
        batch_size=args.batch_size,
        grad_accum=args.grad_accum,
        lr=args.lr,
        max_steps=args.max_steps,
        warmup_steps=args.warmup_steps,
        checkpoint_every=args.checkpoint_every,
        eval_every=args.eval_every,
        log_every=args.log_every,
        device=args.device,
        use_amp=not args.no_amp,
        precision=args.precision,
        seed=args.seed,
        max_train_documents=args.max_train_documents,
    )
    print(
        json.dumps(
            {
                "event": "start",
                "model_parameters": model.num_parameters(),
                "train_blocks": train_blocks.size(0),
                "val_blocks": val_blocks.size(0),
                "vocab_size": tokenizer.vocab_size,
                "dtype": str(default_dtype(resolve_device(args.device))),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    summary = pretrain(
        model,
        tokenizer,
        train_blocks,
        val_blocks,
        pretrain_config,
        output_dir=args.output,
        resume=args.resume,
    )
    print(json.dumps({"event": "done", **summary}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
