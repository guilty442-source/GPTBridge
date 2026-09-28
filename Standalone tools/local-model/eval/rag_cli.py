from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_WSROOT = Path(__file__).resolve().parents[3]
_LMSRC = _WSROOT / "Standalone tools" / "local-model" / "src"
_SHARED = _WSROOT / "shared-layer" / "src"
for _p in (_LMSRC, _SHARED, _WSROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
del _LMSRC, _SHARED, _WSROOT, _p

from backend.services.xingcheng.application.local_rag import LocalRagService  # noqa: E402
from backend.services.xingcheng.infrastructure.native_runtime import (  # noqa: E402
    StarNativeRuntime,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GPTBridge 本機共用 RAG")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="檢查 vectord、PostgreSQL FTS 與模型設定")
    index = subparsers.add_parser("index", help="將檔案或資料夾加入共用知識庫")
    index.add_argument("paths", nargs="+", help="E:\\GPTBridge 內的檔案或資料夾")
    query = subparsers.add_parser("query", help="查詢共用知識庫")
    query.add_argument("question")
    query.add_argument("--mode", choices=sorted(LocalRagService.RAG_MODELS))
    query.add_argument("--top-k", type=int, default=6)
    query.add_argument("--retrieve-only", action="store_true")
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    tool_root = Path(__file__).resolve().parents[1]
    runtime = StarNativeRuntime(enabled=True)
    rag = LocalRagService(tool_root, runtime)
    payload: dict[str, Any]
    if arguments.command == "status":
        result = rag.status()
    elif arguments.command == "index":
        result = rag.ingest({"paths": arguments.paths})
    else:
        payload = {
            "question": arguments.question,
            "top_k": arguments.top_k,
            "generate": not arguments.retrieve_only,
        }
        if arguments.mode:
            payload["rag_mode"] = arguments.mode
        result = rag.query(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("ok") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
