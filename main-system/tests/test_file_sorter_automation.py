from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from file_sorter.application import cli
from file_sorter.application.automation_service import FileSorterAutomationService
from file_sorter.infrastructure.sorter_engine import load_profile, save_profile


def test_disabled_profile_is_not_monitored_or_run(
    tmp_path: Path,
    isolated_sorter_state: Path,
) -> None:
    target = tmp_path / "inbox"
    target.mkdir()
    profile = load_profile(target, state_root=isolated_sorter_state)
    save_profile(profile, enabled=False)
    assert cli.enabled_profile_targets(state_root=isolated_sorter_state) == []
    assert cli.run_enabled_profiles_once(state_root=isolated_sorter_state) == []


def test_duplicate_recycling_requires_two_identical_observations(
    tmp_path: Path,
    isolated_sorter_state: Path,
    monkeypatch: Any,
) -> None:
    target = tmp_path / "inbox"
    target.mkdir()
    keep = target / "old.bin"
    duplicate = target / "new.bin"
    keep.write_bytes(b"duplicate")
    duplicate.write_bytes(b"duplicate")
    os.utime(keep, ns=(1_000_000_000, 1_000_000_000))
    os.utime(duplicate, ns=(2_000_000_000, 2_000_000_000))
    profile = load_profile(target, state_root=isolated_sorter_state)
    save_profile(
        profile,
        duplicate_trash_enabled=True,
        quiet_seconds=0,
    )

    def fake_recycle(_target: Path, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        for candidate in candidates:
            Path(candidate["path"]).unlink()
        return {
            "ok": True,
            "target_dir": str(target),
            "recycled_count": len(candidates),
            "recycled": candidates,
            "errors": [],
        }

    monkeypatch.setattr(cli, "recycle_exact_duplicate_candidates", fake_recycle)
    first = cli.run_enabled_profiles_once(state_root=isolated_sorter_state)
    assert first[0]["recycled_count"] == 0
    assert duplicate.exists()
    second = cli.run_enabled_profiles_once(state_root=isolated_sorter_state)
    assert second[0]["recycled_count"] == 1
    assert keep.exists()
    assert not duplicate.exists()


def test_adaptive_monitoring_slows_down_when_folder_stays_idle(tmp_path: Path) -> None:
    service = FileSorterAutomationService(tmp_path, poll_interval=86_400)
    baseline = {str(tmp_path): tuple((f"file-{index}", 1, 1) for index in range(20))}

    initial = service._adaptive_scan_interval(baseline, settling=False)
    service._unchanged_scan_count = 1
    after_one_unchanged_scan = service._adaptive_scan_interval(
        baseline, settling=False
    )
    service._unchanged_scan_count = 2
    after_two_unchanged_scans = service._adaptive_scan_interval(
        baseline, settling=False
    )
    service._unchanged_scan_count = 30
    idle = service._adaptive_scan_interval(baseline, settling=False)

    assert initial == 10.0
    assert after_one_unchanged_scan == initial
    assert after_two_unchanged_scans == 30.0
    assert initial < idle <= 86_400
    assert service._adaptive_scan_interval(baseline, settling=True) == 10.0


def test_adaptive_monitoring_never_exceeds_one_day(tmp_path: Path) -> None:
    service = FileSorterAutomationService(tmp_path, poll_interval=86_400)
    service._unchanged_scan_count = 10_000

    assert service._adaptive_scan_interval({}, settling=False) == 86_400


def test_adaptive_monitoring_uses_explicit_interval_tiers(tmp_path: Path) -> None:
    service = FileSorterAutomationService(tmp_path, poll_interval=86_400)
    baseline: dict[str, tuple[tuple[str, int, int], ...]] = {}
    expected = {
        0: 10.0,
        2: 30.0,
        4: 60.0,
        6: 300.0,
        8: 900.0,
        10: 3_600.0,
        12: 21_600.0,
        14: 43_200.0,
        16: 86_400.0,
    }

    for unchanged_count, interval in expected.items():
        service._unchanged_scan_count = unchanged_count
        assert service._adaptive_scan_interval(baseline, settling=False) == interval


def test_new_keyword_scan_returns_only_the_selected_target(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    target = tmp_path / "inbox"
    target.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(
        cli,
        "run_enabled_profiles_once",
        lambda **_kwargs: [
            {"target_dir": str(other), "moved_count": 8},
            {"target_dir": str(target), "moved_count": 1},
        ],
    )

    report = cli.scan_after_keyword_addition(target)

    assert report is not None
    assert report["target_dir"] == str(target)
