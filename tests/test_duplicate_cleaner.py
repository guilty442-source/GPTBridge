from __future__ import annotations

import ast
import builtins
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "platform_tools"
    / "tool-mpz30cfk-hfnf"
    / "src"
    / "main.py"
)
SPEC = importlib.util.spec_from_file_location("duplicate_cleaner_main", MODULE_PATH)
assert SPEC and SPEC.loader
duplicate_cleaner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = duplicate_cleaner
SPEC.loader.exec_module(duplicate_cleaner)


def test_backend_user_facing_strings_are_ascii() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    non_ascii_strings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if any(ord(char) > 127 for char in node.value):
            non_ascii_strings.append((node.lineno, node.value))

    assert non_ascii_strings == []


def test_visible_sources_do_not_contain_mojibake_codepoints() -> None:
    source_paths = [
        MODULE_PATH,
        *sorted((ROOT / "platform_tools").glob("*/src/ui/toolWindowRunner.ts")),
        ROOT
        / "platform_tools"
        / "tool-mpz30cfk-hfnf"
        / "src"
        / "ui"
        / "DuplicateCleanerWindowApp.tsx",
        ROOT / "src-ui" / "renderer" / "locales" / "zh-TW.ts",
    ]
    offenders = []
    for path in source_paths:
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if "\ufffd" in line or any(
                0xE000 <= ord(char) <= 0xF8FF for char in line
            ):
                offenders.append(f"{path}:{line_number}: {line}")

    assert offenders == []


