"""Directory Master Catalog Package — A232.

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

from .master_catalog import (
    DirectoryDomain,
    DirectoryClass,
    DirectoryEntry,
    MasterCatalog,
    DirectoryMasterCatalog,
    create_directory_master_catalog,
)

__all__ = [
    "DirectoryDomain",
    "DirectoryClass",
    "DirectoryEntry",
    "MasterCatalog",
    "DirectoryMasterCatalog",
    "create_directory_master_catalog",
]