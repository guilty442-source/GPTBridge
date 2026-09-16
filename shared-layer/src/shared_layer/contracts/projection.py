"""Contract projections — one canonical spec → per-language type + wire
form + Python-side validator.

Six targets, all derived from the same TypeSpec:

    python      runtime model types (int/str/bytes/datetime/…)
    typescript  TS types; i64/u64 → bigint + decimal-string wire
    c           C ABI types — fixed-width only, never ``long``
    cpp         C++ private impl types — ``std::`` fixed-width, no ``bool``
    csharp      C# adapter types (UTF-16 string internal only)
    sql         PostgreSQL column types

Parity tests assert that every projection preserves range, presence and
wire semantics — not merely that a name exists.
"""
from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from typing import Any

from .types import (
    CanonicalKind,
    INT_RANGES,
    JS_SAFE_INTEGER,
    Presence,
    TypeSpec,
)
from .registry import ContractError

FORBIDDEN_C_TYPES = ("long", "size_t", "ssize_t", "HANDLE", "DWORD", "WPARAM")
FORBIDDEN_CPP_TYPES = ("bool", "std::string_view*", "long")

_KIND_PROJECTIONS: dict[CanonicalKind, dict[str, str]] = {
    CanonicalKind.I32: {
        "python": "int", "typescript": "number",
        "c": "int32_t", "cpp": "std::int32_t", "csharp": "int",
        "sql": "INTEGER",
    },
    CanonicalKind.U32: {
        "python": "int", "typescript": "number",
        "c": "uint32_t", "cpp": "std::uint32_t", "csharp": "uint",
        "sql": "INTEGER",
    },
    CanonicalKind.I64: {
        # number is deliberately absent — the full range is not JS-safe.
        "python": "int", "typescript": "bigint",
        "c": "int64_t", "cpp": "std::int64_t", "csharp": "long",
        "sql": "BIGINT",
    },
    CanonicalKind.U64: {
        "python": "int", "typescript": "bigint",
        "c": "uint64_t", "cpp": "std::uint64_t", "csharp": "ulong",
        "sql": "NUMERIC(20,0)",  # u64 exceeds signed BIGINT
    },
    CanonicalKind.F32: {
        "python": "float", "typescript": "number",
        "c": "float", "cpp": "float", "csharp": "float",
        "sql": "REAL",
    },
    CanonicalKind.F64: {
        "python": "float", "typescript": "number",
        "c": "double", "cpp": "double", "csharp": "double",
        "sql": "DOUBLE PRECISION",
    },
    CanonicalKind.BOOL: {
        # C ABI uses uint8_t — C++ bool is not a boundary type.
        "python": "bool", "typescript": "boolean",
        "c": "uint8_t", "cpp": "std::uint8_t", "csharp": "bool",
        "sql": "BOOLEAN",
    },
    CanonicalKind.STRING: {
        # UTF-8 on the wire; UTF-16 lives only inside the C# adapter.
        "python": "str", "typescript": "string",
        "c": "const char *", "cpp": "std::string_view", "csharp": "string",
        "sql": "TEXT",
    },
    CanonicalKind.BYTES: {
        "python": "bytes", "typescript": "Uint8Array",
        "c": "const uint8_t *", "cpp": "std::span<const std::uint8_t>",
        "csharp": "byte[]", "sql": "BYTEA",
    },
    CanonicalKind.TIMESTAMP_UTC: {
        "python": "datetime", "typescript": "string",  # ISO-8601 UTC
        "c": "int64_t", "cpp": "std::int64_t",  # epoch microseconds
        "csharp": "DateTimeOffset", "sql": "TIMESTAMPTZ",
    },
    CanonicalKind.DURATION: {
        # monotonic elapsed — i64 nanoseconds everywhere
        "python": "int", "typescript": "bigint",
        "c": "int64_t", "cpp": "std::int64_t", "csharp": "long",
        "sql": "BIGINT",
    },
    CanonicalKind.DEADLINE: {
        # monotonic instant — f64 seconds from a monotonic clock
        "python": "float", "typescript": "number",
        "c": "double", "cpp": "double", "csharp": "double",
        "sql": "DOUBLE PRECISION",
    },
    CanonicalKind.ENUM: {
        "python": "int", "typescript": "number",
        "c": "int32_t", "cpp": "std::int32_t", "csharp": "int",
        "sql": "INTEGER",
    },
    CanonicalKind.IDENTIFIER: {
        "python": "str", "typescript": "string",
        "c": "const char *", "cpp": "std::string_view",
        "csharp": "string", "sql": "TEXT",
    },
    CanonicalKind.LIST: {
        "python": "list", "typescript": "unknown[]",
        "c": "/* framed list */ const void *",
        "cpp": "std::span<const std::byte>",
        "csharp": "IReadOnlyList<object>", "sql": "JSONB",
    },
    CanonicalKind.OBJECT: {
        "python": "dict", "typescript": "Record<string, unknown>",
        "c": "/* framed object */ const void *",
        "cpp": "std::span<const std::byte>",
        "csharp": "IReadOnlyDictionary<string, object>", "sql": "JSONB",
    },
    CanonicalKind.TYPED_BUFFER: {
        # bounded binary frame — never a JSON number array
        "python": "memoryview", "typescript": "Uint8Array",
        "c": "const uint8_t *", "cpp": "std::span<const std::uint8_t>",
        "csharp": "byte[]", "sql": "BYTEA",
    },
}


