"""AI_COLLABORATION independent tool entry point (manifest ``runtime.entry``).

The governed launcher (ToolboxService) spawns ``src/channel_runtime.py``
directly with launcher-issued IPC credentials.  This declared runtime entry
delegates to the same governed runtime so the tool is a real executable —
the tool window process is hosted separately by the governed source-UI host
and neither depends on the main-system window as a supervisor.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
_TOOL_ROOT = _SRC_DIR.parent
_WORKSPACE_ROOT = _TOOL_ROOT.parent.parent


def main() -> int:
    root = _WORKSPACE_ROOT
    if not str(os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT") or "").strip():
        os.environ["GPTBRIDGE_GOVERNANCE_PROJECT_ROOT"] = str(root)
    if not str(os.environ.get("GPTBRIDGE_TOOL_DIR") or "").strip():
        os.environ["GPTBRIDGE_TOOL_DIR"] = str(_TOOL_ROOT)
    if not str(os.environ.get("GPTBRIDGE_IPC_SESSION_TOKEN") or "").strip():
        # The governed channel requires launcher-issued IPC credentials
        # (GPTBRIDGE_IPC_SESSION_TOKEN / GPTBRIDGE_IPC_PORT /
        # GPTBRIDGE_SHUTDOWN_TOKEN).  Without them there is no Information
        # Layer session to serve; fail closed with an actionable message
        # instead of pretending the tool is running.
        print(
            "AI_COLLABORATION 需要由 GPTBridge 治理啟動器簽發 IPC 工作階段"
            "（GPTBRIDGE_IPC_SESSION_TOKEN / GPTBRIDGE_IPC_PORT）。"
            "請從應用程式啟動器開啟外部協作工具。",
            file=sys.stderr,
        )
        return 2
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    import channel_runtime

    asyncio.run(channel_runtime.main())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
