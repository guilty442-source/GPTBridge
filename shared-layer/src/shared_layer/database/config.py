from __future__ import annotations

import os
import re
from dataclasses import dataclass


_SQL_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class DatabaseSettings:
    """Validated settings. Passwords are accepted only through the environment."""

    admin_dsn: str
    database: str = "gptbridge"
    owner_role: str = "gptbridge_owner"
    runtime_role: str = "gptbridge_runtime"
    reader_role: str = "gptbridge_xingcheng_reader"

    def __post_init__(self) -> None:
        if not self.admin_dsn.strip():
            raise ValueError("GPTBRIDGE_POSTGRES_ADMIN_DSN_REQUIRED")
        for field in ("database", "owner_role", "runtime_role", "reader_role"):
            if not _SQL_IDENTIFIER.fullmatch(getattr(self, field)):
                raise ValueError(f"INVALID_POSTGRES_IDENTIFIER:{field}")

    @classmethod
    def from_environment(cls) -> "DatabaseSettings":
        return cls(
            admin_dsn=os.environ.get("GPTBRIDGE_POSTGRES_ADMIN_DSN", ""),
            database=os.environ.get("GPTBRIDGE_POSTGRES_DATABASE", "gptbridge"),
            owner_role=os.environ.get("GPTBRIDGE_POSTGRES_OWNER_ROLE", "gptbridge_owner"),
            runtime_role=os.environ.get("GPTBRIDGE_POSTGRES_RUNTIME_ROLE", "gptbridge_runtime"),
            reader_role=os.environ.get("GPTBRIDGE_POSTGRES_READER_ROLE", "gptbridge_xingcheng_reader"),
        )
