from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "platform_tools"
    / "file-sorter"
    / "src"
    / "cleanup.py"
)
SPEC = importlib.util.spec_from_file_location("file_sorter_cleanup", MODULE_PATH)
assert SPEC and SPEC.loader
cleanup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup
SPEC.loader.exec_module(cleanup)

MAIN_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "platform_tools"
    / "file-sorter"
    / "src"
    / "main.py"
)
MAIN_SPEC = importlib.util.spec_from_file_location("file_sorter_main_for_cleanup", MAIN_MODULE_PATH)
assert MAIN_SPEC and MAIN_SPEC.loader
file_sorter = importlib.util.module_from_spec(MAIN_SPEC)
sys.modules[MAIN_SPEC.name] = file_sorter
MAIN_SPEC.loader.exec_module(file_sorter)


def _video_bytes(seed: bytes) -> bytes:
    return (seed * 1600)[:96_000]


def test_cleanup_scan_keeps_similar_video_and_removes_duplicate_modes(tmp_path: Path) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    unique = tmp_path / "unique.mp4"
    first.write_bytes(_video_bytes(b"similar-video-a"))
    second.write_bytes(_video_bytes(b"similar-video-a"))
    unique.write_bytes(_video_bytes(b"very-different-video"))

    report = cleanup.run_cleanup_scan(
        tmp_path,
        image_cleanup=False,
        video_cleanup=True,
        similar_video_analysis=True,
        similar_video_threshold=96,
        parallel_analysis=False,
    )

    assert "duplicate_analysis_enabled" not in report
    assert "duplicate_group_count" not in report
    assert "duplicate_copy_count" not in report
    assert "similar_image_analysis_enabled" not in report
    assert "similar_image_duplicate_count" not in report
    assert report["similar_video_analysis_enabled"] is True
    assert report["similar_video_duplicate_count"] == 1
    assert report["similar_video_duplicates"][0]["path"] == "second.mp4"
    assert report["similar_video_duplicates"][0]["similar_to"] == "first.mp4"

    categories = {
        category
        for found_file in report["found_files"]
        for category in found_file["categories"]
    }
    assert "exact_duplicate" not in categories
    assert "similar_image_duplicate" not in categories
    assert categories == {"similar_video_duplicate"}


def test_cleanup_scan_reports_video_issues_without_duplicate_categories(tmp_path: Path) -> None:
    (tmp_path / "too-small.mp4").write_bytes(b"tiny")

    report = cleanup.run_cleanup_scan(
        tmp_path,
        image_cleanup=False,
        video_cleanup=True,
        similar_video_analysis=False,
    )

    assert report["bad_video_file_count"] == 1
    assert report["found_files"][0]["categories"] == ["bad_video_file"]
    assert report["similar_video_duplicate_count"] == 0
    assert "duplicate_group_count" not in report


def test_cleanup_cli_outputs_json_from_file_sorter_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "first.mp4").write_bytes(_video_bytes(b"cli-similar"))
    (tmp_path / "second.mp4").write_bytes(_video_bytes(b"cli-similar"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "file-sorter",
            str(tmp_path),
            "--cleanup-scan",
            "--similar-video-analysis",
            "--json",
            "--no-parallel-analysis",
        ],
    )

    exit_code = file_sorter.main()
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["tool"] == "file-sorter-cleanup"
    assert report["similar_video_duplicate_count"] == 1
    assert "duplicate_analysis_enabled" not in report
