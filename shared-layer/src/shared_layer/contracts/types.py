"""Canonical type model — language-neutral contract vocabulary.

One canonical kind per cross-language type.  Every spec carries:
range, presence (missing/null/value — never conflated), serialization
repr, and the per-language projection rules enforced by parity tests.

Hard rules encoded here:
- No platform-dependent types at any boundary (C ``long``, ``size_t``,
  raw Windows types are forbidden contract forms).
- i64/u64 cross TypeScript as ``bigint`` with a decimal-string wire form
  (JS ``number`` cannot represent the full range).
- bool crosses as ``uint8_t``/``BOOLEAN`` — never C++ ``bool`` at the ABI.
- Text is UTF-8 canonical; UTF-16 exists only inside the C# adapter.
- timestamp (UTC instant), duration (monotonic elapsed), deadline
  (monotonic instant) are three distinct semantics.
- Large binary/numeric payloads use bounded typed buffers, never
  expanded JSON number arrays.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Tuple


class CanonicalKind(str, Enum):
    I32 = "i32"
    U32 = "u32"
    I64 = "i64"
    U64 = "u64"
    F32 = "f32"
    F64 = "f64"
    BOOL = "bool"
    STRING = "string"          # UTF-8 canonical
    BYTES = "bytes"
    TIMESTAMP_UTC = "timestamp_utc"   # wall-clock instant, UTC
    DURATION = "duration"             # monotonic elapsed, i64 nanoseconds
    DEADLINE = "deadline"             # monotonic instant, f64 seconds
    ENUM = "enum"                     # stable explicit values + UNKNOWN
    IDENTIFIER = "identifier"         # machine key — never display text
    LIST = "list"
    OBJECT = "object"
    TYPED_BUFFER = "typed_buffer"     # bounded binary/numeric payload


class Presence(str, Enum):
    """Three distinct presence semantics — do not merge.

    REQUIRED:  key must exist AND hold a value (missing or null = error).
    NULLABLE:  key must exist; value may be explicitly null.
    OPTIONAL:  key may be absent; absence resolves to ``default``.
    """

    REQUIRED = "required"
    NULLABLE = "nullable"
    OPTIONAL = "optional"


class WireRepr(str, Enum):
    """Serialization representation on the wire."""

    NUMBER = "number"
    DECIMAL_STRING = "decimal_string"   # i64/u64 — JS-safe
    BOOL = "bool"
    STRING = "string"
    BASE64 = "base64"
    ISO8601_UTC = "iso8601_utc"
    INT_NS = "int_ns"                   # duration as i64 nanoseconds
    F64_SECONDS = "f64_seconds"         # deadline as monotonic seconds
    INT_VALUE = "int_value"             # enum stable ordinal
    JSON_ARRAY = "json_array"
    JSON_OBJECT = "json_object"
    BINARY_FRAME = "binary_frame"       # typed buffer — bounded, framed


INT_RANGES: dict[CanonicalKind, Tuple[int, int]] = {
    CanonicalKind.I32: (-(2**31), 2**31 - 1),
    CanonicalKind.U32: (0, 2**32 - 1),
    CanonicalKind.I64: (-(2**63), 2**63 - 1),
    CanonicalKind.U64: (0, 2**64 - 1),
}
JS_SAFE_INTEGER = 2**53 - 1  # beyond this JS ``number`` is lossy


@dataclass(frozen=True)
class TypeSpec:
    kind: CanonicalKind
    presence: Presence = Presence.REQUIRED
    wire: WireRepr = WireRepr.NUMBER
    default: Any = None                    # used only when OPTIONAL
    # kind-specific payloads
    element: Optional["TypeSpec"] = None   # LIST element type
    fields: Tuple["FieldSpec", ...] = ()   # OBJECT fields
    enum: Optional["EnumSpec"] = None      # ENUM values
    identifier: Optional["IdentifierSpec"] = None
    buffer: Optional["TypedBufferSpec"] = None

    @property
    def int_range(self) -> Optional[Tuple[int, int]]:
        return INT_RANGES.get(self.kind)


@dataclass(frozen=True)
class FieldSpec:
    name: str
    spec: TypeSpec


@dataclass(frozen=True)
class EnumValue:
    name: str
    value: int                            # stable explicit ordinal


@dataclass(frozen=True)
class EnumSpec:
    values: Tuple[EnumValue, ...]
    unknown: str = "UNKNOWN"              # always reserved, ordinal 0

    def __post_init__(self) -> None:
        names = [v.name for v in self.values]
        if self.unknown not in names:
            raise ValueError(f"enum must reserve {self.unknown}")
        if names[0] != self.unknown or self.values[0].value != 0:
            raise ValueError(f"{self.unknown} must be ordinal 0")
        ordinals = [v.value for v in self.values]
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("enum ordinals must be unique")


@dataclass(frozen=True)
class IdentifierSpec:
    """Machine identifier — strictly separated from display names."""
    pattern: str = r"^[a-z0-9][a-z0-9._:-]{0,127}$"
    max_length: int = 128
    display_name: bool = False            # identifiers are never display text


@dataclass(frozen=True)
class TypedBufferSpec:
    """Bounded binary/numeric buffer — never a JSON number array."""
    dtype: CanonicalKind                  # numeric kind or BYTES
    max_bytes: int
    rank: int = 1                         # flat=1, matrix=2
    nullable_elements: bool = False

    def __post_init__(self) -> None:
        if self.dtype not in {
            CanonicalKind.I32, CanonicalKind.U32,
            CanonicalKind.I64, CanonicalKind.U64,
            CanonicalKind.F32, CanonicalKind.F64,
            CanonicalKind.BYTES,
        }:
            raise ValueError("typed buffer dtype must be numeric or bytes")
        if self.max_bytes <= 0:
            raise ValueError("typed buffer must declare a byte bound")
        if self.nullable_elements:
            raise ValueError("typed buffer elements are never nullable")


@dataclass(frozen=True)
class ModuleIdentity:
    """Module identity for governance and channel communication."""
    module_id: str
    version: str = "1.0.0"
    instance_id: str = ""
    capabilities: tuple[str, ...] = ()


__all__ = [
    "CanonicalKind",
    "Presence",
    "WireRepr",
    "TypeSpec",
    "FieldSpec",
    "EnumValue",
    "EnumSpec",
    "IdentifierSpec",
    "TypedBufferSpec",
    "ModuleIdentity",
    "INT_RANGES",
    "JS_SAFE_INTEGER",
]
