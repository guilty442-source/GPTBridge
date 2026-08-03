from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _background_subprocess_kwargs() -> dict[str, int]:
    if sys.platform != "win32":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


def main() -> int:
    project_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="GPTBridge project entry point.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    subparsers = parser.add_subparsers(
        dest="command",
        help="Available commands",
    )

    serve_parser = subparsers.add_parser(
        "serve",
        aliases=["start"],
        help="Start the GPTBridge server.",
    )
    serve_parser.add_argument(
        "--profile",
        default="main",
        help="Specify the browser profile name.",
    )
    serve_parser.add_argument(
        "--auto-kill-backend-port",
        action="store_true",
        help=(
            "Automatically terminate a previous GPTBridge backend holding "
            "port 8765 before starting."
        ),
    )

    args = parser.parse_args()
    if args.command in ("serve", "start"):
        command = [
            sys.executable,
            str(project_root / "src-core" / "main.py"),
            "--serve",
            "--profile",
            args.profile,
        ]
        if args.auto_kill_backend_port:
            command.append("--auto-kill-backend-port")
        return subprocess.run(
            command,
            cwd=str(project_root),
            check=False,
            **_background_subprocess_kwargs(),
        ).returncode
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
