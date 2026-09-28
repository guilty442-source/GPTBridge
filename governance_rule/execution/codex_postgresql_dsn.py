"""DSN resolution and shared constants for the PostgreSQL Codex authority.

Fail-closed boundary: both entry points raise unless the corresponding
environment variable is set — no silent fallback to another engine or to
superuser credentials.
"""

from __future__ import annotations

import os
import re
from typing import Final

CODEX_SCHEMA: Final[str] = "gptbridge_codex"
CODEX_AUTHORITY_URI: Final[str] = "postgresql://local/gptbridge_codex"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def runtime_dsn() -> str:
    value = os.environ.get("GPTBRIDGE_POSTGRES_DSN", "").strip()
    if not value:
        raise RuntimeError("GPTBRIDGE_POSTGRES_DSN_REQUIRED")
    return value


def admin_dsn() -> str:
    value = os.environ.get("GPTBRIDGE_POSTGRES_ADMIN_DSN", "").strip()
    if not value:
        raise RuntimeError("GPTBRIDGE_POSTGRES_ADMIN_DSN_REQUIRED")
    return value
