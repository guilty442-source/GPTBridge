"""file-sorter consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "global-cleaner" / "src"),
    str(_ROOT / "ai-assistant" / "src"),
    str(_ROOT / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "vaultly" / "src" / "backend" / "services"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p


# -- CONSOLIDATED TEST SUITE --

########################################################################
# source: main-system/tests/test_file_sorter.py
########################################################################
from pathlib import Path

from file_sorter.application.cli import keyword_matches, resolve_destination_dir
from file_sorter.infrastructure.sorter_engine import load_profile


def test_ascii_keyword_matching_uses_word_boundaries() -> None:
    assert keyword_matches("annual report 2026", "report")
    assert not keyword_matches("reporting 2026", "report")
    assert keyword_matches("偶像_演唱會", "偶像")


def test_destination_must_be_existing_direct_child(tmp_path: Path) -> None:
    target = tmp_path / "inbox"
    destination = target / "reports"
    destination.mkdir(parents=True)
    assert resolve_destination_dir(target.resolve(), "reports") == destination.resolve()


def test_new_profile_automation_defaults_on(
    tmp_path: Path,
    isolated_sorter_state: Path,
) -> None:
    target = tmp_path / "inbox"
    target.mkdir()
    profile = load_profile(target, state_root=isolated_sorter_state)
    assert profile.enabled is True
    assert profile.duplicate_trash_enabled is False



########################################################################
# source: main-system/tests/test_file_sorter_automation.py
########################################################################
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



########################################################################
# source: main-system/tests/test_file_sorter_cleanup.py
########################################################################
import os
import json
from pathlib import Path
from unittest.mock import patch

from file_sorter.infrastructure.cleanup import (
    find_exact_duplicate_candidates,
    recycle_exact_duplicate_candidates,
    run_cleanup_scan,
)
from file_sorter.infrastructure.minicpm_visual_service import (
    MODEL_NAME,
    MiniCPMVisualRecognitionService,
)


def test_exact_duplicates_keep_oldest_and_recycle_only_extra(tmp_path: Path) -> None:
    target = tmp_path / "library"
    trash = tmp_path / "trash"
    target.mkdir()
    trash.mkdir()
    keep = target / "original.bin"
    duplicate = target / "copy.bin"
    keep.write_bytes(b"same-content")
    duplicate.write_bytes(b"same-content")
    os.utime(keep, ns=(1_000_000_000, 1_000_000_000))
    os.utime(duplicate, ns=(2_000_000_000, 2_000_000_000))

    candidates = find_exact_duplicate_candidates(target, quiet_seconds=0)
    assert len(candidates) == 1
    assert Path(candidates[0]["keep_path"]) == keep.resolve()
    assert Path(candidates[0]["path"]) == duplicate.resolve()

    def fake_recycler(path: Path) -> None:
        path.replace(trash / path.name)

    result = recycle_exact_duplicate_candidates(
        target,
        candidates,
        recycler=fake_recycler,
    )
    assert result["ok"] is True
    assert result["recycled_count"] == 1
    assert keep.exists()
    assert not duplicate.exists()


def test_minicpm_visual_service_recognizes_decodable_image(tmp_path: Path) -> None:
    from PIL import Image

    image_path = tmp_path / "empty-scene.png"
    Image.new("RGB", (640, 480), color=(240, 240, 240)).save(image_path)
    response_body = json.dumps({
        "message": {"content": json.dumps({
            "classification": "non_person_candidate",
            "confidence": 0.91,
            "reason": "畫面中沒有真人",
            "tags": ["空景"],
            "summary": "淺色空白畫面",
        }, ensure_ascii=False)}
    }, ensure_ascii=False).encode("utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return response_body

    with patch(
        "file_sorter.infrastructure.minicpm_visual_service.request.urlopen",
        return_value=FakeResponse(),
    ) as mocked:
        result = MiniCPMVisualRecognitionService().predict(image_path)

    assert result.classification == "non_person_candidate"
    assert result.model == MODEL_NAME
    assert result.tags == ("空景",)
    sent = json.loads(mocked.call_args.args[0].data.decode("utf-8"))
    assert sent["model"] == MODEL_NAME
    assert sent["messages"][0]["images"]


def test_minicpm_visual_service_compares_images_with_model(tmp_path: Path) -> None:
    from PIL import Image

    left = tmp_path / "left.png"
    right = tmp_path / "right.png"
    Image.new("RGB", (320, 240), color="navy").save(left)
    Image.new("RGB", (320, 240), color="navy").save(right)
    response_body = json.dumps({
        "message": {"content": json.dumps({
            "similarity": 99,
            "same_content": True,
            "reason": "主體與構圖相同",
        }, ensure_ascii=False)}
    }, ensure_ascii=False).encode("utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return response_body

    with patch(
        "file_sorter.infrastructure.minicpm_visual_service.request.urlopen",
        return_value=FakeResponse(),
    ) as mocked:
        result = MiniCPMVisualRecognitionService().compare(left, right, media_type="image")

    assert result.similarity == 99
    assert result.same_content is True
    sent = json.loads(mocked.call_args.args[0].data.decode("utf-8"))
    assert sent["model"] == MODEL_NAME
    assert len(sent["messages"][0]["images"]) == 2


def test_plain_file_scan_does_not_start_minicpm(tmp_path: Path) -> None:
    target = tmp_path / "library"
    target.mkdir()
    (target / "notes.txt").write_text("plain classification", encoding="utf-8")
    with patch(
        "file_sorter.infrastructure.minicpm_visual_service.MiniCPMVisualRecognitionService",
        side_effect=AssertionError("MiniCPM-V must stay off"),
    ):
        report = run_cleanup_scan(
            target,
            image_cleanup=False,
            similar_image_analysis=False,
            video_cleanup=True,
            similar_video_analysis=False,
        )
    assert report["ok"] is True
    assert report["visual_recognition_service"] == "openbmb/minicpm-v4.6:q8_0"
    assert report["visual_recognition_service_enabled"] is False



########################################################################
# source: main-system/tests/test_file_sorter_metadata_v2.py
########################################################################
from pathlib import Path

from file_sorter.infrastructure.sorter_engine import (
    _atomic_write_json,
    new_plan,
    prune_state,
    save_plan,
)


def test_prune_removes_expired_plans_but_keeps_journals_by_default(
    tmp_path: Path,
    isolated_sorter_state: Path,
) -> None:
    target = tmp_path / "inbox"
    target.mkdir()
    plan = new_plan(
        target,
        profile_id="target-12345678",
        rules_revision=0,
        quiet_seconds=0,
        operations=[],
        skipped=[],
    )
    plan.expires_at = "2000-01-01T00:00:00+00:00"
    plan_path = save_plan(plan, state_root=isolated_sorter_state)
    journal_path = isolated_sorter_state / "journals" / "12345678-journal.json"
    _atomic_write_json(
        journal_path,
        {
            "status": "committed",
            "created_at": "2000-01-01T00:00:00+00:00",
            "updated_at": "2000-01-01T00:00:00+00:00",
        },
    )

    default_result = prune_state(state_root=isolated_sorter_state)
    assert default_result["removed_plans"] == 1
    assert not plan_path.exists()
    assert journal_path.exists()

    retained_result = prune_state(
        state_root=isolated_sorter_state,
        journal_retention_days=30,
    )
    assert retained_result["removed_journals"] == 1
    assert not journal_path.exists()



########################################################################
# source: main-system/tests/test_file_sorter_ui.py
########################################################################
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


def test_ui_exposes_explicit_safe_automation_controls() -> None:
    source = (
        WORKSPACE_ROOT / "file-sorter" / "src" / "ui" / "FileSorterWindowApp.tsx"
    ).read_text(encoding="utf-8")
    assert "自動將完全重複檔移至 Windows 資源回收筒" in source
    assert "MiniCPM-V 4.6" in source
    assert "gptbridge.file-sorter.last-target-dir.v1" in source
    assert "invoke?.('dialog:validate-folder', savedTarget)" in source
    assert "localStorage.removeItem(LAST_TARGET_DIR_STORAGE_KEY)" in source
    assert "window.localStorage.setItem(LAST_TARGET_DIR_STORAGE_KEY, value)" in source
    assert "style={{ display: 'none' }}" not in source
    assert "setProfileEnabled, 'true'" not in source
    assert "'--cleanup-scan', '--json', '--progress-jsonl'" not in source
    assert "result.cancelled || cleanupStopRequestedRef.current" in source


def test_file_sorter_ui_is_in_main_typecheck_scope() -> None:
    tsconfig = (WORKSPACE_ROOT / "main-system" / "tsconfig.json").read_text(
        encoding="utf-8"
    )
    assert "../file-sorter/src/ui/**/*.tsx" in tsconfig



########################################################################
# source: main-system/tests/test_file_sorter_v2.py
########################################################################
from pathlib import Path

from file_sorter.application.cli import apply_organize_plan, preview_organize_files
from file_sorter.infrastructure.sorter_engine import (
    load_profile,
    undo_last_transaction,
)


def test_preview_apply_and_undo_round_trip(
    tmp_path: Path,
    isolated_sorter_state: Path,
) -> None:
    target = tmp_path / "inbox"
    destination = target / "reports"
    destination.mkdir(parents=True)
    source = target / "annual reports.txt"
    source.write_text("important", encoding="utf-8")
    load_profile(target, state_root=isolated_sorter_state)

    plan = preview_organize_files(
        target,
        quiet_seconds=0,
        state_root=isolated_sorter_state,
    )
    assert len(plan.operations) == 1
    applied = apply_organize_plan(
        plan.plan_id,
        target_dir=target,
        state_root=isolated_sorter_state,
    )
    assert applied["ok"] is True
    assert not source.exists()
    assert (destination / source.name).exists()

    undone = undo_last_transaction(target, state_root=isolated_sorter_state)
    assert undone["ok"] is True
    assert source.exists()
    assert not (destination / source.name).exists()
