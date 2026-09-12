from __future__ import annotations

import ctypes
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .cleanup_constants import _parse_iso


class FileUtilsMixin:
    """File hashing, locking detection, and quarantine batch document helpers."""

    @contextmanager
    def _open_shared_read(self, source: Path) -> Iterator[BinaryIO]:
        if os.name != "nt":
            with source.open("rb") as source_file:
                yield source_file
            return
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = create_file(
            str(source),
            0x80000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x00000080 | 0x08000000,
            None,
        )
        invalid = wintypes.HANDLE(-1).value
        if handle in (None, invalid):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(source))
        try:
            descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
        except OSError:
            close_handle(handle)
            raise
        with os.fdopen(descriptor, "rb") as source_file:
            yield source_file

    def _file_sha256(self, path: Path) -> str:
        chunk_size = max(64 * 1024, int(self.rules.get("analysis", {}).get("hash_chunk_bytes") or 1024 * 1024))
        digest = hashlib.sha256()
        with self._open_shared_read(path) as source:
            while chunk := source.read(chunk_size):
                digest.update(chunk)
        return digest.hexdigest()

    def _content_digest(self, path: Path, item_type: str) -> str:
        if item_type == "file":
            return self._file_sha256(path)
        digest = hashlib.sha256()
        for current, dirnames, filenames in os.walk(path, followlinks=False):
            current_path = Path(current)
            dirnames[:] = [
                name
                for name in sorted(dirnames)
                if not self._is_link_or_reparse_point(current_path / name)
            ]
            for filename in sorted(filenames):
                child = current_path / filename
                if self._is_link_or_reparse_point(child):
                    continue
                rel = child.relative_to(path).as_posix()
                digest.update(rel.encode("utf-8"))
                digest.update(self._file_sha256(child).encode("ascii"))
        return digest.hexdigest()

    def _regular_files_below(self, path: Path) -> list[Path]:
        files: list[Path] = []
        for current, dirnames, filenames in os.walk(path, followlinks=False):
            current_path = Path(current)
            dirnames[:] = [
                name
                for name in sorted(dirnames)
                if not self._is_link_or_reparse_point(current_path / name)
            ]
            for filename in sorted(filenames):
                child = current_path / filename
                if not self._is_link_or_reparse_point(child) and child.is_file():
                    files.append(child)
        return files

    def _is_locked(self, path: Path) -> bool:
        if os.name != "nt":
            return False
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            0x00010000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000 if path.is_dir() else 0x00000080,
            None,
        )
        invalid = wintypes.HANDLE(-1).value
        if handle in (None, invalid):
            return ctypes.get_last_error() in {32, 33}
        kernel32.CloseHandle(handle)
        return False

    def _locked_descendant(self, path: Path) -> Path | None:
        if self._is_locked(path):
            return path
        if not path.is_dir():
            return None
        try:
            for current, dirnames, filenames in os.walk(path, followlinks=False):
                current_path = Path(current)
                safe_directories: list[str] = []
                for name in sorted(dirnames):
                    child = current_path / name
                    if self._is_link_or_reparse_point(child):
                        continue
                    if self._is_locked(child):
                        return child
                    safe_directories.append(name)
                dirnames[:] = safe_directories
                for name in sorted(filenames):
                    child = current_path / name
                    if self._is_link_or_reparse_point(child) or self._is_locked(child):
                        return child
        except OSError:
            return path
        return None

    @staticmethod
    def _locking_processes(path: Path) -> list[dict[str, Any]]:
        if os.name != "nt":
            return []
        try:
            from ctypes import wintypes

            class RM_UNIQUE_PROCESS(ctypes.Structure):
                _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]

            class RM_PROCESS_INFO(ctypes.Structure):
                _fields_ = [
                    ("Process", RM_UNIQUE_PROCESS),
                    ("strAppName", wintypes.WCHAR * 256),
                    ("strServiceShortName", wintypes.WCHAR * 64),
                    ("ApplicationType", wintypes.UINT),
                    ("AppStatus", wintypes.ULONG),
                    ("TSSessionId", wintypes.DWORD),
                    ("bRestartable", wintypes.BOOL),
                ]

            restart_manager = ctypes.WinDLL("Rstrtmgr")
            session = wintypes.DWORD()
            key = ctypes.create_unicode_buffer(33)
            if restart_manager.RmStartSession(ctypes.byref(session), 0, key) != 0:
                return []
            try:
                resources = (wintypes.LPCWSTR * 1)(str(path))
                if restart_manager.RmRegisterResources(session, 1, resources, 0, None, 0, None) != 0:
                    return []
                needed = wintypes.UINT(0)
                count = wintypes.UINT(0)
                reason = wintypes.DWORD(0)
                result = restart_manager.RmGetList(
                    session,
                    ctypes.byref(needed),
                    ctypes.byref(count),
                    None,
                    ctypes.byref(reason),
                )
                if result != 234 or needed.value == 0:
                    return []
                entries = (RM_PROCESS_INFO * needed.value)()
                count = wintypes.UINT(needed.value)
                if restart_manager.RmGetList(
                    session,
                    ctypes.byref(needed),
                    ctypes.byref(count),
                    entries,
                    ctypes.byref(reason),
                ) != 0:
                    return []
                return [
                    {
                        "pid": int(entries[index].Process.dwProcessId),
                        "name": str(entries[index].strAppName),
                        "restartable": bool(entries[index].bRestartable),
                    }
                    for index in range(count.value)
                ]
            finally:
                restart_manager.RmEndSession(session)
        except Exception:
            return []

    @staticmethod
    def _process_is_alive(process_id: int) -> bool:
        if process_id <= 0:
            return False
        if process_id == os.getpid():
            return True
        if os.name == "nt":
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            process = kernel32.OpenProcess(0x1000, False, process_id)
            if not process:
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(
                    process,
                    ctypes.byref(exit_code),
                ):
                    return True
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(process)
        try:
            os.kill(process_id, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    @staticmethod
    def _is_sha256_text(value: str) -> bool:
        normalized = str(value or "").strip()
        return len(normalized) == 64 and all(
            character in "0123456789abcdefABCDEF"
            for character in normalized
        )

    def _unique_quarantine_target(self, batch_dir: Path, rel_path: str) -> Path:
        target = batch_dir / "items" / rel_path
        if not target.exists():
            return target
        for index in range(1, 1000):
            candidate = target.with_name(f"{target.stem}.{index}{target.suffix}")
            if not candidate.exists():
                return candidate
        return target.with_name(f"{target.stem}.{uuid.uuid4().hex[:8]}{target.suffix}")

    @staticmethod
    def _batch_document_sort_key(
        path: Path,
        payload: dict[str, Any],
    ) -> tuple[int, float, int]:
        try:
            revision = max(0, int(payload.get("revision") or 0))
        except (TypeError, ValueError):
            revision = 0
        updated_at = _parse_iso(str(payload.get("updated_at") or ""))
        updated_timestamp = updated_at.timestamp() if updated_at is not None else 0.0
        # The journal is the write-ahead record, so prefer it only when both
        # monotonic revision and timestamp are otherwise identical.
        journal_tiebreaker = int(path.name == "journal.json")
        return revision, updated_timestamp, journal_tiebreaker

    def _valid_batch_documents(
        self,
        batch_dir: Path,
    ) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
        valid: list[tuple[Path, dict[str, Any]]] = []
        errors: list[str] = []
        found = False
        for path in (batch_dir / "manifest.json", batch_dir / "journal.json"):
            if not path.exists():
                continue
            found = True
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                errors.append(f"{path.name}: invalid items")
                continue
            valid.append((path, payload))
        if not found:
            errors.append("quarantine manifest not found")
        return valid, errors

    def _batch_document_path(self, batch_dir: Path) -> Path | None:
        valid, _errors = self._valid_batch_documents(batch_dir)
        if not valid:
            return None
        return max(
            valid,
            key=lambda item: self._batch_document_sort_key(item[0], item[1]),
        )[0]

    def _read_batch_document(self, batch_dir: Path) -> tuple[dict[str, Any] | None, str]:
        valid, errors = self._valid_batch_documents(batch_dir)
        if not valid:
            if errors == ["quarantine manifest not found"]:
                return None, errors[0]
            return None, f"invalid quarantine documents: {'; '.join(errors)}"
        _path, payload = max(
            valid,
            key=lambda item: self._batch_document_sort_key(item[0], item[1]),
        )
        return payload, ""

    def _write_batch_document(self, batch_dir: Path, document: dict[str, Any]) -> None:
        persisted, _error = self._read_batch_document(batch_dir)
        persisted_revision = 0
        if persisted is not None:
            try:
                persisted_revision = max(0, int(persisted.get("revision") or 0))
            except (TypeError, ValueError):
                persisted_revision = 0
        try:
            document_revision = max(0, int(document.get("revision") or 0))
        except (TypeError, ValueError):
            document_revision = 0
        document["revision"] = max(persisted_revision, document_revision) + 1
        document["updated_at"] = self._iso_now()
        self._atomic_write_json(batch_dir / "journal.json", document)
        if str(document.get("status") or "legacy") != "applying":
            self._atomic_write_json(batch_dir / "manifest.json", document)

    @staticmethod
    def _is_restorable_item(item: dict[str, Any]) -> bool:
        status = str(item.get("status") or "")
        return bool(item.get("quarantine_path")) and (
            status in {"moved", "moving", "pending", "error"}
            or (not status and bool(item.get("original_path")))
        )