def project(spec: TypeSpec, language: str) -> str:
    """Return the type expression for ``language`` honouring presence."""
    try:
        base = _KIND_PROJECTIONS[spec.kind][language]
    except KeyError:
        raise ContractError(f"no {language} projection for {spec.kind}")

    # i64/u64 guard: TypeScript must never project to ``number``.
    if language == "typescript" and spec.kind in {
        CanonicalKind.I64, CanonicalKind.U64,
    }:
        if base == "number":
            raise ContractError("i64/u64 may not project to TS number")

    if language == "typescript" and spec.presence is Presence.NULLABLE:
        return f"{base} | null"
    if language == "csharp" and spec.presence is Presence.NULLABLE:
        return f"{base}?"
    if language == "python" and spec.presence is Presence.NULLABLE:
        return f"Optional[{base}]"
    if language == "sql" and spec.presence is Presence.REQUIRED:
        return f"{base} NOT NULL"
    if language == "sql":
        return base
    return base


def check_no_forbidden_types(language: str) -> None:
    """Assert projections never emit platform-dependent types."""
    table = FORBIDDEN_C_TYPES if language == "c" else FORBIDDEN_CPP_TYPES
    for kind_map in _KIND_PROJECTIONS.values():
        expr = kind_map.get(language, "")
        for bad in table:
            if bad in expr:
                raise ContractError(
                    f"forbidden {language} contract type in "
                    f"{expr!r}: {bad}"
                )


# ---------------------------------------------------------------------------
# Python-side validation
# ---------------------------------------------------------------------------

MISSING = object()  # sentinel: field absent (≠ null, ≠ value)
_MISSING = MISSING


