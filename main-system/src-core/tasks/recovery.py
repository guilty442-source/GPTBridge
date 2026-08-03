from __future__ import annotations

import json
from pathlib import Path
from typing import Any


RECOVERABLE_STATUSES = {"running", "queued", "pending", "interrupted"}


def recover_task(
    task: dict[str, Any] | None = None,
    *,
    state_file: str | Path | None = None,
) -> dict[str, Any]:
    """Mark an interrupted task as pending so it can be retried safely."""

    if task is None and state_file is not None:
        path = Path(state_file)
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "recovered": False, "message": f"cannot read task state: {exc}"}
        if isinstance(loaded, dict):
            task = loaded

    if not isinstance(task, dict):
        return {"ok": True, "recovered": False, "message": "no task supplied"}

    status = str(task.get("status") or "").strip().lower()
    if status not in RECOVERABLE_STATUSES:
        return {
            "ok": True,
            "recovered": False,
            "task": dict(task),
            "message": f"task status does not require recovery: {status or 'unknown'}",
        }

    recovered = dict(task)
    recovered["previous_status"] = status
    recovered["status"] = "pending"
    recovered["recovered"] = True
    recovered.setdefault("recovery_count", 0)
    recovered["recovery_count"] = int(recovered.get("recovery_count") or 0) + 1

    if state_file is not None:
        Path(state_file).write_text(
            json.dumps(recovered, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    return {"ok": True, "recovered": True, "task": recovered, "message": "task recovered"}
