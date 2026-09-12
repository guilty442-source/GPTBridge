from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from backend.services.xingcheng.application.local_rag import LocalRagService
from backend.services.xingcheng.infrastructure.transformer_runtime import (
    StarTransformerRuntime,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GPTBridge 本機共用 RAG")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="檢查 Qdrant、PostgreSQL FTS 與模型設定")
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
    runtime = StarTransformerRuntime(enabled=True)
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
