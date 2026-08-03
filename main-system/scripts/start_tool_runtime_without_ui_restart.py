from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

import websockets


async def start_runtime(
    tool_id: str,
    project_root: Path,
    *,
    background: bool,
) -> dict[str, object]:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", "")).expanduser()
    token_candidates = [
        local_app_data / "GPTBridge" / "ipc" / "session-token",
        project_root / "main-system" / "runtime" / "ipc" / "session-token",
    ]
    token_path = next((path for path in token_candidates if path.is_file()), None)
    if token_path is None:
        raise RuntimeError("GPTBridge IPC session token file is unavailable")
    token = token_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[a-f0-9]{64}", token, re.IGNORECASE):
        raise RuntimeError("GPTBridge IPC session token is unavailable")
    request_id = f"hot-runtime-start-{tool_id}-{time.time_ns()}"
    normalized_root = os.path.normcase(str(project_root.resolve())).replace("\\", "/")
    instance = hashlib.sha256(normalized_root.encode("utf-8")).hexdigest()[:24]
    async with websockets.connect(
        f"ws://127.0.0.1:8765/?token={token}&instance={instance}",
        origin="file://",
    ) as socket:
        await socket.send(
            json.dumps(
                {
                    "command": "toolbox_start_tool",
                    "payload": {
                        "tool_id": tool_id,
                        "request_id": request_id,
                        "runtime_mode": "source",
                        "background": background,
                    },
                }
            )
        )
        while True:
            frame = json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
            if frame.get("event") != "toolbox_start_tool_result":
                continue
            payload = frame.get("payload")
            if not isinstance(payload, dict):
                raise RuntimeError("GPTBridge returned an invalid tool-start result")
            if str(payload.get("request_id") or "") != request_id:
                continue
            return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start a governed tool runtime without closing its foreground UI."
    )
    parser.add_argument("tool_id")
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Also create or activate the tool window.",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", args.tool_id):
        parser.error("invalid tool id")
    project_root = Path(__file__).resolve().parents[2]
    result = asyncio.run(
        start_runtime(
            args.tool_id,
            project_root,
            background=not args.foreground,
        )
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
