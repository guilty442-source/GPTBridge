"""Path and directory resolution helpers for the File Sorter application layer."""

from __future__ import annotations

import hashlib
import os
import re
import stat as stat_module
import unicodedata
from pathlib import Path

from ._constants import LEGACY_RULES_FILE_NAME
from .models import FileSorterError


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def normalize_match_text(value: str) -> str:
    return re.sub(r"[\s_]+", " ", normalize_text(value))


def _link_or_reparse_status(path: Path) -> bool | None:
    """Return link/reparse status; inaccessible paths fail closed as ``None``."""

    try:
        value = path.lstat()
    except OSError:
        return None
    attributes = int(getattr(value, "st_file_attributes", 0) or 0)
    return stat_module.S_ISLNK(value.st_mode) or bool(attributes & 0x400)


def resolve_target_dir(target_dir: str | Path) -> Path:
    selected = Path(target_dir).expanduser()
    if not _is_lexically_canonical_absolute(selected):
        raise FileSorterError(f"目標資料夾必須是正規化的絕對路徑：{selected}")
    link_status = _link_or_reparse_status(selected)
    if link_status is None:
        raise FileSorterError(f"無法安全存取目標資料夾：{selected}")
    if link_status:
        raise FileSorterError("目標資料夾不得是符號連結、junction 或 reparse point。")
    try:
        target = selected.resolve(strict=True)
    except OSError as error:
        raise FileSorterError(f"無法解析目標資料夾：{selected}；{error}") from error
    if os.path.normcase(str(selected)) != os.path.normcase(str(target)):
        raise FileSorterError(f"目標資料夾必須使用正規路徑：{selected}")
    if not target.is_dir():
        raise FileSorterError(f"找不到目錄：{target}")
    return target


def is_absolute_destination(folder: str) -> bool:
    return Path(folder.strip()).expanduser().is_absolute()


def is_local_folder_name(folder: str) -> bool:
    folder_name = folder.strip()
    return (
        bool(folder_name)
        and folder_name not in {".", ".."}
        and Path(folder_name).name == folder_name
        and "/" not in folder_name
        and "\\" not in folder_name
    )


def _is_lexically_canonical_absolute(path: Path) -> bool:
    if not path.is_absolute():
        return False
    normalized = Path(os.path.abspath(os.path.normpath(str(path))))
    return os.path.normcase(str(path)) == os.path.normcase(str(normalized))


def _profile_id_for_canonical_target(
    target: Path,
    profile: str | None,
) -> str:
    normalized = os.path.normcase(str(target))
    target_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    if not profile:
        return f"target-{target_digest}"
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", profile.strip()).strip(".-")
    if not slug:
        raise FileSorterError("Profile name must contain a letter or number.")
    name_digest = hashlib.sha256(profile.strip().encode("utf-8")).hexdigest()[:10]
    return f"{slug[:37]}-{name_digest}-{target_digest}"


def resolve_destination_dir(
    target_dir: Path,
    folder: str,
) -> Path:
    folder_name = folder.strip()
    if is_absolute_destination(folder_name):
        raise FileSorterError("分類目的地不得離開目前選定的資料夾。")
    if (
        not folder_name
        or folder_name in {".", ".."}
        or Path(folder_name).name != folder_name
        or "/" in folder_name
        or "\\" in folder_name
    ):
        raise FileSorterError("分類資料夾必須是目標目錄內的單層資料夾名稱。")

    selected_destination = target_dir / folder_name
    link_status = _link_or_reparse_status(selected_destination)
    if link_status is None:
        raise FileSorterError(f"分類資料夾不存在或無法安全存取：{folder_name}")
    if link_status:
        raise FileSorterError("分類目的地不得是符號連結、junction 或 reparse point。")
    destination = selected_destination.resolve()
    if destination.parent != target_dir:
        raise FileSorterError("分類資料夾不可離開目標目錄。")
    if destination.exists() and not destination.is_dir():
        raise FileSorterError(f"分類目的地不是資料夾：{folder_name}")
    if not destination.is_dir():
        raise FileSorterError(f"分類資料夾不存在：{folder_name}；禁止自動建立子資料夾。")
    return destination


def destination_rule_value(folder: str, destination: Path) -> str:
    return destination.name


def destination_exists_for_rule(target_dir: Path, folder: str) -> bool:
    try:
        resolve_destination_dir(target_dir, folder)
    except FileSorterError:
        return False
    return True


def list_destination_folders(target_dir: str | Path) -> list[str]:
    target = resolve_target_dir(target_dir)
    return [
        item.name
        for item in sorted(target.iterdir(), key=lambda path: normalize_text(path.name))
        if _link_or_reparse_status(item) is False
        and item.is_dir()
        and not item.name.startswith(".")
    ]


def list_source_files(target_dir: str | Path) -> list[dict[str, int | str]]:
    target = resolve_target_dir(target_dir)
    files: list[dict[str, int | str]] = []
    for item in sorted(target.iterdir(), key=lambda path: normalize_text(path.name)):
        if (
            _link_or_reparse_status(item) is not False
            or not item.is_file()
            or item.name == LEGACY_RULES_FILE_NAME
        ):
            continue
        try:
            stat = item.stat()
        except OSError:
            continue
        files.append(
            {
                "name": item.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return files
