"""Certification Result (A369, A499, A449).

One machine-verifiable record per certified database version.  A version
enters production runtime only when ``certification_status == "PASS"`` —
every integrity/verification field must be truthy; a single ``False``
or missing (``None``) field makes the whole result FAIL.  Missing
evidence is never PASS (A449: incomplete evidence is not pass).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional


TEST_SUITE_VERSION = "chaos-matrix-v1"


@dataclass
class CertificationResult:
    """Unified certification record — the release gate for a DB version."""

    database_generation: int
    schema_version: str
    test_suite_version: str = TEST_SUITE_VERSION

    postgresql_integrity: Optional[bool] = None
    sqlite_integrity: Optional[bool] = None
    qdrant_integrity: Optional[bool] = None
    rls_verified: Optional[bool] = None
    migration_verified: Optional[bool] = None
    reconcile_verified: Optional[bool] = None
    restore_verified: Optional[bool] = None

    evidence: dict[str, Any] = field(default_factory=dict)
    certified_at: str = field(
        default_factory=lambda: time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
    )

    _VERIFIED_FIELDS = (
        "postgresql_integrity",
        "sqlite_integrity",
        "qdrant_integrity",
        "rls_verified",
        "migration_verified",
        "reconcile_verified",
        "restore_verified",
    )

    @property
    def certification_status(self) -> str:
        """PASS only when every verification field is explicitly True."""
        for name in self._VERIFIED_FIELDS:
            if getattr(self, name) is not True:
                return "FAIL"
        return "PASS"

    @property
    def missing(self) -> list[str]:
        return [
            name
            for name in self._VERIFIED_FIELDS
            if getattr(self, name) is not True
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "database_generation": self.database_generation,
            "schema_version": self.schema_version,
            "test_suite_version": self.test_suite_version,
            "postgresql_integrity": self.postgresql_integrity,
            "sqlite_integrity": self.sqlite_integrity,
            "qdrant_integrity": self.qdrant_integrity,
            "rls_verified": self.rls_verified,
            "migration_verified": self.migration_verified,
            "reconcile_verified": self.reconcile_verified,
            "restore_verified": self.restore_verified,
            "certification_status": self.certification_status,
            "missing": self.missing,
            "certified_at": self.certified_at,
            "evidence": self.evidence,
        }

    def digest(self) -> str:
        """Content hash of the certification payload (excluding evidence)."""
        payload = self.to_dict()
        payload.pop("evidence", None)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()


def build_certification(
    *,
    database_generation: int,
    schema_version: str,
    chaos_ok: bool | None = None,
    rls_ok: bool | None = None,
    migration_ok: bool | None = None,
    reconcile_ok: bool | None = None,
    restore_ok: bool | None = None,
    evidence: Optional[dict[str, Any]] = None,
) -> CertificationResult:
    """Assemble a result from subsystem outcomes.

    Mapping: chaos report covers all three engines; split it per-engine
    only when the caller provides per-engine booleans via ``evidence``.
    """
    result = CertificationResult(
        database_generation=database_generation,
        schema_version=schema_version,
        postgresql_integrity=chaos_ok,
        sqlite_integrity=chaos_ok,
        qdrant_integrity=chaos_ok,
        rls_verified=rls_ok,
        migration_verified=migration_ok,
        reconcile_verified=reconcile_ok,
        restore_verified=restore_ok,
        evidence=evidence or {},
    )
    return result


__all__ = [
    "CertificationResult",
    "TEST_SUITE_VERSION",
    "build_certification",
]
