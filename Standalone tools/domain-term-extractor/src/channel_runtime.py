from __future__ import annotations

from shared_layer.application.channel_runtime import BaseChannelRuntime


class DomainTermExtractorChannelRuntime(BaseChannelRuntime):
    TOOL_ID = "domain-term-extractor"