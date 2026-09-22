"""Windows Credential Manager secret store (G89).

Secrets live in the per-user Credential Manager vault — never in the
database, environment, or release payloads.  Callers reference a secret by
target suffix (``postgres/dsn/runtime``); the full target is namespaced as
``GPTBridge/<suffix>`` so vault entries stay attributable and enumerable.

Resolution contract used by ``dsn_policy.resolve_dsn``:

- env value ``credman:GPTBridge/<suffix>`` → resolved through this store,
- canonical lookup ``GPTBridge/postgres/dsn/<purpose>`` → used when the env
  var is absent,
- plain env value → literal DSN (development / CI fallback only).

Only the metadata registry (``secrets.py``) may record fingerprints; the
plaintext never crosses the trust boundary.
"""

from __future__ import annotations

import os
import sys
from typing import Final

CREDENTIAL_TARGET_PREFIX: Final[str] = "GPTBridge/"
CREDMAN_SCHEME: Final[str] = "credman:"
DSN_TARGET_TEMPLATE: Final[str] = "postgres/dsn/{purpose}"


class CredentialStoreError(RuntimeError):
    """Raised when the governed credential store cannot satisfy a request."""


def _platform_supported() -> bool:
    return sys.platform == "win32"


def credential_target(suffix: str) -> str:
    suffix = str(suffix or "").strip().strip("/")
    if not suffix:
        raise CredentialStoreError("CREDENTIAL_SUFFIX_EMPTY")
    return CREDENTIAL_TARGET_PREFIX + suffix


def is_credential_reference(value: str) -> bool:
    return str(value or "").strip().casefold().startswith(CREDMAN_SCHEME)


def _cred_win32():
    try:
        import win32cred  # type: ignore
    except ImportError as exc:  # pragma: no cover - platform guard
        raise CredentialStoreError(
            "CREDENTIAL_BACKEND_UNAVAILABLE:pywin32"
        ) from exc
    return win32cred


def store_secret(suffix: str, secret: str, *, comment: str = "") -> str:
    """Persist ``secret`` under ``GPTBridge/<suffix>``; returns the target."""
    if not _platform_supported():
        raise CredentialStoreError("CREDENTIAL_STORE_WINDOWS_ONLY")
    target = credential_target(suffix)
    win32cred = _cred_win32()
    credential = {
        "Type": win32cred.CRED_TYPE_GENERIC,
        "TargetName": target,
        # pywin32 marshals str CredentialBlob as UTF-16 automatically.
        "CredentialBlob": secret,
        "Comment": comment or "GPTBridge governed secret",
        "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        "UserName": "gptbridge",
    }
    try:
        win32cred.CredWrite(credential, 0)
    except Exception as exc:  # noqa: BLE001
        raise CredentialStoreError(f"CREDENTIAL_WRITE_FAILED:{exc}") from exc
    return target


def read_secret(suffix: str) -> str | None:
    """Read one secret; ``None`` when the target does not exist."""
    if not _platform_supported():
        raise CredentialStoreError("CREDENTIAL_STORE_WINDOWS_ONLY")
    win32cred = _cred_win32()
    target = credential_target(suffix)
    try:
        credential = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "1168" in message or "not found" in message.casefold():
            return None
        raise CredentialStoreError(f"CREDENTIAL_READ_FAILED:{exc}") from exc
    blob = credential.get("CredentialBlob")
    if blob is None:
        return None
    raw = bytes(blob)
    try:
        return raw.decode("utf-16-le")
    except UnicodeDecodeError:
        return raw.decode("utf-8")


def delete_secret(suffix: str) -> bool:
    """Remove one secret; ``False`` when the target did not exist."""
    if not _platform_supported():
        raise CredentialStoreError("CREDENTIAL_STORE_WINDOWS_ONLY")
    win32cred = _cred_win32()
    target = credential_target(suffix)
    try:
        win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "1168" in message or "not found" in message.casefold():
            return False
        raise CredentialStoreError(f"CREDENTIAL_DELETE_FAILED:{exc}") from exc
    return True


def resolve_credential_reference(value: str) -> str | None:
    """Resolve ``credman:<target-or-suffix>`` to the stored secret."""
    text = str(value or "").strip()
    if not is_credential_reference(text):
        return None
    target = text[len(CREDMAN_SCHEME):].strip()
    if target.casefold().startswith(CREDENTIAL_TARGET_PREFIX.casefold()):
        suffix = target[len(CREDENTIAL_TARGET_PREFIX):]
    else:
        suffix = target
    return read_secret(suffix)


def dsn_env_value(purpose_value: str) -> str:
    """The env value that defers resolution to the credential store."""
    return f"{CREDMAN_SCHEME}{credential_target(DSN_TARGET_TEMPLATE.format(purpose=purpose_value))}"


__all__ = [
    "CREDMAN_SCHEME",
    "CREDENTIAL_TARGET_PREFIX",
    "CredentialStoreError",
    "DSN_TARGET_TEMPLATE",
    "credential_target",
    "delete_secret",
    "dsn_env_value",
    "is_credential_reference",
    "read_secret",
    "resolve_credential_reference",
    "store_secret",
]
