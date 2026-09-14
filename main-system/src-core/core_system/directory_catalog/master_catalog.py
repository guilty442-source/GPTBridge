"""Directory Master Catalog ??A232 Implementation.

A232: MASTER-DIRECTORY: directory-master-catalog is the exhaustive registry of every governed directory class.

REQUIRED-DOMAINS:
- law-structure
- special-law
- provision-classification
- project-architecture
- identity
- permission
- information-layer
- startup
- runtime
- data
- resource
- integration
- release
- dependency
- health
- cleanup
- repair
- learning
- priority
- runtime-state
- automatic-log
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .master_catalog_populator import DirectoryCatalogPopulatorMixin

_logger = logging.getLogger("gptbridge.directory_catalog")


class DirectoryDomain(Enum):
    """A232 Required domains for directory master catalog."""
    LAW_STRUCTURE = "law-structure"
    SPECIAL_LAW = "special-law"
    PROVISION_CLASSIFICATION = "provision-classification"
    PROJECT_ARCHITECTURE = "project-architecture"
    IDENTITY = "identity"
    PERMISSION = "permission"
    INFORMATION_LAYER = "information-layer"
    STARTUP = "startup"
    RUNTIME = "runtime"
    DATA = "data"
    RESOURCE = "resource"
    INTEGRATION = "integration"
    RELEASE = "release"
    DEPENDENCY = "dependency"
    HEALTH = "health"
    CLEANUP = "cleanup"
    REPAIR = "repair"
    LEARNING = "learning"
    PRIORITY = "priority"
    RUNTIME_STATE = "runtime-state"
    AUTOMATIC_LOG = "automatic-log"


class DirectoryClass(Enum):
    """Directory classes per A222-A230."""
    FAULT_CODE = "fault-code"
    COMMAND_CODE = "command-code"
    MAINTENANCE_MANUAL = "maintenance-manual"
    TEST_FLOW = "test-flow"
    PROJECT_ARCHITECTURE = "project-architecture"
    # Additional classes from A244-A251
    RELATIONSHIP = "relationship"
    AUDIT = "audit"
    VERSION = "version"
    TOOL = "tool"
    CONTRACT = "contract"
    ARTIFACT = "artifact"
    BUILD = "build"
    CERTIFICATION = "certification"
    ACTIVATION = "activation"
    HOT_UPDATE = "hot-update"
    HOT_RELOAD = "hot-reload"
    ROLLBACK = "rollback"
    DEPENDENCY = "dependency"
    LICENSE = "license"
    PROVENANCE = "provenance"
    SBOM = "sbom"
    VULNERABILITY = "vulnerability"
    NETWORK_ACCESS = "network-access"
    EXTERNAL_SERVICE = "external-service"
    DOWNLOAD = "download"
    UPDATE = "update"
    AUDIT_EVENT = "audit-event"
    EVIDENCE = "evidence"
    TIMESTAMP = "timestamp"
    CLOCK_TRUST = "clock-trust"
    LINEAGE = "lineage"
    RETENTION = "retention"
    REDACTION = "redaction"
    VERIFICATION = "verification"
    UI_WINDOW = "ui-window"
    OFFICIAL_ENTRY = "official-entry"
    FRONTEND_PROJECTION = "frontend-projection"
    BACKEND_STATE = "backend-state"
    SESSION = "session"
    RECONNECT = "reconnect"
    RENDERER_RELEASE = "renderer-release"
    GIT_HISTORY = "git-history"
    INDEX = "index"
    WORKTREE = "worktree"
    COMMIT = "commit"
    MERGE = "merge"
    COORDINATOR_PUSH = "coordinator-push"
    HOOK = "hook"
    AUDIT_LOG = "audit-log"
    ROLLBACK_LOG = "rollback-log"
    REDACTED_OUTCOME = "redacted-outcome"
    LEARNING_EVIDENCE = "learning-evidence"
    CANDIDATE_GENERATION = "candidate-generation"
    EVALUATION = "evaluation"
    RETENTION_POLICY = "retention-policy"
    PROMOTION_BOUNDARY = "promotion-boundary"
    HEALTH_DETECTION = "health-detection"
    DIAGNOSIS = "diagnosis"
    CONTAINMENT = "containment"
    MAINTENANCE_MANUAL = "maintenance-manual"
    REPAIR_EXECUTION = "repair-execution"
    VERIFICATION = "verification"
    ROLLBACK_EXECUTION = "rollback-execution"
    ESCALATION = "escalation"
    PERSONAL_DATA = "personal-data"
    SENSITIVE_DATA = "sensitive-data"
    PRIVATE_DATA = "private-data"
    BIOMETRIC_DATA = "biometric-data"
    FINANCIAL_DATA = "financial-data"
    BEHAVIORAL_DATA = "behavioral-data"
    CRYPTO_ALGORITHM = "crypto-algorithm"
    KEY = "key"
    CERTIFICATE = "certificate"
    TOKEN = "token"
    SIGNATURE = "signature"
    ENCRYPTION = "encryption"
    ROTATION = "rotation"
    REVOCATION = "revocation"
    HARDWARE_HANDLE = "hardware-handle"
    BACKUP = "backup"
    RESTORE = "restore"
    DISASTER_RECOVERY = "disaster-recovery"
    CONTINUITY = "continuity"
    CHECKPOINT = "checkpoint"
    FAILOVER = "failover"
    REBUILD = "rebuild"
    RECONCILIATION = "reconciliation"
    UNIT_TEST = "unit-test"
    CONTRACT_TEST = "contract-test"
    INTEGRATION_TEST = "integration-test"
    SYSTEM_TEST = "system-test"
    FAILURE_INJECTION = "failure-injection"
    SECURITY_TEST = "security-test"
    PERFORMANCE_TEST = "performance-test"
    RELEASE_TEST = "release-test"
    MODEL_EVALUATION = "model-evaluation"
    LEGACY_PROVISION = "legacy-provision"
    SPECIAL_LAW = "special-law"
    DIRECTORY_SCHEMA = "directory-schema"
    CHINESE_CODEX = "chinese-codex"
    PERMISSION_DIR = "permission-dir"
    MASTER_CATALOG = "master-catalog"
    FORMAT_CONTRACT = "format-contract"
    FAULT_CODE_DIR = "fault-code-dir"
    COMMAND_CODE_DIR = "command-code-dir"
    MAINTENANCE_MANUAL_DIR = "maintenance-manual-dir"
    TEST_FLOW_DIR = "test-flow-dir"
    PROJECT_ARCH_DIR = "project-arch-dir"
    RELATIONSHIP_DIR = "relationship-dir"
    INFO_CHANNEL = "info-channel"
    SOVEREIGN_CHANNEL = "sovereign-channel"
    TOOL_CHANNEL = "tool-channel"
    EVENT_CHANNEL = "event-channel"
    STATE_CHANNEL = "state-channel"
    HEALTH_CHANNEL = "health-channel"
    STARTUP_CHANNEL = "startup-channel"
    REPAIR_CHANNEL = "repair-channel"


@dataclass(frozen=True)
class DirectoryEntry:
    """A232: Single directory catalog entry."""
    class_id: DirectoryClass
    domain: DirectoryDomain
    path: str
    schema_version: str
    identity_format: str  # e.g., "fault-code+canonical-name+owner"
    owner_sovereign: str
    access_control: str  # e.g., "permission-sovereign-decision"
    lifecycle: str  # e.g., "persistent", "ephemeral", "archived"
    validation_rules: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "active"  # active, deprecated, archived
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MasterCatalog:
    """A232: Directory Master Catalog - exhaustive registry of every governed directory class."""

    entries: dict[DirectoryClass, DirectoryEntry] = field(default_factory=dict)
    version: str = "1.0.0"
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def add_entry(self, entry: DirectoryEntry) -> None:
        """Add or update a directory entry."""
        self.entries[entry.class_id] = entry
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def get_entry(self, class_id: DirectoryClass) -> Optional[DirectoryEntry]:
        """Get a directory entry by class."""
        return self.entries.get(class_id)

    def get_entries_by_domain(self, domain: DirectoryDomain) -> list[DirectoryEntry]:
        """Get all entries for a domain."""
        return [e for e in self.entries.values() if e.domain == domain]

    def get_entries_by_owner(self, owner: str) -> list[DirectoryEntry]:
        """Get all entries owned by a sovereign."""
        return [e for e in self.entries.values() if e.owner_sovereign == owner]

    def validate_completeness(self) -> list[str]:
        """A231: Validate that all required domains are covered (CLOSURE-SCOPE)."""
        missing = []
        covered_domains = {e.domain for e in self.entries.values()}
        for domain in DirectoryDomain:
            if domain not in covered_domains:
                missing.append(f"Missing domain: {domain.value}")
        return missing

    def to_json(self) -> str:
        """Serialize catalog to JSON."""
        data = {
            "version": self.version,
            "updated_at": self.updated_at,
            "entries": {
                k.value: {
                    "class_id": v.class_id.value,
                    "domain": v.domain.value,
                    "path": v.path,
                    "schema_version": v.schema_version,
                    "identity_format": v.identity_format,
                    "owner_sovereign": v.owner_sovereign,
                    "access_control": v.access_control,
                    "lifecycle": v.lifecycle,
                    "validation_rules": v.validation_rules,
                    "created_at": v.created_at,
                    "updated_at": v.updated_at,
                    "status": v.status,
                    "metadata": v.metadata,
                }
                for k, v in self.entries.items()
            }
        }
        return json.dumps(data, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "MasterCatalog":
        """Deserialize catalog from JSON."""
        data = json.loads(json_str)
        catalog = cls(version=data.get("version", "1.0.0"))
        for class_id_str, entry_data in data.get("entries", {}).items():
            entry = DirectoryEntry(
                class_id=DirectoryClass(class_id_str),
                domain=DirectoryDomain(entry_data["domain"]),
                path=entry_data["path"],
                schema_version=entry_data["schema_version"],
                identity_format=entry_data["identity_format"],
                owner_sovereign=entry_data["owner_sovereign"],
                access_control=entry_data["access_control"],
                lifecycle=entry_data["lifecycle"],
                validation_rules=entry_data.get("validation_rules", []),
                created_at=entry_data.get("created_at", ""),
                updated_at=entry_data.get("updated_at", ""),
                status=entry_data.get("status", "active"),
                metadata=entry_data.get("metadata", {}),
            )
            catalog.entries[entry.class_id] = entry
        return catalog


class DirectoryMasterCatalog(DirectoryCatalogPopulatorMixin):
    """A232: Directory Master Catalog Service.

    The exhaustive registry of every governed directory class.
    Owned by permission-sovereign per A228.
    """

    REQUIRED_DOMAINS = set(DirectoryDomain)

    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.catalog = self._load_or_create()

    def _load_or_create(self) -> MasterCatalog:
        if self.store_path.exists():
            try:
                content = self.store_path.read_text(encoding="utf-8")
                return MasterCatalog.from_json(content)
            except Exception as exc:
                _logger.warning("Failed to load master catalog: %s", exc)

        # Create default catalog with all required domains
        catalog = MasterCatalog()
        self._populate_default_entries(catalog)
        self._save(catalog)
        return catalog

    def _save(self, catalog: MasterCatalog) -> None:
        """Save catalog to file."""
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(catalog.to_json(), encoding="utf-8")
        os.replace(tmp, self.store_path)

    def register_directory(
        self,
        class_id: DirectoryClass,
        domain: DirectoryDomain,
        path: str,
        schema_version: str,
        identity_format: str,
        owner_sovereign: str,
        access_control: str,
        lifecycle: str,
        validation_rules: list[str] = None,
        metadata: dict[str, Any] = None,
    ) -> DirectoryEntry:
        """Register a new directory class in the master catalog."""
        entry = DirectoryEntry(
            class_id=class_id,
            domain=domain,
            path=path,
            schema_version=schema_version,
            identity_format=identity_format,
            owner_sovereign=owner_sovereign,
            access_control=access_control,
            lifecycle=lifecycle,
            validation_rules=validation_rules or [],
            metadata=metadata or {},
        )
        self.catalog.add_entry(entry)
        self._save(self.catalog)
        _logger.info("DirectoryMasterCatalog: registered %s", class_id.value)
        return entry

    def get_directory(self, class_id: DirectoryClass) -> Optional[DirectoryEntry]:
        """Get directory entry by class."""
        return self.catalog.get_entry(class_id)

    def list_directories(
        self,
        domain: Optional[DirectoryDomain] = None,
        owner: Optional[str] = None,
        status: str = "active",
    ) -> list[DirectoryEntry]:
        """List directories with optional filters."""
        entries = list(self.catalog.entries.values())
        if domain:
            entries = [e for e in entries if e.domain == domain]
        if owner:
            entries = [e for e in entries if e.owner_sovereign == owner]
        if status:
            entries = [e for e in entries if e.status == status]
        return entries

    def validate_completeness(self) -> list[str]:
        """A231: Validate all required domains are covered (CLOSURE-SCOPE A231)."""
        return self.catalog.validate_completeness()

    def get_catalog_version(self) -> str:
        return self.catalog.version

    def export_json(self) -> str:
        return self.catalog.to_json()

def create_directory_master_catalog(store_path: Path) -> DirectoryMasterCatalog:
    """Factory function to create DirectoryMasterCatalog."""
    return DirectoryMasterCatalog(store_path)


__all__ = [
    "DirectoryDomain",
    "DirectoryClass",
    "DirectoryEntry",
    "MasterCatalog",
    "DirectoryMasterCatalog",
    "create_directory_master_catalog",
]