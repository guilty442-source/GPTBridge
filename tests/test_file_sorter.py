from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "platform_tools"
    / "file-sorter"
    / "src"
    / "main.py"
)
SPEC = importlib.util.spec_from_file_location("file_sorter_main", MODULE_PATH)
assert SPEC and SPEC.loader
file_sorter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = file_sorter
SPEC.loader.exec_module(file_sorter)


@pytest.fixture(autouse=True)
def isolate_code_rules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rules_path = tmp_path.parent / f"{tmp_path.name}-keyword-rules.py"
    monkeypatch.setattr(file_sorter, "RULES_FILE_PATH", rules_path)


def test_uses_longest_keyword_and_avoids_ascii_substring_false_positive(
    tmp_path: Path,
) -> None:
    (tmp_path / "cat").mkdir()
    (tmp_path / "catalog").mkdir()
    (tmp_path / "catalog_report.txt").write_text("catalog", encoding="utf-8")
    (tmp_path / "concatenate.txt").write_text("cat", encoding="utf-8")

    result = file_sorter.organize_files(tmp_path)

    assert result.errors == []
    assert (tmp_path / "catalog" / "catalog_report.txt").exists()
    assert (tmp_path / "concatenate.txt").exists()
    assert not (tmp_path / "cat" / "catalog_report.txt").exists()


def test_adds_custom_keywords_and_normalizes_unicode(tmp_path: Path) -> None:
    (tmp_path / "音樂").mkdir()
    file_sorter.add_keywords(tmp_path, ["kpop", "Café"], "音樂")
    (tmp_path / "KPOP-stage.mp3").write_text("kpop", encoding="utf-8")
    (tmp_path / "Cafe\u0301-song.mp3").write_text("cafe", encoding="utf-8")

    result = file_sorter.organize_files(tmp_path)

    assert result.errors == []
    assert (tmp_path / "音樂" / "KPOP-stage.mp3").exists()
    assert (tmp_path / "音樂" / "Cafe\u0301-song.mp3").exists()
    rules_path = file_sorter.get_rules_path()
    rules_text = rules_path.read_text(encoding="utf-8")
    rules_data = __import__('json').loads(rules_text)
    assert isinstance(rules_data, list)
    assert file_sorter.LEGACY_RULES_FILE_NAME not in {item.name for item in tmp_path.iterdir()}
    assert any(rule.get('keyword') == 'kpop' for rule in rules_data)
    assert any(rule.get('keyword') == 'Café' for rule in rules_data)


def test_never_overwrites_existing_files(tmp_path: Path) -> None:
    destination = tmp_path / "cat"
    destination.mkdir()
    (tmp_path / "cat_note.txt").write_text("source", encoding="utf-8")
    (destination / "cat_note.txt").write_text("existing", encoding="utf-8")
    (destination / "cat_note_1.txt").write_text("existing one", encoding="utf-8")

    result = file_sorter.organize_files(tmp_path)

    assert result.errors == []
    assert (destination / "cat_note.txt").read_text(encoding="utf-8") == "existing"
    assert (destination / "cat_note_1.txt").read_text(encoding="utf-8") == "existing one"
    assert (destination / "cat_note_2.txt").read_text(encoding="utf-8") == "source"


def test_rejects_destination_outside_target(tmp_path: Path) -> None:
    with pytest.raises(file_sorter.FileSorterError):
        file_sorter.add_keywords(tmp_path, ["unsafe"], "..")


def test_never_creates_missing_destination_folder(tmp_path: Path) -> None:
    destination = tmp_path / "禁止自動建立"

    with pytest.raises(file_sorter.FileSorterError):
        file_sorter.add_keywords(tmp_path, ["keyword"], destination.name)

    assert not destination.exists()


def test_unmatched_files_stay_in_place(tmp_path: Path) -> None:
    (tmp_path / "music").mkdir()
    unmatched = tmp_path / "document_without_keyword.txt"
    unmatched.write_text("keep me", encoding="utf-8")

    result = file_sorter.organize_files(tmp_path)

    assert result.errors == []
    assert result.moved_count == 0
    assert result.unmatched_count == 1
    assert unmatched.read_text(encoding="utf-8") == "keep me"


def test_spaces_and_underscores_are_equivalent(tmp_path: Path) -> None:
    destination = tmp_path / "BLACK PINK"
    destination.mkdir()
    (tmp_path / "BLACK_PINK-stage.mp4").write_text("underscore", encoding="utf-8")
    file_sorter.add_keywords(tmp_path, ["red_velvet"], destination.name)
    (tmp_path / "RED VELVET live.mp4").write_text("space", encoding="utf-8")

    result = file_sorter.organize_files(tmp_path)

    assert result.errors == []
    assert (destination / "BLACK_PINK-stage.mp4").exists()
    assert (destination / "RED VELVET live.mp4").exists()


def test_updates_existing_code_keyword_and_destination(tmp_path: Path) -> None:
    (tmp_path / "舊分類").mkdir()
    (tmp_path / "新分類").mkdir()
    file_sorter.add_keywords(tmp_path, ["old_keyword"], "舊分類")

    updated = file_sorter.update_keyword(
        tmp_path,
        "old keyword",
        "new keyword",
        "新分類",
    )

    assert updated.keyword == "new keyword"
    assert updated.folder == "新分類"
    assert file_sorter.read_custom_rules() == [updated]