@pytest.fixture(autouse=True)
def isolate_duplicate_cleaner_app_data(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_data = tmp_path_factory.mktemp("duplicate-cleaner-app-data")
    monkeypatch.setenv("LOCALAPPDATA", str(app_data))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


def write_scenic_image(path: Path, size: tuple[int, int] = (900, 450)) -> None:
    image_module = pytest.importorskip("PIL.Image")
    draw_module = pytest.importorskip("PIL.ImageDraw")
    width, height = size
    image = image_module.new("RGB", size, color=(95, 174, 235))
    draw = draw_module.Draw(image)
    horizon = int(height * 0.48)
    draw.rectangle((0, horizon, width, height), fill=(42, 145, 72))
    draw.polygon(
        [
            (0, horizon),
            (int(width * 0.30), int(height * 0.26)),
            (int(width * 0.58), horizon),
        ],
        fill=(72, 94, 84),
    )
    draw.polygon(
        [
            (int(width * 0.34), horizon),
            (int(width * 0.72), int(height * 0.22)),
            (width, horizon),
        ],
        fill=(62, 86, 80),
    )
    draw.rectangle(
        (0, int(height * 0.78), width, height),
        fill=(37, 120, 63),
    )
    image.save(path)


class FakeLandscapeImage:
    def __init__(
        self,
        rows: list[list[tuple[int, int, int]]],
        size: tuple[int, int] = (900, 450),
    ) -> None:
        self.rows = rows
        self.size = size

    def convert(self, _mode: str) -> "FakeLandscapeImage":
        return self

    def resize(self, size: tuple[int, int]) -> "FakeLandscapeSample":
        return FakeLandscapeSample(self.rows, size)


class FakeLandscapeSample(FakeLandscapeImage):
    def getpixel(self, point: tuple[int, int]) -> tuple[int, int, int]:
        x, y = point
        return self.rows[y][x]


def fake_scenic_image() -> FakeLandscapeImage:
    rows = [[(95, 174, 235) for _x in range(64)] for _y in range(64)]
    for y in range(31, 64):
        for x in range(64):
            rows[y][x] = (42, 145, 72)
    for y in range(16, 34):
        span = y - 16
        for x in range(max(0, 18 - span), min(64, 18 + span + 1)):
            rows[y][x] = (72, 94, 84)
        for x in range(max(0, 45 - span), min(64, 45 + span + 1)):
            rows[y][x] = (88, 110, 94)
    for y in range(42, 64, 4):
        for x in range(0, 64, 8):
            rows[y][x] = (34, 115, 58)
    return FakeLandscapeImage(rows)


def fake_flat_split_image() -> FakeLandscapeImage:
    rows = [[(95, 174, 235) for _x in range(64)] for _y in range(64)]
    for y in range(32, 64):
        for x in range(64):
            rows[y][x] = (42, 145, 72)
    return FakeLandscapeImage(rows)


def fake_product_card_image() -> FakeLandscapeImage:
    rows = [[(245, 245, 245) for _x in range(64)] for _y in range(64)]
    for y in range(0, 18):
        for x in range(64):
            rows[y][x] = (80, 160, 230)
    for y in range(26, 56):
        for x in range(4, 28):
            rows[y][x] = (45, 150, 90)
        for x in range(34, 60):
            rows[y][x] = (210, 210, 210)
    return FakeLandscapeImage(rows)


def test_dry_run_reports_duplicates_without_moving_files(tmp_path: Path) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "nested" / "second.bin"
    second.parent.mkdir()
    first.write_bytes(b"same content")
    second.write_bytes(b"same content")
    (tmp_path / "unique.bin").write_bytes(b"unique")

    report = duplicate_cleaner.DuplicateCleaner(tmp_path).run()

    assert report["duplicate_group_count"] == 1
    assert report["duplicate_copy_count"] == 1
    assert report["moved_file_count"] == 0
    assert first.exists()
    assert second.exists()
    assert not (tmp_path / duplicate_cleaner.REPORT_FILE_NAME).exists()
    assert not (tmp_path / duplicate_cleaner.BACKUP_DIR_NAME).exists()
    report_path = Path(report["report_file"])
    assert report_path.exists()
    assert report_path.name == duplicate_cleaner.REPORT_FILE_NAME
    assert report_path.parent.name == duplicate_cleaner.REPORTS_DIR_NAME
    assert tmp_path not in report_path.parents
    assert report["report_file"] == str(report_path)


def test_auto_move_keeps_one_copy_and_uses_app_data_backup_folder(
    tmp_path: Path,
) -> None:
    duplicate_paths = [
        tmp_path / "a.bin",
        tmp_path / "nested" / "b.bin",
        tmp_path / "nested" / "c.bin",
    ]
    (tmp_path / "nested").mkdir()
    for path in duplicate_paths:
        path.write_bytes(b"duplicate")

    report = duplicate_cleaner.DuplicateCleaner(tmp_path, auto_move=True).run()

    assert report["duplicate_group_count"] == 1
    assert report["duplicate_copy_count"] == 2
    assert report["moved_file_count"] == 2
    assert sum(path.exists() for path in duplicate_paths) == 1
    assert not (tmp_path / duplicate_cleaner.BACKUP_DIR_NAME).exists()
    backup_root = Path(report["backup_dir"])
    assert tmp_path not in backup_root.parents
    backup_files = [
        path
        for path in (backup_root / "exact_duplicates").rglob("*")
        if path.is_file()
    ]
    assert len(backup_files) == 2


def test_delete_exact_duplicates_during_scan_keeps_one_copy(
    tmp_path: Path,
) -> None:
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    third = tmp_path / "c.bin"
    for path in (first, second, third):
        path.write_bytes(b"duplicate")
    events: list[dict[str, object]] = []

    report = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        delete_exact_during_scan=True,
        progress_event_callback=events.append,
    ).run()

    assert first.exists()
    assert not second.exists()
    assert not third.exists()
    assert report["duplicate_group_count"] == 0
    assert report["duplicate_copy_count"] == 0
    assert report["deleted_during_scan_count"] == 2
    assert report["deleted_during_scan_bytes"] == len(b"duplicate") * 2
    assert [entry["path"] for entry in report["deleted_during_scan"]] == [
        "b.bin",
        "c.bin",
    ]
    found_by_path = {entry["path"]: entry for entry in report["found_files"]}
    assert found_by_path["b.bin"]["categories"] == ["exact_duplicate"]
    assert found_by_path["b.bin"]["deleted_during_scan"] is True
    assert found_by_path["b.bin"]["kept_path"] == "a.bin"
    assert any(
        event["phase"] == "found_file"
        and event["found_file"]["path"] == "b.bin"
        and event["found_file"]["deleted_during_scan"] is True
        for event in events
    )


def test_delete_exact_during_scan_skips_deleted_media_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keep = tmp_path / "a.jpg"
    duplicate = tmp_path / "b.jpg"
    keep.write_bytes(b"duplicate image")
    duplicate.write_bytes(b"duplicate image")
    analyzed: list[str] = []

    def fake_process_image_candidate(
        self: object,
        path: Path,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        analyzed.append(path.name)

    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_process_image_candidate",
        fake_process_image_candidate,
    )

    duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_images=True,
        delete_exact_during_scan=True,
        image_analysis_workers=1,
    ).run()

    assert analyzed == ["a.jpg"]
    assert keep.exists()
    assert not duplicate.exists()


