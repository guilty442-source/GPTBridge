from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def repair_tool() -> dict[str, Any]:
    tool_root = Path(__file__).resolve().parents[1]
    manifest_path = tool_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tool_id = str(manifest.get("id") or "").strip()
    if tool_root.name != tool_id:
        raise RuntimeError("tool identity does not match its directory")

    runtime_root = tool_root / "runtime"
    state_root = runtime_root / "state"
    recovery_root = runtime_root / "recovery" / "database"
    report_root = runtime_root / "maintenance"
    for directory in (state_root, recovery_root, report_root):
        directory.mkdir(parents=True, exist_ok=True)
        if not _inside(directory, tool_root):
            raise RuntimeError("maintenance path escaped the tool root")

    checked: list[str] = []
    quarantined: list[str] = []
    errors: list[str] = []
    for database in sorted(state_root.rglob("*.sqlite*")):
        if not database.is_file() or not _inside(database, tool_root):
            continue
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=3)
            try:
                result = connection.execute("PRAGMA integrity_check").fetchone()
            finally:
                connection.close()
            if not result or str(result[0]).lower() != "ok":
                raise sqlite3.DatabaseError(str(result))
            checked.append(str(database.relative_to(tool_root)))
        except (OSError, sqlite3.DatabaseError) as error:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            destination = recovery_root / f"{database.name}.{stamp}.corrupt"
            try:
                os.replace(database, destination)
                quarantined.append(str(destination.relative_to(tool_root)))
            except OSError as move_error:
                errors.append(f"{database.name}: {type(move_error).__name__}")
            errors.append(f"{database.name}: {type(error).__name__}")

    report: dict[str, Any] = {
        "ok": not errors,
        "tool_id": tool_id,
        "scope": "tool-root-only",
        "database_root": str(state_root),
        "checked_databases": checked,
        "quarantined_databases": quarantined,
        "errors": errors,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    report_path = report_root / "latest-auto-repair.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Tool-scoped automatic repair")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = repair_tool()
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"{result['tool_id']}: {'ok' if result['ok'] else 'repair required'}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

