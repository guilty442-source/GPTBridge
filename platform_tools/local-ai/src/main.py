from __future__ import annotations

import asyncio
import json
from pathlib import Path

from backend.services.local_ai.service import LocalAiService


async def main() -> None:
    service = LocalAiService(Path(__file__).resolve().parents[1])
    _event, result = await service.handle("local_ai_status", {})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
