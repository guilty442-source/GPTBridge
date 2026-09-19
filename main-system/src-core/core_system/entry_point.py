"""Main Entry Point.

Application entry point with argument parsing and server startup.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from ipc.server import run_server


async def main(app_instance: Any | None = None) -> None:
    """Main entry point.

    ``app_instance`` is supplied by the composition root (``main.py``) so this
    module never imports it back: the class lives in the top-level ``main``
    module and importing it here would create an import cycle at boot.  The
    deferred fallback import keeps direct invocation working.
    """
    parser = argparse.ArgumentParser(description="GPTBridge Mother Tool Entry")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the IPC server for the mother tool.",
    )
    parser.add_argument(
        "--auto-kill-backend-port",
        action="store_true",
        help="Automatically terminate a previous GPTBridge backend holding the configured IPC port before starting.",
    )
    parser.add_argument(
        "--profile",
        default="main",
        help="Browser profile name forwarded by run.py; accepted for compatibility but not used by the server.",
    )

    args = parser.parse_args()

    if app_instance is None:
        from main import GPTBridgeApp

        app_instance = GPTBridgeApp()

    try:
        await run_server(
            app_instance,
            auto_kill_backend_port=args.auto_kill_backend_port,
        )
    finally:
        await app_instance.shutdown()


if __name__ == "__main__":
    try:
        from startup import run_cli
        run_cli()
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)