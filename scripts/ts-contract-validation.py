"""§10.9 TypeScript Validation executor.

Runs the renderer/main TypeScript type check (``npm run type-check`` —
``tsc --noEmit`` over the UI sources) and records the result against the
IPC contract axis, closing the "Contract Definition → Python Validation
→ TypeScript Validation → IPC Compatibility Test" chain.

Writes ``runtime/logs/ts-validation-<ts>.json``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

ROOT = Path(__file__).resolve().parent.parent
MS = ROOT / "main-system"
LOG_DIR = MS / "runtime" / "logs"
TIMEOUT_S = 300


def main() -> int:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["npm.cmd", "run", "type-check"],
            cwd=MS,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            creationflags=_CREATE_NO_WINDOW,
        )
        rc = proc.returncode
        stdout = (proc.stdout or "")[-4000:]
        stderr = (proc.stderr or "")[-4000:]
        error_lines = [
            line for line in stdout.splitlines() + stderr.splitlines()
            if "error TS" in line
        ]
    except subprocess.TimeoutExpired:
        rc = -1
        stdout = ""
        stderr = f"type-check exceeded {TIMEOUT_S}s"
        error_lines = [stderr]
    elapsed = time.monotonic() - started

    report = {
        "schema": "ts-validation/v1",
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": "npm run type-check (tsc --noEmit)",
        "returncode": rc,
        "elapsed_s": round(elapsed, 1),
        "passed": rc == 0,
        "ts_errors": len(error_lines),
        "error_samples": error_lines[:15],
        "tail": (stdout or stderr)[-1500:],
    }
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"ts-validation-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "passed": report["passed"],
        "elapsed_s": report["elapsed_s"],
        "ts_errors": report["ts_errors"],
        "report": str(path),
    }, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