def validate(name: str, spec: TypeSpec, value: Any) -> Any:
    """Validate ``value`` against the canonical spec; returns the value.

    Presence is resolved BEFORE kind validation so missing, null and
    value remain three distinct states.
    """
    if value is _MISSING:
        if spec.presence is Presence.REQUIRED:
            raise ContractError(f"{name}: required field missing")
        if spec.presence is Presence.OPTIONAL:
            return spec.default
        raise ContractError(f"{name}: nullable field may not be absent")
    if value is None:
        if spec.presence is not Presence.NULLABLE:
            raise ContractError(f"{name}: null not permitted")
        return None

    kind = spec.kind
    if kind in INT_RANGES:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError(f"{name}: expected {kind.value}")
        lo, hi = INT_RANGES[kind]
        if not lo <= value <= hi:
            raise ContractError(f"{name}: {value} outside {kind.value}")
        return value
    if kind is CanonicalKind.F32 or kind is CanonicalKind.F64:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ContractError(f"{name}: expected {kind.value}")
        return float(value)
    if kind is CanonicalKind.BOOL:
        if not isinstance(value, bool):
            raise ContractError(f"{name}: expected bool")
        return value
    if kind in (CanonicalKind.STRING, CanonicalKind.IDENTIFIER):
        if not isinstance(value, str):
            raise ContractError(f"{name}: expected string")
        if spec.identifier is not None:
            if len(value) > spec.identifier.max_length:
                raise ContractError(f"{name}: identifier too long")
            if not re.fullmatch(spec.identifier.pattern, value):
                raise ContractError(f"{name}: invalid identifier {value!r}")
        value.encode("utf-8")  # UTF-8 encodability is the contract
        return value
    if kind is CanonicalKind.BYTES:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ContractError(f"{name}: expected bytes")
        return bytes(value)
    if kind is CanonicalKind.TIMESTAMP_UTC:
        if not isinstance(value, datetime):
            raise ContractError(f"{name}: expected datetime")
        if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
            raise ContractError(f"{name}: timestamp must be UTC-aware")
        return value
    if kind is CanonicalKind.DURATION:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError(f"{name}: duration is i64 nanoseconds")
        lo, hi = INT_RANGES[CanonicalKind.I64]
        if not lo <= value <= hi:
            raise ContractError(f"{name}: duration outside i64")
        return value
    if kind is CanonicalKind.DEADLINE:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ContractError(f"{name}: deadline is f64 monotonic seconds")
        return float(value)
    if kind is CanonicalKind.ENUM:
        assert spec.enum is not None
        valid = {v.value for v in spec.enum.values}
        if not isinstance(value, int) or value not in valid:
            raise ContractError(f"{name}: unknown enum ordinal {value!r}")
        return value
    if kind is CanonicalKind.LIST:
        if not isinstance(value, (list, tuple)):
            raise ContractError(f"{name}: expected list")
        if spec.element is not None:
            return [validate(name, spec.element, v) for v in value]
        return list(value)
    if kind is CanonicalKind.OBJECT:
        if not isinstance(value, dict):
            raise ContractError(f"{name}: expected object")
        if spec.fields:
            out: dict[str, Any] = {}
            for f in spec.fields:
                out[f.name] = validate(
                    f"{name}.{f.name}", f.spec, value.get(f.name, _MISSING)
                )
            return out
        return dict(value)
    if kind is CanonicalKind.TYPED_BUFFER:
        assert spec.buffer is not None
        raw = bytes(value) if isinstance(
            value, (bytes, bytearray, memoryview)
        ) else None
        if raw is None:
            raise ContractError(f"{name}: typed buffer must be binary")
        if len(raw) > spec.buffer.max_bytes:
            raise ContractError(
                f"{name}: buffer {len(raw)}B exceeds bound "
                f"{spec.buffer.max_bytes}B"
            )
        return raw
    raise ContractError(f"{name}: unhandled kind {kind}")


def wire_encode(spec: TypeSpec, value: Any) -> Any:
    """Canonical wire form — used by parity tests to pin serialization."""
    if value is None:
        return None
    kind = spec.kind
    if kind in (CanonicalKind.I64, CanonicalKind.U64):
        return str(value)                       # decimal string — JS-safe
    if kind is CanonicalKind.TIMESTAMP_UTC:
        assert isinstance(value, datetime)
        return value.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    if kind is CanonicalKind.DURATION:
        return value                            # i64 ns integer
    if kind is CanonicalKind.DEADLINE:
        return float(value)                     # f64 monotonic seconds
    if kind is CanonicalKind.ENUM:
        return int(value)                       # stable ordinal
    if kind is CanonicalKind.BYTES or kind is CanonicalKind.TYPED_BUFFER:
        return base64.b64encode(bytes(value)).decode("ascii")
    return value


def ts_wire_safe(spec: TypeSpec, value: Any) -> bool:
    """True iff the wire form survives a TypeScript round-trip losslessly."""
    encoded = wire_encode(spec, value)
    if spec.kind in (CanonicalKind.I64, CanonicalKind.U64):
        # decimal string round-trips losslessly through JSON
        return isinstance(encoded, str) and int(encoded) == value
    if isinstance(encoded, float) and spec.kind in INT_RANGES:
        return abs(encoded) <= JS_SAFE_INTEGER
    return True


__all__ = [
    "MISSING",
    "project",
    "validate",
    "wire_encode",
    "ts_wire_safe",
    "check_no_forbidden_types",
    "FORBIDDEN_C_TYPES",
    "FORBIDDEN_CPP_TYPES",
]
