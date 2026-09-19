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


@dataclass
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


@torch.no_grad()
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

    model.to(device)
    model.train()
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
        if config.grad_clip > 0:
            if scaler is not None:
                scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), config.grad_clip)
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
        "large": XingChengConfig.large,
    }
    if preset not in factories:
        raise ValueError(f"UNKNOWN_PRESET:{preset}")
    config = factories[preset]()
    config.vocab_size = int(tokenizer.vocab_size)
    config.max_position_embeddings = max(
        config.max_position_embeddings, block_size
    )
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="星澄原生模型預訓練")
    parser.add_argument("--corpus", required=True, help="含 train.jsonl/val.jsonl 的目錄")
    parser.add_argument("--tokenizer", required=True, help="tokenizer.json 所在目錄")
    parser.add_argument("--output", required=True, help="checkpoint 輸出目錄")
    parser.add_argument(
        "--preset", default="small", choices=["small", "medium", "base", "large"]
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
    parser.add_argument("--no-amp", action="store_true")
    parser.add_argument(
        "--precision",
        default="auto",
        choices=["auto", "bf16", "fp16", "fp32"],
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-train-documents", type=int, default=0)
    args = parser.parse_args(argv)

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
