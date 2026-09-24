"""Untrusted external content isolation.

Every byte an external AI returns is ``UNTRUSTED_EXTERNAL_CONTENT``.  It is
stored and displayed as data only — it is never converted into a shell
command, file mutation, git operation, database mutation, permission grant
or tool execution.  Action-looking text is surfaced only as a
pending-confirmation suggestion.
"""

from __future__ import annotations

import re
from typing import Any

EXTERNAL_CONTENT_CLASS = "UNTRUSTED_EXTERNAL_CONTENT"

# Lines that *look like* instructions to this system are quarantined into a
# suggestion list; they are never executed and never gain authority, no
# matter how many providers propose them.
_ACTION_HINT = re.compile(
    r"(?m)^\s*(?:[-*>\d.\)]+\s*)?(?:"
    r"```(?:bash|sh|shell|powershell|cmd|ps1)|"
    r"(?:rm|del|sudo|git\s+(?:push|reset|checkout|commit)|curl|wget|"
    r"invoke-|remove-item|drop\s+table|delete\s+from|execute|run)\b"
    r")",
    re.IGNORECASE,
)

MAX_EXTERNAL_TEXT_CHARS = 64_000


def seal_external_text(text: Any) -> str:
    """Return provider text bounded and marked safe to store as data."""
    value = str(text or "")[:MAX_EXTERNAL_TEXT_CHARS]
    return value


def suggested_actions(text: str) -> list[str]:
    """Extract action-looking lines as suggestions only.

    Suggestions carry no execution authority — a human must still route any
    chosen action through the normal permission / executor gates.
    """
    hints: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped and _ACTION_HINT.search(stripped):
            hints.append(stripped[:240])
        if len(hints) >= 16:
            break
    return hints


def seal_provider_response(
    provider_id: str,
    request_id: str,
    response_id: str,
    text: str,
    *,
    capture_method: str,
    completion_evidence: str,
    adapter_version: str,
    captured_at: str,
    status: str = "completed",
) -> dict[str, Any]:
    """Build the standard per-provider reply record (always untrusted)."""
    sealed = seal_external_text(text)
    return {
        "request_id": request_id,
        "provider_id": provider_id,
        "response_id": response_id,
        "response_text": sealed,
        "response_status": status,
        "capture_method": capture_method,
        "captured_at": captured_at,
        "completion_evidence": completion_evidence,
        "adapter_version": adapter_version,
        "content_class": EXTERNAL_CONTENT_CLASS,
        "suggested_actions": suggested_actions(sealed),
    }
