"""R1：受治理訓練子程序工作進入點。

由 :class:`TrainingJobExecutor` 產生——把 ``_default_train_fn`` 隔離到獨立
Python 進程，使常駐推論服務（channel runtime）不承載訓練負載與生命週期。
跨程序僅透過檔案契約（spec／summary／error JSON），不共享記憶體、不繞過
治理通道。

用法（由 executor 產生，不供人工直接呼叫）::

    python -m xingcheng.infrastructure.training_job_worker <spec.json>

spec.json::

    {
      "schema": "star-training-job-spec/v1",
      "train_documents_file": "<path>",   # JSON array of documents
      "val_documents_file": "<path>",
      "configuration": {...},
      "output_dir": "<path>",
      "resume": "<path|null>"
    }

成功：``<output_dir>/train-summary.json``；失敗：``train-error.json``＋非零退出。
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

SPEC_SCHEMA = "star-training-job-spec/v1"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: training_job_worker <spec.json>", file=sys.stderr)
        return 2
    spec_path = Path(sys.argv[1])
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"SPEC_INVALID: {exc}", file=sys.stderr)
        return 2
    if spec.get("schema") != SPEC_SCHEMA:
        print("SPEC_SCHEMA_MISMATCH", file=sys.stderr)
        return 2

    output_dir = Path(spec["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        train_docs = json.loads(
            Path(spec["train_documents_file"]).read_text(encoding="utf-8")
        )
        val_docs = json.loads(
            Path(spec["val_documents_file"]).read_text(encoding="utf-8")
        )
        resume = Path(spec["resume"]) if spec.get("resume") else None
        configuration = dict(spec["configuration"])

        from .training_job_executor import _default_train_fn

        summary = _default_train_fn(
            train_docs,
            val_docs,
            configuration,
            output_dir=output_dir,
            resume=resume,
        )
    except Exception as exc:  # noqa: BLE001 - 子程序邊界：任何失敗寫 error JSON
        (output_dir / "train-error.json").write_text(
            json.dumps(
                {"error": str(exc)[:500], "traceback": traceback.format_exc()[-4000:]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 1

    (output_dir / "train-summary.json").write_text(
        json.dumps(summary or {}, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
