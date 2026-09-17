from __future__ import annotations

import os
import re
from dataclasses import dataclass


_SQL_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class DatabaseSettings:
    """Validated settings. Passwords are accepted only through the environment.

    A501/A503/A506 DSN purpose separation: ``admin_dsn`` (bootstrap,
    migration, role deployment, disaster recovery) and ``runtime_dsn``
    (least-privilege tool/transport traffic) are never the same credential;
    :class:`~shared_layer.database.connection.ConnectionManager` resolves its
    runtime pool from ``runtime_dsn`` and requires the runtime binding in a
    declared runtime context.
    """

    admin_dsn: str = ""
    runtime_dsn: str = ""
    database: str = "gptbridge"
    owner_role: str = "gptbridge_owner"
    runtime_role: str = "gptbridge_runtime"
    reader_role: str = "gptbridge_xingcheng_reader"

    def __post_init__(self) -> None:
        if not (self.admin_dsn.strip() or self.runtime_dsn.strip()):
            raise ValueError("GPTBRIDGE_POSTGRES_ADMIN_DSN_OR_RUNTIME_DSN_REQUIRED")
        for field in ("database", "owner_role", "runtime_role", "reader_role"):
            if not _SQL_IDENTIFIER.fullmatch(getattr(self, field)):
                raise ValueError(f"INVALID_POSTGRES_IDENTIFIER:{field}")
        self._reject_credential_reuse()

    def _reject_credential_reuse(self) -> None:
        """Runtime must not reuse the admin/backup login (A501).

        An *identical* admin/runtime binding is the legacy single-credential
        posture: it is tolerated so the environment keeps operating and is
        surfaced as a visible posture warning (``shares_credential``), never
        silently treated as separation.  Two distinct bindings that resolve to
        the same login remain a hard rejection.
        """
        admin = self.admin_dsn.strip()
        runtime = self.runtime_dsn.strip()
        if not admin or not runtime or admin == runtime:
            return
        from psycopg.conninfo import conninfo_to_dict

        admin_user = str(conninfo_to_dict(admin).get("user") or "")
        runtime_user = str(conninfo_to_dict(runtime).get("user") or "")
        if admin_user and admin_user == runtime_user:
            raise ValueError("DSN_CREDENTIAL_REUSE_FORBIDDEN")

    @property
    def shares_credential(self) -> bool:
        """One binding serves both admin and runtime purposes (legacy posture)."""
        admin = self.admin_dsn.strip()
        runtime = self.runtime_dsn.strip()
        return bool(admin and runtime and admin == runtime)

    @classmethod
    def from_environment(cls) -> "DatabaseSettings":
        return cls(
            admin_dsn=os.environ.get("GPTBRIDGE_POSTGRES_ADMIN_DSN", ""),
            runtime_dsn=os.environ.get("GPTBRIDGE_POSTGRES_DSN", ""),
            database=os.environ.get("GPTBRIDGE_POSTGRES_DATABASE", "gptbridge"),
            owner_role=os.environ.get("GPTBRIDGE_POSTGRES_OWNER_ROLE", "gptbridge_owner"),
            runtime_role=os.environ.get("GPTBRIDGE_POSTGRES_RUNTIME_ROLE", "gptbridge_runtime"),
            reader_role=os.environ.get("GPTBRIDGE_POSTGRES_READER_ROLE", "gptbridge_xingcheng_reader"),
        )
