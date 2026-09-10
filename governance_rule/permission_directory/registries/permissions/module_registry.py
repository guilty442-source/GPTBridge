"""Source-structure module registry — A185/E160 MANAGED-DIRECTORIES.

Per A185/E160: ``MANAGED-DIRECTORIES:module-registry+source-owner-registry+
canonical-folder/layer-registry+top-level-root-allowlist+tool-source-root-
registry+test-source-registry+generated-artifact-registry+source-size-
exception-registry`` and ``DIRECTORY-OWNER:permission-sovereign exclusively
manages operational source-structure-directories``.

This registry lists all codex-implementation submodules in
``main-system/src-core/core_system/`` that were split to comply with
A185/E160 source-size limits.  Each entry records the canonical path,
module group, role, codex basis, and sovereign owner.

This is a declarative registry only — it contains no executable logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class ModuleRegistryEntry:
    """A single module registry entry (A185: module-registry)."""

    path: str
    module_group: str
    role: str
    codex_basis: str
    owner: str


MODULE_REGISTRY: Final[tuple[ModuleRegistryEntry, ...]] = (
    # active_release (A181/E156, A182/E157)
    ModuleRegistryEntry("main-system/src-core/core_system/active_release.py", "active_release", "facade", "A181/E156+A182/E157", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/active_release_types.py", "active_release", "types", "A181/E156+A182/E157", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/active_release_persistence.py", "active_release", "persistence", "A181/E156", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/active_release_ledger.py", "active_release", "ledger", "A181/E156", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/active_release_verify.py", "active_release", "verify", "A181/E156", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/active_release_status.py", "active_release", "status", "A181/E156", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/active_release_mismatch.py", "active_release", "mismatch", "A182/E157", "permission-sovereign"),
    # tool_separation (A184/E159)
    ModuleRegistryEntry("main-system/src-core/core_system/tool_separation.py", "tool_separation", "facade", "A184/E159", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/tool_separation_types.py", "tool_separation", "types", "A184/E159", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/tool_separation_verify.py", "tool_separation", "verify", "A184/E159", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/tool_separation_signal.py", "tool_separation", "signal", "A184/E159", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/tool_separation_aggregate.py", "tool_separation", "aggregate", "A184/E159", "permission-sovereign"),
    # source_size (A185/E160)
    ModuleRegistryEntry("main-system/src-core/core_system/source_size.py", "source_size", "facade", "A185/E160", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/source_size_types.py", "source_size", "types", "A185/E160", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/source_size_report.py", "source_size", "report", "A185/E160", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/source_size_measure.py", "source_size", "measure", "A185/E160", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/source_size_verify.py", "source_size", "verify", "A185/E160", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/source_size_signal.py", "source_size", "signal", "A185/E160", "permission-sovereign"),
    # view_access (A186/E161)
    ModuleRegistryEntry("main-system/src-core/core_system/view_access.py", "view_access", "facade", "A186/E161", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/view_access_types.py", "view_access", "types", "A186/E161", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/view_access_verify.py", "view_access", "verify", "A186/E161", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/view_access_signal.py", "view_access", "signal", "A186/E161", "permission-sovereign"),
    # validation_chain (A187/E162)
    ModuleRegistryEntry("main-system/src-core/core_system/validation_chain.py", "validation_chain", "facade", "A187/E162", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/validation_chain_types.py", "validation_chain", "types", "A187/E162", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/validation_chain_verify.py", "validation_chain", "verify", "A187/E162", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/validation_chain_signal.py", "validation_chain", "signal", "A187/E162", "permission-sovereign"),
    # sovereign_collaboration (A188/E163)
    ModuleRegistryEntry("main-system/src-core/core_system/sovereign_collaboration.py", "sovereign_collaboration", "facade", "A188/E163", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/sovereign_collaboration_types.py", "sovereign_collaboration", "types", "A188/E163", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/sovereign_collaboration_verify.py", "sovereign_collaboration", "verify", "A188/E163", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/sovereign_collaboration_signal.py", "sovereign_collaboration", "signal", "A188/E163", "permission-sovereign"),
    # xingcheng_channel (A189/E164)
    ModuleRegistryEntry("main-system/src-core/core_system/xingcheng_channel.py", "xingcheng_channel", "facade", "A189/E164", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/xingcheng_channel_types.py", "xingcheng_channel", "types", "A189/E164", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/xingcheng_channel_verify.py", "xingcheng_channel", "verify", "A189/E164", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/xingcheng_channel_signal.py", "xingcheng_channel", "signal", "A189/E164", "permission-sovereign"),
    # governed_startup (A191/E166, A192/E167)
    ModuleRegistryEntry("main-system/src-core/core_system/governed_startup.py", "governed_startup", "facade", "A191/E166+A192/E167", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/governed_startup_types.py", "governed_startup", "types", "A191/E166+A192/E167", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/governed_startup_verify.py", "governed_startup", "verify", "A191/E166+A192/E167", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/governed_startup_signal.py", "governed_startup", "signal", "A191/E166+A192/E167", "permission-sovereign"),
    # startup_lifecycle (A193-A196/E168-E171)
    ModuleRegistryEntry("main-system/src-core/core_system/startup_lifecycle.py", "startup_lifecycle", "facade", "A193-A196/E168-E171", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/startup_lifecycle_types.py", "startup_lifecycle", "types", "A193-A196/E168-E171", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/startup_lifecycle_verify.py", "startup_lifecycle", "verify", "A193-A196/E168-E171", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/startup_lifecycle_signal.py", "startup_lifecycle", "signal", "A193-A196/E168-E171", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/startup_lifecycle_sync.py", "startup_lifecycle", "sync", "A195-A196/E170-E171", "permission-sovereign"),
    # third_party_governance (A197-A199/E171-E173)
    ModuleRegistryEntry("main-system/src-core/core_system/third_party_governance.py", "third_party_governance", "facade", "A197-A199/E171-E173", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/third_party_types.py", "third_party_governance", "types", "A197-A199/E171-E173", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/third_party_verify.py", "third_party_governance", "verify", "A197-A199/E171-E173", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/third_party_signal.py", "third_party_governance", "signal", "A199/E173", "permission-sovereign"),
    # root_containment (A201-A202/E175-E176)
    ModuleRegistryEntry("main-system/src-core/core_system/root_containment.py", "root_containment", "facade", "A201-A202/E175-E176", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/root_containment_types.py", "root_containment", "types", "A201-A202/E175-E176", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/root_containment_verify.py", "root_containment", "verify", "A201-A202/E175-E176", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/core_system/root_containment_signal.py", "root_containment", "signal", "A201-A202/E175-E176", "permission-sovereign"),
    # toolbox_process (A184/E159 — tool process detection and manifest listing)
    ModuleRegistryEntry("main-system/src-core/tasks/tool_process_registry.py", "toolbox_process", "persistence", "A184/E159", "permission-sovereign"),
    ModuleRegistryEntry("main-system/src-core/tasks/toolbox_manifest.py", "toolbox_process", "facade", "A184/E159", "permission-sovereign"),
)


def module_registry_snapshot() -> dict[str, object]:
    """Return a snapshot of the module registry for directory status."""
    groups: dict[str, list[str]] = {}
    for entry in MODULE_REGISTRY:
        groups.setdefault(entry.module_group, []).append(entry.path)
    return {
        "authority": "permission-sovereign",
        "directory_entry": "permission-directory://source-structure",
        "basis": "A185/E160",
        "total_modules": len(MODULE_REGISTRY),
        "module_groups": len(groups),
        "groups": {k: sorted(v) for k, v in sorted(groups.items())},
    }


__all__ = (
    "MODULE_REGISTRY",
    "ModuleRegistryEntry",
    "module_registry_snapshot",
)
