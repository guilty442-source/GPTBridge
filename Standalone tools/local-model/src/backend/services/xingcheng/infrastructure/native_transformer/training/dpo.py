"""星澄偏好訓練（DPO）：以 chosen/rejected 偏好對微調原生權重。

語料來源：``language_preference_pair``（品質閘門拒絕的 rejected 半邊
配上 owner 驗證的 chosen 半邊）。凍結一份 reference 權重計算 KL 錨定
的 DPO loss，產出可續訓 checkpoint——與 pretrain 相同的存讀與審計
慣例。
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import torch
import torch.nn.functional as F

from ..checkpoint import load_checkpoint, save_checkpoint
from ..execution.backend import resolve_device
from ..modules.model import XingChengForCausalLM


@dataclass
class DpoConfig:
    beta: float = 0.1
    lr: float = 5e-5
    weight_decay: float = 0.0
    max_steps: int = 100
    batch_size: int = 4
    grad_clip: float = 1.0
    max_seq_len: int = 256
    log_every: int = 10
    checkpoint_every: int = 50
    seed: int = 42
    device: str | None = None


def _encode_pair(
    tokenizer: Any, prompt: str, completion: str, max_len: int
) -> tuple[list[int], int]:
    """prompt + completion 編碼與 prompt 長度（供 completion 遮罩）。"""
    prompt_ids = tokenizer.encode(prompt, add_bos=True, add_eos=False)
    full_ids = prompt_ids + tokenizer.encode(
        completion, add_bos=False, add_eos=True
    )
    full_ids = full_ids[:max_len]
    prompt_len = min(len(prompt_ids), len(full_ids))
    return full_ids, prompt_len


def _pad_batch(
    rows: list[tuple[list[int], int]], pad_id: int
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """→ (ids, attention_mask, completion_mask)。"""
    width = max(1, max(len(ids) for ids, _ in rows))
    ids = torch.full((len(rows), width), int(pad_id), dtype=torch.long)
    attention = torch.zeros((len(rows), width), dtype=torch.long)
    completion = torch.zeros((len(rows), width), dtype=torch.long)
    for index, (row_ids, prompt_len) in enumerate(rows):
        ids[index, : len(row_ids)] = torch.tensor(row_ids, dtype=torch.long)
        attention[index, : len(row_ids)] = 1
        completion[index, prompt_len : len(row_ids)] = 1
    return ids, attention, completion


@torch.no_grad()
def _sequence_log_probs(
    model: XingChengForCausalLM,
    ids: torch.Tensor,
    attention_mask: torch.Tensor,
    completion_mask: torch.Tensor,
) -> torch.Tensor:
    """每列 completion token 的 log p 總和（僅 completion 區段）。"""
    logits = model(ids, attention_mask=attention_mask)["logits"]
    log_probs = F.log_softmax(logits[:, :-1], dim=-1)
    targets = ids[:, 1:]
    token_lp = log_probs.gather(-1, targets.unsqueeze(-1)).squeeze(-1)
    mask = completion_mask[:, 1:].to(token_lp.dtype)
    return (token_lp * mask).sum(dim=-1)


def dpo_loss(
    policy_chosen: torch.Tensor,
    policy_rejected: torch.Tensor,
    ref_chosen: torch.Tensor,
    ref_rejected: torch.Tensor,
    beta: float,
) -> tuple[torch.Tensor, torch.Tensor]:
    """DPO loss + 隱含 reward margin；回傳 (loss, accuracy)。"""
    margin = (policy_chosen - policy_rejected) - (ref_chosen - ref_rejected)
    logits = float(beta) * margin
    loss = -F.logsigmoid(logits).mean()
    accuracy = (logits > 0).to(torch.float32).mean()
    return loss, accuracy


def dpo_train(
    policy: XingChengForCausalLM,
    tokenizer: Any,
    pairs: Sequence[Mapping[str, Any]],
    config: DpoConfig,
    *,
    output_dir: str | Path,
    resume: str | Path | None = None,
) -> dict[str, Any]:
    """以偏好對執行 DPO；reference 為 checkpoint 載入時的凍結權重副本。"""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    device = resolve_device(config.device)

    reference = copy.deepcopy(policy)
    reference.eval()
    for param in reference.parameters():
        param.requires_grad_(False)

    start_step = 0
    if resume is not None:
        loaded = load_checkpoint(resume, map_location="cpu")
        policy.load_state_dict(loaded["model"].state_dict())
        start_step = int((loaded.get("extra") or {}).get("step") or 0)

    policy.to(device)
    reference.to(device)
    policy.train()
    optimizer = torch.optim.AdamW(
        policy.parameters(), lr=config.lr, weight_decay=config.weight_decay
    )
    if resume is not None:
        optimizer_state = loaded.get("optimizer_state")
        if optimizer_state is not None:
            optimizer.load_state_dict(optimizer_state)
            for group in optimizer.param_groups:
                group["lr"] = config.lr

    normalized_pairs = [
        {
            "prompt": str(pair.get("prompt_text") or pair.get("prompt") or ""),
            "chosen": str(
                pair.get("chosen_text") or pair.get("chosen") or ""
            ),
            "rejected": str(
                pair.get("rejected_text") or pair.get("rejected") or ""
            ),
        }
        for pair in pairs
    ]
    normalized_pairs = [
        pair
        for pair in normalized_pairs
        if pair["prompt"] and pair["chosen"] and pair["rejected"]
    ]
    if not normalized_pairs:
        raise ValueError("DPO_EMPTY_PREFERENCE_PAIRS")

    pad_id = int(getattr(tokenizer, "pad_id", 0) or 0)
    generator = torch.Generator(device="cpu")
    generator.manual_seed(config.seed)
    history: list[float] = []
    accuracies: list[float] = []
    pairs_seen = 0
    started = time.time()
    checkpoint_paths: list[str] = []

    for step in range(start_step, config.max_steps):
        picks = torch.randint(
            0, len(normalized_pairs), (config.batch_size,), generator=generator
        )
        chosen_rows: list[tuple[list[int], int]] = []
        rejected_rows: list[tuple[list[int], int]] = []
        for index in picks.tolist():
            pair = normalized_pairs[index]
            chosen_rows.append(
                _encode_pair(
                    tokenizer, pair["prompt"], pair["chosen"], config.max_seq_len
                )
            )
            rejected_rows.append(
                _encode_pair(
                    tokenizer, pair["prompt"], pair["rejected"], config.max_seq_len
                )
            )
        c_ids, c_attn, c_mask = _pad_batch(chosen_rows, pad_id)
        r_ids, r_attn, r_mask = _pad_batch(rejected_rows, pad_id)
        c_ids, c_attn, c_mask = (
            c_ids.to(device),
            c_attn.to(device),
            c_mask.to(device),
        )
        r_ids, r_attn, r_mask = (
            r_ids.to(device),
            r_attn.to(device),
            r_mask.to(device),
        )

        logits = policy(c_ids, attention_mask=c_attn)["logits"]
        lp = F.log_softmax(logits[:, :-1], dim=-1)
        p_chosen = (
            lp.gather(-1, c_ids[:, 1:].unsqueeze(-1)).squeeze(-1)
            * c_mask[:, 1:].to(lp.dtype)
        ).sum(dim=-1)
        logits_r = policy(r_ids, attention_mask=r_attn)["logits"]
        lp_r = F.log_softmax(logits_r[:, :-1], dim=-1)
        p_rejected = (
            lp_r.gather(-1, r_ids[:, 1:].unsqueeze(-1)).squeeze(-1)
            * r_mask[:, 1:].to(lp_r.dtype)
        ).sum(dim=-1)
        ref_chosen = _sequence_log_probs(reference, c_ids, c_attn, c_mask)
        ref_rejected = _sequence_log_probs(reference, r_ids, r_attn, r_mask)

        loss, accuracy = dpo_loss(
            p_chosen, p_rejected, ref_chosen, ref_rejected, config.beta
        )
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        if config.grad_clip > 0:
            torch.nn.utils.clip_grad_norm_(policy.parameters(), config.grad_clip)
        optimizer.step()
        pairs_seen += config.batch_size
        history.append(float(loss.item()))
        accuracies.append(float(accuracy.item()))

        if config.log_every > 0 and (step + 1) % config.log_every == 0:
            print(
                json.dumps(
                    {
                        "event": "dpo-train",
                        "step": step + 1,
                        "loss": round(history[-1], 4),
                        "accuracy": round(accuracies[-1], 4),
                        "pairs_seen": pairs_seen,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        if config.checkpoint_every > 0 and (step + 1) % config.checkpoint_every == 0:
            info = save_checkpoint(
                target / "latest.pt",
                policy,
                tokenizer=tokenizer,
                optimizer=optimizer,
                metadata={
                    "phase": "dpo",
                    "step": step + 1,
                    "pairs_seen": pairs_seen,
                    "beta": config.beta,
                },
                extra={"step": step + 1, "pairs_seen": pairs_seen},
            )
            checkpoint_paths.append(info["path"])

    final = save_checkpoint(
        target / "final.pt",
        policy,
        tokenizer=tokenizer,
        optimizer=optimizer,
        metadata={
            "phase": "dpo",
            "step": config.max_steps,
            "pairs_seen": pairs_seen,
            "beta": config.beta,
        },
        extra={"step": config.max_steps, "pairs_seen": pairs_seen},
    )
    checkpoint_paths.append(final["path"])
    elapsed = max(1e-6, time.time() - started)
    summary = {
        "phase": "dpo",
        "steps": config.max_steps,
        "start_step": start_step,
        "pairs": len(normalized_pairs),
        "pairs_seen": pairs_seen,
        "beta": config.beta,
        "first_loss": history[0] if history else None,
        "final_loss": history[-1] if history else None,
        "final_accuracy": accuracies[-1] if accuracies else None,
        "elapsed_seconds": round(elapsed, 2),
        "checkpoints": checkpoint_paths,
        "config": asdict(config),
    }
    (target / "dpo_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def load_preference_pairs(
    repository: Any,
    *,
    limit: int = 500,
    min_text_chars: int = 1,
) -> list[dict[str, str]]:
    """從角色 DB 讀取已配對偏好語料，過濾不完整列。

    ``repository`` 為任何提供 ``language_preference_pairs()`` 的
    儲存層（``paired=1`` 列）。回傳列僅保留 ``dpo_train``
    需要的三個欄位，並帶上 ``pair_id`` 供稽核追溯。
    """
    raw = repository.language_preference_pairs(limit=limit)
    pairs: list[dict[str, str]] = []
    for row in raw:
        prompt = str(row.get("prompt_text") or "").strip()
        chosen = str(row.get("chosen_text") or "").strip()
        rejected = str(row.get("rejected_text") or "").strip()
        if (
            len(prompt) < min_text_chars
            or len(chosen) < min_text_chars
            or len(rejected) < min_text_chars
        ):
            continue
        pairs.append(
            {
                "pair_id": str(row.get("pair_id") or ""),
                "prompt_text": prompt,
                "chosen_text": chosen,
                "rejected_text": rejected,
            }
        )
    return pairs


def dpo_train_from_repository(
    policy: XingChengForCausalLM,
    tokenizer: Any,
    repository: Any,
    config: DpoConfig,
    *,
    output_dir: str | Path,
    limit: int = 500,
) -> dict[str, Any]:
    """從治理儲存層載入偏好對並執行 DPO 訓練。"""
    pairs = load_preference_pairs(repository, limit=limit)
    summary = dpo_train(
        policy, tokenizer, pairs, config, output_dir=output_dir
    )
    summary["source"] = "language_preference_pair"
    summary["pairs_available"] = len(pairs)
    return summary


__all__ = [
    "DpoConfig",
    "dpo_loss",
    "dpo_train",
    "dpo_train_from_repository",
    "load_preference_pairs",
]
