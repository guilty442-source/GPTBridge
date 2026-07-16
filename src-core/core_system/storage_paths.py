from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import stat as stat_module
import threading
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Iterator


CLEANER_DATA_RELATIVE_PATH = Path("platform_tools") / "project-cleaner" / "data"
BACKUP_ROOT_NAME = "backups"
AUDIT_ROOT_NAME = "audit"
LOG_ROOT_NAME = "logs"
LEGACY_BACKUP_ROOT_NAME = "backups"
DESIGN_BACKUP_DIR_NAME = "design-mode"
MAIN_BACKUP_DIR_NAME = "main-system"
LEGACY_MAIN_BACKUP_DIR_NAMES = ("rescue-mode", "mother-audit")

SANDBOX_ROOT_NAME = ".GPTBridge_RuntimeSandbox"
SANDBOX_CHILD_DIRS = (
    "dev",
    "tool_build",
    "tool_test",
    "runtime",
    "logs",
    "cache",
    "artifacts",
    "temp",
)

_MIGRATION_THREAD_LOCK = threading.RLock()


def _is_link_or_reparse(path: Path) -> bool:
    value = path.lstat()
    attributes = int(getattr(value, "st_file_attributes", 0) or 0)
    return stat_module.S_ISLNK(value.st_mode) or bool(attributes & 0x400)


def _validated_project_root(project_root: Path) -> Path:
    lexical = Path(os.path.abspath(project_root))
    if not lexical.is_dir():
        raise RuntimeError(f"project root is unavailable: {lexical}")
    for candidate in reversed((lexical, *lexical.parents)):
        if candidate.exists() and _is_link_or_reparse(candidate):
            raise RuntimeError(
                f"storage path cannot traverse a link or reparse point: {candidate}"
            )
    canonical = lexical.resolve(strict=True)
    if os.path.normcase(str(canonical)) != os.path.normcase(str(lexical)):
        raise RuntimeError(f"project root changed identity: {lexical}")
    return lexical


def _validate_storage_path(
    project_root: Path,
    candidate: Path,
    *,
    require_exists: bool,
) -> Path:
    root = _validated_project_root(project_root)
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(root)
    except ValueError as error:
        raise RuntimeError(f"storage path escaped project root: {lexical}") from error
    current = root
    for part in relative.parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            if require_exists:
                raise RuntimeError(f"storage path is unavailable: {current}")
            break
        if _is_link_or_reparse(current):
            raise RuntimeError(
                f"storage path cannot be a link or reparse point: {current}"
            )
        resolved = current.resolve(strict=True)
        if os.path.normcase(str(resolved)) != os.path.normcase(str(current)):
            raise RuntimeError(f"storage path changed identity: {current}")
    return lexical


def _ensure_storage_directory(project_root: Path, candidate: Path) -> Path:
    root = _validated_project_root(project_root)
    lexical = _validate_storage_path(root, candidate, require_exists=False)
    relative = lexical.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_link_or_reparse(current) or not current.is_dir():
                raise RuntimeError(f"unsafe storage directory: {current}")
        else:
            current.mkdir()
        _validate_storage_path(root, current, require_exists=True)
    return lexical


def project_root_from(path: Path) -> Path:
    """Find the GPTBridge project root from a file or directory path."""
    project_root_override = os.environ.get("GPTBRIDGE_PROJECT_ROOT")
    if project_root_override:
        return _validated_project_root(Path(project_root_override))

    current = path.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "package.json").exists() and (candidate / "src-core").exists():
            return _validated_project_root(candidate)
    raise RuntimeError(f"GPTBridge project root not found from {path}")


def backup_root(project_root: Path) -> Path:
    return cleaner_data_root(project_root) / BACKUP_ROOT_NAME


def cleaner_data_root(project_root: Path) -> Path:
    return _validated_project_root(project_root) / CLEANER_DATA_RELATIVE_PATH


def audit_root(project_root: Path) -> Path:
    return cleaner_data_root(project_root) / AUDIT_ROOT_NAME


def log_root(project_root: Path) -> Path:
    return cleaner_data_root(project_root) / LOG_ROOT_NAME


def legacy_backup_root(project_root: Path) -> Path:
    return _validated_project_root(project_root) / LEGACY_BACKUP_ROOT_NAME


