"""File metadata capture, serialization, and verification."""

from __future__ import annotations

import errno
import hashlib
import os
import stat as stat_module
from pathlib import Path

from ._constants import SorterV2Error
from ._io_utils import sha256_file
from ._models import FileFingerprint, PlanOperation, _FileMetadata
from ._paths import _is_link_or_reparse


def _fingerprint(path: Path) -> FileFingerprint:
    stat = path.stat()
    return FileFingerprint(stat.st_size, stat.st_mtime_ns)


def _expected_fingerprint(operation: PlanOperation) -> FileFingerprint:
    return FileFingerprint(operation.source_size, operation.source_mtime_ns)


def _ensure_source_unchanged(source: Path, operation: PlanOperation) -> None:
    if _is_link_or_reparse(source):
        raise SorterV2Error(
            f"Source was replaced by a link or reparse point: {source}"
        )
    try:
        actual = _fingerprint(source)
    except OSError as error:
        raise SorterV2Error(f"Source unavailable: {source}: {error}") from error
    if actual != _expected_fingerprint(operation):
        raise SorterV2Error(f"Source changed after preview: {source}")


def _windows_alternate_streams(
    path: Path,
) -> tuple[tuple[str, int, str], ...] | None:
    """Return named NTFS streams, or ``None`` when streams are unsupported."""

    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _WIN32_FIND_STREAM_DATA(ctypes.Structure):
        _fields_ = [
            ("StreamSize", ctypes.c_longlong),
            ("cStreamName", wintypes.WCHAR * (260 + 36)),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    find_first = kernel32.FindFirstStreamW
    find_first.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(_WIN32_FIND_STREAM_DATA),
        wintypes.DWORD,
    ]
    find_first.restype = wintypes.HANDLE
    find_next = kernel32.FindNextStreamW
    find_next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_WIN32_FIND_STREAM_DATA),
    ]
    find_next.restype = wintypes.BOOL
    find_close = kernel32.FindClose
    find_close.argtypes = [wintypes.HANDLE]
    find_close.restype = wintypes.BOOL

    data = _WIN32_FIND_STREAM_DATA()
    handle = find_first(str(path), 0, ctypes.byref(data), 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        if error in {1, 2, 38, 50, 87}:
            return None
        raise ctypes.WinError(error)

    streams: list[tuple[str, int, str]] = []
    try:
        while True:
            name = str(data.cStreamName)
            if name and name.casefold() != "::$data":
                stream_path = f"{path}{name}"
                streams.append(
                    (name, int(data.StreamSize), sha256_file(stream_path))
                )
            if find_next(handle, ctypes.byref(data)):
                continue
            error = ctypes.get_last_error()
            if error != 38:
                raise ctypes.WinError(error)
            break
    finally:
        find_close(handle)
    return tuple(sorted(streams, key=lambda item: item[0].casefold()))


def _posix_extended_attributes(
    path: Path,
) -> tuple[tuple[str, str], ...] | None:
    """Return content hashes for supported POSIX extended attributes."""

    list_xattr = getattr(os, "listxattr", None)
    get_xattr = getattr(os, "getxattr", None)
    if os.name == "nt" or not callable(list_xattr) or not callable(get_xattr):
        return None
    try:
        try:
            names = list_xattr(path, follow_symlinks=False)
        except TypeError:
            names = list_xattr(path)
        values: list[tuple[str, str]] = []
        for raw_name in names:
            try:
                raw_value = get_xattr(path, raw_name, follow_symlinks=False)
            except TypeError:
                raw_value = get_xattr(path, raw_name)
            values.append(
                (
                    str(raw_name),
                    hashlib.sha256(raw_value).hexdigest(),
                )
            )
        return tuple(sorted(values, key=lambda item: item[0]))
    except OSError as error:
        unsupported = {
            errno.ENOSYS,
            getattr(errno, "ENOTSUP", 95),
            getattr(errno, "EOPNOTSUPP", 95),
        }
        if error.errno in unsupported:
            return None
        raise


def _capture_file_metadata(path: Path) -> _FileMetadata:
    value = path.stat()
    attributes = getattr(value, "st_file_attributes", None)
    return _FileMetadata(
        mtime_ns=value.st_mtime_ns,
        permissions=stat_module.S_IMODE(value.st_mode),
        file_attributes=int(attributes) if attributes is not None else None,
        alternate_streams=_windows_alternate_streams(path),
        extended_attributes=_posix_extended_attributes(path),
    )


def _file_metadata_to_dict(value: _FileMetadata) -> dict[str, object]:
    return {
        "mtime_ns": value.mtime_ns,
        "permissions": value.permissions,
        "file_attributes": value.file_attributes,
        "alternate_streams": (
            None
            if value.alternate_streams is None
            else [
                {"name": name, "size": size, "sha256": digest}
                for name, size, digest in value.alternate_streams
            ]
        ),
        "extended_attributes": (
            None
            if value.extended_attributes is None
            else [
                {"name": name, "sha256": digest}
                for name, digest in value.extended_attributes
            ]
        ),
    }


def _file_metadata_from_dict(value: object) -> _FileMetadata | None:
    from typing import Mapping as _Mapping

    if not isinstance(value, _Mapping):
        return None
    alternate_value = value.get("alternate_streams")
    alternate_streams: tuple[tuple[str, int, str], ...] | None
    if alternate_value is None:
        alternate_streams = None
    elif isinstance(alternate_value, list):
        parsed: list[tuple[str, int, str]] = []
        for item in alternate_value:
            if not isinstance(item, _Mapping):
                return None
            parsed.append(
                (
                    str(item.get("name", "")),
                    int(item.get("size", -1)),
                    str(item.get("sha256", "")),
                )
            )
        alternate_streams = tuple(parsed)
    else:
        return None
    extended_value = value.get("extended_attributes")
    extended_attributes: tuple[tuple[str, str], ...] | None
    if extended_value is None:
        extended_attributes = None
    elif isinstance(extended_value, list):
        extended_parsed: list[tuple[str, str]] = []
        for item in extended_value:
            if not isinstance(item, _Mapping):
                return None
            extended_parsed.append(
                (
                    str(item.get("name", "")),
                    str(item.get("sha256", "")),
                )
            )
        extended_attributes = tuple(extended_parsed)
    else:
        return None
    try:
        attributes = value.get("file_attributes")
        return _FileMetadata(
            mtime_ns=int(value["mtime_ns"]),
            permissions=int(value["permissions"]),
            file_attributes=(
                int(attributes) if attributes is not None else None
            ),
            alternate_streams=alternate_streams,
            extended_attributes=extended_attributes,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _verify_file_metadata(
    path: Path,
    expected: _FileMetadata,
    *,
    label: str,
) -> None:
    actual = _capture_file_metadata(path)
    mismatches: list[str] = []
    if actual.mtime_ns != expected.mtime_ns:
        mismatches.append("modification timestamp")
    if actual.permissions != expected.permissions:
        mismatches.append("permissions")
    if (
        expected.file_attributes is not None
        and actual.file_attributes != expected.file_attributes
    ):
        mismatches.append("Windows file attributes")
    if (
        expected.alternate_streams is not None
        and actual.alternate_streams != expected.alternate_streams
    ):
        mismatches.append("Windows alternate data streams")
    if (
        expected.extended_attributes is not None
        and actual.extended_attributes != expected.extended_attributes
    ):
        mismatches.append("extended attributes")
    if mismatches:
        raise SorterV2Error(
            f"{label} metadata verification failed ({', '.join(mismatches)}): {path}"
        )
