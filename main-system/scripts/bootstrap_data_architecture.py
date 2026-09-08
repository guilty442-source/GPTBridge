from __future__ import annotations

"""bootstrap_data_architecture — codex-native local PostgreSQL architecture bootstrap.

Provisions and migrates the governed local PostgreSQL architecture
(A44/E30).  Uses PostgreSQL/psycopg as the canonical structured-data engine;
runs without external management tools or services.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "main-system" / "scripts"


def _run(script: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / script)],
        cwd=str(ROOT),
        check=False,
        env=dict(os.environ),
        creationflags=(
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
            if os.name == "nt" else 0
        ),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"PYTHON_EXECUTOR_FAILED:{script}:{completed.returncode}")


def main() -> int:
    _run("provision_postgresql_architecture.py")
    _run("migrate_legacy_indexes_to_postgresql.py")
    print(json.dumps({
        "ok": True,
        "executor": "python",
        "engine": "postgresql",
        "external_management_tools": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())