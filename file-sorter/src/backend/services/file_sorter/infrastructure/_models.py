"""Data models and time utilities for the durable sorter engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ._constants import (
    DEFAULT_PLAN_TTL_SECONDS,
    SCHEMA_VERSION,
)


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    mtime_ns: int

    def to_dict(self) -> dict[str, int]:
        return {"size": self.size, "mtime_ns": self.mtime_ns}


@dataclass(frozen=True)
class _FileMetadata:
    """Metadata that must survive a staged, cross-volume transfer."""

    mtime_ns: int
    permissions: int
    file_attributes: int | None
    alternate_streams: tuple[tuple[str, int, str], ...] | None
    extended_attributes: tuple[tuple[str, str], ...] | None


@dataclass(frozen=True)
class StabilityResult:
    stable: bool
    reason: str | None
    fingerprint: FileFingerprint | None


@dataclass
class PlanOperation:
    operation_id: str
    source: str
    destination: str
    keyword: str
    folder: str
    rule_source: str
    source_size: int
    source_mtime_ns: int
    transfer: str
    status: str = "ready"
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "source": self.source,
            "destination": self.destination,
            "keyword": self.keyword,
            "folder": self.folder,
            "rule_source": self.rule_source,
            "source_size": self.source_size,
            "source_mtime_ns": self.source_mtime_ns,
            "transfer": self.transfer,
            "status": self.status,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanOperation":
        return cls(
            operation_id=str(value["operation_id"]),
            source=str(value["source"]),
            destination=str(value["destination"]),
            keyword=str(value.get("keyword", "")),
            folder=str(value.get("folder", "")),
            rule_source=str(value.get("rule_source", "custom")),
            source_size=int(value["source_size"]),
            source_mtime_ns=int(value["source_mtime_ns"]),
            transfer=str(value.get("transfer", "unknown")),
            status=str(value.get("status", "ready")),
            reason=(
                None
                if value.get("reason") is None
                else str(value.get("reason"))
            ),
        )


@dataclass
class SkippedFile:
    source: str
    category: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "category": self.category,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SkippedFile":
        return cls(
            source=str(value["source"]),
            category=str(value["category"]),
            reason=str(value["reason"]),
        )


@dataclass
class OrganizePlan:
    plan_id: str
    target_dir: str
    profile_id: str
    rules_revision: int
    quiet_seconds: float
    created_at: str = field(default_factory=lambda: _utc_now())
    expires_at: str = field(
        default_factory=lambda: _utc_after(DEFAULT_PLAN_TTL_SECONDS)
    )
    operations: list[PlanOperation] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self.skipped:
            counts[item.category] = counts.get(item.category, 0) + 1
        return {
            "ok": True,
            "type": "file-sorter-plan",
            "schema_version": SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "target_dir": self.target_dir,
            "profile_id": self.profile_id,
            "rules_revision": self.rules_revision,
            "quiet_seconds": self.quiet_seconds,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "operations": [item.to_dict() for item in self.operations],
            "skipped": [item.to_dict() for item in self.skipped],
            "summary": {
                "ready": len(self.operations),
                "skipped": len(self.skipped),
                "unmatched": counts.get("unmatched", 0),
                "unstable": counts.get("unstable", 0),
                "filtered": counts.get("filtered", 0),
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OrganizePlan":
        created_at = str(value.get("created_at", _utc_now()))
        return cls(
            plan_id=str(value["plan_id"]),
            target_dir=str(value["target_dir"]),
            profile_id=str(value["profile_id"]),
            rules_revision=int(value.get("rules_revision", 0)),
            quiet_seconds=float(value.get("quiet_seconds", 0.0)),
            created_at=created_at,
            expires_at=str(
                value.get("expires_at")
                or _utc_after(DEFAULT_PLAN_TTL_SECONDS, base=created_at)
            ),
            operations=[
                PlanOperation.from_dict(item)
                for item in value.get("operations", [])
            ],
            skipped=[
                SkippedFile.from_dict(item)
                for item in value.get("skipped", [])
            ],
        )


@dataclass(frozen=True)
class ProfileSnapshot:
    profile_id: str
    profile_name: str | None
    target_dir: str
    revision: int
    enabled: bool
    duplicate_trash_enabled: bool
    quiet_seconds: float
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    rules: tuple[dict[str, str], ...]
    path: Path
    migrated_from: str | None = None
    migration_required_review: bool = False
    migration_rejected_rule_count: int = 0

    def to_dict(self, *, include_rules: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "target": self.target_dir,
            "target_dir": self.target_dir,
            "revision": self.revision,
            "enabled": self.enabled,
            "duplicate_trash_enabled": self.duplicate_trash_enabled,
            "quiet_seconds": self.quiet_seconds,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "path": str(self.path),
            "migrated_from": self.migrated_from,
            "migration_required_review": self.migration_required_review,
            "migration_rejected_rule_count": self.migration_rejected_rule_count,
        }
        if include_rules:
            value["rules"] = [dict(item) for item in self.rules]
        return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_after(seconds: float, *, base: str | None = None) -> str:
    if base:
        try:
            base_time = datetime.fromisoformat(base.replace("Z", "+00:00"))
            if base_time.tzinfo is None:
                base_time = base_time.replace(tzinfo=timezone.utc)
        except ValueError:
            base_time = datetime.now(timezone.utc)
    else:
        base_time = datetime.now(timezone.utc)
    return datetime.fromtimestamp(
        base_time.timestamp() + max(0.0, seconds),
        timezone.utc,
    ).isoformat()


def _plan_expired(plan: OrganizePlan) -> bool:
    try:
        expires_at = datetime.fromisoformat(plan.expires_at.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except (AttributeError, ValueError):
        return True
    return expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc)