def test_custom_report_path_still_overrides_default_reports_folder(
    tmp_path: Path,
) -> None:
    custom_report = tmp_path / "custom" / "report.json"
    (tmp_path / "first.bin").write_bytes(b"duplicate")
    (tmp_path / "second.bin").write_bytes(b"duplicate")

    report = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        report_path=custom_report,
    ).run()

    assert custom_report.exists()
    assert report["report_file"] == str(custom_report.resolve())
    assert not (tmp_path / duplicate_cleaner.REPORT_FILE_NAME).exists()


def test_backup_and_existing_report_are_excluded_from_future_scans(
    tmp_path: Path,
) -> None:
    (tmp_path / "first.bin").write_bytes(b"duplicate")
    (tmp_path / "second.bin").write_bytes(b"duplicate")
    duplicate_cleaner.DuplicateCleaner(tmp_path, auto_move=True).run()

    report = duplicate_cleaner.DuplicateCleaner(tmp_path).run()

    assert report["duplicate_group_count"] == 0
    assert report["duplicate_copy_count"] == 0


def test_auto_move_preserves_relative_paths_for_same_file_names(
    tmp_path: Path,
) -> None:
    first = tmp_path / "a" / "same.txt"
    second = tmp_path / "b" / "same.txt"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("duplicate", encoding="utf-8")
    second.write_text("duplicate", encoding="utf-8")

    report = duplicate_cleaner.DuplicateCleaner(tmp_path, auto_move=True).run()

    moved = report["moved_files"][0]
    destination = Path(moved["destination"])
    backup_relative = Path(moved["destination_backup_path"])
    assert destination.is_absolute()
    assert "exact_duplicates" in destination.parts
    assert destination.parent.name == "b"
    assert destination.name == "same.txt"
    assert backup_relative.parts[-2:] == ("b", "same.txt")
    assert tmp_path not in destination.parents
    assert first.exists()
    assert not second.exists()


def test_cli_json_outputs_parseable_report_only(tmp_path: Path) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"duplicate")
    second.write_bytes(b"duplicate")

    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            str(tmp_path),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    report = json.loads(completed.stdout)
    assert completed.stderr == ""
    assert report["duplicate_group_count"] == 1
    assert report["duplicate_copy_count"] == 1


def test_image_analysis_is_opt_in_by_default(tmp_path: Path) -> None:
    image_module = pytest.importorskip("PIL.Image")
    image_module.new("RGB", (900, 450), color=(40, 120, 200)).save(
        tmp_path / "wide_landscape.jpg"
    )

    report = duplicate_cleaner.DuplicateCleaner(tmp_path).run()

    assert report["image_analysis_enabled"] is False
    assert report["image_file_count"] == 1
    assert report["landscape_images"] == []
    assert report["non_portrait_images"] == []
    assert report["found_files"] == []


def test_cli_image_analysis_requires_explicit_flag(tmp_path: Path) -> None:
    write_scenic_image(tmp_path / "wide_landscape.jpg")

    disabled = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            str(tmp_path),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    enabled = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            str(tmp_path),
            "--json",
            "--image-analysis",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    disabled_report = json.loads(disabled.stdout)
    enabled_report = json.loads(enabled.stdout)
    assert disabled_report["image_analysis_enabled"] is False
    assert disabled_report["found_files"] == []
    assert enabled_report["image_analysis_enabled"] is True
    assert enabled_report["landscape_images"] == ["wide_landscape.jpg"]


def test_image_analysis_reports_landscape_and_non_portrait_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_module = pytest.importorskip("PIL.Image")
    landscape = tmp_path / "wide_landscape.jpg"
    portrait = tmp_path / "tall_portrait.jpg"
    write_scenic_image(landscape)
    image_module.new("RGB", (450, 900), color=(220, 180, 160)).save(portrait)
    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_load_face_detector",
        staticmethod(lambda: (object(), "test-face-detector")),
    )
    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_image_has_face",
        staticmethod(lambda _image, _detector, _config=None: False),
    )

    report = duplicate_cleaner.DuplicateCleaner(tmp_path, analyze_images=True).run()

    assert report["image_analysis_enabled"] is True
    assert report["image_file_count"] == 2
    assert "wide_landscape.jpg" in report["landscape_images"]
    assert "tall_portrait.jpg" not in report["landscape_images"]
    assert report["non_portrait_images"] == []
    found_by_path = {entry["path"]: entry for entry in report["found_files"]}
    assert found_by_path["wide_landscape.jpg"]["categories"] == ["landscape_image"]
    assert found_by_path["wide_landscape.jpg"]["thumbnail"].startswith(
        "data:image/jpeg;base64,"
    )
    assert found_by_path["wide_landscape.jpg"]["ai_landscape_score"] >= 0.56
    assert "sky" in found_by_path["wide_landscape.jpg"]["dominant_landscape_features"]
    assert report["face_detection"]["profile"] == "safe"
    assert report["face_detection"]["allow_no_face_non_portrait"] is False


