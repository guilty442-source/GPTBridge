from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from shared_layer.application.bootstrap import bootstrap_tool

from .backend.services.domain_term_extractor.application.service import (
    DomainTermExtractorService,
)


async def main() -> int:
    return await bootstrap_tool(
        tool_id="domain-term-extractor",
        service_cls=DomainTermExtractorService,
        args=sys.argv[1:],
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))