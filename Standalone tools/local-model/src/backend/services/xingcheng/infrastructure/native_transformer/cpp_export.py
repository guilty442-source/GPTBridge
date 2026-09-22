"""Export a PyTorch checkpoint to the C++ inference bundle format.

Format ``star-native-inference-bundle/v1`` is intentionally simple and
fail-closed: a JSON manifest declares config, tensor shapes, dtypes, offsets
and the SHA-256 of a little-endian FP64 blob. C++ owns production loading;
Python owns training and this one-way export path.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import torch

from .checkpoint import load_checkpoint
from .kernels import pack_int4, quantize_per_tensor

BUNDLE_SCHEMA = "star-native-inference-bundle/v1"

# R-quant: weight-only per-tensor symmetric quantization, mirroring
# ``quantization.quantizer.quantize_model`` — every nn.Linear weight except
# ``lm_head`` / MoE ``router`` (kept FP32-equivalent for routing/output
# fidelity) and non-Linear tensors (embeddings, norms).
_QUANTIZED_ENV = "XINGCHENG_NATIVE_QUANTIZATION"


def _quantize_bits(quantize_bits: int | None) -> int:
    if quantize_bits is not None:
        bits = int(quantize_bits)
    else:
        env = os.environ.get(_QUANTIZED_ENV, "").strip().lower()
        bits = {"8": 8, "int8": 8, "4": 4, "int4": 4}.get(env, 0)
    if bits not in (0, 4, 8):
        raise ValueError("CPP_EXPORT_QUANTIZE_BITS_UNSUPPORTED")
    return bits


def _is_quantizable_weight(name: str, value: torch.Tensor) -> bool:
    if value.dim() != 2 or "embeddings" in name:
        return False
    return not (name == "lm_head.weight" or name.endswith("router.weight"))


def export_checkpoint_for_cpp(
    checkpoint_path: str | Path,
    output_dir: str | Path,
    quantize_bits: int | None = None,
) -> dict[str, Any]:
    """Export ``checkpoint_path`` into ``output_dir`` for the C++ engine.

    ``quantize_bits`` selects weight-only per-tensor symmetric quantization
    (8 → ``int8`` blob, 4 → true 4-bit packed two-values-per-byte blob);
    ``None`` falls back to ``XINGCHENG_NATIVE_QUANTIZATION`` then FP64.
    """
    loaded = load_checkpoint(checkpoint_path, map_location="cpu", strict=True)
    model = loaded["model"]
    config = loaded["config"]
    if getattr(config, "quantization", "none") != "none":
        raise ValueError("CPP_EXPORT_QUANTIZED_CHECKPOINT_UNSUPPORTED")
    bits = _quantize_bits(quantize_bits)
    quantization = {0: "none", 8: "int8", 4: "int4"}[bits]

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
            value = value.detach().cpu().contiguous()
            info: dict[str, Any]
            if bits and _is_quantizable_weight(name, value):
                q, scale = quantize_per_tensor(value, n_bits=bits)
                if bits == 4:
                    raw = pack_int4(q).numpy().astype("uint8").tobytes(order="C")
                    dtype = "int4_packed"
                else:
                    raw = q.numpy().astype("<i1").tobytes(order="C")
                    dtype = "int8"
                info = {
                    "dtype": dtype,
                    "endianness": "little",
                    "shape": [int(dim) for dim in value.shape],
                    "offset": offset,
                    "bytes": len(raw),
                    "scale": float(scale),
                }
            else:
                array = (
                    value.to(torch.float64)
                    .numpy()
                    .astype("<f8", copy=False)
                )
                raw = array.tobytes(order="C")
                info = {
                    "dtype": "float64",
                    "endianness": "little",
                    "shape": [int(dim) for dim in array.shape],
                    "offset": offset,
                    "bytes": len(raw),
                }
            blob.write(raw)
            digest.update(raw)
            tensors[name] = info
            offset += len(raw)

    tokenizer = loaded.get("tokenizer")
    tokenizer_exported = False
    if tokenizer is not None:
        state = tokenizer.state_dict()
        payload = str(state.get("tokenizer_json") or "")
        if payload:
            tokenizer_path.write_text(payload, encoding="utf-8")
            tokenizer_exported = True

    config_dict = config.to_dict()
    config_dict["quantization"] = quantization
    manifest = {
        "schema_version": BUNDLE_SCHEMA,
        "created_by": "cpp_export.py",
        "checkpoint_sha256": str(loaded.get("state_sha256") or ""),
        "config": config_dict,
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
        "quantization": quantization,
        "tokenizer_exported": tokenizer_exported,
    }


__all__ = ["BUNDLE_SCHEMA", "export_checkpoint_for_cpp"]