def test_non_portrait_requires_explicit_no_face_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    landscape = tmp_path / "wide_landscape.jpg"
    write_scenic_image(landscape)
    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_load_face_detector",
        staticmethod(lambda: (object(), "test-face-detector")),
    )
    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_image_has_face",
        staticmethod(lambda _image, _detector, _config=None: False),
    )

    report = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_images=True,
        allow_no_face_non_portrait=True,
    ).run()

    assert report["non_portrait_images"] == ["wide_landscape.jpg"]
    found_by_path = {entry["path"]: entry for entry in report["found_files"]}
    assert found_by_path["wide_landscape.jpg"]["categories"] == [
        "landscape_image",
        "non_portrait_image",
    ]


def test_ai_landscape_features_detect_portrait_scenic_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenic = tmp_path / "portrait_scenery.jpg"
    write_scenic_image(scenic, size=(450, 900))
    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_load_face_detector",
        staticmethod(lambda: (None, "pillow+orientation-heuristic")),
    )

    report = duplicate_cleaner.DuplicateCleaner(tmp_path, analyze_images=True).run()

    assert report["landscape_images"] == ["portrait_scenery.jpg"]
    assert report["ai_landscape_features_enabled"] is True
    detail = report["image_details"][0]
    assert detail["shape_landscape"] is False
    assert detail["ai_landscape_detected"] is True
    assert detail["classification_reason"] == "semantic_landscape_features"
    assert {"sky", "vegetation"}.issubset(
        set(detail["dominant_landscape_features"])
    )


def test_ai_landscape_features_can_be_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenic = tmp_path / "portrait_scenery.jpg"
    write_scenic_image(scenic, size=(450, 900))
    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_load_face_detector",
        staticmethod(lambda: (None, "pillow+orientation-heuristic")),
    )

    report = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_images=True,
        analyze_landscape_features=False,
    ).run()

    assert report["landscape_images"] == []
    assert report["found_files"] == []
    assert report["ai_landscape_features_enabled"] is False
    assert report["image_details"][0]["ai_landscape_features"]["enabled"] is False


def test_landscape_detector_requires_spatial_structure_without_pillow(
    tmp_path: Path,
) -> None:
    cleaner = duplicate_cleaner.DuplicateCleaner(tmp_path, analyze_images=True)

    scenic = cleaner._analyze_landscape_features(fake_scenic_image())
    flat_split = cleaner._analyze_landscape_features(fake_flat_split_image())
    product_card = cleaner._analyze_landscape_features(fake_product_card_image())

    assert scenic["detected"] is True
    assert "spatial_structure" in scenic["dominant_features"]
    assert flat_split["detected"] is False
    assert flat_split["reason"] in {
        "graphic_or_flat_layout_rejected",
        "insufficient_spatial_structure",
        "partial_landscape_features",
    }
    assert product_card["detected"] is False


def test_landscape_candidate_runs_through_face_gate_without_pillow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeImage:
        size = (900, 450)

    def landscape_hit(_self: object, _image: object) -> dict[str, object]:
        return {
            "enabled": True,
            "method": "test",
            "score": 0.95,
            "threshold": 0.56,
            "detected": True,
            "confidence": "high",
            "features": [],
            "dominant_features": ["sky", "vegetation"],
            "reason": "semantic_landscape_features",
        }

    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_analyze_landscape_features",
        landscape_hit,
    )
    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_image_has_face",
        staticmethod(lambda _image, _detector, _config=None: True),
    )
    cleaner = duplicate_cleaner.DuplicateCleaner(tmp_path, analyze_images=True)

    classification = cleaner._classify_image(FakeImage(), object())

    assert classification["is_landscape"] is False
    assert classification["reason"] == "face_detected"
    assert classification["landscape_features"]["raw_detected"] is True
    assert classification["landscape_features"]["detected"] is False
    assert classification["landscape_features"]["reason"] == "face_detected"


