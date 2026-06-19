from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parent
    
    # Inject governance enforcement path
    sys.path.insert(0, str(project_root / "src-core"))
    from governance.enforcer import GovernanceEnforcer
    try:
        enforcer = GovernanceEnforcer(project_root, None)
    except TypeError:
        enforcer = GovernanceEnforcer(project_root)

    parser = argparse.ArgumentParser(
        description="GPTBridge project entry point.",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Serve command
    serve_parser = subparsers.add_parser("serve", aliases=["start"], help="Start the GPTBridge server.")
    serve_parser.add_argument("--profile", default="main", help="Specify the browser profile name.")
    serve_parser.add_argument(
        "--auto-kill-backend-port",
        action="store_true",
        help="Automatically terminate a previous GPTBridge backend holding port 8765 before starting.",
    )

    # Generate child tool command
    generate_parser = subparsers.add_parser(
        "generate_child_tool", help="Generate a new child tool."
    )
    generate_parser.add_argument(
        "tool_name",
        nargs="?",  # Optional argument
        default="ChildTool",
        help="The name of the child tool to generate (default: ChildTool).",
    )

    # Clean command
    subparsers.add_parser("clean", help="Clean up temporary, cache, and build directories.")

    args = parser.parse_args()

    if args.command in ("serve", "start"):
        command = [sys.executable, str(project_root / "src-core" / "main.py"), "--serve", "--profile", args.profile]
        if getattr(args, "auto_kill_backend_port", False):
            command.append("--auto-kill-backend-port")
        return subprocess.run(command, cwd=str(project_root)).returncode
    elif args.command == "generate_child_tool":
        command = [
            sys.executable,
            str(project_root / "src-core" / "main.py"),
            "--generate-child-tool",
            args.tool_name,
        ]
        return subprocess.run(command, cwd=str(project_root)).returncode
    elif args.command == "clean":
        import shutil
        import time
        now = time.time()
        unwanted_dirs = [
            "dist-ui", "dist", "build", "release",
            ".pytest_cache", ".ruff_cache", ".mypy_cache",
            "tmp", "temp", "cache",
            ".GPTBridge_RuntimeSandbox/temp",
            ".GPTBridge_RuntimeSandbox/cache"
        ]
        print("Cleaning up unnecessary folders...")
        for d in unwanted_dirs:
            target = project_root / d
            check = enforcer.can_delete_file(target, "cleanup", "Scheduled cleanup")
            if not check["allowed"]:
                print(f"BLOCKED: rule_id={check['rule_id']} reason={check['reason']}")
                continue
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                print(f"Deleted: {d}")
        for p in project_root.rglob("__pycache__"):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                print(f"Deleted: {p.relative_to(project_root)}")
        for ext in ("*.pyc", "*.tmp", "LOG.old"):
            for p in project_root.rglob(ext):
                if p.is_file() and ".venv" not in p.parts:
                    check = enforcer.can_delete_file(p, "cleanup", "Scheduled cleanup")
                    if not check["allowed"]:
                        print(f"BLOCKED: {check['rule_id']}")
                        continue
                    try: p.unlink(); print(f"Deleted: {p.relative_to(project_root)}")
                    except Exception: pass

        backup_dir = project_root / "backups"
        if backup_dir.exists():
            print("Cleaning up old backups (> 30 days)...")
            thirty_days_sec = 30 * 24 * 60 * 60
            for sub_dir in backup_dir.iterdir():
                if sub_dir.is_dir():
                    for item in sub_dir.iterdir():
                        try:
                            if now - item.stat().st_mtime > thirty_days_sec:
                                shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink()
                                print(f"Deleted old backup: {item.relative_to(project_root)}")
                        except Exception:
                            pass

        logs_dir = project_root / "runtime" / "logs"
        if logs_dir.exists():
            print("Cleaning up old logs (> 7 days)...")
            seven_days_sec = 7 * 24 * 60 * 60
            for item in logs_dir.rglob("*"):
                try:
                    if item.is_file() and (now - item.stat().st_mtime > seven_days_sec):
                        item.unlink()
                        print(f"Deleted old log: {item.relative_to(project_root)}")
                except Exception:
                    pass

        print("Cleanup complete.")
        return 0
    else:
        # If no command is given, or an invalid one, argparse will print help and exit.
        # This block is for cases where 'args.command' might be None if no subcommand was required
        # and no default was set, but with 'dest="command"' and required subparsers,
        # it should always have a value if parsing succeeds.
        parser.print_help()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
