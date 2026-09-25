"""Signing helpers, nonce store, and process-ancestry utilities."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from dataclasses import asdict, replace
from pathlib import Path

from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
)
from governance_rule.permission_directory.execution.path_guard import (
    permission_denied,
    resolve_project_path,
)

from .auth_models import (
    LauncherIdentityAttestation,
    _KeyRing,
    _SigningKey,
)


def _windows_parent_process_id(process_id: int) -> int | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32W),
    )
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32W),
    )
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle:
        return None
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return None
        while True:
            if int(entry.th32ProcessID) == process_id:
                return int(entry.th32ParentProcessID)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return None
    finally:
        kernel32.CloseHandle(snapshot)


def _is_launcher_ancestor(process_id: int, *, maximum_depth: int = 4) -> bool:
    if process_id <= 0:
        return False
    if os.name != "nt":
        return os.getppid() == process_id
    current = os.getpid()
    for _ in range(maximum_depth):
        parent = _windows_parent_process_id(current)
        if parent is None or parent <= 0:
            return False
        if parent == process_id:
            return True
        current = parent
    return False


def _canonical(value: object, *, omit_signature: bool = False) -> bytes:
    data = asdict(value)
    if omit_signature:
        data.pop("signature", None)
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    try:
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, UnicodeError) as exc:
        raise permission_denied() from exc


def sign_launcher_attestation(
    launcher_key: bytes,
    *,
    actor: str,
    bound_tool_id: str,
    caller_path: str,
    process_id: int,
    issued_at: int,
    key_id: str,
) -> LauncherIdentityAttestation:
    policy = directory_authority_snapshot().key_management_policy
    if not isinstance(launcher_key, bytes) or len(launcher_key) < policy.minimum_key_bytes:
        raise permission_denied()
    if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
        raise permission_denied()
    if not isinstance(issued_at, int) or isinstance(issued_at, bool):
        raise permission_denied()
    values = (actor, bound_tool_id, caller_path, key_id)
    if any(not isinstance(value, str) or not value for value in values):
        raise permission_denied()
    unsigned = LauncherIdentityAttestation(
        issuer=policy.identity_attestation_issuer,
        actor=actor,
        bound_tool_id=bound_tool_id,
        caller_path=caller_path,
        process_id=process_id,
        issued_at=issued_at,
        expires_at=issued_at + policy.identity_attestation_ttl_seconds,
        nonce=secrets.token_hex(16),
        key_id=key_id,
        signature="",
    )
    signature = hmac.new(
        launcher_key,
        _canonical(unsigned, omit_signature=True),
        hashlib.sha256,
    ).hexdigest()
    return replace(unsigned, signature=signature)


class _NonceStore:
    # Bounded migration-window fallback path (A501): used when the policy
    # authority moved to PostgreSQL but ``GPTBRIDGE_NONCE_ENGINE=sqlite``
    # selects this store for the migration window or test isolation.
    SQLITE_FALLBACK_PATH = "main-system/runtime/state/governance_authentication.sqlite3"

    def __init__(
        self,
        project_root: Path,
        policy: object,
        *,
        path: str | None = None,
    ) -> None:
        database = resolve_project_path(
            project_root, path or policy.nonce_store_path
        )
        directory = directory_authority_snapshot()
        main_boundary = next(
            (
                boundary
                for boundary in directory.boundaries
                if boundary.key == "main_system"
            ),
            None,
        )
        if main_boundary is None or len(main_boundary.runtime_writable_roots) != 1:
            raise permission_denied()
        runtime_root = resolve_project_path(
            project_root,
            main_boundary.runtime_writable_roots[0],
        )
        try:
            database.relative_to(runtime_root)
        except ValueError as exc:
            raise permission_denied() from exc
        database.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(
                str(database),
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS governance_used_nonces ("
                "namespace TEXT NOT NULL, actor TEXT NOT NULL, "
                "nonce TEXT NOT NULL, expires_at INTEGER NOT NULL, "
                "PRIMARY KEY (namespace, actor, nonce)) WITHOUT ROWID"
            )
        except sqlite3.Error as exc:
            raise permission_denied() from exc
        self._lock = threading.RLock()
        self._clock_skew = policy.allowed_clock_skew_seconds

    def consume(
        self,
        namespace: str,
        actor: str,
        nonce: str,
        expires_at: int,
        now: int,
    ) -> None:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "DELETE FROM governance_used_nonces WHERE expires_at < ?",
                    (now - self._clock_skew,),
                )
                self._connection.execute(
                    "INSERT INTO governance_used_nonces "
                    "(namespace, actor, nonce, expires_at) VALUES (?, ?, ?, ?)",
                    (namespace, actor, nonce, expires_at),
                )
                self._connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._connection.execute("ROLLBACK")
                raise permission_denied() from exc
            except sqlite3.Error as exc:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise permission_denied() from exc

    def close(self) -> None:
        with self._lock:
            self._connection.close()


_PG_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class _PostgresNonceStore:
    """Authoritative nonce ledger in the central PostgreSQL transport schema.

    A348/A610: PostgreSQL is the sole structured-data authority; the
    ``sqlite`` store below survives only as the bounded migration-window
    fallback selected by ``GPTBRIDGE_NONCE_ENGINE``.
    """

    def __init__(self, policy: object) -> None:
        target = str(getattr(policy, "nonce_store_path", "") or "")
        _, _, rest = target.partition("postgresql:")
        schema, _, table = rest.partition(":")
        if (
            not rest
            or not table
            or not _PG_IDENT.fullmatch(schema)
            or not _PG_IDENT.fullmatch(table)
        ):
            raise permission_denied()
        try:
            import psycopg
            from psycopg import sql as _sql

            from shared_layer.security.dsn_policy import (
                DsnPurpose,
                resolve_dsn,
            )
        except Exception as exc:
            raise permission_denied() from exc
        self._psycopg = psycopg
        self._sql = _sql
        self._table = _sql.SQL("{}.{}").format(
            _sql.Identifier(schema), _sql.Identifier(table)
        )
        try:
            binding = resolve_dsn(DsnPurpose.RUNTIME)
            self._connection = psycopg.connect(binding.dsn)
        except Exception as exc:
            raise permission_denied() from exc
        self._lock = threading.RLock()
        self._clock_skew = policy.allowed_clock_skew_seconds

    def consume(
        self,
        namespace: str,
        actor: str,
        nonce: str,
        expires_at: int,
        now: int,
    ) -> None:
        psycopg = self._psycopg
        _sql = self._sql
        with self._lock:
            try:
                with self._connection.transaction():
                    self._connection.execute(
                        _sql.SQL("DELETE FROM {} WHERE expires_at < %s").format(
                            self._table
                        ),
                        (now - self._clock_skew,),
                    )
                    self._connection.execute(
                        _sql.SQL(
                            "INSERT INTO {} (namespace, actor, nonce, expires_at) "
                            "VALUES (%s, %s, %s, %s)"
                        ).format(self._table),
                        (namespace, actor, nonce, expires_at),
                    )
            except psycopg.errors.UniqueViolation as exc:
                raise permission_denied() from exc
            except psycopg.Error as exc:
                raise permission_denied() from exc

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _build_nonce_store(project_root: Path, policy: object):
    """Select the nonce-store engine (A501 bounded fallback contract).

    ``policy.nonce_store_engine`` is authoritative; the
    ``GPTBRIDGE_NONCE_ENGINE`` env var may only downgrade to ``sqlite``
    during the bounded migration window.
    """

    engine = str(getattr(policy, "nonce_store_engine", "") or "").strip().lower()
    override = str(os.environ.get("GPTBRIDGE_NONCE_ENGINE", "")).strip().lower()
    if override:
        engine = override
    if engine == "postgresql":
        return _PostgresNonceStore(policy)
    if engine == "sqlite":
        target = str(getattr(policy, "nonce_store_path", "") or "")
        fallback = (
            _NonceStore.SQLITE_FALLBACK_PATH
            if target.startswith("postgresql:")
            else None
        )
        return _NonceStore(project_root, policy, path=fallback)
    raise permission_denied()


def _new_key(policy: object, now: int) -> _SigningKey:
    return _SigningKey(
        key_id=secrets.token_hex(16),
        secret=secrets.token_bytes(policy.minimum_key_bytes),
        issued_at=now,
        expires_at=now + policy.maximum_key_lifetime_seconds,
    )
