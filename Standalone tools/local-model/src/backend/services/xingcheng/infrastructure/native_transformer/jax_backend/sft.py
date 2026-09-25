"""星澄 JAX SFT 訓練器 — A612 唯一訓練框架實作。

資料契約沿用服務層 ``star-transformer-sft/v1``（``prompt`` +
``completion``）；prompt 與 padding 的損失遮罩、lr 排程（warmup +
cosine）、grad clip、時間預算與 ``training/sft.py`` 的 torch lineage
對齊。輸出 ``star-jax-checkpoint/v1``；產物可由 C++ 推論引擎經
``cpp_export`` 對映層轉出，或由 JAX 端直接載入評估。

不依賴 torch；j記憶體由 XLA 管理（A612 automatic memory management）。
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

import jax
import jax.numpy as jnp
import numpy as np

from .checkpoint import count_params, save_jax_checkpoint
from .model import forward_logits, init_params

SFT_TEXT_SEPARATOR = "\n\n"


def sft_text(prompt: str, completion: str) -> str:
    """與 ``training.sft.sft_text`` / 服務層 ``sft_dataset`` 同模板。"""
    return f"{str(prompt).strip()}{SFT_TEXT_SEPARATOR}{str(completion).strip()}"


@dataclass
class JaxSFTConfig:
    """SFT 超參數（與 torch lineage ``SFTConfig`` 同名欄位對齊）。"""

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
    max_train_seconds: float = 0


def _encode_example(
    tokenizer: Any,
    prompt: str,
    completion: str,
    *,
    max_length: int,
    pad_id: int,
) -> tuple[list[int], list[int]]:
    """(input_ids, labels)；prompt 與首 token 以 pad_id 遮罩。"""
    full = tokenizer.encode(
        sft_text(prompt, completion),
        add_bos=True,
        add_eos=True,
        max_length=max_length,
    )
    prefix = tokenizer.encode(
        sft_text(prompt, ""), add_bos=True, add_eos=False, max_length=max_length
    )
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
    if labels:
        labels[0] = pad_id
    return full, labels


def _build_samples(
    records: Sequence[dict[str, Any]],
    tokenizer: Any,
    *,
    max_length: int,
    pad_id: int,
) -> list[tuple[list[int], list[int]]]:
    samples: list[tuple[list[int], list[int]]] = []
    for record in records:
        prompt = str(record.get("prompt") or "")
        completion = str(record.get("completion") or "")
        if not completion.strip():
            continue
        input_ids, labels = _encode_example(
            tokenizer, prompt, completion, max_length=max_length, pad_id=pad_id
        )
        if len(input_ids) < 4 or not any(label != pad_id for label in labels):
            continue
        samples.append((input_ids, labels))
    return samples


def _collate(
    batch: Sequence[tuple[list[int], list[int]]], pad_id: int
) -> tuple[jnp.ndarray, jnp.ndarray]:
    width = max(len(ids) for ids, _ in batch)
    inputs = np.full((len(batch), width), pad_id, dtype=np.int32)
    labels = np.full((len(batch), width), pad_id, dtype=np.int32)
    for row, (ids, lab) in enumerate(batch):
        inputs[row, : len(ids)] = ids
        labels[row, : len(lab)] = lab
    return jnp.asarray(inputs), jnp.asarray(labels)


def _lr_scale(step: int, config: JaxSFTConfig) -> float:
    if step < config.warmup_steps:
        return (step + 1) / max(1, config.warmup_steps)
    progress = (step - config.warmup_steps) / max(
        1, config.max_steps - config.warmup_steps
    )
    cosine = 0.5 * (1.0 + math.cos(math.pi * min(1.0, progress)))
    return max(float(config.min_lr_ratio), cosine)


def _masked_loss(
    params: dict[str, Any],
    config: Any,
    input_ids: jnp.ndarray,
    labels: jnp.ndarray,
    pad_id: int,
) -> jnp.ndarray:
    logits = forward_logits(params, config, input_ids)
    # next-token CE：logits[t] 預測 labels[t+1]
    shift_logits = logits[:, :-1, :]
    shift_labels = labels[:, 1:]
    mask = (shift_labels != pad_id).astype(jnp.float32)
    logp = jax.nn.log_softmax(shift_logits, axis=-1)
    token_loss = -jnp.take_along_axis(
        logp, shift_labels[..., None], axis=-1
    ).squeeze(-1)
    return (token_loss * mask).sum() / jnp.maximum(mask.sum(), 1.0)


def _adamw_init(params: dict[str, Any]) -> dict[str, Any]:
    zeros = jax.tree.map(lambda x: jnp.zeros_like(x), params)
    return {"m": zeros, "v": zeros, "t": jnp.asarray(0, dtype=jnp.int32)}


def _adamw_step(
    params: dict[str, Any],
    grads: dict[str, Any],
    state: dict[str, Any],
    *,
    lr: float,
    weight_decay: float,
    beta1: float = 0.9,
    beta2: float = 0.999,
    eps: float = 1e-8,
) -> tuple[dict[str, Any], dict[str, Any]]:
    t = state["t"] + 1
    m = jax.tree.map(lambda m_, g: beta1 * m_ + (1 - beta1) * g, state["m"], grads)
    v = jax.tree.map(lambda v_, g: beta2 * v_ + (1 - beta2) * g * g, state["v"], grads)
    bc1 = 1.0 - beta1 ** int(t)
    bc2 = 1.0 - beta2 ** int(t)

    def update(p: jnp.ndarray, m_: jnp.ndarray, v_: jnp.ndarray) -> jnp.ndarray:
        mhat = m_ / bc1
        vhat = v_ / bc2
        return p - lr * (mhat / (jnp.sqrt(vhat) + eps) + weight_decay * p)

    new_params = jax.tree.map(update, params, m, v)
    return new_params, {"m": m, "v": v, "t": t}


def jax_sft_train(
    config: Any,
    tokenizer: Any,
    train_records: Sequence[dict[str, Any]],
    val_records: Sequence[dict[str, Any]],
    train_config: JaxSFTConfig,
    *,
    output_dir: str | Path,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """JAX SFT 訓練迴圈。

    ``config`` 為 XingChengConfig（dense 主線）；``params`` 省略時以
    ``train_config.seed`` 初始化。回傳 summary dict（與 torch lineage
    ``sft_train`` 相容欄位：history/eval/final_checkpoint/…）。
    """
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    pad_id = int(getattr(tokenizer, "pad_id", getattr(tokenizer, "pad_token_id", 0)))

    samples = _build_samples(
        train_records, tokenizer, max_length=train_config.max_length, pad_id=pad_id
    )
    if not samples:
        raise ValueError("SFT_DATASET_EMPTY")
    val_samples = _build_samples(
        val_records, tokenizer, max_length=train_config.max_length, pad_id=pad_id
    )

    if params is None:
        params = init_params(config, seed=train_config.seed)
    opt_state = _adamw_init(params)

    loss_and_grad = jax.jit(
        jax.value_and_grad(
            lambda p, ids, lab: _masked_loss(p, config, ids, lab, pad_id)
        )
    )

    rng = np.random.default_rng(train_config.seed)
    order = np.arange(len(samples))
    history: list[float] = []
    checkpoints: list[str] = []
    last_eval: dict[str, float] = {}
    started = time.time()
    stopped_reason: str | None = None
    step = 0

    while step < train_config.max_steps:
        lr = train_config.lr * _lr_scale(step, train_config)
        accumulated = 0.0
        for _ in range(max(1, train_config.grad_accum)):
            rng.shuffle(order)
            batch = [samples[i] for i in order[: train_config.batch_size]]
            input_ids, labels = _collate(batch, pad_id)
            loss, grads = loss_and_grad(params, input_ids, labels)
            accumulated += float(loss)
            if train_config.grad_clip > 0:
                norm = float(
                    jnp.sqrt(
                        sum(
                            jnp.sum(jnp.square(g))
                            for g in jax.tree.leaves(grads)
                            if g is not None
                        )
                    )
                )
                if norm > train_config.grad_clip:
                    scale = train_config.grad_clip / (norm + 1e-6)
                    grads = jax.tree.map(
                        lambda g: g * scale if g is not None else g, grads
                    )
            params, opt_state = _adamw_step(
                params, grads, opt_state,
                lr=lr, weight_decay=train_config.weight_decay,
            )
        step += 1
        history.append(accumulated / max(1, train_config.grad_accum))

        if step % train_config.eval_every == 0 and val_samples:
            val_loss = 0.0
            seen = 0
            for i in range(0, len(val_samples), train_config.batch_size):
                ids, lab = _collate(
                    val_samples[i : i + train_config.batch_size], pad_id
                )
                val_loss += float(
                    _masked_loss(params, config, ids, lab, pad_id)
                )
                seen += 1
            last_eval = {"val_loss": val_loss / max(1, seen)}

        if step % train_config.checkpoint_every == 0:
            ckpt = target / f"step-{step}"
            save_jax_checkpoint(
                params,
                _config_payload(config),
                ckpt,
                step=step,
            )
            checkpoints.append(str(ckpt))

        if train_config.max_train_seconds > 0 and (
            time.time() - started > train_config.max_train_seconds
        ):
            stopped_reason = "max_train_seconds"
            break

    final = target / "final"
    save_jax_checkpoint(params, _config_payload(config), final, step=step)
    checkpoints.append(str(final))
    return {
        "framework": "jax",
        "framework_version": str(jax.__version__),
        "steps": step,
        "history": history,
        "final_loss": history[-1] if history else None,
        "eval": last_eval,
        "checkpoints": checkpoints,
        "final_checkpoint": str(final),
        "params": count_params(params),
        "stopped_reason": stopped_reason,
        "train_seconds": round(time.time() - started, 3),
    }


def _config_payload(config: Any) -> dict[str, Any]:
    """XingChengConfig → JSON 可序列化 manifest payload。"""
    if hasattr(config, "__dataclass_fields__"):
        raw = asdict(config)
    elif isinstance(config, dict):
        raw = dict(config)
    else:
        raw = {k: getattr(config, k) for k in vars(config)}
    return {
        key: (list(value) if isinstance(value, tuple) else value)
        for key, value in raw.items()
        if isinstance(value, (int, float, str, bool, list, dict, type(None)))
    }
