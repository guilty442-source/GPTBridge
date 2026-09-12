"""Test runner entry — delegates to pytest with CREATE_NO_WINDOW."""
from __future__ import annotations
import os, subprocess, sys
from pathlib import Path
_F = 0x08000000 if os.name == "nt" else 0
def main() -> int:
    t = Path(__file__).resolve().parents[1]
    return subprocess.run([sys.executable, "-m", "pytest", str(t / "tests"), "-v"], cwd=str(t), creationflags=_F).returncode
if __name__ == "__main__":
    raise SystemExit(main())
