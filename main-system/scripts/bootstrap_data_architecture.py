from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "main-system" / "scripts"


def _require_connection() -> None:
    dsn = str(os.environ.get("GPTBRIDGE_POSTGRES_ADMIN_DSN") or "").strip()
    if not dsn:
        raise RuntimeError("GPTBRIDGE_POSTGRES_ADMIN_DSN_REQUIRED")
    with psycopg.connect(dsn) as connection:
        connection.execute("SELECT 1")


def _run(script: str) -> None:
    environment = dict(os.environ)
    environment.setdefault(
        "GPTBRIDGE_POSTGRES_DSN",
        str(environment.get("GPTBRIDGE_POSTGRES_ADMIN_DSN") or ""),
    )
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / script)],
        cwd=str(ROOT),
        check=False,
        env=environment,
        creationflags=(
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
            if os.name == "nt" else 0
        ),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"PYTHON_EXECUTOR_FAILED:{script}:{completed.returncode}")


def main() -> int:
    _require_connection()
    _run("provision_postgresql_architecture.py")
    _run("migrate_legacy_indexes_to_postgresql.py")
    print(json.dumps({"ok": True, "executor": "python", "external_management_tools": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
