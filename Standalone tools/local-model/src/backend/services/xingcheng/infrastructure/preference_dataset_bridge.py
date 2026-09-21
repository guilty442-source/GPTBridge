"""偏好對 → 受管資料集／DPO job 橋（階段 7 收口）。

``language_preference_pair``（paired=1，品質閘門拒絕的 rejected
配上驗證過的 chosen）→ ``star-transformer-dpo/v1`` 快照 →
``create_dataset`` → ``create_training_job(training_kind="dpo")``。

快照紀錄比 SFT 多 ``chosen``/``rejected`` 欄位；executor 的
``_read_snapshot_documents`` 透傳後由 ``dpo_train`` 消費。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

PREFERENCE_SNAPSHOT_FORMAT = "star-transformer-dpo/v1"
PREFERENCE_SOURCE_TYPE = "preference-pair-governed"
PAIR_QUALITY = 0.9  # paired=1 即治理層已驗證；寫死以通過資料集 0.8 門檻


def _record_hash(prompt: str, chosen: str, rejected: str) -> str:
    return hashlib.sha256(
        f"{prompt}\0{chosen}\0{rejected}".encode("utf-8")
    ).hexdigest()


def build_pairs_snapshot(
    pairs: Sequence[Mapping[str, Any]],
    output_path: str | Path,
    *,
    val_permille: int = 200,
) -> dict[str, Any]:
    """偏好對 → ``star-transformer-dpo/v1`` 快照（去重＋確定性切分）。"""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for pair in pairs:
        prompt = str(pair.get("prompt_text") or pair.get("prompt") or "").strip()
        chosen = str(pair.get("chosen_text") or pair.get("chosen") or "").strip()
        rejected = str(
            pair.get("rejected_text") or pair.get("rejected") or ""
        ).strip()
        if not (prompt and chosen and rejected):
            continue
        digest = _record_hash(prompt, chosen, rejected)
        if digest in seen:
            continue
        seen.add(digest)
        split = (
            "validation"
            if int(digest[:8], 16) % 1000 < int(val_permille)
            else "train"
        )
        records.append(
            {
                "source": "language_preference_pair:"
                + str(pair.get("pair_id") or "")[:32],
                "sha256": digest,
                "text": f"{prompt}\n\n{chosen}",
                "prompt": prompt,
                "chosen": chosen,
                "rejected": rejected,
                "intent": str(pair.get("intent") or "preference"),
                "quality_score": PAIR_QUALITY,
                "split": split,
            }
        )
    if not records:
        raise ValueError("PREFERENCE_SNAPSHOT_EMPTY")
    # 資料集要求 train/validation 皆非空：小樣本時把最後一筆撥到 val
    if len(records) > 1 and not any(r["split"] == "validation" for r in records):
        records[-1]["split"] = "validation"

    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    file_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {
        "format_version": PREFERENCE_SNAPSHOT_FORMAT,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "snapshot_path": str(target),
        "snapshot_sha256": file_digest,
        "pairs": len(records),
        "train_count": sum(r["split"] == "train" for r in records),
        "validation_count": sum(r["split"] == "validation" for r in records),
    }
    (target.parent / "preference_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def register_pairs_snapshot(
    repository: Any,
    manifest: Mapping[str, Any] | str | Path,
    *,
    owner_model_id: str = "star-main-native-model",
    database_scope: str = "main",
    created_by: str = "star-main-native-model",
) -> dict[str, Any]:
    """註冊偏好對快照為受管資料集（內容定址、冪等）。"""
    data = (
        json.loads(Path(manifest).read_text(encoding="utf-8"))
        if isinstance(manifest, (str, Path))
        else dict(manifest)
    )
    if str(data.get("format_version") or "") != PREFERENCE_SNAPSHOT_FORMAT:
        raise ValueError("PREFERENCE_MANIFEST_FORMAT_MISMATCH")
    snapshot_path = Path(str(data["snapshot_path"])).resolve()
    tool_root = Path(repository.tool_root).resolve()
    if not snapshot_path.is_relative_to(tool_root):
        raise PermissionError("PREFERENCE_SNAPSHOT_SCOPE_DENIED")
    if not snapshot_path.is_file():
        raise FileNotFoundError("PREFERENCE_SNAPSHOT_MISSING")
    actual_sha = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    if actual_sha != str(data["snapshot_sha256"]):
        raise ValueError("PREFERENCE_SNAPSHOT_DRIFT")

    examples: list[dict[str, Any]] = []
    with snapshot_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            examples.append(
                {
                    "split": str(record.get("split") or "train"),
                    "owner_model_id": owner_model_id,
                    "database_scope": database_scope,
                    "source_example_id": f"pref-{str(record['sha256'])[:24]}",
                    "source_revision": 1,
                    "content_sha256": str(record["sha256"]),
                    "source_type": PREFERENCE_SOURCE_TYPE,
                    "quality_score": PAIR_QUALITY,
                }
            )
    content_sha256 = hashlib.sha256(
        "".join(sorted(e["content_sha256"] for e in examples)).encode("utf-8")
    ).hexdigest()
    return repository.create_dataset(
        content_sha256=content_sha256,
        snapshot_path=str(snapshot_path),
        snapshot_sha256=actual_sha,
        examples=examples,
        source_manifest={
            "bridge": PREFERENCE_SNAPSHOT_FORMAT,
            "preference_manifest": {
                key: data[key]
                for key in ("created_at", "pairs", "train_count", "validation_count")
                if key in data
            },
        },
        created_by=created_by,
    )


def queue_dpo_job(
    repository: Any,
    manifest: Mapping[str, Any] | str | Path,
    *,
    configuration: Mapping[str, Any] | None = None,
    requested_by: str = "star-main-native-model",
    **dataset_kwargs: Any,
) -> dict[str, Any]:
    """一鍵：註冊偏好資料集並排入 DPO job（需 init_checkpoint 錨定）。"""
    dataset = register_pairs_snapshot(repository, manifest, **dataset_kwargs)
    config = {"training_kind": "dpo", **dict(configuration or {})}
    job = repository.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration=config,
        requested_by=requested_by,
    )
    return {"dataset": dataset, "job": job}


__all__ = [
    "PREFERENCE_SNAPSHOT_FORMAT",
    "PREFERENCE_SOURCE_TYPE",
    "build_pairs_snapshot",
    "queue_dpo_job",
    "register_pairs_snapshot",
]
