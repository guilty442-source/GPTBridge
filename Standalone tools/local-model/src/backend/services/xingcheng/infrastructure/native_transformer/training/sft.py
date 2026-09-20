"""星澄監督微調（SFT）訓練器：把已核准的 SFT 快照訓練成對話權重。

資料格式與服務層 ``sft_dataset.py`` 的 ``star-transformer-sft/v1`` 快照一致
（``prompt`` + ``completion``，以空行分隔）；本模組只負責訓練——prompt 與
padding 的損失會被遮罩，只學習 completion。權重存讀沿用既有的
``star-transformer-checkpoint/v1`` 與 optimizer 續訓慣例。
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

import torch
from torch.utils.data import DataLoader, Dataset

from ..bpe import NativeBPETokenizer
from ..checkpoint import load_checkpoint, save_checkpoint
from ..config import XingChengConfig
from ..execution.backend import resolve_device
from ..modules.model import XingChengForCausalLM
from .precision import resolve_precision

SFT_TEXT_SEPARATOR = "\n\n"


def sft_text(prompt: str, completion: str) -> str:
    """與服務層 ``sft_dataset.sft_text`` 相同的固定模板。"""
    return f"{str(prompt).strip()}{SFT_TEXT_SEPARATOR}{str(completion).strip()}"


@dataclass
class SFTConfig:
    max_length: int = 512
    batch_size: int = 8
    grad_accum: int = 2
    lr: float = 1e-4
    min_lr_ratio: float = 0.1
    weight_decay: float = 0.01
    warmup_steps: int = 20
    max_steps: int = 500
    grad_clip: float = 1.0
    eval_every: int = 50
    checkpoint_every: int = 100
    log_every: int = 10
    seed: int = 42
    device: str | None = None
    use_amp: bool = True
    precision: str = "auto"


def _prompt_prefix(tokenizer, prompt: str) -> list[int]:
    """只遮罩 prompt 本身：讓模型自己學會生成分隔符與回應。

    推論端因此可以直接餵使用者原文（不需要知道 SFT 模板）。
    """
    return tokenizer.encode(
        str(prompt).strip(),
        add_bos=True,
        add_eos=False,
    )


def encode_sft_example(
    tokenizer,
    prompt: str,
    completion: str,
    *,
    max_length: int,
    pad_id: int,
) -> tuple[list[int], list[int]]:
    """回傳 (input_ids, labels)；prompt 與 padding 以 pad_id 遮罩。"""
    full = tokenizer.encode(
        sft_text(prompt, completion), add_bos=True, add_eos=True, max_length=max_length
    )
    prefix = _prompt_prefix(tokenizer, prompt)
    prefix_len = 0
    while (
        prefix_len < len(prefix)
        and prefix_len < len(full)
        and prefix[prefix_len] == full[prefix_len]
    ):
        prefix_len += 1
    labels = list(full)
    for index in range(prefix_len):
        labels[index] = pad_id
    labels[0] = pad_id
    return full, labels


class SFTDataset(Dataset):
    """已遮罩提示的對話資料集。"""

    def __init__(
        self,
        records: Sequence[dict],
        tokenizer,
        *,
        max_length: int = 512,
    ) -> None:
        self.samples: list[tuple[list[int], list[int]]] = []
        pad_id = int(getattr(tokenizer, "pad_id", 0))
        for record in records:
            if record.get("messages"):
                # star-chat-format/v1 多輪對話：只訓練 assistant 輪次
                from ..chat_format import encode_conversation

                try:
                    input_ids, labels = encode_conversation(
                        tokenizer,
                        record["messages"],
                        max_length=max_length,
                        pad_id=pad_id,
                    )
                except ValueError:
                    continue
            else:
                prompt, completion = _record_texts(record)
                if not prompt or not completion:
                    continue
                input_ids, labels = encode_sft_example(
                    tokenizer, prompt, completion, max_length=max_length, pad_id=pad_id
                )
            if len(input_ids) < 4:
                continue
            if not any(label != pad_id for label in labels):
                continue
            self.samples.append((input_ids, labels))

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> tuple[list[int], list[int]]:
        return self.samples[index]


def _record_texts(record: dict) -> tuple[str, str]:
    prompt = str(record.get("prompt") or record.get("input_text") or "").strip()
    completion = str(
        record.get("completion") or record.get("response") or record.get("target_text") or ""
    ).strip()
    return prompt, completion


def collate_sft(
    batch: Sequence[tuple[list[int], list[int]]],
    pad_id: int,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    max_len = max(len(input_ids) for input_ids, _ in batch)
    input_ids = torch.full((len(batch), max_len), pad_id, dtype=torch.long)
    labels = torch.full((len(batch), max_len), pad_id, dtype=torch.long)
    attention = torch.zeros((len(batch), max_len), dtype=torch.long)
    for row, (ids, label_ids) in enumerate(batch):
        input_ids[row, : len(ids)] = torch.tensor(ids, dtype=torch.long)
        labels[row, : len(label_ids)] = torch.tensor(label_ids, dtype=torch.long)
        attention[row, : len(ids)] = 1
    return input_ids, labels, attention


def make_sft_loader(
    dataset: SFTDataset,
    *,
    batch_size: int,
    pad_id: int,
    shuffle: bool,
) -> DataLoader:
    return DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        collate_fn=lambda batch: collate_sft(batch, pad_id),
    )


def read_sft_jsonl(path: str | Path) -> list[dict]:
    """讀取 ``star-transformer-sft/v1`` 快照（接受 response/completion 欄位）。"""
    records: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _precision_plan(device: torch.device, config: SFTConfig):
    requested = config.precision if config.use_amp else "fp32"
    return resolve_precision(device, requested)


@torch.inference_mode()
def evaluate_sft(
    model: XingChengForCausalLM,
    loader: DataLoader,
    config: SFTConfig,
    *,
    batches: int = 4,
) -> dict[str, float]:
    model.eval()
    device = next(model.parameters()).device
    plan = _precision_plan(device, config)
    total = 0.0
    count = 0
    for input_ids, labels, attention in loader:
        if count >= batches:
            break
        input_ids = input_ids.to(device)
        labels = labels.to(device)
        attention = attention.to(device)
        with plan.autocast():
            out = model(input_ids, labels=labels, attention_mask=attention)
        total += float(out["loss"].item())
        count += 1
    model.train()
    if count == 0:
        return {"loss": float("nan"), "perplexity": float("nan"), "batches": 0}
    mean = total / count
    return {"loss": mean, "perplexity": float(math.exp(min(20.0, mean))), "batches": count}


def _lr_scale(step: int, config: SFTConfig) -> float:
    if config.warmup_steps > 0 and step < config.warmup_steps:
        return max(1e-8, (step + 1) / config.warmup_steps)
    progress = (step - config.warmup_steps) / max(1, config.max_steps - config.warmup_steps)
    progress = min(1.0, max(0.0, progress))
    return config.min_lr_ratio + (1.0 - config.min_lr_ratio) * 0.5 * (
        1.0 + math.cos(math.pi * progress)
    )


def sft_train(
    model: XingChengForCausalLM,
    tokenizer,
    train_records: Sequence[dict],
    val_records: Sequence[dict],
    config: SFTConfig,
    *,
    output_dir: str | Path,
    resume: str | Path | None = None,
) -> dict[str, Any]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    device = resolve_device(config.device)
    pad_id = int(getattr(tokenizer, "pad_id", 0))

    start_step = 0
    if resume is not None:
        loaded = load_checkpoint(resume, map_location=device)
        model.load_state_dict(loaded["model"].state_dict())
        start_step = int((loaded.get("extra") or {}).get("step") or 0)

    model.to(device)
    model.train()
    train_dataset = SFTDataset(train_records, tokenizer, max_length=config.max_length)
    val_dataset = SFTDataset(val_records, tokenizer, max_length=config.max_length)
    if len(train_dataset) == 0:
        raise ValueError("SFT_DATASET_EMPTY")
    train_loader = make_sft_loader(
        train_dataset,
        batch_size=config.batch_size,
        pad_id=pad_id,
        shuffle=True,
    )
    val_loader = make_sft_loader(
        val_dataset,
        batch_size=config.batch_size,
        pad_id=pad_id,
        shuffle=False,
    )

    optimizer = torch.optim.AdamW(
        model.parameters(), lr=config.lr, weight_decay=config.weight_decay
    )
    if resume is not None:
        state = load_checkpoint(resume, map_location="cpu").get("optimizer_state")
        if state is not None:
            optimizer.load_state_dict(state)

    plan = _precision_plan(device, config)
    scaler = plan.scaler()
    history: list[float] = []
    checkpoints: list[str] = []
    started = time.time()
    step = start_step
    last_eval: dict[str, float] = {}
    train_iterator = iter(train_loader)
    while step < config.max_steps:
        scale = _lr_scale(step, config)
        for group in optimizer.param_groups:
            group["lr"] = config.lr * scale
        optimizer.zero_grad(set_to_none=True)
        accumulated_tensor = torch.zeros((), device=device)
        for _ in range(max(1, config.grad_accum)):
            try:
                input_ids, labels, attention = next(train_iterator)
            except StopIteration:
                train_iterator = iter(train_loader)
                input_ids, labels, attention = next(train_iterator)
            batch_ids = input_ids.to(device)
            batch_labels = labels.to(device)
            batch_attention = attention.to(device)
            with plan.autocast():
                out = model(batch_ids, labels=batch_labels, attention_mask=batch_attention)
                loss = out["loss"] / max(1, config.grad_accum)
            if scaler is not None:
                scaler.scale(loss).backward()
            else:
                loss.backward()
            accumulated_tensor = accumulated_tensor + loss.detach()
        accumulated = float(accumulated_tensor.item())
        if config.grad_clip > 0:
            if scaler is not None:
                scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), config.grad_clip)
        if scaler is not None:
            scaler.step(optimizer)
            scaler.update()
        else:
            optimizer.step()
        step += 1
        history.append(accumulated)

        if config.log_every > 0 and step % config.log_every == 0:
            print(
                json.dumps(
                    {
                        "event": "sft",
                        "step": step,
                        "loss": round(accumulated, 4),
                        "lr": round(config.lr * scale, 7),
                        "elapsed_seconds": round(time.time() - started, 1),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        if config.eval_every > 0 and step % config.eval_every == 0:
            last_eval = evaluate_sft(model, val_loader, config)
            print(
                json.dumps({"event": "eval", "step": step, **last_eval}, ensure_ascii=False),
                flush=True,
            )
        if config.checkpoint_every > 0 and step % config.checkpoint_every == 0:
            info = save_checkpoint(
                target / "latest.pt",
                model,
                tokenizer=tokenizer,
                optimizer=optimizer,
                metadata={
                    "phase": "supervised-fine-tuning",
                    "step": step,
                    "eval": last_eval,
                },
                extra={"step": step},
            )
            checkpoints.append(info["path"])

    final_eval = evaluate_sft(model, val_loader, config) if len(val_loader) else {}
    info = save_checkpoint(
        target / "final.pt",
        model,
        tokenizer=tokenizer,
        optimizer=optimizer,
        metadata={"phase": "supervised-fine-tuning", "step": step, "eval": final_eval},
        extra={"step": step},
    )
    checkpoints.append(info["path"])
    summary = {
        "steps": step,
        "start_step": start_step,
        "train_examples": len(train_loader.dataset),
        "val_examples": len(val_loader.dataset),
        "first_loss": history[0] if history else None,
        "final_loss": history[-1] if history else None,
        "eval": final_eval or last_eval,
        "checkpoints": checkpoints,
        "elapsed_seconds": round(time.time() - started, 2),
        "config": asdict(config),
    }
    (target / "sft_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="星澄原生模型監督微調")
    parser.add_argument("--tokenizer", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--dataset-jsonl", required=True, help="star-transformer-sft/v1 快照")
    parser.add_argument("--init-checkpoint", default=None, help="由預訓練權重開始")
    parser.add_argument("--resume", default=None)
    parser.add_argument("--preset", default="medium", choices=["small", "medium", "base", "large"])
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--grad-accum", type=int, default=2)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--max-steps", type=int, default=500)
    parser.add_argument("--warmup-steps", type=int, default=20)
    parser.add_argument("--checkpoint-every", type=int, default=100)
    parser.add_argument("--eval-every", type=int, default=50)
    parser.add_argument("--log-every", type=int, default=10)
    parser.add_argument("--val-ratio", type=float, default=0.15)
    parser.add_argument("--device", default=None)
    parser.add_argument(
        "--precision",
        default="auto",
        choices=["auto", "bf16", "fp16", "fp32"],
    )
    parser.add_argument(
        "--cpu-threads", type=int, default=0, help="CPU 訓練執行緒上限（0=自動）"
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    from .pretrain import limit_cpu_threads

    limit_cpu_threads(args.device, threads=args.cpu_threads)

    tokenizer = NativeBPETokenizer.load(args.tokenizer)
    records = read_sft_jsonl(args.dataset_jsonl)
    if not records:
        raise SystemExit("SFT_DATASET_EMPTY")

    split = max(1, int(len(records) * (1.0 - args.val_ratio)))
    train_records = records[:split]
    val_records = records[split:] or records[:1]

    if args.init_checkpoint or args.resume:
        loaded = load_checkpoint(
            args.init_checkpoint or args.resume, map_location=args.device or "cpu"
        )
        model = loaded["model"]
        model_config = loaded["config"]
    else:
        factories = {
            "small": XingChengConfig.small,
            "medium": XingChengConfig.medium,
            "base": XingChengConfig.base,
            "large": XingChengConfig.large,
        }
        model_config = factories[args.preset]()
        model_config.vocab_size = tokenizer.vocab_size
        model_config.max_position_embeddings = max(
            model_config.max_position_embeddings, args.max_length
        )
        model = XingChengForCausalLM(model_config)
    model_config.max_position_embeddings = max(
        model_config.max_position_embeddings, args.max_length
    )

    summary = sft_train(
        model,
        tokenizer,
        train_records,
        val_records,
        SFTConfig(
            max_length=args.max_length,
            batch_size=args.batch_size,
            grad_accum=args.grad_accum,
            lr=args.lr,
            max_steps=args.max_steps,
            warmup_steps=args.warmup_steps,
            checkpoint_every=args.checkpoint_every,
            eval_every=args.eval_every,
            log_every=args.log_every,
            device=args.device,
            precision=args.precision,
            seed=args.seed,
        ),
        output_dir=args.output,
        resume=args.resume,
    )
    print(json.dumps({"event": "done", **summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "SFT_TEXT_SEPARATOR",
    "SFTConfig",
    "SFTDataset",
    "collate_sft",
    "encode_sft_example",
    "evaluate_sft",
    "make_sft_loader",
    "read_sft_jsonl",
    "sft_text",
    "sft_train",
]
