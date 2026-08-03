from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .backup import BackupOrchestrator
from .bootstrap import DatabaseBootstrap
from .config import DatabaseSettings
from .health import DatabaseHealthCheck


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GPTBridge PostgreSQL programmatic manager")
    parser.add_argument("command", choices=("bootstrap", "health", "backup", "restore"))
    parser.add_argument("--file", type=Path)
    args = parser.parse_args(argv)
    settings = DatabaseSettings.from_environment()
    shared_root = Path(__file__).resolve().parents[3]
    if args.command == "bootstrap":
        report = DatabaseBootstrap(
            settings,
            shared_root / "migrations",
            shared_root / "sql" / "central_index.sql",
        ).run()
        print(json.dumps(asdict(report), ensure_ascii=False, default=str))
        return 0
    if args.command == "health":
        report = DatabaseHealthCheck(settings).run()
        print(json.dumps(asdict(report), ensure_ascii=False, default=str))
        return 0 if report.available else 1
    if args.file is None:
        parser.error("--file is required for backup/restore")
    orchestrator = BackupOrchestrator(settings)
    if args.command == "backup":
        print(json.dumps(asdict(orchestrator.backup(args.file)), ensure_ascii=False, default=str))
    else:
        orchestrator.restore(args.file)
        print(json.dumps({"restored": str(args.file.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
