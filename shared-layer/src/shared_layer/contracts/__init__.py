"""Canonical cross-language type & data contracts.

The versioned ``CanonicalTypeRegistry`` is the single language-neutral
source from which Python models, TypeScript types, C ABI declarations,
C# interop mappings and SQL column types are projected.
"""
from .projection import (
    MISSING,
    check_no_forbidden_types,
    project,
    ts_wire_safe,
    validate,
    wire_encode,
)
from .registry import (
    DEFAULT_REGISTRY,
    REGISTRY_VERSION,
    CanonicalTypeRegistry,
    ContractError,
    STATUS_ENUM,
)
from .sql_presence import SqlPresence, read_cell, row_presence
from .types import (
    INT_RANGES,
    JS_SAFE_INTEGER,
    CanonicalKind,
    EnumSpec,
    EnumValue,
    FieldSpec,
    IdentifierSpec,
    ModuleIdentity,
    Presence,
    TypeSpec,
    TypedBufferSpec,
    WireRepr,
)

__all__ = [
    "CanonicalKind",
    "CanonicalTypeRegistry",
    "ContractError",
    "DEFAULT_REGISTRY",
    "EnumSpec",
    "EnumValue",
    "FieldSpec",
    "IdentifierSpec",
    "INT_RANGES",
    "JS_SAFE_INTEGER",
    "MISSING",
    "ModuleIdentity",
    "Presence",
    "REGISTRY_VERSION",
    "STATUS_ENUM",
    "SqlPresence",
    "TypeSpec",
    "TypedBufferSpec",
    "WireRepr",
    "check_no_forbidden_types",
    "project",
    "read_cell",
    "row_presence",
    "ts_wire_safe",
    "validate",
    "wire_encode",
]
