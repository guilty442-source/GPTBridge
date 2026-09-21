"""``star-transformer-sft/v1``：已核准語言訓練範例 → 快照 JSONL 序列化器。

Role database 的 ``language_training_example`` 是唯一語料權威；此模組只做
唯讀匯出——把已核准（active 且 quality>=0.8）的範例序列化為快照檔與
``TransformerTrainingRepository.create_dataset`` 所需的範例中繼資料。
不複製原始內容進訓練庫，只記雜湊與出處。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

SFT_FORMAT_VERSION = "star-transformer-sft/v1"

_SCOPE_MAP = {
    "main": "main",
    "investment": "investment",
    "mathematical": "mathematical",
    "coding": "coding",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sft_text(prompt: str, completion: str) -> str:
    """SFT 訓練文本模板：prompt 與 completion 以固定分隔串接。"""
    return f"{str(prompt).strip()}\n\n{str(completion).strip()}"


def serialize_sft_example(example: Mapping[str, Any]) -> dict[str, Any]:
    """單筆 ``language_training_example`` → 快照記錄（含 sha256）。"""
    prompt = str(example.get("input_text") or "").strip()
    completion = str(example.get("target_text") or "").strip()
    if not prompt or not completion:
        raise ValueError("sft example requires non-empty input_text/target_text")
    text = sft_text(prompt, completion)
    return {
        "source": str(example.get("source_type") or ""),
        "sha256": _sha256_text(text),
        "text": text,
        "prompt": prompt,
        "completion": completion,
        "intent": str(example.get("intent") or ""),
        "source_example_id": str(example.get("example_id") or ""),
        "source_revision": int(example.get("revision") or 0),
        "quality_score": float(example.get("quality_score") or 0.0),
    }


def build_sft_dataset(
    *,
    output_path: str | Path,
    examples_by_scope: Mapping[str, Sequence[Mapping[str, Any]]],
    val_permille: int = 50,
) -> dict[str, Any]:
    """把各 scope 的已核准範例寫成快照 JSONL，回傳可直接餵給
    ``create_dataset`` 的登錄套件（snapshot 路徑/雜湊、範例中繼資料、
    content_sha256、manifest）。切分以 sha256 千分比決定——可重現。"""
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not 0 < int(val_permille) < 1000:
        raise ValueError("val_permille must be in (0, 1000)")

    registered_examples: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for scope, examples in examples_by_scope.items():
        database_scope = _SCOPE_MAP.get(str(scope).casefold())
        if database_scope is None:
            raise ValueError(f"unsupported database_scope: {scope}")
        for example in examples:
            record = serialize_sft_example(example)
            if record["sha256"] in seen:
                continue
            seen.add(record["sha256"])
            split = (
                "validation"
                if int(record["sha256"][:8], 16) % 1000 < int(val_permille)
                else "train"
            )
            record["split"] = split
            records.append(record)
            registered_examples.append(
                {
                    "split": split,
                    "owner_model_id": str(
                        example.get("owner_model_id") or "star-main-native-model"
                    ),
                    "database_scope": database_scope,
                    "source_example_id": record["source_example_id"],
                    "source_revision": record["source_revision"],
                    "content_sha256": record["sha256"],
                    "source_type": record["source"],
                    "quality_score": record["quality_score"],
                }
            )

    if not records:
        raise ValueError("no approved language training examples supplied")
    if not any(r["split"] == "train" for r in records) or not any(
        r["split"] == "validation" for r in records
    ):
        raise ValueError("dataset requires train and validation examples")

    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    content_sha256 = _sha256_text(
        _canonical_json(sorted(record["sha256"] for record in records))
    )
    manifest = {
        "format_version": SFT_FORMAT_VERSION,
        "snapshot_path": str(target),
        "snapshot_sha256": _file_sha256(target),
        "content_sha256": content_sha256,
        "example_count": len(records),
        "train_count": sum(r["split"] == "train" for r in records),
        "validation_count": sum(r["split"] == "validation" for r in records),
        "scopes": {
            scope: len(examples)
            for scope, examples in sorted(examples_by_scope.items())
        },
    }
    return {
        "format_version": SFT_FORMAT_VERSION,
        "snapshot_path": str(target),
        "snapshot_sha256": manifest["snapshot_sha256"],
        "content_sha256": content_sha256,
        "examples": registered_examples,
        "source_manifest": {
            "format": SFT_FORMAT_VERSION,
            "scopes": manifest["scopes"],
            "val_permille": int(val_permille),
        },
        "manifest": manifest,
    }


__all__ = [
    "SFT_FORMAT_VERSION",
    "build_sft_dataset",
    "serialize_sft_example",
    "sft_text",
]