def test_update_rejects_missing_or_conflicting_keyword(tmp_path: Path) -> None:
    (tmp_path / "分類").mkdir()
    file_sorter.add_keywords(tmp_path, ["first", "second"], "分類")

    with pytest.raises(file_sorter.FileSorterError):
        file_sorter.update_keyword(tmp_path, "missing", "new")
    with pytest.raises(file_sorter.FileSorterError):
        file_sorter.update_keyword(tmp_path, "first", "second")


def test_add_rejects_existing_keyword_and_requires_update(tmp_path: Path) -> None:
    (tmp_path / "分類").mkdir()
    file_sorter.add_keywords(tmp_path, ["red_velvet"], "分類")

    with pytest.raises(file_sorter.FileSorterError):
        file_sorter.add_keywords(tmp_path, ["red velvet"], "分類")


def test_update_keeps_existing_destination_when_folder_is_blank(tmp_path: Path) -> None:
    (tmp_path / "分類").mkdir()
    file_sorter.add_keywords(tmp_path, ["old"], "分類")

    updated = file_sorter.update_keyword(tmp_path, "old", "new")

    assert updated == file_sorter.KeywordRule(keyword="new", folder="分類")


def test_adds_keyword_to_external_destination_folder(tmp_path: Path) -> None:
    external_destination = tmp_path.parent / f"{tmp_path.name}-external"
    external_destination.mkdir()
    file_sorter.add_keywords(tmp_path, ["external_artist"], str(external_destination))
    source = tmp_path / "external_artist_clip.mp4"
    source.write_text("move me", encoding="utf-8")

    result = file_sorter.organize_files(tmp_path)

    assert result.errors == []
    assert result.moved_count == 1
    assert not source.exists()
    assert (external_destination / "external_artist_clip.mp4").exists()
    assert file_sorter.read_custom_rules() == [
        file_sorter.KeywordRule(
            keyword="external_artist",
            folder=str(external_destination.resolve()),
        )
    ]


def test_updates_keyword_to_external_destination_folder(tmp_path: Path) -> None:
    local_destination = tmp_path / "local"
    external_destination = tmp_path.parent / f"{tmp_path.name}-external-update"
    local_destination.mkdir()
    external_destination.mkdir()
    file_sorter.add_keywords(tmp_path, ["old"], local_destination.name)

    updated = file_sorter.update_keyword(
        tmp_path,
        "old",
        "new",
        str(external_destination),
    )

    assert updated == file_sorter.KeywordRule(
        keyword="new",
        folder=str(external_destination.resolve()),
    )


def test_upsert_existing_keyword_updates_external_destination_and_organizes(
    tmp_path: Path,
) -> None:
    local_destination = tmp_path / "local"
    external_destination = tmp_path.parent / f"{tmp_path.name}-upsert-external"
    local_destination.mkdir()
    external_destination.mkdir()
    file_sorter.add_keywords(tmp_path, ["artist"], local_destination.name)

    upsert_result = file_sorter.upsert_keywords(
        tmp_path,
        ["artist"],
        str(external_destination),
    )
    source = tmp_path / "artist_video.mp4"
    source.write_text("move me", encoding="utf-8")
    organize_result = file_sorter.organize_files(tmp_path)

    assert upsert_result.added == []
    assert upsert_result.updated == [
        file_sorter.KeywordRule(
            keyword="artist",
            folder=str(external_destination.resolve()),
        )
    ]
    assert organize_result.errors == []
    assert organize_result.moved_count == 1
    assert not source.exists()
    assert (external_destination / "artist_video.mp4").exists()


def test_lists_source_files_for_auto_detection(tmp_path: Path) -> None:
    (tmp_path / "destination").mkdir()
    (tmp_path / "new_file.txt").write_text("new", encoding="utf-8")
    (tmp_path / file_sorter.LEGACY_RULES_FILE_NAME).write_text("legacy", encoding="utf-8")
    (tmp_path / "destination" / "nested.txt").write_text("nested", encoding="utf-8")

    files = file_sorter.list_source_files(tmp_path)

    assert [item["name"] for item in files] == ["new_file.txt"]
    assert files[0]["size"] == 3


def test_scans_new_subfolders_without_creating_any(tmp_path: Path) -> None:
    (tmp_path / "原有").mkdir()
    assert file_sorter.list_destination_folders(tmp_path) == ["原有"]

    (tmp_path / "新增").mkdir()
    (tmp_path / ".隱藏").mkdir()

    assert set(file_sorter.list_destination_folders(tmp_path)) == {"新增", "原有"}
    assert not (tmp_path / "其他").exists()


def test_scans_destination_folders_from_separate_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    external_scan_root = tmp_path / "external-root"
    source.mkdir()
    external_scan_root.mkdir()
    (source / "來源分類").mkdir()
    (external_scan_root / "跨硬碟分類").mkdir()

    assert file_sorter.list_destination_folders(external_scan_root) == ["跨硬碟分類"]


def test_default_cli_does_not_print_keyword_rule_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "music").mkdir()
    (tmp_path / "music_video.mp4").write_text("move me", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["file-sorter", str(tmp_path)])

    exit_code = file_sorter.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "目前關鍵字規則" not in captured.out
    assert "歸檔完成" in captured.out
