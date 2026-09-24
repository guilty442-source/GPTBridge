"""Provider adapter registry for embedded-browser collaboration.

Each adapter is the module-local contract for one external AI website:
which DOM selectors locate the composer, the send control, the streaming
response, and the "still generating" indicator; which hosts the provider
session is expected on; and which capture capabilities the adapter
supports.  Selector and URL details belong to this module contract — they
are never governance-codex content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


ADAPTER_VERSION = "1.1.0"

VERIFICATION_MARKERS = (
    "captcha",
    "cloudflare",
    "verify you are human",
    "checking your browser",
    "are you a robot",
    "請驗證",
    "驗證你是人類",
)


@dataclass(frozen=True)
class ProviderAdapter:
    """DOM-level adapter contract for one external AI provider."""

    provider_identity: str
    display_name: str
    input_selectors: tuple[str, ...]
    send_selectors: tuple[str, ...]
    response_selectors: tuple[str, ...]
    generating_selectors: tuple[str, ...] = field(default=())
    expected_hosts: tuple[str, ...] = ()
    send_capability: str = "dom-fill-submit"
    response_capture_capability: str = "dom-state-poll"
    adapter_version: str = ADAPTER_VERSION

    def matches_host(self, url: str) -> bool:
        """True when ``url`` belongs to one of this provider's hosts."""
        host = str(urlparse(str(url or "")).hostname or "").casefold()
        if not host:
            return False
        return any(
            host == expected or host.endswith(f".{expected}")
            for expected in self.expected_hosts
        )


PROVIDER_ADAPTERS: dict[str, ProviderAdapter] = {
    "chatgpt": ProviderAdapter(
        provider_identity="chatgpt",
        display_name="ChatGPT",
        input_selectors=("#prompt-textarea", '[contenteditable="true"]', "textarea"),
        send_selectors=(
            'button[data-testid="send-button"]',
            'button[data-testid*="send" i]',
            'button[aria-label*="Send" i]',
            'button[aria-label*="傳送" i]',
        ),
        response_selectors=('[data-message-author-role="assistant"]',),
        generating_selectors=(
            'button[data-testid="stop-button"]',
            'button[aria-label*="Stop" i]',
            'button[aria-label*="停止" i]',
        ),
        expected_hosts=("chatgpt.com", "chat.openai.com"),
    ),
    "claude": ProviderAdapter(
        provider_identity="claude",
        display_name="Claude",
        input_selectors=(
            'div[contenteditable="true"].ProseMirror',
            'div[contenteditable="true"]',
            "textarea",
        ),
        send_selectors=(
            'button[aria-label*="Send" i]',
            'button[data-testid*="send" i]',
        ),
        response_selectors=(
            '[data-testid*="assistant" i]',
            ".font-claude-response",
            '[class*="assistant" i]',
        ),
        generating_selectors=(
            'button[aria-label*="Stop" i]',
            '[data-testid*="stop" i]',
        ),
        expected_hosts=("claude.ai",),
    ),
    "gemini": ProviderAdapter(
        provider_identity="gemini",
        display_name="Gemini",
        input_selectors=(
            "rich-textarea .ql-editor",
            'div[contenteditable="true"]',
            "textarea",
        ),
        send_selectors=(
            "button.send-button",
            'button[aria-label*="Send" i]',
            'button[aria-label*="傳送"]',
        ),
        response_selectors=(
            "model-response",
            ".model-response-text",
            '[class*="response" i]',
        ),
        generating_selectors=(
            'button[aria-label*="Stop" i]',
            ".stop-button",
        ),
        expected_hosts=("gemini.google.com",),
    ),
    "grok": ProviderAdapter(
        provider_identity="grok",
        display_name="Grok",
        input_selectors=("textarea", 'div[contenteditable="true"]'),
        send_selectors=(
            'button[aria-label*="Submit" i]',
            'button[aria-label*="Send" i]',
            'button[type="submit"]',
        ),
        response_selectors=(
            '[data-testid*="assistant" i]',
            '[data-testid="message-bubble"]',
            '[class*="assistant" i]',
        ),
        generating_selectors=('button[aria-label*="Stop" i]',),
        expected_hosts=("grok.com", "x.ai"),
    ),
    "deepseek": ProviderAdapter(
        provider_identity="deepseek",
        display_name="DeepSeek",
        input_selectors=("textarea", 'div[contenteditable="true"]'),
        send_selectors=(
            'button[aria-label*="Send" i]',
            'button[type="submit"]',
        ),
        response_selectors=(".ds-markdown", '[class*="assistant" i]', ".markdown"),
        generating_selectors=('[class*="stop" i]',),
        expected_hosts=("chat.deepseek.com", "deepseek.com"),
    ),
    "perplexity": ProviderAdapter(
        provider_identity="perplexity",
        display_name="Perplexity",
        input_selectors=("textarea", 'div[contenteditable="true"]'),
        send_selectors=(
            'button[aria-label*="Submit" i]',
            'button[aria-label*="Send" i]',
            'button[type="submit"]',
        ),
        response_selectors=(
            '[data-testid*="answer" i]',
            '[class*="answer" i]',
            ".prose",
        ),
        generating_selectors=(
            'button[aria-label*="Stop" i]',
            '[class*="stop" i]',
        ),
        expected_hosts=("perplexity.ai", "www.perplexity.ai"),
    ),
}


def adapter_for(provider: str) -> ProviderAdapter | None:
    """Return the registered adapter for ``provider`` or None."""
    return PROVIDER_ADAPTERS.get(str(provider or "").strip().casefold())


__all__ = [
    "ADAPTER_VERSION",
    "PROVIDER_ADAPTERS",
    "ProviderAdapter",
    "VERIFICATION_MARKERS",
    "adapter_for",
]
