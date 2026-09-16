"""CanonicalTypeRegistry — versioned, language-neutral type source.

The single authority for cross-language data contracts.  Registry content
is append-only per version: raising ``REGISTRY_VERSION`` is the only way
to change semantics, and parity tests pin every projection.
"""
from __future__ import annotations

from typing import Iterator

from .types import (
    CanonicalKind,
    EnumSpec,
    EnumValue,
    FieldSpec,
    IdentifierSpec,
    Presence,
    TypeSpec,
    TypedBufferSpec,
    WireRepr,
)

REGISTRY_VERSION = 1


def _t(kind: CanonicalKind, **kw) -> TypeSpec:
    return TypeSpec(kind=kind, **kw)


# ---------------------------------------------------------------------------
# Canonical scalar registrations
# ---------------------------------------------------------------------------

_CANONICAL: dict[str, TypeSpec] = {
    # Fixed-width integers — i64/u64 cross TypeScript as bigint with a
    # decimal-string wire form (JS number is lossy beyond 2**53).
    "i32": _t(CanonicalKind.I32, wire=WireRepr.NUMBER),
    "u32": _t(CanonicalKind.U32, wire=WireRepr.NUMBER),
    "i64": _t(CanonicalKind.I64, wire=WireRepr.DECIMAL_STRING),
    "u64": _t(CanonicalKind.U64, wire=WireRepr.DECIMAL_STRING),
    "f32": _t(CanonicalKind.F32, wire=WireRepr.NUMBER),
    "f64": _t(CanonicalKind.F64, wire=WireRepr.NUMBER),
    # bool: Python bool / TS boolean / C uint8_t / C# bool / SQL BOOLEAN.
    "bool": _t(CanonicalKind.BOOL, wire=WireRepr.BOOL),
    # UTF-8 canonical text; UTF-16 confined to the C# adapter internals.
    "string": _t(CanonicalKind.STRING, wire=WireRepr.STRING),
    "bytes": _t(CanonicalKind.BYTES, wire=WireRepr.BASE64),
    # Three distinct time semantics:
    #   timestamp_utc — wall-clock instant, ISO-8601 UTC / TIMESTAMPTZ
    #   duration      — monotonic elapsed, i64 nanoseconds
    #   deadline      — monotonic instant, f64 seconds (never wall clock)
    "timestamp_utc": _t(CanonicalKind.TIMESTAMP_UTC, wire=WireRepr.ISO8601_UTC),
    "duration": _t(CanonicalKind.DURATION, wire=WireRepr.INT_NS),
    "deadline": _t(CanonicalKind.DEADLINE, wire=WireRepr.F64_SECONDS),
    # Machine identifier — never carries display text.
    "identifier": _t(
        CanonicalKind.IDENTIFIER,
        wire=WireRepr.STRING,
        identifier=IdentifierSpec(),
    ),
    # Generic containers.
    "list": _t(CanonicalKind.LIST, wire=WireRepr.JSON_ARRAY),
    "object": _t(CanonicalKind.OBJECT, wire=WireRepr.JSON_OBJECT),
    # Bounded typed buffer — binary frame on the wire, never a JSON
    # number array.
    "typed_buffer": _t(
        CanonicalKind.TYPED_BUFFER,
        wire=WireRepr.BINARY_FRAME,
        buffer=TypedBufferSpec(dtype=CanonicalKind.U32, max_bytes=1 << 20),
    ),
}

# Common enum used by contract tests and downstream registrations.
STATUS_ENUM = EnumSpec(
    values=(
        EnumValue("UNKNOWN", 0),
        EnumValue("PENDING", 1),
        EnumValue("RUNNING", 2),
        EnumValue("SUCCEEDED", 3),
        EnumValue("FAILED", 4),
        EnumValue("CANCELLED", 5),
    )
)

_CANONICAL["status"] = _t(
    CanonicalKind.ENUM, wire=WireRepr.INT_VALUE, enum=STATUS_ENUM
)


class ContractError(ValueError):
    """Canonical contract violation."""


class CanonicalTypeRegistry:
    """Versioned registry; lookups are total — unknown names raise."""

    def __init__(self, version: int = REGISTRY_VERSION) -> None:
        self.version = version
        self._types = dict(_CANONICAL)

    def register(self, name: str, spec: TypeSpec) -> None:
        if not name or not name.isidentifier() or not name.islower():
            raise ContractError(f"invalid contract name: {name!r}")
        if name in self._types:
            raise ContractError(f"contract already registered: {name}")
        self._types[name] = spec

    def get(self, name: str) -> TypeSpec:
        try:
            return self._types[name]
        except KeyError:
            raise ContractError(f"unregistered contract type: {name}")

    def require(self, name: str, kind: CanonicalKind) -> TypeSpec:
        spec = self.get(name)
        if spec.kind is not kind:
            raise ContractError(f"{name}: expected {kind}, got {spec.kind}")
        return spec

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._types))

    def __iter__(self) -> Iterator[tuple[str, TypeSpec]]:
        for name in sorted(self._types):
            yield name, self._types[name]


DEFAULT_REGISTRY = CanonicalTypeRegistry()


__all__ = [
    "REGISTRY_VERSION",
    "ContractError",
    "CanonicalTypeRegistry",
    "DEFAULT_REGISTRY",
    "STATUS_ENUM",
]