def test_landscape_candidate_passes_after_no_face_check_without_pillow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeImage:
        size = (900, 450)

    def landscape_hit(_self: object, _image: object) -> dict[str, object]:
        return {
            "enabled": True,
            "method": "test",
            "score": 0.95,
            "threshold": 0.56,
            "detected": True,
            "confidence": "high",
            "features": [],
            "dominant_features": ["sky", "vegetation"],
            "reason": "semantic_landscape_features",
        }

    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_analyze_landscape_features",
        landscape_hit,
    )
    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_image_has_face",
        staticmethod(lambda _image, _detector, _config=None: False),
    )
    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_image_has_person",
        staticmethod(lambda _image, _detector, _config=None: False),
    )
    cleaner = duplicate_cleaner.DuplicateCleaner(tmp_path, analyze_images=True)

    classification = cleaner._classify_image(FakeImage(), object(), object())

    assert classification["is_landscape"] is True
    assert classification["reason"] == "semantic_landscape_features"
    assert classification["has_face"] is False
    assert classification["has_person"] is False


def test_uncertain_face_detection_does_not_create_non_portrait_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_module = pytest.importorskip("PIL.Image")
    plain_wide = tmp_path / "plain_wide.jpg"
    image_module.new("RGB", (900, 450), color=(40, 120, 200)).save(plain_wide)
    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_load_face_detector",
        staticmethod(lambda: (None, "pillow+orientation-heuristic")),
    )

    report = duplicate_cleaner.DuplicateCleaner(tmp_path, analyze_images=True).run()

    assert report["landscape_images"] == []
    assert report["non_portrait_images"] == []
    assert report["found_files"] == []
    assert report["image_details"][0]["classification_reason"] == (
        "wide_image_face_detection_uncertain"
    )
    assert report["image_details"][0]["ai_landscape_detected"] is False


def test_duplicate_mode_can_be_disabled_without_hiding_image_results(
    tmp_path: Path,
) -> None:
    (tmp_path / "first.bin").write_bytes(b"duplicate")
    (tmp_path / "second.bin").write_bytes(b"duplicate")
    write_scenic_image(tmp_path / "wide_landscape.jpg")

    report = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_duplicates=False,
        analyze_images=True,
    ).run()

    assert report["duplicate_analysis_enabled"] is False
    assert report["duplicate_group_count"] == 0
    assert report["duplicate_copy_count"] == 0
    assert [entry["path"] for entry in report["found_files"]] == ["wide_landscape.jpg"]


def test_image_analysis_does_not_treat_missed_face_as_non_portrait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_module = pytest.importorskip("PIL.Image")
    portrait = tmp_path / "portrait_face_missed.jpg"
    image_module.new("RGB", (450, 900), color=(220, 180, 160)).save(portrait)

    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_load_face_detector",
        staticmethod(lambda: (object(), "test-face-detector")),
    )
    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_image_has_face",
        staticmethod(lambda _image, _detector, _config=None: False),
    )

    report = duplicate_cleaner.DuplicateCleaner(tmp_path, analyze_images=True).run()

    assert report["non_portrait_images"] == []
    assert report["found_files"] == []
    assert report["image_details"][0]["classification_reason"] == (
        "no_face_detected_but_shape_is_portrait_like"
    )


def test_no_face_opt_in_reports_portrait_shaped_image_as_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_module = pytest.importorskip("PIL.Image")
    portrait_shaped = tmp_path / "poster_without_face.jpg"
    image_module.new("RGB", (450, 900), color=(80, 120, 160)).save(portrait_shaped)

    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_load_face_detector",
        staticmethod(lambda: (object(), "test-face-detector")),
    )
    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_image_has_face",
        staticmethod(lambda _image, _detector, _config=None: False),
    )

    report = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_images=True,
        analyze_landscape_features=False,
        allow_no_face_non_portrait=True,
    ).run()

    assert report["landscape_images"] == []
    assert report["non_portrait_images"] == ["poster_without_face.jpg"]
    found_by_path = {entry["path"]: entry for entry in report["found_files"]}
    assert found_by_path["poster_without_face.jpg"]["categories"] == [
        "non_portrait_image"
    ]
    assert report["image_details"][0]["classification_reason"] == "no_face_detected"


def test_no_face_opt_in_classifies_any_shape_without_pillow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeImage:
        size = (450, 900)

    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_image_has_face",
        staticmethod(lambda _image, _detector, _config=None: False),
    )
    cleaner = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_images=True,
        analyze_landscape_features=False,
        allow_no_face_non_portrait=True,
    )

    classification = cleaner._classify_image(FakeImage(), object())

    assert classification["is_non_portrait"] is True
    assert classification["has_face"] is False
    assert classification["reason"] == "no_face_detected"


