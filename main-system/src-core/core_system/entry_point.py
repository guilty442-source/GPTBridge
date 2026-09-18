"""Main Entry Point.

Application entry point with argument parsing and server startup.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ipc.server import run_server

from core_system.main import GPTBridgeApp


async def main() -> None:
    """Main entry point."""
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