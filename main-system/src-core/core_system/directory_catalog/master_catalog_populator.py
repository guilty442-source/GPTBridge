"""Default-entry population mixin for DirectoryMasterCatalog (A185 split).

Contains the 13 domain-specific `_populate_*` methods that seed the
catalog with all required A222-A251 directory classes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .master_catalog import MasterCatalog, DirectoryEntry, DirectoryClass, DirectoryDomain


class DirectoryCatalogPopulatorMixin:
    """Default-entry population methods for DirectoryMasterCatalog."""

    def _populate_default_entries(self, catalog: "MasterCatalog") -> None:
        """Populate catalog with all required A222-A251 directory classes."""
        self._populate_law_structure(catalog)
        self._populate_project_architecture(catalog)
        self._populate_identity_permission(catalog)
        self._populate_information_layer(catalog)
        self._populate_startup_runtime(catalog)
        self._populate_data_git(catalog)
        self._populate_models_resources(catalog)
        self._populate_tools_artifacts(catalog)
        self._populate_dependencies(catalog)
        self._populate_audit_evidence(catalog)
        self._populate_ui_official(catalog)
        self._populate_git_history(catalog)
        self._populate_version_release(catalog)
        self._populate_health(catalog)
        self._populate_tests(catalog)

    def _populate_law_structure(self, catalog: "MasterCatalog") -> None:
        """A223, A224, A225: Fault Code, Command Code, Maintenance Manual."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.FAULT_CODE,
            domain=DirectoryDomain.LAW_STRUCTURE,
            path="E:/GPTBridge/governance/fault-codes",
            schema_version="1.0",
            identity_format="fault-code+canonical-name+owner",
            owner_sovereign="permission-sovereign",
            access_control="permission-sovereign-decision",
            lifecycle="persistent",
            validation_rules=["fault-code+canonical-name+owner+severity+remedy+decision-basis"],
        ))
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.COMMAND_CODE,
            domain=DirectoryDomain.PROVISION_CLASSIFICATION,
            path="E:/GPTBridge/governance/command-codes",
            schema_version="1.0",
            identity_format="command-code+canonical-command-id/name+owner",
            owner_sovereign="permission-sovereign",
            access_control="permission-sovereign-decision",
            lifecycle="persistent",
            validation_rules=["command-code+canonical-command-id/name+owner+action+schema"],
        ))
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.MAINTENANCE_MANUAL,
            domain=DirectoryDomain.SPECIAL_LAW,
            path="E:/GPTBridge/governance/maintenance-manuals",
            schema_version="1.0",
            identity_format="manual-code+canonical-name+owner",
            owner_sovereign="permission-sovereign",
            access_control="permission-sovereign-decision",
            lifecycle="persistent",
            validation_rules=["manual-code+canonical-name+owner+scope+steps+rollback"],
        ))

    def _populate_project_architecture(self, catalog: "MasterCatalog") -> None:
        """A226, A227: Test Flow, Project Architecture."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.TEST_FLOW,
            domain=DirectoryDomain.PROJECT_ARCHITECTURE,
            path="E:/GPTBridge/governance/test-flows",
            schema_version="1.0",
            identity_format="test-flow-code+canonical-name+owner",
            owner_sovereign="permission-sovereign",
            access_control="permission-sovereign-decision",
            lifecycle="persistent",
            validation_rules=["test-flow-code+canonical-name+owner+requirement-ids+evidence"],
        ))
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.PROJECT_ARCHITECTURE,
            domain=DirectoryDomain.PROJECT_ARCHITECTURE,
            path="E:/GPTBridge/docs/architecture",
            schema_version="1.0",
            identity_format="architecture-code+canonical-name+owner",
            owner_sovereign="permission-sovereign",
            access_control="permission-sovereign-decision",
            lifecycle="persistent",
            validation_rules=["architecture-code+canonical-name+owner+layers+boundaries"],
        ))

    def _populate_identity_permission(self, catalog: "MasterCatalog") -> None:
        """A235: Identity/permission directories."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.IDENTITY,
            domain=DirectoryDomain.IDENTITY,
            path="E:/GPTBridge/governance/identities",
            schema_version="1.0",
            identity_format="identity-group+canonical-name+owner",
            owner_sovereign="permission-sovereign",
            access_control="permission-sovereign-decision",
            lifecycle="persistent",
        ))

    def _populate_information_layer(self, catalog: "MasterCatalog") -> None:
        """A326: Information layer channels."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.INFO_CHANNEL,
            domain=DirectoryDomain.INFORMATION_LAYER,
            path="E:/GPTBridge/main-system/information-layer/channels",
            schema_version="1.0",
            identity_format="channel-id+type+owner",
            owner_sovereign="automation-sovereign",
            access_control="automation-sovereign-decision",
            lifecycle="ephemeral",
        ))

    def _populate_startup_runtime(self, catalog: "MasterCatalog") -> None:
        """A237: Startup/runtime directories."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.STARTUP,
            domain=DirectoryDomain.STARTUP,
            path="E:/GPTBridge/main-system/startup",
            schema_version="1.0",
            identity_format="startup-phase+order+owner",
            owner_sovereign="runtime-sovereign",
            access_control="runtime-sovereign-decision",
            lifecycle="persistent",
        ))

    def _populate_data_git(self, catalog: "MasterCatalog") -> None:
        """A238: Git/SQL directories."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.GIT_HISTORY,
            domain=DirectoryDomain.DATA,
            path="E:/GPTBridge/.git",
            schema_version="1.0",
            identity_format="commit-hash+author+timestamp",
            owner_sovereign="automation-sovereign",
            access_control="automation-sovereign-decision",
            lifecycle="persistent",
        ))

    def _populate_models_resources(self, catalog: "MasterCatalog") -> None:
        """A239: Models/GPU/CPU/Memory."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.MODEL,
            domain=DirectoryDomain.RESOURCE,
            path="E:/GPTBridge/Standalone tools/local-model",
            schema_version="1.0",
            identity_format="model-id+version+owner",
            owner_sovereign="automation-sovereign",
            access_control="automation-sovereign",
            lifecycle="persistent",
        ))

    def _populate_tools_artifacts(self, catalog: "MasterCatalog") -> None:
        """A241: Application/tool/contract/artifact directories."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.TOOL,
            domain=DirectoryDomain.TOOL,
            path="E:/GPTBridge/Standalone tools",
            schema_version="1.0",
            identity_format="tool-id+runtime+owner",
            owner_sovereign="automation-sovereign",
            access_control="automation-sovereign",
            lifecycle="persistent",
        ))

    def _populate_dependencies(self, catalog: "MasterCatalog") -> None:
        """A242: Dependencies/licenses/provenance."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.DEPENDENCY,
            domain=DirectoryDomain.DEPENDENCY,
            path="E:/GPTBridge/governance/dependencies",
            schema_version="1.0",
            identity_format="dependency-id+version+license+owner",
            owner_sovereign="automation-sovereign",
            access_control="automation-sovereign",
            lifecycle="persistent",
        ))

    def _populate_audit_evidence(self, catalog: "MasterCatalog") -> None:
        """A243: Audit events/evidence/timestamps."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.AUDIT_EVENT,
            domain=DirectoryDomain.AUDIT,
            path="E:/GPTBridge/main-system/runtime/audit",
            schema_version="1.0",
            identity_format="audit-event+timestamp+actor+action",
            owner_sovereign="decision-sovereign",
            access_control="decision-sovereign",
            lifecycle="persistent",
        ))

    def _populate_ui_official(self, catalog: "MasterCatalog") -> None:
        """A244: UI/official entries."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.UI_WINDOW,
            domain=DirectoryDomain.INFORMATION_LAYER,
            path="E:/GPTBridge/main-system/src-ui",
            schema_version="1.0",
            identity_format="window-id+session+owner",
            owner_sovereign="runtime-sovereign",
            access_control="permission-sovereign",
            lifecycle="ephemeral",
        ))

    def _populate_git_history(self, catalog: "MasterCatalog") -> None:
        """A245: Git history."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.GIT_HISTORY,
            domain=DirectoryDomain.DATA,
            path="E:/GPTBridge",
            schema_version="1.0",
            identity_format="commit+branch+author+timestamp",
            owner_sovereign="automation-sovereign",
            access_control="automation-sovereign-decision",
            lifecycle="persistent",
        ))

    def _populate_version_release(self, catalog: "MasterCatalog") -> None:
        """A246: Version/release directories."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.VERSION,
            domain=DirectoryDomain.RELEASE,
            path="E:/GPTBridge/main-system/runtime/release",
            schema_version="1.0",
            identity_format="release-id+application-version+artifact-root+contract",
            owner_sovereign="automation-sovereign",
            access_control="automation-sovereign",
            lifecycle="persistent",
        ))

    def _populate_health(self, catalog: "MasterCatalog") -> None:
        """A247: Health/detection/diagnosis."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.HEALTH_DETECTION,
            domain=DirectoryDomain.HEALTH,
            path="E:/GPTBridge/main-system/runtime/health",
            schema_version="1.0",
            identity_format="health-signal+severity+component+owner",
            owner_sovereign="decision-sovereign",
            access_control="decision-sovereign",
            lifecycle="ephemeral",
        ))

    def _populate_tests(self, catalog: "MasterCatalog") -> None:
        """A251: Test directories."""
        from .master_catalog import DirectoryClass, DirectoryDomain, DirectoryEntry
        catalog.add_entry(DirectoryEntry(
            class_id=DirectoryClass.UNIT_TEST,
            domain=DirectoryDomain.PROJECT_ARCHITECTURE,
            path="E:/GPTBridge/main-system/tests",
            schema_version="1.0",
            identity_format="test-suite+test-case+owner",
            owner_sovereign="permission-sovereign",
            access_control="permission-sovereign-decision",
            lifecycle="persistent",
        ))


__all__ = ["DirectoryCatalogPopulatorMixin"]
