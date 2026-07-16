from __future__ import annotations

import re
import stat
import zipfile
from pathlib import Path, PurePosixPath


_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    """Extract a ZIP only when every member stays inside target_dir."""
    target_dir.mkdir(parents=True, exist_ok=True)
    target_root = target_dir.resolve()

    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            raw_name = member.filename
            normalized_name = raw_name.replace("\\", "/")
            member_path = PurePosixPath(normalized_name)
            mode = member.external_attr >> 16

            if (
                not raw_name
                or "\x00" in raw_name
                or member_path.is_absolute()
                or _WINDOWS_DRIVE_PREFIX.match(normalized_name)
                or ".." in member_path.parts
                or stat.S_ISLNK(mode)
            ):
                raise RuntimeError(f"Unsafe zip member: {raw_name}")

            target = target_root.joinpath(*member_path.parts).resolve()
            try:
                target.relative_to(target_root)
            except ValueError as exc:
                raise RuntimeError(f"Unsafe zip member: {raw_name}") from exc

        archive.extractall(target_root)
