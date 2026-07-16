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


def test_video_fingerprint_cache_is_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FILE_SORTER_STATE_ROOT", str(tmp_path / "state"))
    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "first.mp4").write_bytes(_video_bytes(b"cached-video"))
    (media_root / "second.mp4").write_bytes(_video_bytes(b"cached-video"))

    first = cleanup.run_cleanup_scan(
        media_root,
        image_cleanup=False,
        video_cleanup=True,
        similar_video_analysis=True,
        parallel_analysis=False,
    )
    second = cleanup.run_cleanup_scan(
        media_root,
        image_cleanup=False,
        video_cleanup=True,
        similar_video_analysis=True,
        parallel_analysis=False,
    )

    assert first["video_fingerprint_cache_hit_count"] == 0
    assert second["video_fingerprint_cache_hit_count"] == 2
    assert second["similar_video_duplicate_count"] == 1


def test_large_similarity_scan_uses_lsh_candidate_buckets(tmp_path: Path) -> None:
    scanner = cleanup.CleanupScanner(
        tmp_path,
        similar_video_threshold=96,
        parallel_analysis=False,
    )
    candidates = [
        {"path": f"{index:03}.mp4", "hashes": [f"{index * 0x1000100010001:016x}"]}
        for index in range(80)
    ]
    candidates[79]["hashes"] = list(candidates[0]["hashes"])

    neighbors = scanner._video_candidate_neighbors(candidates)
    candidate_pairs = sum(len(items) for items in neighbors) // 2

    assert 79 in neighbors[0]
    assert candidate_pairs < (80 * 79) // 2


def test_frame_difference_hash_is_content_sensitive() -> None:
    flat = bytes([10] * 72)
    gradient = bytes(range(72))

    assert cleanup.CleanupScanner._frame_difference_hash(flat) is not None
    assert cleanup.CleanupScanner._frame_difference_hash(gradient) is not None
    assert cleanup.CleanupScanner._frame_difference_hash(flat) != (
        cleanup.CleanupScanner._frame_difference_hash(gradient)
    )


def test_cleanup_prunes_nested_protected_folders(tmp_path: Path) -> None:
    protected = tmp_path / "nested" / ".git"
    protected.mkdir(parents=True)
    (protected / "hidden.mp4").write_bytes(_video_bytes(b"hidden"))

    report = cleanup.run_cleanup_scan(
        tmp_path,
        image_cleanup=False,
        video_cleanup=True,
        similar_video_analysis=False,
    )

    assert report["source_file_count"] == 0
    assert report["video_file_count"] == 0
