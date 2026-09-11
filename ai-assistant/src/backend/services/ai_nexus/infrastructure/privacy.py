from __future__ import annotations

import base64
import ctypes
import json
import os
import uuid
from ctypes import wintypes
from typing import Any


ENVELOPE_VERSION = 1
DATABASE_ENVELOPE_VERSION = 2
DPAPI_DESCRIPTION = "GPTBridge AI Investment Manager"
DEFAULT_ENTROPY = b"GPTBridge/AI-Investment-Manager/v2"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi_available() -> bool:
    return os.name == "nt"


def protect_bytes(data: bytes, *, entropy_value: bytes | None = None) -> bytes:
    if not _dpapi_available():
        raise OSError("DPAPI_UNAVAILABLE: encryption requires Windows DPAPI")
    source, source_buffer = _blob(data)
    entropy, entropy_buffer = _blob(entropy_value or DEFAULT_ENTROPY)
    target = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ok = crypt32.CryptProtectData(
        ctypes.byref(source),
        DPAPI_DESCRIPTION,
        ctypes.byref(entropy),
        None,
        None,
        0x01,
        ctypes.byref(target),
    )
    del source_buffer, entropy_buffer
    if not ok:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error))
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def unprotect_bytes(data: bytes, *, entropy_value: bytes | None = None) -> bytes:
    if not _dpapi_available():
        raise OSError("DPAPI_UNAVAILABLE: decryption requires Windows DPAPI")
    source, source_buffer = _blob(data)
    entropy, entropy_buffer = _blob(entropy_value or DEFAULT_ENTROPY)
    target = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        ctypes.byref(entropy),
        None,
        None,
        0x01,
        ctypes.byref(target),
    )
    del source_buffer, entropy_buffer
    if not ok:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error))
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def encode_json_document(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    protected = protect_bytes(encoded)
    envelope = {
        "envelope_version": ENVELOPE_VERSION,
        "protection": "windows-dpapi-current-user",
        "ciphertext": base64.b64encode(protected).decode("ascii"),
    }
    return json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"


def decode_json_document(text: str) -> dict[str, Any]:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("investment state must be a JSON object")
    if payload.get("protection") != "windows-dpapi-current-user":
        return payload
    ciphertext = base64.b64decode(str(payload.get("ciphertext") or ""), validate=True)
    decoded = unprotect_bytes(ciphertext)
    value = json.loads(decoded.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("decrypted investment state must be a JSON object")
    return value


def encode_binary_document(
    data: bytes,
    *,
    purpose: str,
    key_id: str | None = None,
) -> bytes:
    generation = key_id or uuid.uuid4().hex
    entropy = os.urandom(32)
    protected = protect_bytes(data, entropy_value=entropy)
    envelope = {
        "envelope_version": DATABASE_ENVELOPE_VERSION,
        "protection": "windows-dpapi-current-user",
        "purpose": str(purpose or "binary"),
        "key_id": generation,
        "entropy": base64.b64encode(entropy).decode("ascii"),
        "ciphertext": base64.b64encode(protected).decode("ascii"),
    }
    return (json.dumps(envelope, ensure_ascii=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def decode_binary_document(raw: bytes) -> tuple[bytes, dict[str, Any]]:
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid encrypted binary envelope") from exc
    if not isinstance(envelope, dict) or "ciphertext" not in envelope:
        raise ValueError("invalid encrypted binary envelope")
    entropy_text = str(envelope.get("entropy") or "")
    entropy = base64.b64decode(entropy_text, validate=True) if entropy_text else None
    ciphertext = base64.b64decode(str(envelope.get("ciphertext") or ""), validate=True)
    protection = str(envelope.get("protection") or "")
    if protection == "windows-dpapi-current-user":
        decoded = unprotect_bytes(ciphertext, entropy_value=entropy)
    else:
        raise ValueError(f"unsupported binary protection: {protection}")
    return decoded, envelope


def protect_text(value: str) -> str:
    text = str(value or "")
    if not text:
        return text
    encrypted = protect_bytes(text.encode("utf-8"))
    return "dpapi:" + base64.b64encode(encrypted).decode("ascii")


def unprotect_text(value: str) -> str:
    text = str(value or "")
    if not text.startswith("dpapi:"):
        return text
    encrypted = base64.b64decode(text[6:], validate=True)
    return unprotect_bytes(encrypted).decode("utf-8")


def privacy_status() -> dict[str, Any]:
    available = _dpapi_available()
    return {
        "state_encryption": "windows-dpapi-current-user" if available else "unavailable-fail-closed",
        "sensitive_field_encryption": "windows-dpapi-current-user" if available else "unavailable-fail-closed",
        "database_encryption": "windows-dpapi-whole-database" if available else "unavailable-fail-closed",
        "key_rotation": "dpapi-protection-generation" if available else "not-available",
        "platform_protected": available,
    }
