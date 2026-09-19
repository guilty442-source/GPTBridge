"""教師蒸餾快照 → 受管訓練資料集／SFT job 橋（階段 6 收口）。

``build_distillation_snapshot`` 產出的 ``star-transformer-sft/v1``
快照只有檔案；本模組把它註冊進 ``TransformerTrainingRepository``：

    蒸餾快照 jsonl（已過品質閘門的 accepted 範例）
        → create_dataset（逐筆 sha256、train/validation 切分）
        → create_training_job（training_kind="sft"）

品質下限沿用 repository 的 0.8 門檻：低於門檻的紀錄被剔除並計數，
不會靜默進入訓練資料。資料集以內容雜湊定址——同一份快照重複
註冊只會回傳既有 dataset（inserted=False）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

DISTILL_BRIDGE_VERSION = "star-distill-bridge/v1"
DISTILL_SOURCE_TYPE = "teacher-distillation-verified"
MIN_QUALITY = 0.8


def _load_manifest(manifest: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(manifest, (str, Path)):
        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    else:
        data = dict(manifest)
    if str(data.get("format_version") or "") != "star-transformer-sft/v1":
        raise ValueError("DISTILL_MANIFEST_FORMAT_MISMATCH")
    return data


def _snapshot_records(snapshot_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with snapshot_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            records.append(record)
    return records


def register_distillation_snapshot(
    repository: Any,
    manifest: Mapping[str, Any] | str | Path,
    *,
    owner_model_id: str = "star-main-native-model",
    database_scope: str = "main",
    created_by: str = "star-main-native-model",
) -> dict[str, Any]:
    """把蒸餾快照註冊為受管訓練資料集（內容定址、可重入）。"""
    data = _load_manifest(manifest)
    snapshot_path = Path(str(data["snapshot_path"])).resolve()
    tool_root = Path(repository.tool_root).resolve()
    if not snapshot_path.is_relative_to(tool_root):
        raise PermissionError("DISTILL_SNAPSHOT_SCOPE_DENIED")
    if not snapshot_path.is_file():
        raise FileNotFoundError("DISTILL_SNAPSHOT_MISSING")
    actual_sha = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    if actual_sha != str(data["snapshot_sha256"]):
        raise ValueError("DISTILL_SNAPSHOT_DRIFT")

    records = _snapshot_records(snapshot_path)
    examples: list[dict[str, Any]] = []
    dropped = 0
    for index, record in enumerate(records, start=1):
        quality = float(record.get("quality_score") or 0.0)
        if quality < MIN_QUALITY:
            dropped += 1
            continue
        examples.append(
            {
                "split": str(record.get("split") or "train"),
                "owner_model_id": owner_model_id,
                "database_scope": database_scope,
                "source_example_id": f"distill-{str(record['sha256'])[:24]}",
                "source_revision": 1,
                "content_sha256": str(record["sha256"]),
                "source_type": DISTILL_SOURCE_TYPE,
                "quality_score": quality,
            }
        )
    if not examples:
        raise ValueError("DISTILL_DATASET_EMPTY_AFTER_QUALITY_FILTER")

    content_sha256 = hashlib.sha256(
        "".join(sorted(item["content_sha256"] for item in examples)).encode("utf-8")
    ).hexdigest()
    dataset = repository.create_dataset(
        content_sha256=content_sha256,
        snapshot_path=str(snapshot_path),
        snapshot_sha256=actual_sha,
        examples=examples,
        source_manifest={
            "bridge": DISTILL_BRIDGE_VERSION,
            "distillation_manifest": {
                key: data[key]
                for key in (
                    "created_at", "examples", "train_count", "validation_count",
                    "rejected", "failures", "teacher_models",
                )
                if key in data
            },
            "dropped_below_quality": dropped,
        },
        created_by=created_by,
    )
    dataset["dropped_below_quality"] = dropped
    return dataset


def queue_distillation_sft_job(
    repository: Any,
    manifest: Mapping[str, Any] | str | Path,
    *,
    configuration: Mapping[str, Any] | None = None,
    requested_by: str = "star-main-native-model",
    **dataset_kwargs: Any,
) -> dict[str, Any]:
    """一鍵：註冊蒸餾資料集並排入 SFT 訓練 job（回傳 job 列）。"""
    dataset = register_distillation_snapshot(
        repository, manifest, **dataset_kwargs
    )
    config = {"training_kind": "sft", **dict(configuration or {})}
    job = repository.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration=config,
        requested_by=requested_by,
    )
    return {"dataset": dataset, "job": job}


__all__ = [
    "DISTILL_BRIDGE_VERSION",
    "DISTILL_SOURCE_TYPE",
    "MIN_QUALITY",
    "queue_distillation_sft_job",
    "register_distillation_snapshot",
]
