from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RECOVERY_SCHEMA_VERSION = 1


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _content_entries(path: Path) -> list[dict[str, Any]]:
    """Return a deterministic, restorable inventory without following links."""

    if _is_link_or_reparse_point(path):
        raise ValueError(f"recovery source cannot be a link or reparse point: {path}")
    if path.is_file():
        metadata = path.stat(follow_symlinks=False)
        return [
            {
                "path": ".",
                "type": "file",
                "size_bytes": int(metadata.st_size),
                "sha256": _file_sha256(path),
            }
        ]
    if not path.is_dir():
        raise ValueError(f"recovery source must be a regular file or directory: {path}")

    entries: list[dict[str, Any]] = [{"path": ".", "type": "directory"}]
    for current, dirnames, filenames in os.walk(path, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for dirname in sorted(dirnames):
            child = current_path / dirname
            if _is_link_or_reparse_point(child):
                raise ValueError(
                    f"recovery source contains a link or reparse point: {child}"
                )
            safe_directories.append(dirname)
            entries.append(
                {
                    "path": child.relative_to(path).as_posix(),
                    "type": "directory",
                }
            )
        dirnames[:] = safe_directories
        for filename in sorted(filenames):
            child = current_path / filename
            if _is_link_or_reparse_point(child) or not child.is_file():
                raise ValueError(f"recovery source contains an unsafe file: {child}")
            metadata = child.stat(follow_symlinks=False)
            entries.append(
                {
                    "path": child.relative_to(path).as_posix(),
                    "type": "file",
                    "size_bytes": int(metadata.st_size),
                    "sha256": _file_sha256(child),
                }
            )
    return sorted(entries, key=lambda item: (str(item["path"]), str(item["type"])))


def _entries_digest(entries: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    try:
        with partial.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, path)
    finally:
        # The partial is transaction-owned and was never published as user data.
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass


def quarantine_path(
    source: Path,
    recovery_root: Path,
    *,
    operation: str,
    allowed_root: Path,
    original_path: str | None = None,
) -> dict[str, Any]:
    """Move a path into versioned recovery with verified SHA-256 evidence.

    The source is never copied-and-deleted. ``os.replace`` keeps the mutation on
    one filesystem; if that atomic move is unavailable the source remains in
    place and the prepared recovery record is retained for diagnosis.
    """

    source_path = source.absolute()
    allowed = allowed_root.resolve()
    try:
        source_path.resolve(strict=True).relative_to(allowed)
    except (OSError, ValueError) as exc:
        raise ValueError("recovery source must stay inside the allowed root") from exc
    if _is_link_or_reparse_point(source_path):
        raise ValueError("recovery source cannot be a link or reparse point")

    entries = _content_entries(source_path)
    content_sha256 = _entries_digest(entries)
    created_at = datetime.now(timezone.utc)
    safe_operation = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in str(operation or "recovery")
    ).strip("-") or "recovery"
    operation_id = (
        f"{created_at.strftime('%Y%m%dT%H%M%S_%fZ')}-"
        f"{safe_operation}-{uuid.uuid4().hex[:12]}"
    )
    recovery_base = recovery_root.absolute()
    current = Path(recovery_base.anchor)
    for part in recovery_base.parts[1:]:
        current = current / part
        try:
            current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(
                f"recovery destination metadata could not be verified: {current}"
            ) from exc
        if _is_link_or_reparse_point(current):
            raise ValueError(
                f"recovery destination cannot traverse a link or reparse point: {current}"
            )
    try:
        recovery_base.resolve(strict=False).relative_to(
            source_path.resolve(strict=True)
        )
    except ValueError:
        pass
    else:
        raise ValueError("recovery destination cannot be inside the recovery source")

    operation_dir = recovery_base / operation_id
    operation_dir.mkdir(parents=True, exist_ok=False)
    recovery_path = operation_dir / "items" / source_path.name
    recovery_path.parent.mkdir(parents=True, exist_ok=False)

    manifest_path = operation_dir / "manifest.json"
    tombstone_path = operation_dir / "tombstone.json"
    manifest: dict[str, Any] = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "operation_id": operation_id,
        "operation": safe_operation,
        "status": "prepared",
        "created_at": created_at.isoformat(),
        "original_path": original_path or str(source_path),
        "absolute_original_path": str(source_path),
        "recovery_path": str(recovery_path),
        "entries": entries,
        "content_sha256": content_sha256,
    }
    _atomic_write_json(manifest_path, manifest)

    # Do not fall back to shutil.move: its cross-volume copy/delete path can
    # destroy the only durable source after a partial copy.
    os.replace(source_path, recovery_path)
    recovered_entries = _content_entries(recovery_path)
    recovered_digest = _entries_digest(recovered_entries)
    if recovered_digest != content_sha256:
        manifest["status"] = "integrity_mismatch"
        manifest["recovered_content_sha256"] = recovered_digest
        _atomic_write_json(manifest_path, manifest)
        raise OSError(
            f"recovery verification failed; retained at {recovery_path}"
        )

    manifest["status"] = "retained"
    manifest["retained_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(manifest_path, manifest)
    manifest_sha256 = _file_sha256(manifest_path)
    tombstone = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "operation_id": operation_id,
        "operation": safe_operation,
        "status": "recoverable",
        "created_at": created_at.isoformat(),
        "original_path": manifest["original_path"],
        "recovery_path": str(recovery_path),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "content_sha256": content_sha256,
    }
    _atomic_write_json(tombstone_path, tombstone)
    return {
        "operation_id": operation_id,
        "original_path": manifest["original_path"],
        "recovery_path": str(recovery_path),
        "recovery_root": str(operation_dir),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "tombstone_path": str(tombstone_path),
        "content_sha256": content_sha256,
        "size_bytes": sum(
            int(item.get("size_bytes") or 0)
            for item in entries
            if item.get("type") == "file"
        ),
    }
