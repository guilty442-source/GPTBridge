from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
import uuid
from pathlib import Path
from typing import Any


def _is_link_or_reparse(path: Path) -> bool:
    path_stat = path.lstat()
    attributes = int(getattr(path_stat, "st_file_attributes", 0) or 0)
    return stat_module.S_ISLNK(path_stat.st_mode) or bool(attributes & 0x400)


def _sha256_regular_file(path: Path) -> str:
    path_stat = path.lstat()
    if _is_link_or_reparse(path) or not stat_module.S_ISREG(path_stat.st_mode):
        raise RuntimeError(
            f"Package recovery requires a regular non-reparse file: {path}"
        )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    final_stat = path.lstat()
    if (
        _is_link_or_reparse(path)
        or not stat_module.S_ISREG(final_stat.st_mode)
        or (
            int(final_stat.st_dev),
            int(final_stat.st_ino),
            int(final_stat.st_size),
            int(final_stat.st_mtime_ns),
        )
        != (
            int(path_stat.st_dev),
            int(path_stat.st_ino),
            int(path_stat.st_size),
            int(path_stat.st_mtime_ns),
        )
    ):
        raise RuntimeError(f"Package recovery file changed while hashing: {path}")
    return digest.hexdigest()


def _inventory_package_tree(root: Path) -> dict[str, Any]:
    root = Path(os.path.abspath(root))
    root_stat = root.lstat()
    if _is_link_or_reparse(root) or not stat_module.S_ISDIR(root_stat.st_mode):
        raise RuntimeError(
            f"Package tree must be a regular non-reparse directory: {root}"
        )
    if root.resolve(strict=True) != root:
        raise RuntimeError(f"Package tree escaped its lexical root: {root}")
    entries: list[dict[str, Any]] = []

    def walk_error(error: OSError) -> None:
        raise RuntimeError(
            f"Package recovery inventory is incomplete: {error}"
        ) from error

    for current_root, dirs, files in os.walk(
        root,
        followlinks=False,
        onerror=walk_error,
    ):
        current = Path(current_root)
        if (
            _is_link_or_reparse(current)
            or not current.is_dir()
            or current.resolve(strict=True) != current
        ):
            raise RuntimeError(f"Unsafe package recovery directory: {current}")
        for name in sorted(dirs):
            child = current / name
            child_stat = child.lstat()
            if _is_link_or_reparse(child) or not stat_module.S_ISDIR(
                child_stat.st_mode
            ):
                raise RuntimeError(
                    f"Unsafe package recovery directory entry: {child}"
                )
            entries.append(
                {
                    "path": child.relative_to(root).as_posix(),
                    "type": "directory",
                }
            )
        for name in sorted(files):
            child = current / name
            child_stat = child.lstat()
            if _is_link_or_reparse(child) or not stat_module.S_ISREG(
                child_stat.st_mode
            ):
                raise RuntimeError(
                    f"Unsafe package recovery file entry: {child}"
                )
            entries.append(
                {
                    "path": child.relative_to(root).as_posix(),
                    "type": "file",
                    "size": int(child_stat.st_size),
                    "sha256": _sha256_regular_file(child),
                }
            )
    entries.sort(key=lambda entry: (str(entry["path"]), str(entry["type"])))
    digest = hashlib.sha256(
        json.dumps(
            entries,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {"entries": entries, "tree_digest": digest}


def _persist_package_document(
    path: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"Package journal already exists: {path}")
    document = {
        **payload,
        "document_digest": hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    partial = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.partial"
    )
    try:
        with partial.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(partial, path, follow_symlinks=False)
        except FileExistsError as error:
            raise RuntimeError(
                f"Package journal appeared during publication: {path}"
            ) from error
        with path.open("rb+") as stream:
            os.fsync(stream.fileno())
        partial.unlink()
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    persisted = json.loads(path.read_text(encoding="utf-8"))
    if persisted != document:
        raise RuntimeError(f"Package journal verification failed: {path}")
    return document


def _process_is_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if process_id == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
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


def _inventory_file_map(inventory: dict[str, Any]) -> dict[str, tuple[int, str]]:
    return {
        str(entry["path"]): (
            int(entry["size"]),
            str(entry["sha256"]),
        )
        for entry in inventory["entries"]
        if entry.get("type") == "file"
    }
