from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

_ROOT = Path(
    os.environ.get(
        "GPTBRIDGE_GOVERNANCE_PROJECT_ROOT",
        str(Path(__file__).resolve().parents[3]),
    )
).resolve()
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared-layer" / "src"))

from backend.services.xingcheng.application.service import LocalAiService


async def main() -> None:
    service = LocalAiService(
        Path(__file__).resolve().parents[1], enable_transformer=True
    )
    _event, result = await service.handle("xingcheng_status", {})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