def design_backup_root(project_root: Path) -> Path:
    return backup_root(project_root) / DESIGN_BACKUP_DIR_NAME


def main_backup_root(project_root: Path) -> Path:
    return backup_root(project_root) / MAIN_BACKUP_DIR_NAME


def legacy_main_backup_roots(project_root: Path) -> list[Path]:
    root = legacy_backup_root(project_root)
    return [root / name for name in LEGACY_MAIN_BACKUP_DIR_NAMES]


def sandbox_root(project_root: Path) -> Path:
    return _validated_project_root(project_root) / SANDBOX_ROOT_NAME


def ensure_backup_layout(project_root: Path) -> None:
    root = _validated_project_root(project_root)
    assert_backup_sandbox_separated(root)
    _ensure_storage_directory(root, design_backup_root(root))
    _ensure_storage_directory(root, main_backup_root(root))
    _ensure_storage_directory(root, audit_root(root))
    _ensure_storage_directory(root, log_root(root))
    migrate_legacy_backups(root)


def ensure_sandbox_layout(project_root: Path) -> Path:
    root = _validated_project_root(project_root)
    assert_backup_sandbox_separated(root)
    sandbox = sandbox_root(root)
    _ensure_storage_directory(root, sandbox)
    for name in SANDBOX_CHILD_DIRS:
        _ensure_storage_directory(root, sandbox / name)
    return sandbox


def assert_backup_sandbox_separated(project_root: Path) -> None:
    root = _validated_project_root(project_root)
    backups = _validate_storage_path(
        root,
        backup_root(root),
        require_exists=False,
    )
    sandbox = _validate_storage_path(
        root,
        sandbox_root(root),
        require_exists=False,
    )
    if backups == sandbox:
        raise RuntimeError("backup root cannot equal sandbox root")
    if backups in sandbox.parents:
        raise RuntimeError("sandbox root cannot be inside backup root")
    if sandbox in backups.parents:
        raise RuntimeError("backup root cannot be inside sandbox root")


def storage_layout(project_root: Path) -> dict[str, str | bool | list[str]]:
    root = _validated_project_root(project_root)
    return {
        "storage_authority": "project-cleaner",
        "cleaner_data_root": str(cleaner_data_root(root)),
        "backup_root": str(backup_root(root)),
        "audit_root": str(audit_root(root)),
        "log_root": str(log_root(root)),
        "legacy_backup_root": str(legacy_backup_root(root)),
        "design_backup_root": str(design_backup_root(root)),
        "main_backup_root": str(main_backup_root(root)),
        "legacy_main_backup_roots": [str(path) for path in legacy_main_backup_roots(root)],
        "sandbox_root": str(sandbox_root(root)),
        "backup_sandbox_separated": True,
    }


