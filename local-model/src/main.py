from __future__ import annotations

import asyncio
import json
from pathlib import Path

from backend.services.local_ai.application.service import LocalAiService


async def main() -> None:
    service = LocalAiService(
        Path(__file__).resolve().parents[1], enable_transformer=True
    )
    _event, result = await service.handle("local_ai_status", {})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