def test_no_face_opt_in_keeps_small_images_out_without_pillow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SmallImage:
        size = (180, 240)

    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_image_has_face",
        staticmethod(lambda _image, _detector, _config=None: False),
    )
    cleaner = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_images=True,
        analyze_landscape_features=False,
        allow_no_face_non_portrait=True,
    )

    classification = cleaner._classify_image(SmallImage(), object())

    assert classification["is_non_portrait"] is False
    assert classification["reason"] == "image_too_small_for_reliable_filter"


def test_detection_sensitivity_speed_and_parallel_settings_are_configurable(
    tmp_path: Path,
) -> None:
    cleaner = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_images=True,
        face_sensitivity=90,
        person_sensitivity=20,
        landscape_sensitivity=80,
        analysis_speed=85,
        parallel_analysis=False,
    )

    assert cleaner.face_detection.face_sensitivity == 90
    assert cleaner.face_detection.person_sensitivity == 20
    assert cleaner.face_detection.analysis_speed == 85
    assert cleaner.landscape_sensitivity == 80
    assert cleaner.image_analysis_workers == 1
    assert cleaner._face_detection_report()["landscape_sensitivity"] == 80


def test_person_detection_blocks_no_face_candidate_without_pillow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeImage:
        size = (900, 450)

    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_image_has_face",
        staticmethod(lambda _image, _detector, _config=None: False),
    )
    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_image_has_person",
        staticmethod(lambda _image, _detector, _config=None: True),
    )
    cleaner = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_images=True,
        analyze_landscape_features=False,
        allow_no_face_non_portrait=True,
    )

    classification = cleaner._classify_image(FakeImage(), object(), object())

    assert classification["is_non_portrait"] is False
    assert classification["has_face"] is False
    assert classification["has_person"] is True
    assert classification["reason"] == "person_detected"


def test_similar_image_detection_is_separate_from_exact_duplicates(
    tmp_path: Path,
) -> None:
    keep = tmp_path / "a.jpg"
    similar = tmp_path / "b.jpg"
    exact = tmp_path / "exact.jpg"
    for path in (keep, similar, exact):
        path.write_bytes(b"image")
    cleaner = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_images=True,
        analyze_similar_images=True,
        similar_image_threshold=98,
    )
    image_report = cleaner._empty_image_report(
        enabled=True,
        image_count=3,
        method="test",
    )
    image_report["image_details"] = [
        {"path": "a.jpg", "perceptual_hash": "0000000000000000"},
        {"path": "b.jpg", "perceptual_hash": "0000000000000001"},
        {"path": "exact.jpg", "perceptual_hash": "0000000000000001"},
    ]
    exact_group = duplicate_cleaner.DuplicateGroup(
        sha256="digest",
        size=5,
        keep=keep,
        duplicates=(exact,),
    )

    cleaner._append_similar_media_results(image_report, [exact_group])

    assert image_report["similar_image_duplicate_count"] == 1
    assert image_report["similar_image_duplicates"][0]["path"] == "b.jpg"
    assert image_report["similar_image_duplicates"][0]["similar_to"] == "a.jpg"
    assert image_report["similar_image_duplicates"][0]["image_similarity"] == 98


def test_similar_video_detection_uses_its_own_threshold(tmp_path: Path) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"video-one")
    second.write_bytes(b"video-two")
    cleaner = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_similar_videos=True,
        similar_video_threshold=98,
    )
    image_report = cleaner._empty_image_report(
        enabled=False,
        image_count=0,
        method="disabled",
    )
    image_report["video_details"] = [
        {
            "path": "first.mp4",
            "perceptual_hashes": ["0000000000000000"],
        },
        {
            "path": "second.mp4",
            "perceptual_hashes": ["0000000000000001"],
        },
    ]

    cleaner._append_similar_media_results(image_report, [])

    assert image_report["similar_video_duplicate_count"] == 1
    assert image_report["similar_video_duplicates"][0]["path"] == "second.mp4"
    assert image_report["similar_video_duplicates"][0]["similar_to"] == "first.mp4"
    assert image_report["similar_video_duplicates"][0]["video_similarity"] == 98


