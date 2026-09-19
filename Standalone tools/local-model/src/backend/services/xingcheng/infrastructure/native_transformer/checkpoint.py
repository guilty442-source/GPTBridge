"""Checkpoint：原生模型權重存讀（原子寫入 + 內容雜湊）。

格式 `star-transformer-checkpoint/v1` 內含：
  - 設定（JSON，可讀）
  - 模型 state_dict（tensor）
  - tokenizer 狀態（可選）
  - metadata（僅限 JSON 可序列化內容）
  - state_dict 內容雜湊（載入時驗證，fail-closed）
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

import torch

from .config import XingChengConfig
from .modules.model import XingChengForCausalLM
from .tokenizer import XingChengTokenizer

FORMAT_VERSION = "star-transformer-checkpoint/v1"


def default_checkpoint_dir() -> Path:
    """藍圖預設落點：``Standalone tools/local-model/xingcheng/runtime/models/``。"""
    # .../services/xingcheng/infrastructure/native_transformer/checkpoint.py
    local_model_root = Path(__file__).resolve().parents[6]
    return local_model_root / "xingcheng" / "runtime" / "models"


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _state_digest(state: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    for key in sorted(state):
        value = state[key]
        digest.update(key.encode("utf-8"))
        if isinstance(value, torch.Tensor):
            digest.update(str(value.dtype).encode("ascii"))
            digest.update(str(tuple(value.shape)).encode("ascii"))
            digest.update(value.detach().cpu().contiguous().numpy().tobytes())
        else:
            digest.update(repr(value).encode("utf-8"))
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_checkpoint(
    path: str | Path,
    model: XingChengForCausalLM,
    *,
    tokenizer: XingChengTokenizer | None = None,
    config: XingChengConfig | None = None,
    metadata: Mapping[str, Any] | None = None,
    optimizer: torch.optim.Optimizer | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """將模型權重寫入 checkpoint；原子寫入並輸出 `.sha256` 側檔。

    `optimizer` 與 `extra`（JSON 可序列化的訓練進度）用於續訓。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    cfg = config or model.config
    if tokenizer is not None and tokenizer.vocab_size > cfg.vocab_size:
        raise ValueError("TOKENIZER_VOCAB_EXCEEDS_MODEL_VOCAB")

    state = model.state_dict()
    payload = {
        "format_version": FORMAT_VERSION,
        "created_at": _iso_now(),
        "config_json": json.dumps(
            cfg.to_dict(), ensure_ascii=False, sort_keys=True
        ),
        "model_state": state,
        "tokenizer_state": tokenizer.state_dict() if tokenizer is not None else None,
        "metadata_json": json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True),
        "state_sha256": _state_digest(state),
        "optimizer_state": optimizer.state_dict() if optimizer is not None else None,
        "extra_json": json.dumps(dict(extra or {}), ensure_ascii=False, sort_keys=True),
    }

    tmp = target.with_name(target.name + ".tmp")
    try:
        torch.save(payload, tmp)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()

    file_sha256 = _file_sha256(target)
    target.with_name(target.name + ".sha256").write_text(
        file_sha256 + "\n", encoding="ascii"
    )
    return {
        "path": str(target),
        "format_version": FORMAT_VERSION,
        "bytes": target.stat().st_size,
        "file_sha256": file_sha256,
        "state_sha256": payload["state_sha256"],
        "created_at": payload["created_at"],
    }


def load_checkpoint(
    path: str | Path,
    *,
    map_location: str | torch.device = "cpu",
    verify: bool = True,
    strict: bool = True,
) -> dict[str, Any]:
    """載入 checkpoint 並重建模型 / tokenizer；雜湊不符時拒絕載入。"""
    target = Path(path)
    try:
        payload = torch.load(target, map_location=map_location, weights_only=True)
    except TypeError:
        payload = torch.load(target, map_location=map_location)
    if not isinstance(payload, dict) or payload.get("format_version") != FORMAT_VERSION:
        raise ValueError("CHECKPOINT_FORMAT_UNSUPPORTED")

    config_data = json.loads(payload["config_json"])
    if isinstance(config_data.get("backend_preference"), list):
        config_data["backend_preference"] = tuple(config_data["backend_preference"])
    config = XingChengConfig(**config_data)
    state = payload["model_state"]
    if verify:
        declared = payload.get("state_sha256")
        if not declared or _state_digest(state) != declared:
            raise ValueError("CHECKPOINT_STATE_HASH_MISMATCH")

    model = XingChengForCausalLM(config)
    model.load_state_dict(state, strict=strict)
    model.eval()

    tokenizer_state = payload.get("tokenizer_state")
    tokenizer = None
    if tokenizer_state is not None:
        if tokenizer_state.get("kind") == "byte-level-bpe" or "tokenizer_json" in tokenizer_state:
            from .bpe import NativeBPETokenizer

            tokenizer = NativeBPETokenizer.from_state(tokenizer_state)
        else:
            tokenizer = XingChengTokenizer.from_state(tokenizer_state)
    return {
        "model": model,
        "config": config,
        "tokenizer": tokenizer,
        "metadata": json.loads(payload.get("metadata_json") or "{}"),
        "optimizer_state": payload.get("optimizer_state"),
        "extra": json.loads(payload.get("extra_json") or "{}"),
        "state_sha256": payload.get("state_sha256"),
        "created_at": payload.get("created_at"),
        "format_version": payload["format_version"],
    }


__all__ = [
    "FORMAT_VERSION",
    "default_checkpoint_dir",
    "save_checkpoint",
    "load_checkpoint",
]
