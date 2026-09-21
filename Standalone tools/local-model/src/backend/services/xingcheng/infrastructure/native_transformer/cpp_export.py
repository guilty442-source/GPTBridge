"""Export a PyTorch checkpoint to the C++ inference bundle format.

Format ``star-native-inference-bundle/v1`` is intentionally simple and
fail-closed: a JSON manifest declares config, tensor shapes, dtypes, offsets
and the SHA-256 of a little-endian FP64 blob. C++ owns production loading;
Python owns training and this one-way export path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import torch

from .checkpoint import load_checkpoint

BUNDLE_SCHEMA = "star-native-inference-bundle/v1"


def export_checkpoint_for_cpp(
    checkpoint_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Export ``checkpoint_path`` into ``output_dir`` for the C++ engine."""
    loaded = load_checkpoint(checkpoint_path, map_location="cpu", strict=True)
    model = loaded["model"]
    config = loaded["config"]
    if getattr(config, "use_moe", False):
        raise ValueError("CPP_EXPORT_MOE_UNSUPPORTED")
    if getattr(config, "quantization", "none") != "none":
        raise ValueError("CPP_EXPORT_QUANTIZED_CHECKPOINT_UNSUPPORTED")

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    weights_path = target / "weights.bin"
    manifest_path = target / "manifest.json"
    tokenizer_path = target / "tokenizer.json"

    tensors: dict[str, dict[str, Any]] = {}
    offset = 0
    digest = hashlib.sha256()
    with weights_path.open("wb") as blob:
        for name, value in sorted(model.state_dict().items()):
            array = (
                value.detach()
                .cpu()
                .contiguous()
                .to(torch.float64)
                .numpy()
                .astype("<f8", copy=False)
            )
            raw = array.tobytes(order="C")
            blob.write(raw)
            digest.update(raw)
            tensors[name] = {
                "dtype": "float64",
                "endianness": "little",
                "shape": [int(dim) for dim in array.shape],
                "offset": offset,
                "bytes": len(raw),
            }
            offset += len(raw)

    tokenizer = loaded.get("tokenizer")
    tokenizer_exported = False
    if tokenizer is not None:
        state = tokenizer.state_dict()
        payload = str(state.get("tokenizer_json") or "")
        if payload:
            tokenizer_path.write_text(payload, encoding="utf-8")
            tokenizer_exported = True

    manifest = {
        "schema_version": BUNDLE_SCHEMA,
        "created_by": "cpp_export.py",
        "checkpoint_sha256": str(loaded.get("state_sha256") or ""),
        "config": config.to_dict(),
        "weights_file": weights_path.name,
        "weights_sha256": digest.hexdigest(),
        "tokenizer_file": tokenizer_path.name if tokenizer_exported else None,
        "tensors": tensors,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": BUNDLE_SCHEMA,
        "output_dir": str(target),
        "manifest": str(manifest_path),
        "weights": str(weights_path),
        "weights_sha256": digest.hexdigest(),
        "tensor_count": len(tensors),
        "weights_bytes": offset,
        "tokenizer_exported": tokenizer_exported,
    }


__all__ = ["BUNDLE_SCHEMA", "export_checkpoint_for_cpp"]
