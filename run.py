from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def run_project_cleaner(
    project_root: Path,
    *,
    scope: str = "global",
) -> int:
    """Delegate cleanup and repair to the standalone project-cleaner owner."""

    root = project_root.resolve()
    cleaner_entry = (
        root
        / "platform_tools"
        / "project-cleaner"
        / "src"
        / "main.py"
    )
    if not cleaner_entry.is_file():
        print(
            f"Project Cleaner entry is unavailable: {cleaner_entry}",
            file=sys.stderr,
        )
        return 1
    environment = os.environ.copy()
    environment["GPTBRIDGE_PROJECT_ROOT"] = str(root)
    completed = subprocess.run(
        [
            sys.executable,
            str(cleaner_entry),
            "--auto-clean",
            "--force",
            "--scope",
            scope,
            "--json",
        ],
        cwd=str(root),
        env=environment,
        check=False,
    )
    return completed.returncode


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

    generate_parser = subparsers.add_parser(
        "generate_child_tool",
        help="Generate a new child tool.",
    )
    generate_parser.add_argument(
        "tool_name",
        nargs="?",
        default="ChildTool",
        help="The name of the child tool to generate (default: ChildTool).",
    )

    clean_parser = subparsers.add_parser(
        "clean",
        help="Run recoverable cleanup and anomaly repair through Project Cleaner.",
    )
    clean_parser.add_argument(
        "--scope",
        choices=("global", "runtime", "sandbox"),
        default="global",
        help="Limit cleanup to the selected area inside this project.",
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
        ).returncode
    if args.command == "generate_child_tool":
        return subprocess.run(
            [
                sys.executable,
                str(project_root / "src-core" / "generate_child_tool.py"),
                args.tool_name,
            ],
            cwd=str(project_root),
            check=False,
        ).returncode
    if args.command == "clean":
        return run_project_cleaner(project_root, scope=args.scope)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
