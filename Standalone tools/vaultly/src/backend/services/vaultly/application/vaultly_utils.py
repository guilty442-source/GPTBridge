from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


class VaultlyUtilsMixin:
    """Shared utility helpers used across all Vaultly service mixins."""

    @staticmethod
    def _short_error(error: BaseException | str, limit: int = 180) -> str:
        if isinstance(error, BaseException):
            message = str(error).strip() or error.__class__.__name__
        else:
            message = str(error).strip()
        message = re.sub(r"\s+", " ", message)
        if len(message) <= limit:
            return message
        return f"{message[: max(0, limit - 3)]}..."

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