def test_image_hash_prefers_flattened_data_without_deprecated_getdata() -> None:
    class FakeSample:
        def get_flattened_data(self) -> list[int]:
            return list(range(72))

        def getdata(self) -> list[int]:
            raise AssertionError("getdata should not be called")

    class FakeImage:
        def convert(self, _mode: str) -> "FakeImage":
            return self

        def resize(self, size: tuple[int, int]) -> FakeSample:
            assert size == (9, 8)
            return FakeSample()

    assert duplicate_cleaner.DuplicateCleaner._image_perceptual_hash(FakeImage())


def test_video_fingerprint_uses_file_sampling_without_cv2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "sample.mp4"
    video.write_bytes((b"ftypmp42-video-data-" * 128))
    real_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "cv2":
            raise AssertionError("cv2 should not be imported for video fingerprint")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    cleaner = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_similar_videos=True,
    )

    detail = cleaner._video_fingerprint(video)

    assert detail is not None
    assert detail["method"] == duplicate_cleaner.VIDEO_FINGERPRINT_METHOD
    assert detail["perceptual_hashes"]


def test_similar_video_scan_uses_file_sampling(tmp_path: Path) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    payload = b"ftypmp42" + bytes(range(256)) * 16
    first.write_bytes(payload)
    second.write_bytes(payload)

    report = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_duplicates=False,
        analyze_similar_videos=True,
        similar_video_threshold=100,
    ).run()

    assert report["video_analysis_method"] == duplicate_cleaner.VIDEO_FINGERPRINT_METHOD
    assert report["video_file_count"] == 2
    assert report["video_details"][0]["method"] == duplicate_cleaner.VIDEO_FINGERPRINT_METHOD
    assert report["similar_video_duplicate_count"] == 1
    assert report["similar_video_duplicates"][0]["path"] == "second.mp4"
    assert report["found_files"][0]["categories"] == ["similar_video_duplicate"]


def test_large_videos_are_listed_without_duplicate_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"large-video-a")
    second.write_bytes(b"large-video-b")

    def fail_duplicate_processing(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("large videos should not enter duplicate hashing")

    monkeypatch.setattr(duplicate_cleaner, "VIDEO_FINGERPRINT_MAX_BYTES", 8)
    monkeypatch.setattr(
        duplicate_cleaner.DuplicateCleaner,
        "_process_duplicate_candidate",
        fail_duplicate_processing,
    )

    report = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_duplicates=True,
        analyze_similar_videos=True,
    ).run()

    assert report["large_video_file_count"] == 2
    assert report["bad_video_file_count"] == 0
    assert report["video_details"] == []
    assert report["duplicate_copy_count"] == 0
    assert {
        tuple(entry["categories"])
        for entry in report["found_files"]
    } == {("large_video_file",)}


def test_bad_videos_are_listed_separately(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"")

    report = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_similar_videos=True,
    ).run()

    assert report["bad_video_file_count"] == 1
    assert report["large_video_file_count"] == 0
    assert report["bad_video_files"] == [
        {"path": "bad.mp4", "size": 0, "reason": "empty_file"}
    ]
    assert report["video_details"] == []
    assert report["found_files"][0]["categories"] == ["bad_video_file"]


def test_image_analysis_ignores_small_wide_images_to_reduce_false_positives(
    tmp_path: Path,
) -> None:
    image_module = pytest.importorskip("PIL.Image")
    banner = tmp_path / "small_banner.jpg"
    image_module.new("RGB", (300, 100), color=(40, 120, 200)).save(banner)

    report = duplicate_cleaner.DuplicateCleaner(tmp_path, analyze_images=True).run()

    assert report["landscape_images"] == []
    assert report["non_portrait_images"] == []
    assert report["found_files"] == []
    assert report["image_details"][0]["classification_reason"] == (
        "image_too_small_for_reliable_filter"
    )


def test_delete_selected_removes_only_checked_relative_files(
    tmp_path: Path,
) -> None:
    keep = tmp_path / "keep.bin"
    duplicate = tmp_path / "nested" / "delete_me.bin"
    duplicate.parent.mkdir()
    keep.write_bytes(b"duplicate")
    duplicate.write_bytes(b"duplicate")
    (tmp_path / "safe.bin").write_bytes(b"safe")
    scan_report = duplicate_cleaner.DuplicateCleaner(tmp_path).run()
    selected_path = scan_report["found_files"][0]["path"]

    delete_report = duplicate_cleaner.DuplicateCleaner(tmp_path).delete_selected(
        [selected_path, "../outside.bin", duplicate_cleaner.REPORT_FILE_NAME]
    )

    assert delete_report["deleted_file_count"] == 1
    assert delete_report["skipped_file_count"] == 2
    assert not duplicate.exists()
    assert keep.exists()
    assert (tmp_path / "safe.bin").exists()


