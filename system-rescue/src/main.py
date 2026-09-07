"""system-rescue runtime entry.

This module is referenced by ``manifest.json`` → ``runtime.entry``.
The actual governed runtime lives in ``channel_runtime.py`` (referenced by
``request_channel.runtime_entry``). This entry point delegates to the
governed channel runtime so that both entry paths start the same service.
"""
from __future__ import annotations

import asyncio

from channel_runtime import main as _channel_main


def main() -> int:
    """system-rescue main entry — delegates to the governed channel runtime."""
    asyncio.run(_channel_main())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
