from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


def _background_subprocess_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


def main() -> int:
    command = sys.argv[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print("usage: test_runner.py -- <test command>", file=sys.stderr)
        return 2

    tool_root = Path(__file__).resolve().parents[5]
    sandbox_root = (
        tool_root / "runtime" / "temp" / "development" / "test-artifacts"
    ).resolve()
    run_root = sandbox_root / f"run-{uuid.uuid4().hex}"
    sandbox_compare = os.path.normcase(os.path.abspath(str(sandbox_root)))
    run_compare = os.path.normcase(os.path.abspath(str(run_root)))
    if os.path.commonpath((sandbox_compare, run_compare)) != sandbox_compare:
        raise RuntimeError("test sandbox path escaped global-cleaner runtime")
    run_root.mkdir(parents=True, exist_ok=False)

    environment = os.environ.copy()
    environment["PYTHONPYCACHEPREFIX"] = str(run_root / "pycache")
    pytest_cache = (run_root / "pytest-cache").as_posix()
    environment["PYTEST_ADDOPTS"] = (
        f"-o cache_dir={pytest_cache} "
        + str(environment.get("PYTEST_ADDOPTS") or "")
    ).strip()
    environment["TEMP"] = str(run_root / "temp")
    environment["TMP"] = str(run_root / "temp")
    environment["TMPDIR"] = str(run_root / "temp")
    environment["COVERAGE_FILE"] = str(run_root / "coverage.sqlite3")
    (run_root / "temp").mkdir()

    try:
        completed = subprocess.run(
            command,
            env=environment,
            check=False,
            **_background_subprocess_kwargs(),
        )
        return int(completed.returncode)
    finally:
        # Only remove the per-run directory. The shared sandbox_root must not be
        # removed while other workers may still be using it.
        shutil.rmtree(run_root, ignore_errors=False)


if __name__ == "__main__":
    raise SystemExit(main())
