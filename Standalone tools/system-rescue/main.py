"""system-rescue entry point.

Central repair and platform packaging integration tool.
Runs under main-system governance authorization.
Delegates to the governed channel runtime (same as ``src/main.py``).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from channel_runtime import main as _channel_main  # noqa: E402


def main() -> int:
    """system-rescue main entry — delegates to the governed channel runtime."""
    asyncio.run(_channel_main())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
