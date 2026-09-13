"""Star-Chat Channel Runtime Entry."""

from __future__ import annotations

import asyncio
import sys


async def main() -> int:
    """Entry point for star-chat runtime."""
    print("star-chat runtime starting...")
    await asyncio.sleep(0)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))