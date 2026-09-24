"""file-sorter test module (A57/E43)

source: main-system/tests/test_file_sorter_automation.py
"""
from __future__ import annotations

import _sorter_test_boot  # noqa: F401  # sys.path bootstrap

import asyncio
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


def test_automation_classifies_only_into_first_level_subfolders(
    tmp_path: Path,
    isolated_sorter_state: Path,
) -> None:
    target = tmp_path / "inbox"
    reports = target / "reports"
    nested = reports / "2026"
    nested.mkdir(parents=True)
    music = target / "music"
    music.mkdir()
    top_report = target / "annual reports.txt"
    top_report.write_text("report", encoding="utf-8")
    nested_named = target / "2026 review.txt"
    nested_named.write_text("nested-name", encoding="utf-8")
    song = target / "song music.mp3"
    song.write_text("music", encoding="utf-8")

    profile = load_profile(target, state_root=isolated_sorter_state)
    save_profile(profile, enabled=True, quiet_seconds=0)

    first = cli.run_enabled_profiles_once(state_root=isolated_sorter_state)
    assert first[0]["moved_count"] == 0
    second = cli.run_enabled_profiles_once(state_root=isolated_sorter_state)

    report = second[0]
    assert report["ok"] is True
    assert report["moved_count"] == 2
    assert report["unmatched_count"] == 1
    report_destination = reports / "annual reports.txt"
    music_destination = music / "song music.mp3"
    assert report_destination.exists()
    assert music_destination.exists()
    assert not top_report.exists()
    assert not song.exists()
    for destination in (report_destination, music_destination):
        assert destination.parent.parent == target
    assert nested_named.exists()
    assert not (nested / "2026 review.txt").exists()


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

class _StubRunner:
    def __init__(self, calls: list[str], targets: list[str] | None = None) -> None:
        self.calls = calls
        self.targets = targets or []

    def enabled_profile_targets(self) -> list[str]:
        return self.targets

    def recover_transactions(self) -> list[dict[str, Any]]:
        self.calls.append("recover")
        return []

    def prune_state(self) -> dict[str, Any]:
        return {}

    def run_enabled_profiles_once(self) -> list[dict[str, Any]]:
        self.calls.append("profiles")
        return []


def test_first_pass_waits_for_startup_delay(tmp_path: Path) -> None:
    calls: list[str] = []
    service = FileSorterAutomationService(
        tmp_path,
        runner=_StubRunner(calls),
        poll_interval=86_400,
        startup_delay_seconds=0.05,
    )

    async def drive() -> None:
        await service.start()
        await asyncio.sleep(0.01)
        assert calls == []
        await asyncio.sleep(0.3)
        assert "profiles" in calls
        await service.stop()

    asyncio.run(drive())


def test_stop_during_startup_delay_never_runs(tmp_path: Path) -> None:
    calls: list[str] = []
    service = FileSorterAutomationService(
        tmp_path,
        runner=_StubRunner(calls),
        poll_interval=86_400,
        startup_delay_seconds=60.0,
    )

    async def drive() -> None:
        await service.start()
        await asyncio.sleep(0.02)
        await service.stop()

    asyncio.run(drive())
    assert calls == []


def test_snapshot_watches_nested_directories(tmp_path: Path) -> None:
    target = tmp_path / "inbox"
    nested = target / "sub" / "deep"
    nested.mkdir(parents=True)
    (target / "top.txt").write_text("x")
    service = FileSorterAutomationService(
        tmp_path,
        runner=_StubRunner([], targets=[str(target)]),
    )

    first = service._snapshot_enabled_targets()
    entries = first[str(target)]
    kinds = {entry[0] for entry in entries}
    assert kinds == {"f", "d"}
    assert any(
        entry[0] == "d" and entry[1] == str(Path("sub") / "deep")
        for entry in entries
    )

    (nested / "new.bin").write_bytes(b"payload")
    second = service._snapshot_enabled_targets()
    assert second != first


def test_snapshot_ignores_symlinked_directories(tmp_path: Path) -> None:
    target = tmp_path / "inbox"
    outside = tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    (outside / "o.txt").write_text("x")
    link = target / "linked"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    service = FileSorterAutomationService(
        tmp_path,
        runner=_StubRunner([], targets=[str(target)]),
    )
    entries = service._snapshot_enabled_targets()[str(target)]
    assert all(entry[1] != "linked" for entry in entries)