"""JAX 權重檔 — ``star-jax-checkpoint/v1``。

配置走 ``manifest.json``，參數樹走 ``params.npz``（block_i/<name> 扁平
key）。PyTorch ``.pt`` 權重屬 migration-only lineage，本格式不讀寫。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jax
import jax.numpy as jnp
import numpy as np

CHECKPOINT_FORMAT = "star-jax-checkpoint/v1"
PARAMS_NAME = "params.npz"
MANIFEST_NAME = "manifest.json"


def _flatten(params: dict[str, Any]) -> dict[str, jnp.ndarray]:
    flat: dict[str, jnp.ndarray] = {"tok_emb": params["tok_emb"]}
    for index, block in enumerate(params["blocks"]):
        for name, value in block.items():
            flat[f"block_{index}/{name}"] = value
    flat["final_norm"] = params["final_norm"]
    if params.get("lm_head") is not None:
        flat["lm_head"] = params["lm_head"]
    return flat


def _unflatten(flat: dict[str, jnp.ndarray], layers: int) -> dict[str, Any]:
    params: dict[str, Any] = {
        "tok_emb": flat["tok_emb"],
        "blocks": [],
        "final_norm": flat["final_norm"],
        "lm_head": flat.get("lm_head"),
    }
    for index in range(layers):
        prefix = f"block_{index}/"
        params["blocks"].append(
            {
                name[len(prefix):]: value
                for name, value in flat.items()
                if name.startswith(prefix)
            }
        )
    return params


def save_jax_checkpoint(
    params: dict[str, Any],
    config_payload: dict[str, Any],
    output_dir: str | Path,
    *,
    step: int = 0,
) -> Path:
    """寫出 ``manifest.json`` + ``params.npz`` 到 ``output_dir``。"""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": CHECKPOINT_FORMAT,
        "framework": "jax",
        "step": int(step),
        "config": config_payload,
    }
    (target / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    flat = {k: np.asarray(v) for k, v in _flatten(params).items()}
    np.savez(target / PARAMS_NAME, **flat)
    return target


def load_jax_checkpoint(checkpoint_dir: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """回傳 ``(params pytree, manifest)``；格式不符 fail-closed。"""
    target = Path(checkpoint_dir)
    manifest = json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))
    if manifest.get("format") != CHECKPOINT_FORMAT:
        raise ValueError(
            f"JAX_CHECKPOINT_FORMAT: expected {CHECKPOINT_FORMAT}, "
            f"got {manifest.get('format')!r}"
        )
    with np.load(target / PARAMS_NAME) as data:
        flat = {name: jnp.asarray(data[name]) for name in data.files}
    layers = int(manifest["config"]["num_hidden_layers"])
    params = _unflatten(flat, layers)
    if params["lm_head"] is None and "lm_head" in flat:
        params["lm_head"] = flat["lm_head"]
    return params, manifest


def count_params(params: dict[str, Any]) -> int:
    """參數樹總量（tied lm_head 不重複計算）。"""
    return int(sum(int(np.prod(x.shape)) for x in jax.tree.leaves(params) if x is not None))