def migrate_legacy_backups(project_root: Path) -> dict[str, Any]:
    """Copy legacy records into governed storage and permanently retain sources."""

    root = _validated_project_root(project_root)
    backups = _ensure_storage_directory(root, backup_root(root))
    main_root = _ensure_storage_directory(root, main_backup_root(root))
    design_root = _ensure_storage_directory(root, design_backup_root(root))
    lock_path = backups / ".legacy-migration.lock"
    journal_path = backups / ".legacy-migration-journal.jsonl"
    copied: list[str] = []
    retained: list[str] = []
    skipped: list[str] = []

    def fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def migration_lock() -> Iterator[None]:
        with _MIGRATION_THREAD_LOCK:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as stream:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                    os.fsync(stream.fileno())
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def open_verified(path: Path) -> tuple[BinaryIO, os.stat_result]:
        _validate_storage_path(root, path.parent, require_exists=True)
        before = path.lstat()
        if _is_link_or_reparse(path) or not stat_module.S_ISREG(before.st_mode):
            raise RuntimeError(f"legacy backup is not a regular file: {path}")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        stream = os.fdopen(descriptor, "rb")
        opened = os.fstat(stream.fileno())
        identity_before = (before.st_dev, before.st_ino, before.st_size)
        identity_opened = (opened.st_dev, opened.st_ino, opened.st_size)
        if identity_before != identity_opened:
            stream.close()
            raise RuntimeError(f"legacy backup changed while opening: {path}")
        return stream, before

    def digest_file(path: Path) -> tuple[str, int, int]:
        digest = hashlib.sha256()
        stream, before = open_verified(path)
        try:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        finally:
            stream.close()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError(f"legacy backup changed while reading: {path}")
        return digest.hexdigest(), before.st_size, before.st_mtime_ns

    def build_manifest(source: Path) -> tuple[list[dict[str, Any]], str]:
        source_stat = source.lstat()
        if _is_link_or_reparse(source):
            raise RuntimeError(f"legacy backup cannot be a link: {source}")
        entries: list[dict[str, Any]] = []

        def visit(current: Path, relative: Path) -> None:
            _validate_storage_path(root, current, require_exists=True)
            value = current.lstat()
            if _is_link_or_reparse(current):
                raise RuntimeError(
                    f"legacy backup cannot traverse a link: {current}"
                )
            if stat_module.S_ISDIR(value.st_mode):
                entries.append({"path": relative.as_posix(), "type": "directory"})
                try:
                    children = sorted(
                        current.iterdir(),
                        key=lambda item: item.name.casefold(),
                    )
                except OSError as error:
                    raise RuntimeError(
                        f"legacy backup directory cannot be read: {current}"
                    ) from error
                for child in children:
                    visit(child, relative / child.name)
                return
            if not stat_module.S_ISREG(value.st_mode):
                raise RuntimeError(f"unsupported legacy backup node: {current}")
            digest, size, mtime_ns = digest_file(current)
            entries.append(
                {
                    "path": relative.as_posix(),
                    "type": "file",
                    "size": size,
                    "mtime_ns": mtime_ns,
                    "sha256": digest,
                }
            )

        visit(source, Path("."))
        payload = json.dumps(
            entries,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return entries, hashlib.sha256(payload).hexdigest()

    def copy_file(
        source: Path,
        destination: Path,
        expected: dict[str, Any],
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        stream, source_stat = open_verified(source)
        try:
            with destination.open("xb") as writer:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    writer.write(chunk)
                    digest.update(chunk)
                writer.flush()
                os.fsync(writer.fileno())
        finally:
            stream.close()
        if (
            digest.hexdigest() != expected["sha256"]
            or destination.stat().st_size != expected["size"]
        ):
            raise RuntimeError(f"legacy backup copy verification failed: {source}")
        os.chmod(destination, stat_module.S_IMODE(source_stat.st_mode))
        os.utime(
            destination,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
        )

    def copy_item(source: Path, target_dir: Path) -> None:
        try:
            source = _validate_storage_path(root, source, require_exists=True)
            source.relative_to(legacy_backup_root(root))
            entries, manifest_digest = build_manifest(source)
            suffix = source.suffix if source.is_file() else ""
            stem = source.stem if source.is_file() else source.name
            destination = target_dir / (
                f"{stem}-migrated-{manifest_digest[:12]}{suffix}"
            )
            manifest_path = destination.with_name(
                f"{destination.name}.migration.json"
            )
            if destination.exists():
                existing_entries, existing_digest = build_manifest(destination)
                if existing_digest != manifest_digest:
                    raise RuntimeError(
                        f"migration destination digest conflict: {destination}"
                    )
                retained.append(str(source))
                return

            stage = target_dir / (
                f".legacy-migration-{uuid.uuid4().hex}.partial"
            )
            try:
                if stat_module.S_ISDIR(source.lstat().st_mode):
                    stage.mkdir()
                    for entry in entries:
                        relative = Path(str(entry["path"]))
                        if relative == Path("."):
                            continue
                        stage_path = stage / relative
                        source_path = source / relative
                        if entry["type"] == "directory":
                            stage_path.mkdir()
                        else:
                            copy_file(source_path, stage_path, entry)
                    fsync_directory(stage)
                    stage.rename(destination)
                else:
                    file_entry = next(
                        entry for entry in entries if entry["type"] == "file"
                    )
                    copy_file(source, stage, file_entry)
                    os.link(stage, destination)
                    stage.unlink()
                fsync_directory(target_dir)
                verified_entries, verified_digest = build_manifest(destination)
                if (
                    verified_digest != manifest_digest
                    or verified_entries != entries
                ):
                    raise RuntimeError(
                        f"published migration verification failed: {destination}"
                    )
                manifest_document = {
                    "schema_version": 1,
                    "source": str(source),
                    "destination": str(destination),
                    "source_retained": True,
                    "manifest_sha256": manifest_digest,
                    "entries": entries,
                    "committed_at": time.time(),
                }
                temporary_manifest = manifest_path.with_name(
                    f".{manifest_path.name}.{uuid.uuid4().hex}.partial"
                )
                with temporary_manifest.open("x", encoding="utf-8") as stream:
                    json.dump(
                        manifest_document,
                        stream,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary_manifest, manifest_path)
                temporary_manifest.unlink()
                with journal_path.open("a", encoding="utf-8") as journal:
                    journal.write(
                        json.dumps(
                            {
                                "status": "committed",
                                **manifest_document,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    journal.flush()
                    os.fsync(journal.fileno())
                fsync_directory(target_dir)
                copied.append(str(destination))
                retained.append(str(source))
            finally:
                if stage.exists():
                    # A partial copy contains no unique source data. Keep it for
                    # diagnosis instead of deleting it under the no-loss policy.
                    recovery = target_dir / (
                        f".legacy-migration-recovery-{uuid.uuid4().hex}"
                    )
                    try:
                        stage.rename(recovery)
                    except OSError:
                        pass
        except (OSError, RuntimeError, ValueError) as error:
            skipped.append(f"{source}: {error}")

    with migration_lock():
        legacy_backups = legacy_backup_root(root)
        try:
            direct_sources = sorted(
                (
                    item
                    for item in legacy_backups.iterdir()
                    if item.name.lower().endswith(".zip")
                ),
                key=lambda item: item.name.casefold(),
            )
        except OSError as error:
            direct_sources = []
            if legacy_backups.exists():
                skipped.append(f"{legacy_backups}: cannot scan legacy backups: {error}")
        for source in direct_sources:
            copy_item(source, main_root)

        for legacy_root in legacy_main_backup_roots(root):
            try:
                value = legacy_root.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                skipped.append(f"{legacy_root}: cannot safely inspect: {error}")
                continue
            if (
                _is_link_or_reparse(legacy_root)
                or not stat_module.S_ISDIR(value.st_mode)
            ):
                skipped.append(f"{legacy_root}: unsafe legacy backup root")
                continue
            try:
                sources = sorted(
                    legacy_root.iterdir(),
                    key=lambda item: item.name.casefold(),
                )
            except OSError as error:
                skipped.append(f"{legacy_root}: cannot scan: {error}")
                continue
            for source in sources:
                copy_item(source, main_root)

        project_agent_root = legacy_backups / "project_agent"
        if project_agent_root.exists() or project_agent_root.is_symlink():
            try:
                project_agent_stat = project_agent_root.lstat()
                if (
                    _is_link_or_reparse(project_agent_root)
                    or not stat_module.S_ISDIR(project_agent_stat.st_mode)
                ):
                    raise RuntimeError("unsafe project-agent legacy root")
            except (OSError, RuntimeError) as error:
                skipped.append(f"{project_agent_root}: {error}")
                project_agent_stat = None
            if project_agent_stat is None:
                return {
                    "ok": False,
                    "moved": copied,
                    "copied": copied,
                    "source_retained": retained,
                    "skipped": skipped,
                    "journal_path": str(journal_path),
                    "design_backup_root": str(design_root),
                    "main_backup_root": str(main_root),
                }
            target_root = _ensure_storage_directory(
                root,
                main_root / "project-agent",
            )
            try:
                sources = sorted(
                    project_agent_root.iterdir(),
                    key=lambda item: item.name.casefold(),
                )
            except OSError as error:
                skipped.append(f"{project_agent_root}: cannot scan: {error}")
                sources = []
            for source in sources:
                copy_item(source, target_root)

    return {
        "ok": not skipped,
        "moved": copied,
        "copied": copied,
        "source_retained": retained,
        "skipped": skipped,
        "journal_path": str(journal_path),
        "design_backup_root": str(design_root),
        "main_backup_root": str(main_root),
    }
