from __future__ import annotations

import os
import sys
from pathlib import Path


sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

from managers.subsystem_backup import ScopedBackupStore


def test_scoped_backup_store_keeps_only_latest_record(tmp_path: Path) -> None:
    source = tmp_path / "source"
    backup_root = tmp_path / "backups"
    source.mkdir()
    (source / "settings.json").write_text('{"revision": 1}', encoding="utf-8")
    store = ScopedBackupStore(source, backup_root, max_records=1)

    first = Path(str(store.create("settings-1")["backup_file"]))
    (source / "settings.json").write_text('{"revision": 2}', encoding="utf-8")
    second_result = store.create("settings-2")
    second = Path(str(second_result["backup_file"]))

    assert not first.exists()
    assert second.exists()
    assert store.records() == [str(second)]
    assert second_result["retention"]["permanently_deleted"] == 1


def test_scoped_backup_store_prune_does_not_delete_non_zip_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    backup_root = tmp_path / "backups"
    source.mkdir()
    backup_root.mkdir()
    protected = backup_root / "keep.txt"
    protected.write_text("not a backup archive", encoding="utf-8")
    for name in ("20260101_old.zip", "20260102_latest.zip"):
        (backup_root / name).write_bytes(b"backup")

    result = ScopedBackupStore(source, backup_root, max_records=1).prune()

    assert protected.exists()
    assert result["permanently_deleted"] == 1
    assert [Path(path).name for path in result["retained_records"]] == [
        "20260102_latest.zip"
    ]