def test_delete_selected_skips_unverified_unique_files(
    tmp_path: Path,
) -> None:
    unique = tmp_path / "unique.txt"
    unique.write_text("not a duplicate", encoding="utf-8")

    delete_report = duplicate_cleaner.DuplicateCleaner(tmp_path).delete_selected(
        ["unique.txt"]
    )

    assert delete_report["deleted_file_count"] == 0
    assert delete_report["skipped_file_count"] == 1
    assert delete_report["skipped_files"] == [
        {"path": "unique.txt", "reason": "not_verified_exact_duplicate"}
    ]
    assert unique.exists()


def test_cli_deletes_selected_json_paths(tmp_path: Path) -> None:
    doomed = tmp_path / "delete_me.txt"
    keep = tmp_path / "keep.txt"
    doomed.write_text("duplicate", encoding="utf-8")
    keep.write_text("duplicate", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            str(tmp_path),
            "--delete-selected-json",
            json.dumps(["delete_me.txt"]),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    report = json.loads(completed.stdout)
    assert report["action"] == "delete_selected"
    assert report["deleted_file_count"] == 1
    assert not doomed.exists()
    assert keep.exists()


def test_cli_progress_jsonl_reports_folders_and_found_files(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (tmp_path / "first.bin").write_bytes(b"duplicate")
    (nested / "second.bin").write_bytes(b"duplicate")

    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            str(tmp_path),
            "--json",
            "--progress-jsonl",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    progress_events = []
    report_lines = []
    for line in completed.stdout.splitlines():
        if line.startswith(duplicate_cleaner.PROGRESS_JSON_PREFIX):
            progress_events.append(
                json.loads(line[len(duplicate_cleaner.PROGRESS_JSON_PREFIX) :])
            )
        else:
            report_lines.append(line)

    report = json.loads("\n".join(report_lines))
    assert report["duplicate_copy_count"] == 1
    assert any(
        event["phase"] in {"folder_scan", "folder_done"}
        and event["folder_total"] == 2
        for event in progress_events
    )
    assert any(
        event["phase"] == "found_file"
        and event["found_file"]["categories"] == ["exact_duplicate"]
        for event in progress_events
    )


def test_scan_emits_duplicate_result_before_folder_finishes(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    keep = tmp_path / "z_keep.bin"
    duplicate = nested / "a_duplicate.bin"
    keep.write_bytes(b"duplicate")
    duplicate.write_bytes(b"duplicate")
    events: list[dict[str, object]] = []

    report = duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_images=False,
        progress_event_callback=events.append,
    ).run()

    duplicate_path = duplicate_cleaner._relative_path_text(duplicate, tmp_path)
    keep_path = duplicate_cleaner._relative_path_text(keep, tmp_path)
    found_index = next(
        index
        for index, event in enumerate(events)
        if event["phase"] == "found_file"
        and event["found_file"]["path"] == duplicate_path
        and event["found_file"]["categories"] == ["exact_duplicate"]
    )
    folder_done_index = next(
        index
        for index, event in enumerate(events)
        if event["phase"] == "folder_done" and event["current_folder"] == "nested"
    )

    assert found_index < folder_done_index
    assert events[found_index]["current_folder"] == "nested"
    assert report["duplicate_groups"][0]["keep"] == keep_path
    assert report["duplicate_groups"][0]["duplicates"] == [duplicate_path]


def test_scan_emits_image_result_before_folder_finishes(tmp_path: Path) -> None:
    nested = tmp_path / "images"
    nested.mkdir()
    landscape = nested / "wide.jpg"
    write_scenic_image(landscape)
    events: list[dict[str, object]] = []

    duplicate_cleaner.DuplicateCleaner(
        tmp_path,
        analyze_images=True,
        progress_event_callback=events.append,
    ).run()

    landscape_path = duplicate_cleaner._relative_path_text(landscape, tmp_path)
    found_index = next(
        index
        for index, event in enumerate(events)
        if event["phase"] == "found_file"
        and event["found_file"]["path"] == landscape_path
        and "landscape_image" in event["found_file"]["categories"]
    )
    folder_done_index = next(
        index
        for index, event in enumerate(events)
        if event["phase"] == "folder_done" and event["current_folder"] == "images"
    )

    assert found_index < folder_done_index
    assert events[found_index]["current_folder"] == "images"
