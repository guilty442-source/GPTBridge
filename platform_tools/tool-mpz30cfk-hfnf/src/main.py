from __future__ import annotations

import argparse
import base64
import concurrent.futures
import colorsys
import hashlib
import io
import json
import os
import shutil
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


BACKUP_DIR_NAME = "_cleaner_backup"
APP_DATA_DIR_NAME = "GPTBridge"
DUPLICATE_CLEANER_DATA_DIR_NAME = "duplicate-cleaner"
REPORTS_DIR_NAME = "reports"
REPORT_FILE_NAME = "cleaner_report.json"
DEFAULT_HASH_CHUNK_SIZE = 1024 * 1024
VIDEO_FINGERPRINT_METHOD = "file-sampled-video-fingerprint-v1"
VIDEO_FINGERPRINT_MIN_BYTES = 1024
VIDEO_FINGERPRINT_CHUNK_SIZE = 64 * 1024
VIDEO_FINGERPRINT_MAX_BYTES = 4 * 1024 * 1024 * 1024
THUMBNAIL_MAX_EDGE = 128
IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
)
VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".avi",
        ".flv",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".webm",
        ".wmv",
    }
)
LANDSCAPE_ASPECT_RATIO = 1.45
NON_PORTRAIT_ASPECT_RATIO = 1.35
MIN_CLASSIFIABLE_IMAGE_EDGE = 256
AI_LANDSCAPE_FEATURE_METHOD = "pillow-local-landscape-features-v1"
AI_LANDSCAPE_FEATURE_SAMPLE_SIZE = 64
AI_LANDSCAPE_FEATURE_THRESHOLD = 0.56
AI_LANDSCAPE_ACTIVE_FEATURE_SCORE = 0.35
DEFAULT_IMAGE_ANALYSIS_WORKERS = min(4, max(1, os.cpu_count() or 1))
DEFAULT_FACE_DETECTION_SENSITIVITY = 75
DEFAULT_PERSON_DETECTION_SENSITIVITY = 75
DEFAULT_LANDSCAPE_DETECTION_SENSITIVITY = 65
DEFAULT_ANALYSIS_SPEED = 50
DEFAULT_SIMILAR_IMAGE_THRESHOLD = 96
DEFAULT_SIMILAR_VIDEO_THRESHOLD = 96
DEFAULT_DETECTION_SENSITIVITY = DEFAULT_FACE_DETECTION_SENSITIVITY
PROGRESS_JSON_PREFIX = "DUPLICATE_CLEANER_PROGRESS_JSON="
CATEGORY_EXACT_DUPLICATE = "exact_duplicate"
CATEGORY_SIMILAR_IMAGE_DUPLICATE = "similar_image_duplicate"
CATEGORY_SIMILAR_VIDEO_DUPLICATE = "similar_video_duplicate"
CATEGORY_LARGE_VIDEO_FILE = "large_video_file"
CATEGORY_BAD_VIDEO_FILE = "bad_video_file"
CATEGORY_LANDSCAPE_IMAGE = "landscape_image"
CATEGORY_NON_PORTRAIT_IMAGE = "non_portrait_image"
FACE_DETECTION_PROFILES = {
    "safe": {
        "scale_factor": 1.05,
        "min_neighbors": 3,
        "min_size_ratio": 20,
        "min_size_px": 24,
        "allow_shape_only_non_portrait": False,
        "allow_no_face_non_portrait": False,
    },
    "standard": {
        "scale_factor": 1.08,
        "min_neighbors": 4,
        "min_size_ratio": 16,
        "min_size_px": 28,
        "allow_shape_only_non_portrait": False,
        "allow_no_face_non_portrait": False,
    },
    "strict": {
        "scale_factor": 1.1,
        "min_neighbors": 6,
        "min_size_ratio": 12,
        "min_size_px": 36,
        "allow_shape_only_non_portrait": False,
        "allow_no_face_non_portrait": False,
    },
}


ProgressCallback = Callable[[str], None]
ProgressEventCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class FaceDetectionConfig:
    profile: str = "safe"
    sensitivity: int = DEFAULT_FACE_DETECTION_SENSITIVITY
    face_sensitivity: int = DEFAULT_FACE_DETECTION_SENSITIVITY
    person_sensitivity: int = DEFAULT_PERSON_DETECTION_SENSITIVITY
    analysis_speed: int = DEFAULT_ANALYSIS_SPEED
    image_workers: int = DEFAULT_IMAGE_ANALYSIS_WORKERS
    scale_factor: float = 1.05
    min_neighbors: int = 3
    min_size_ratio: int = 20
    min_size_px: int = 24
    relaxed_neighbors: int = 2
    relaxed_min_size_ratio: int = 24
    relaxed_min_size_px: int = 18
    person_hog_weight: float = 0.15
    person_scale: float = 1.08
    person_min_neighbors: int = 2
    face_max_edge: int = 1200
    person_max_edge: int = 920
    allow_shape_only_non_portrait: bool = False
    allow_no_face_non_portrait: bool = False


@dataclass(frozen=True)
class DetectionSensitivityConfig:
    value: int
    scale_factor: float
    min_neighbors: int
    min_size_ratio: int
    min_size_px: int
    relaxed_neighbors: int
    relaxed_min_size_ratio: int
    relaxed_min_size_px: int
    person_hog_weight: float
    person_scale: float
    person_min_neighbors: int


@dataclass(frozen=True)
class AnalysisSpeedConfig:
    value: int
    workers: int
    face_max_edge: int
    person_max_edge: int


def _clamp_percent(value: int | float | None) -> int:
    if value is None:
        return DEFAULT_DETECTION_SENSITIVITY
    return max(1, min(100, int(round(float(value)))))


def _build_detection_sensitivity(value: int | float | None) -> DetectionSensitivityConfig:
    sensitivity = _clamp_percent(value)
    strength = sensitivity / 100
    return DetectionSensitivityConfig(
        value=sensitivity,
        scale_factor=round(1.12 - 0.08 * strength, 4),
        min_neighbors=max(1, round(7 - 5 * strength)),
        min_size_ratio=max(8, round(12 + 18 * strength)),
        min_size_px=max(16, round(44 - 24 * strength)),
        relaxed_neighbors=max(1, round(5 - 4 * strength)),
        relaxed_min_size_ratio=max(8, round(10 + 20 * strength)),
        relaxed_min_size_px=max(16, round(34 - 18 * strength)),
        person_hog_weight=round(0.45 - 0.40 * strength, 4),
        person_scale=round(1.12 - 0.08 * strength, 4),
        person_min_neighbors=max(1, round(4 - 3 * strength)),
    )


def _build_analysis_speed(value: int | float | None) -> AnalysisSpeedConfig:
    speed = _clamp_percent(DEFAULT_ANALYSIS_SPEED if value is None else value)
    strength = speed / 100
    cpu_count = max(1, os.cpu_count() or 1)
    return AnalysisSpeedConfig(
        value=speed,
        workers=max(1, min(8, cpu_count, round(1 + 7 * strength))),
        face_max_edge=max(720, round(1600 - 800 * strength)),
        person_max_edge=max(640, round(1200 - 560 * strength)),
    )


def _landscape_threshold_from_sensitivity(value: int | float | None) -> float:
    sensitivity = _clamp_percent(
        DEFAULT_LANDSCAPE_DETECTION_SENSITIVITY if value is None else value
    )
    return round(max(0.35, min(0.95, 0.9 - 0.52 * (sensitivity / 100))), 4)


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            continue


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_relative_path(path: Path, root: Path) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name)
    return Path(*[part for part in relative.parts if part not in {"", ".", ".."}])


def _relative_path_text(path: Path, root: Path) -> str:
    return str(_safe_relative_path(path, root))


def _app_data_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data).expanduser() / APP_DATA_DIR_NAME

    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        return Path(xdg_data_home).expanduser() / APP_DATA_DIR_NAME

    return Path.home() / ".gptbridge"


def _target_storage_key(target_dir: Path) -> str:
    digest = hashlib.sha256(str(target_dir).casefold().encode("utf-8")).hexdigest()
    slug = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in target_dir.name
    ).strip("_")
    return f"{(slug or 'target')[:48]}-{digest[:16]}"


def _default_storage_root(target_dir: Path) -> Path:
    return (
        _app_data_root()
        / DUPLICATE_CLEANER_DATA_DIR_NAME
        / _target_storage_key(target_dir)
    )


@dataclass(frozen=True)
class DuplicateGroup:
    sha256: str
    size: int
    keep: Path
    duplicates: tuple[Path, ...]

    def to_report(self, root: Path) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "size": self.size,
            "keep": _relative_path_text(self.keep, root),
            "duplicates": [
                _relative_path_text(path, root) for path in self.duplicates
            ],
        }


class DuplicateCleaner:
    def __init__(
        self,
        target_dir: str | Path,
        *,
        auto_move: bool = False,
        delete_exact_during_scan: bool = False,
        analyze_duplicates: bool = True,
        analyze_images: bool = False,
        analyze_landscape_features: bool = True,
        ai_landscape_threshold: float | None = None,
        face_sensitivity: int | None = None,
        person_sensitivity: int | None = None,
        landscape_sensitivity: int | None = None,
        analysis_speed: int | None = None,
        analyze_similar_images: bool = False,
        similar_image_threshold: int | None = None,
        analyze_similar_videos: bool = False,
        similar_video_threshold: int | None = None,
        parallel_analysis: bool = True,
        face_detection_profile: str = "safe",
        face_scale_factor: float | None = None,
        face_min_neighbors: int | None = None,
        face_min_size_ratio: int | None = None,
        face_min_size_px: int | None = None,
        detection_sensitivity: int | None = None,
        allow_shape_only_non_portrait: bool | None = None,
        allow_no_face_non_portrait: bool | None = None,
        image_analysis_workers: int | None = None,
        report_path: str | Path | None = None,
        hash_chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
        progress_callback: ProgressCallback | None = None,
        progress_event_callback: ProgressEventCallback | None = None,
    ) -> None:
        self.target_dir = Path(target_dir).expanduser().resolve()
        self.auto_move = auto_move
        self.delete_exact_during_scan = bool(delete_exact_during_scan)
        self.analyze_duplicates = analyze_duplicates
        self.analyze_similar_images = bool(analyze_similar_images)
        self.similar_image_threshold = _clamp_percent(
            DEFAULT_SIMILAR_IMAGE_THRESHOLD
            if similar_image_threshold is None
            else similar_image_threshold
        )
        self.analyze_similar_videos = bool(analyze_similar_videos)
        self.similar_video_threshold = _clamp_percent(
            DEFAULT_SIMILAR_VIDEO_THRESHOLD
            if similar_video_threshold is None
            else similar_video_threshold
        )
        self.parallel_analysis = bool(parallel_analysis)
        self.analyze_images = bool(analyze_images or self.analyze_similar_images)
        self.analyze_landscape_features = bool(analyze_landscape_features)
        self.landscape_sensitivity = _clamp_percent(
            DEFAULT_LANDSCAPE_DETECTION_SENSITIVITY
            if landscape_sensitivity is None
            else landscape_sensitivity
        )
        if ai_landscape_threshold is None:
            ai_landscape_threshold = _landscape_threshold_from_sensitivity(
                self.landscape_sensitivity
            )
        self.ai_landscape_threshold = max(
            0.35,
            min(0.95, float(ai_landscape_threshold)),
        )
        self.face_detection = self._build_face_detection_config(
            face_detection_profile,
            face_scale_factor=face_scale_factor,
            face_min_neighbors=face_min_neighbors,
            face_min_size_ratio=face_min_size_ratio,
            face_min_size_px=face_min_size_px,
            detection_sensitivity=detection_sensitivity,
            face_sensitivity=face_sensitivity,
            person_sensitivity=person_sensitivity,
            analysis_speed=analysis_speed,
            allow_shape_only_non_portrait=allow_shape_only_non_portrait,
            allow_no_face_non_portrait=allow_no_face_non_portrait,
        )
        configured_workers = max(
            1,
            min(
                8,
                int(
                    image_analysis_workers
                    if image_analysis_workers is not None
                    else self.face_detection.image_workers
                ),
            ),
        )
        self.image_analysis_workers = (
            configured_workers if self.parallel_analysis else 1
        )
        self.backup_root = _default_storage_root(self.target_dir).resolve()
        self.report_path = (
            Path(report_path).expanduser().resolve()
            if report_path is not None
            else self.backup_root / REPORTS_DIR_NAME / REPORT_FILE_NAME
        )
        self.hash_chunk_size = max(64 * 1024, int(hash_chunk_size))
        self.progress_callback = progress_callback
        self.progress_event_callback = progress_event_callback
        self._progress_found_paths: set[str] = set()
        self._progress_found_categories: dict[str, set[str]] = {}
        self._deleted_during_scan: list[dict[str, Any]] = []
        self._image_worker_local = threading.local()

    @staticmethod
    def _build_face_detection_config(
        profile: str,
        *,
        face_scale_factor: float | None,
        face_min_neighbors: int | None,
        face_min_size_ratio: int | None,
        face_min_size_px: int | None,
        detection_sensitivity: int | None,
        face_sensitivity: int | None,
        person_sensitivity: int | None,
        analysis_speed: int | None,
        allow_shape_only_non_portrait: bool | None,
        allow_no_face_non_portrait: bool | None,
    ) -> FaceDetectionConfig:
        profile_key = str(profile or "safe").strip().lower()
        base = FACE_DETECTION_PROFILES.get(
            profile_key,
            FACE_DETECTION_PROFILES["safe"],
        )
        face_sensitivity_cfg = _build_detection_sensitivity(
            face_sensitivity
            if face_sensitivity is not None
            else detection_sensitivity
        )
        person_sensitivity_cfg = _build_detection_sensitivity(
            person_sensitivity
            if person_sensitivity is not None
            else detection_sensitivity
        )
        speed_cfg = _build_analysis_speed(analysis_speed)
        scale_factor = float(
            face_scale_factor
            if face_scale_factor is not None
            else face_sensitivity_cfg.scale_factor
        )
        min_neighbors = int(
            face_min_neighbors
            if face_min_neighbors is not None
            else face_sensitivity_cfg.min_neighbors
        )
        min_size_ratio = int(
            face_min_size_ratio
            if face_min_size_ratio is not None
            else face_sensitivity_cfg.min_size_ratio
        )
        min_size_px = int(
            face_min_size_px
            if face_min_size_px is not None
            else face_sensitivity_cfg.min_size_px
        )
        return FaceDetectionConfig(
            profile=profile_key if profile_key in FACE_DETECTION_PROFILES else "safe",
            sensitivity=face_sensitivity_cfg.value,
            face_sensitivity=face_sensitivity_cfg.value,
            person_sensitivity=person_sensitivity_cfg.value,
            analysis_speed=speed_cfg.value,
            image_workers=speed_cfg.workers,
            scale_factor=max(1.01, min(1.3, scale_factor)),
            min_neighbors=max(1, min(12, min_neighbors)),
            min_size_ratio=max(8, min(40, min_size_ratio)),
            min_size_px=max(16, min(96, min_size_px)),
            relaxed_neighbors=face_sensitivity_cfg.relaxed_neighbors,
            relaxed_min_size_ratio=face_sensitivity_cfg.relaxed_min_size_ratio,
            relaxed_min_size_px=face_sensitivity_cfg.relaxed_min_size_px,
            person_hog_weight=person_sensitivity_cfg.person_hog_weight,
            person_scale=person_sensitivity_cfg.person_scale,
            person_min_neighbors=person_sensitivity_cfg.person_min_neighbors,
            face_max_edge=speed_cfg.face_max_edge,
            person_max_edge=speed_cfg.person_max_edge,
            allow_shape_only_non_portrait=(
                bool(base["allow_shape_only_non_portrait"])
                if allow_shape_only_non_portrait is None
                else bool(allow_shape_only_non_portrait)
            ),
            allow_no_face_non_portrait=(
                bool(base["allow_no_face_non_portrait"])
                if allow_no_face_non_portrait is None
                else bool(allow_no_face_non_portrait)
            ),
        )

    def run(self) -> dict[str, Any]:
        self._validate_target()
        started_at = _utc_now()
        self._progress_found_paths = set()
        self._progress_found_categories = {}
        self._deleted_during_scan = []
        folders = [self.target_dir]
        scan_result = self._scan_folders(folders)
        files = scan_result["files"]
        scan_folder_count = int(scan_result["scan_folder_count"])
        self._log(f"Scanning {len(files)} files.")

        image_report = scan_result["image_report"]
        groups = scan_result["duplicate_groups"]
        found_files = self._build_found_files(groups, image_report)
        moved_files = self._move_duplicate_files(groups) if self.auto_move else []
        report = self._build_report(
            started_at,
            files,
            groups,
            moved_files,
            image_report,
            found_files,
            scan_folder_count,
        )
        self._write_report(report)
        self._emit_progress(
            "scan_complete",
            "Scan complete",
            folder_current=scan_folder_count,
            folder_total=scan_folder_count,
            source_file_count=len(files),
            found_file_count=len(found_files),
            report_file=str(self.report_path),
        )
        self._log(
            "Scan finished: "
            f"{report['duplicate_group_count']} duplicate groups, "
            f"{report['duplicate_copy_count']} duplicate copies, "
            f"{report['landscape_image_count']} landscape images, "
            f"{report['non_portrait_image_count']} non-portrait images."
        )
        return report

    def delete_selected(
        self,
        selected_paths: Sequence[str],
        *,
        allow_unverified: bool = False,
    ) -> dict[str, Any]:
        self._validate_target()
        started_at = _utc_now()
        deleted_files: list[str] = []
        skipped_files: list[dict[str, str]] = []
        missing_files: list[str] = []
        seen: set[str] = set()
        normalized_items: list[tuple[str, str, Path]] = []

        for raw_path in selected_paths:
            normalized = self._normalize_selected_path(raw_path)
            if normalized is None:
                skipped_files.append(
                    {"path": str(raw_path), "reason": "unsafe_or_empty_path"}
                )
                continue

            normalized_text = str(normalized)
            normalized_key = normalized_text.casefold()
            if normalized_key in seen:
                continue
            seen.add(normalized_key)

            target = (self.target_dir / normalized).resolve()
            normalized_items.append((normalized_text, normalized_key, target))

        verified_duplicate_keys = (
            set(seen)
            if allow_unverified
            else self._selected_exact_duplicate_delete_keys(seen)
        )

        for normalized_text, normalized_key, target in normalized_items:
            if not self._is_inside_target(target):
                skipped_files.append({"path": normalized_text, "reason": "outside_target"})
                continue
            if self._is_excluded(target):
                skipped_files.append({"path": normalized_text, "reason": "protected_path"})
                continue
            if not target.exists():
                missing_files.append(normalized_text)
                continue
            if not target.is_file():
                skipped_files.append({"path": normalized_text, "reason": "not_a_file"})
                continue
            if normalized_key not in verified_duplicate_keys:
                skipped_files.append(
                    {
                        "path": normalized_text,
                        "reason": "not_verified_exact_duplicate",
                    }
                )
                continue

            try:
                target.unlink()
            except OSError as exc:
                skipped_files.append({"path": normalized_text, "reason": str(exc)})
                continue
            deleted_files.append(normalized_text)

        report = {
            "ok": True,
            "tool": "duplicate-cleaner",
            "action": "delete_selected",
            "target_dir": str(self.target_dir),
            "started_at": started_at,
            "finished_at": _utc_now(),
            "selected_file_count": len(selected_paths),
            "unique_selected_file_count": len(seen),
            "allow_unverified_delete": allow_unverified,
            "verified_duplicate_file_count": len(verified_duplicate_keys),
            "deleted_file_count": len(deleted_files),
            "skipped_file_count": len(skipped_files),
            "missing_file_count": len(missing_files),
            "deleted_files": deleted_files,
            "skipped_files": skipped_files,
            "missing_files": missing_files,
            "report_file": str(self.report_path),
        }
        self._write_report(report)
        self._log(f"Deleted {len(deleted_files)} selected files.")
        return report

    def _validate_target(self) -> None:
        if not self.target_dir.exists():
            raise FileNotFoundError(f"Target folder does not exist: {self.target_dir}")
        if not self.target_dir.is_dir():
            raise NotADirectoryError(f"Target path is not a folder: {self.target_dir}")

    def _iter_candidate_files(self) -> Iterable[Path]:
        for path in self.target_dir.rglob("*"):
            if not path.is_file():
                continue
            if self._is_excluded(path):
                continue
            yield path

    def _list_scan_folders(self) -> list[Path]:
        folders = [self.target_dir]
        folders.extend(
            path
            for path in self.target_dir.rglob("*")
            if path.is_dir() and not self._is_excluded_folder(path)
        )
        return sorted(
            folders,
            key=lambda item: _relative_path_text(item, self.target_dir).casefold(),
        )

    def _scan_folders(self, folders: list[Path]) -> dict[str, Any]:
        files: list[Path] = []
        files_by_size: dict[int, list[Path]] = {}
        hash_cache: dict[Path, str] = {}
        image_report, image_helpers = self._create_image_report()
        image_executor = self._create_image_executor(image_helpers)
        self._emit_progress(
            "scan_started",
            "Folder scan started",
            folder_current=0,
            folder_total=len(folders),
            source_file_count=0,
            found_file_count=0,
        )

        try:
            folder_index = 0
            while folder_index < len(folders):
                folder = folders[folder_index]
                folder_index += 1
                self._scan_folder(
                    folder,
                    folder_index=folder_index,
                    folder_total=len(folders),
                    folders=folders,
                    files=files,
                    files_by_size=files_by_size,
                    hash_cache=hash_cache,
                    image_report=image_report,
                    image_helpers=image_helpers,
                    image_executor=image_executor,
                )
        finally:
            if image_executor is not None:
                image_executor.shutdown(wait=True)

        duplicate_groups = (
            self._build_duplicate_groups_from_hashes(
                files_by_size,
                hash_cache,
            )
            if self.analyze_duplicates
            else []
        )
        self._append_similar_media_results(image_report, duplicate_groups)

        return {
            "files": files,
            "duplicate_groups": duplicate_groups,
            "image_report": image_report,
            "scan_folder_count": len(folders),
        }

    def _scan_folder(
        self,
        folder: Path,
        *,
        folder_index: int,
        folder_total: int,
        folders: list[Path],
        files: list[Path],
        files_by_size: dict[int, list[Path]],
        hash_cache: dict[Path, str],
        image_report: dict[str, Any],
        image_helpers: dict[str, Any],
        image_executor: concurrent.futures.ThreadPoolExecutor | None,
    ) -> None:
            folder_text = _relative_path_text(folder, self.target_dir)
            self._emit_progress(
                "folder_scan",
                f"Scanning folder {folder_index}/{folder_total}: {folder_text}",
                folder_current=folder_index,
                folder_total=folder_total,
                current_folder=folder_text,
                source_file_count=len(files),
                found_file_count=len(self._progress_found_paths),
            )
            try:
                entries = sorted(
                    folder.iterdir(),
                    key=lambda item: item.name.casefold(),
                )
            except OSError as exc:
                self._emit_progress(
                    "folder_skipped",
                    f"Could not read folder {folder_text}: {exc}",
                    folder_current=folder_index,
                    folder_total=folder_total,
                    current_folder=folder_text,
                    source_file_count=len(files),
                    found_file_count=len(self._progress_found_paths),
                )
                return

            media_jobs: list[
                tuple[
                    concurrent.futures.Future[dict[str, Any]],
                    Path,
                    int,
                ]
            ] = []
            for path in entries:
                if path.is_dir():
                    if not self._is_excluded_folder(path):
                        folders.append(path)
                    continue
                if not path.is_file():
                    continue
                if self._is_excluded(path):
                    continue
                files.append(path)
                try:
                    size = path.stat().st_size
                except OSError as exc:
                    if self.analyze_similar_videos and self._is_video_candidate(path):
                        self._process_video_issue_candidate(
                            path,
                            self._video_issue(
                                CATEGORY_BAD_VIDEO_FILE,
                                "stat_failed",
                                error=str(exc),
                            ),
                            image_report,
                            folder_text=folder_text,
                            folder_current=folder_index,
                            folder_total=folder_total,
                            source_file_count=len(files),
                        )
                    continue
                if self.analyze_similar_videos and self._is_video_candidate(path):
                    video_issue = self._video_direct_issue(path, size)
                    if video_issue:
                        self._process_video_issue_candidate(
                            path,
                            video_issue,
                            image_report,
                            folder_text=folder_text,
                            folder_current=folder_index,
                            folder_total=folder_total,
                            source_file_count=len(files),
                        )
                        continue
                if self.analyze_duplicates:
                    deleted_current = self._process_duplicate_candidate(
                        path,
                        size,
                        files_by_size,
                        hash_cache,
                        folder_text=folder_text,
                        folder_current=folder_index,
                        folder_total=folder_total,
                        source_file_count=len(files),
                    )
                    if deleted_current:
                        continue
                if (
                    image_executor is not None
                    and self.analyze_images
                    and image_helpers.get("Image") is not None
                    and self._is_image_candidate(path)
                ):
                    media_jobs.append(
                        self._submit_image_candidate(
                            image_executor,
                            path,
                            image_report,
                            folder_text=folder_text,
                            folder_current=folder_index,
                            folder_total=folder_total,
                            source_file_count=len(files),
                        )
                    )
                else:
                    self._process_image_candidate(
                        path,
                        image_report,
                        image_helpers,
                        folder_text=folder_text,
                        folder_current=folder_index,
                        folder_total=folder_total,
                        source_file_count=len(files),
                    )
                if self.analyze_similar_videos and self._is_video_candidate(path):
                    if image_executor is not None:
                        media_jobs.append(
                            self._submit_video_candidate(
                                image_executor,
                                path,
                                image_report,
                                folder_text=folder_text,
                                folder_current=folder_index,
                                folder_total=folder_total,
                                source_file_count=len(files),
                            )
                        )
                    else:
                        self._process_video_candidate(
                            path,
                            image_report,
                            folder_text=folder_text,
                            folder_current=folder_index,
                            folder_total=folder_total,
                            source_file_count=len(files),
                        )

            self._collect_image_jobs(
                media_jobs,
                image_report,
                folder_text=folder_text,
                folder_current=folder_index,
                folder_total=folder_total,
            )

            self._emit_progress(
                "folder_done",
                f"Finished folder {folder_index}/{folder_total}: {folder_text}",
                folder_current=folder_index,
                folder_total=len(folders),
                current_folder=folder_text,
                source_file_count=len(files),
                found_file_count=len(self._progress_found_paths),
            )

    def _is_excluded(self, path: Path) -> bool:
        if path.name == REPORT_FILE_NAME:
            return True
        try:
            relative = path.relative_to(self.target_dir)
        except ValueError:
            return True
        return bool(relative.parts and relative.parts[0] == BACKUP_DIR_NAME)

    def _is_excluded_folder(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.target_dir)
        except ValueError:
            return True
        return bool(relative.parts and relative.parts[0] == BACKUP_DIR_NAME)

    def _is_inside_target(self, path: Path) -> bool:
        try:
            path.relative_to(self.target_dir)
        except ValueError:
            return False
        return True

    def _normalize_selected_path(self, raw_path: str) -> Path | None:
        value = str(raw_path).strip().strip('"').strip("'")
        if not value:
            return None

        candidate = Path(value)
        if candidate.is_absolute():
            try:
                candidate = candidate.expanduser().resolve().relative_to(self.target_dir)
            except ValueError:
                return None

        if any(part in {"", ".", ".."} for part in candidate.parts):
            return None
        if not candidate.parts:
            return None
        return Path(*candidate.parts)

    def _process_duplicate_candidate(
        self,
        path: Path,
        size: int,
        files_by_size: dict[int, list[Path]],
        hash_cache: dict[Path, str],
        *,
        folder_text: str,
        folder_current: int,
        folder_total: int,
        source_file_count: int,
    ) -> bool:
        same_size_files = files_by_size.setdefault(size, [])
        if not same_size_files:
            same_size_files.append(path)
            return False

        current_digest = self._hash_file(path)
        if not current_digest:
            same_size_files.append(path)
            return False
        hash_cache[path] = current_digest

        matched_existing = False
        matched_keep: Path | None = None
        for existing in same_size_files:
            existing_digest = hash_cache.get(existing)
            if existing_digest is None:
                existing_digest = self._hash_file(existing)
                if existing_digest:
                    hash_cache[existing] = existing_digest
            if existing_digest and existing_digest == current_digest:
                matched_existing = True
                matched_keep = existing

        if matched_existing:
            relative = _relative_path_text(path, self.target_dir)
            kept_path = (
                _relative_path_text(matched_keep, self.target_dir)
                if matched_keep is not None
                else None
            )
            found_file = {
                "path": relative,
                "categories": [CATEGORY_EXACT_DUPLICATE],
                "size": size,
                "kept_path": kept_path,
            }
            if self.delete_exact_during_scan:
                deleted_at = _utc_now()
                try:
                    path.unlink()
                except OSError as exc:
                    same_size_files.append(path)
                    self._emit_found_file(
                        {
                            **found_file,
                            "delete_error": str(exc),
                        },
                        "Found exact duplicate",
                        current_folder=folder_text,
                        folder_current=folder_current,
                        folder_total=folder_total,
                        source_file_count=source_file_count,
                    )
                    return False
                hash_cache.pop(path, None)
                deleted_entry = {
                    "path": relative,
                    "size": size,
                    "deleted_at": deleted_at,
                    "kept_path": kept_path,
                }
                self._deleted_during_scan.append(deleted_entry)
                self._emit_found_file(
                    {
                        **found_file,
                        "deleted_during_scan": True,
                        "deleted_at": deleted_at,
                    },
                    "Deleted exact duplicate during scan",
                    current_folder=folder_text,
                    folder_current=folder_current,
                    folder_total=folder_total,
                    source_file_count=source_file_count,
                )
                return True

            same_size_files.append(path)
            self._emit_found_file(
                found_file,
                "Found exact duplicate",
                current_folder=folder_text,
                folder_current=folder_current,
                folder_total=folder_total,
                source_file_count=source_file_count,
            )
            return False

        same_size_files.append(path)
        return False

    def _build_duplicate_groups_from_hashes(
        self,
        files_by_size: dict[int, list[Path]],
        hash_cache: dict[Path, str],
    ) -> list[DuplicateGroup]:
        duplicate_groups: list[DuplicateGroup] = []
        for size, paths in files_by_size.items():
            if len(paths) < 2:
                continue
            paths_by_hash: dict[str, list[Path]] = {}
            for path in paths:
                digest = hash_cache.get(path)
                if digest is None:
                    digest = self._hash_file(path)
                    if digest:
                        hash_cache[path] = digest
                if digest:
                    paths_by_hash.setdefault(digest, []).append(path)

            for digest, matching_paths in paths_by_hash.items():
                if len(matching_paths) < 2:
                    continue
                ordered = matching_paths
                duplicate_groups.append(
                    DuplicateGroup(
                        sha256=digest,
                        size=size,
                        keep=ordered[0],
                        duplicates=tuple(ordered[1:]),
                    )
                )

        return sorted(
            duplicate_groups,
            key=lambda group: _relative_path_text(group.keep, self.target_dir).casefold(),
        )

    def _append_similar_media_results(
        self,
        image_report: dict[str, Any],
        duplicate_groups: list[DuplicateGroup],
    ) -> None:
        exact_duplicate_paths = {
            _relative_path_text(path, self.target_dir).casefold()
            for group in duplicate_groups
            for path in group.duplicates
        }
        if self.analyze_similar_images:
            self._append_similar_image_results(image_report, exact_duplicate_paths)
        if self.analyze_similar_videos:
            self._append_similar_video_results(image_report, exact_duplicate_paths)

    def _append_similar_image_results(
        self,
        image_report: dict[str, Any],
        exact_duplicate_paths: set[str],
    ) -> None:
        candidates = [
            {
                "path": str(detail.get("path")),
                "hashes": [str(detail.get("perceptual_hash"))],
            }
            for detail in image_report.get("image_details", [])
            if isinstance(detail, dict)
            and detail.get("path")
            and detail.get("perceptual_hash")
            and str(detail.get("path")).casefold() not in exact_duplicate_paths
        ]
        groups, duplicates = self._build_similar_media_groups(
            candidates,
            threshold=self.similar_image_threshold,
            similarity_key="image_similarity",
        )
        image_report["similar_image_groups"] = groups
        image_report["similar_image_duplicates"] = duplicates
        image_report["similar_image_duplicate_count"] = len(duplicates)
        for duplicate in duplicates:
            self._emit_found_file(
                {
                    "path": duplicate["path"],
                    "categories": [CATEGORY_SIMILAR_IMAGE_DUPLICATE],
                    "size": self._safe_stat_size(duplicate["path"]),
                    "similar_to": duplicate["similar_to"],
                    "image_similarity": duplicate["image_similarity"],
                    "perceptual_distance": duplicate["perceptual_distance"],
                },
                "Found similar image",
            )

    def _append_similar_video_results(
        self,
        image_report: dict[str, Any],
        exact_duplicate_paths: set[str],
    ) -> None:
        candidates = [
            {
                "path": str(detail.get("path")),
                "hashes": [
                    str(value)
                    for value in detail.get("perceptual_hashes", [])
                    if value
                ],
            }
            for detail in image_report.get("video_details", [])
            if isinstance(detail, dict)
            and detail.get("path")
            and detail.get("perceptual_hashes")
            and str(detail.get("path")).casefold() not in exact_duplicate_paths
        ]
        groups, duplicates = self._build_similar_media_groups(
            candidates,
            threshold=self.similar_video_threshold,
            similarity_key="video_similarity",
        )
        image_report["similar_video_groups"] = groups
        image_report["similar_video_duplicates"] = duplicates
        image_report["similar_video_duplicate_count"] = len(duplicates)
        for duplicate in duplicates:
            self._emit_found_file(
                {
                    "path": duplicate["path"],
                    "categories": [CATEGORY_SIMILAR_VIDEO_DUPLICATE],
                    "size": self._safe_stat_size(duplicate["path"]),
                    "similar_to": duplicate["similar_to"],
                    "video_similarity": duplicate["video_similarity"],
                    "perceptual_distance": duplicate["perceptual_distance"],
                },
                "Found similar video",
            )

    def _build_similar_media_groups(
        self,
        candidates: list[dict[str, Any]],
        *,
        threshold: int,
        similarity_key: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        ordered = sorted(candidates, key=lambda item: str(item["path"]).casefold())
        used_duplicates: set[str] = set()
        groups: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        for index, base in enumerate(ordered):
            base_path = str(base["path"])
            if base_path.casefold() in used_duplicates:
                continue
            matches: list[dict[str, Any]] = []
            for candidate in ordered[index + 1 :]:
                candidate_path = str(candidate["path"])
                if candidate_path.casefold() in used_duplicates:
                    continue
                similarity, distance = self._hash_sequence_similarity(
                    base.get("hashes", []),
                    candidate.get("hashes", []),
                )
                if similarity < threshold:
                    continue
                match = {
                    "path": candidate_path,
                    similarity_key: similarity,
                    "perceptual_distance": distance,
                }
                matches.append(match)
            if not matches:
                continue
            for match in matches:
                used_duplicates.add(str(match["path"]).casefold())
                duplicates.append(
                    {
                        "path": match["path"],
                        "similar_to": base_path,
                        similarity_key: match[similarity_key],
                        "perceptual_distance": match["perceptual_distance"],
                    }
                )
            groups.append({"keep": base_path, "matches": matches})
        return groups, duplicates

    @staticmethod
    def _hash_sequence_similarity(
        left_hashes: Sequence[str],
        right_hashes: Sequence[str],
    ) -> tuple[int, int]:
        pairs = [
            (left, right)
            for left, right in zip(left_hashes, right_hashes)
            if left and right
        ]
        if not pairs:
            return 0, 64
        distances = [
            DuplicateCleaner._hex_hamming_distance(left, right)
            for left, right in pairs
        ]
        average_distance = round(sum(distances) / len(distances))
        similarity = round(100 * (1 - average_distance / 64))
        return max(0, min(100, similarity)), average_distance

    @staticmethod
    def _hex_hamming_distance(left: str, right: str) -> int:
        try:
            left_value = int(str(left), 16)
            right_value = int(str(right), 16)
        except ValueError:
            return 64
        return (left_value ^ right_value).bit_count()

    def _create_image_report(self) -> tuple[dict[str, Any], dict[str, Any]]:
        report = self._empty_image_report(
            enabled=self.analyze_images,
            image_count=0,
            method="disabled" if not self.analyze_images else "pending",
        )
        helpers: dict[str, Any] = {
            "Image": None,
            "ImageOps": None,
            "face_detector": None,
            "person_detector": None,
        }
        report["similar_image_analysis_enabled"] = self.analyze_similar_images
        report["similar_image_threshold"] = self.similar_image_threshold
        report["similar_video_analysis_enabled"] = self.analyze_similar_videos
        report["similar_video_threshold"] = self.similar_video_threshold
        report["video_analysis_method"] = (
            VIDEO_FINGERPRINT_METHOD
            if self.analyze_similar_videos
            else "disabled"
        )
        if not self.analyze_images:
            return report, helpers

        try:
            from PIL import Image, ImageOps
        except Exception as exc:
            report["image_analysis_method"] = "unavailable"
            report["warnings"].append(f"Pillow is not available: {exc}")
            return report, helpers

        face_detector, face_method = self._load_face_detector()
        person_detector, person_method = (
            self._load_person_detector()
            if self._person_detection_enabled()
            else (None, "disabled")
        )
        report["image_analysis_method"] = (
            f"{face_method}+{person_method}"
            if person_detector is not None
            else face_method
        )
        report["person_detection_method"] = person_method
        if self.analyze_landscape_features:
            report["ai_landscape_features_enabled"] = True
            report["ai_landscape_method"] = AI_LANDSCAPE_FEATURE_METHOD
            report["ai_landscape_threshold"] = round(
                self.ai_landscape_threshold,
                4,
            )
        else:
            report["ai_landscape_threshold"] = round(
                self.ai_landscape_threshold,
                4,
            )
        helpers.update(
            {
                "Image": Image,
                "ImageOps": ImageOps,
                "face_detector": face_detector,
                "person_detector": person_detector,
            }
        )
        return report, helpers

    def _person_detection_enabled(self) -> bool:
        return (
            self.analyze_landscape_features
            or self.face_detection.allow_no_face_non_portrait
            or self.face_detection.allow_shape_only_non_portrait
        )

    def _create_image_executor(
        self,
        image_helpers: dict[str, Any],
    ) -> concurrent.futures.ThreadPoolExecutor | None:
        if self.image_analysis_workers <= 1:
            return None
        can_analyze_images = self.analyze_images and image_helpers.get("Image") is not None
        if not can_analyze_images and not self.analyze_similar_videos:
            return None
        return concurrent.futures.ThreadPoolExecutor(
            max_workers=self.image_analysis_workers,
            thread_name_prefix="duplicate-cleaner-image",
        )

    @staticmethod
    def _is_image_candidate(path: Path) -> bool:
        return path.suffix.casefold() in IMAGE_EXTENSIONS

    @staticmethod
    def _is_video_candidate(path: Path) -> bool:
        return path.suffix.casefold() in VIDEO_EXTENSIONS

    def _thread_image_helpers(self) -> dict[str, Any]:
        helpers = getattr(self._image_worker_local, "helpers", None)
        if helpers is not None:
            return helpers

        from PIL import Image, ImageOps

        face_detector, _face_method = self._load_face_detector()
        person_detector, _person_method = (
            self._load_person_detector()
            if self._person_detection_enabled()
            else (None, "disabled")
        )
        helpers = {
            "Image": Image,
            "ImageOps": ImageOps,
            "face_detector": face_detector,
            "person_detector": person_detector,
        }
        self._image_worker_local.helpers = helpers
        return helpers

    def _submit_image_candidate(
        self,
        image_executor: concurrent.futures.ThreadPoolExecutor,
        path: Path,
        image_report: dict[str, Any],
        *,
        folder_text: str,
        folder_current: int,
        folder_total: int,
        source_file_count: int,
    ) -> tuple[concurrent.futures.Future[dict[str, Any]], Path, int]:
        image_report["image_file_count"] += 1
        image_index = int(image_report["image_file_count"])
        relative = _relative_path_text(path, self.target_dir)
        self._emit_progress(
            "image_analysis",
            f"Scanning image {image_index}: {relative}",
            image_current=image_index,
            image_total=image_index,
            current_file=relative,
            current_folder=folder_text,
            folder_current=folder_current,
            folder_total=folder_total,
            source_file_count=source_file_count,
            found_file_count=len(self._progress_found_paths),
        )
        return (
            image_executor.submit(self._classify_image_path_for_worker, path),
            path,
            source_file_count,
        )

    def _process_video_issue_candidate(
        self,
        path: Path,
        issue: dict[str, Any],
        image_report: dict[str, Any],
        *,
        folder_text: str,
        folder_current: int,
        folder_total: int,
        source_file_count: int,
    ) -> None:
        if path.suffix.casefold() not in VIDEO_EXTENSIONS:
            return

        image_report["video_file_count"] += 1
        video_index = int(image_report["video_file_count"])
        relative = _relative_path_text(path, self.target_dir)
        self._emit_progress(
            "video_analysis",
            f"Listing video {video_index}: {relative}",
            video_current=video_index,
            video_total=video_index,
            current_file=relative,
            current_folder=folder_text,
            folder_current=folder_current,
            folder_total=folder_total,
            source_file_count=source_file_count,
            found_file_count=len(self._progress_found_paths),
        )
        self._record_video_issue(
            path,
            issue,
            image_report,
            folder_text=folder_text,
            folder_current=folder_current,
            folder_total=folder_total,
            source_file_count=source_file_count,
        )

    def _submit_video_candidate(
        self,
        image_executor: concurrent.futures.ThreadPoolExecutor,
        path: Path,
        image_report: dict[str, Any],
        *,
        folder_text: str,
        folder_current: int,
        folder_total: int,
        source_file_count: int,
    ) -> tuple[concurrent.futures.Future[dict[str, Any]], Path, int]:
        image_report["video_file_count"] += 1
        video_index = int(image_report["video_file_count"])
        relative = _relative_path_text(path, self.target_dir)
        self._emit_progress(
            "video_analysis",
            f"Scanning video {video_index}: {relative}",
            video_current=video_index,
            video_total=video_index,
            current_file=relative,
            current_folder=folder_text,
            folder_current=folder_current,
            folder_total=folder_total,
            source_file_count=source_file_count,
            found_file_count=len(self._progress_found_paths),
        )
        return (
            image_executor.submit(self._classify_video_path, path),
            path,
            source_file_count,
        )

    def _collect_image_jobs(
        self,
        image_jobs: list[
            tuple[concurrent.futures.Future[dict[str, Any]], Path, int]
        ],
        image_report: dict[str, Any],
        *,
        folder_text: str,
        folder_current: int,
        folder_total: int,
    ) -> None:
        jobs_by_future = {
            future: (path, source_file_count)
            for future, path, source_file_count in image_jobs
        }
        for future in concurrent.futures.as_completed(jobs_by_future):
            path, source_file_count = jobs_by_future[future]
            relative = _relative_path_text(path, self.target_dir)
            try:
                result = future.result()
            except Exception as exc:
                image_report["warnings"].append(f"{relative}: {exc}")
                continue
            warning = result.get("warning")
            if warning:
                image_report["warnings"].append(str(warning))
                continue
            classification = result.get("classification")
            if isinstance(classification, dict):
                self._record_image_classification(
                    path,
                    classification,
                    image_report,
                    folder_text=folder_text,
                    folder_current=folder_current,
                    folder_total=folder_total,
                    source_file_count=source_file_count,
                )
                continue
            video_issue = result.get("video_issue")
            if isinstance(video_issue, dict):
                self._record_video_issue(
                    path,
                    video_issue,
                    image_report,
                    folder_text=folder_text,
                    folder_current=folder_current,
                    folder_total=folder_total,
                    source_file_count=source_file_count,
                )
                continue
            video_detail = result.get("video_detail")
            if isinstance(video_detail, dict):
                self._record_video_detail(
                    path,
                    video_detail,
                    image_report,
                    folder_text=folder_text,
                    folder_current=folder_current,
                    folder_total=folder_total,
                    source_file_count=source_file_count,
                )

    def _classify_image_path_for_worker(self, path: Path) -> dict[str, Any]:
        return self._classify_image_path(path, self._thread_image_helpers())

    def _classify_image_path(
        self,
        path: Path,
        image_helpers: dict[str, Any],
    ) -> dict[str, Any]:
        relative = _relative_path_text(path, self.target_dir)
        try:
            with image_helpers["Image"].open(path) as opened_image:
                image = image_helpers["ImageOps"].exif_transpose(opened_image)
                classification = self._classify_image(
                    image,
                    image_helpers.get("face_detector"),
                    image_helpers.get("person_detector"),
                )
        except Exception as exc:
            return {"warning": f"{relative}: {exc}"}
        return {"classification": classification}

    def _classify_video_path(self, path: Path) -> dict[str, Any]:
        relative = _relative_path_text(path, self.target_dir)
        direct_issue = self._video_direct_issue(path)
        if direct_issue:
            return {"video_issue": direct_issue}
        try:
            detail = self._video_fingerprint(path)
        except Exception as exc:
            return {
                "video_issue": self._video_issue(
                    CATEGORY_BAD_VIDEO_FILE,
                    "fingerprint_failed",
                    error=f"{relative}: {exc}",
                )
            }
        if detail is None:
            return {
                "video_issue": self._video_issue(
                    CATEGORY_BAD_VIDEO_FILE,
                    "fingerprint_unavailable",
                )
            }
        return {"video_detail": detail}

    def _process_video_candidate(
        self,
        path: Path,
        image_report: dict[str, Any],
        *,
        folder_text: str,
        folder_current: int,
        folder_total: int,
        source_file_count: int,
    ) -> None:
        if path.suffix.casefold() not in VIDEO_EXTENSIONS:
            return

        image_report["video_file_count"] += 1
        video_index = int(image_report["video_file_count"])
        relative = _relative_path_text(path, self.target_dir)
        self._emit_progress(
            "video_analysis",
            f"Scanning video {video_index}: {relative}",
            video_current=video_index,
            video_total=video_index,
            current_file=relative,
            current_folder=folder_text,
            folder_current=folder_current,
            folder_total=folder_total,
            source_file_count=source_file_count,
            found_file_count=len(self._progress_found_paths),
        )
        result = self._classify_video_path(path)
        warning = result.get("warning")
        if warning:
            image_report["warnings"].append(str(warning))
            return
        video_issue = result.get("video_issue")
        if isinstance(video_issue, dict):
            self._record_video_issue(
                path,
                video_issue,
                image_report,
                folder_text=folder_text,
                folder_current=folder_current,
                folder_total=folder_total,
                source_file_count=source_file_count,
            )
            return
        video_detail = result.get("video_detail")
        if isinstance(video_detail, dict):
            self._record_video_detail(
                path,
                video_detail,
                image_report,
                folder_text=folder_text,
                folder_current=folder_current,
                folder_total=folder_total,
                source_file_count=source_file_count,
            )

    def _record_video_detail(
        self,
        path: Path,
        video_detail: dict[str, Any],
        image_report: dict[str, Any],
        *,
        folder_text: str,
        folder_current: int,
        folder_total: int,
        source_file_count: int,
    ) -> None:
        relative = _relative_path_text(path, self.target_dir)
        image_report["video_details"].append(
            {
                "path": relative,
                "size": self._safe_stat_size(relative),
                **video_detail,
            }
        )

    def _record_video_issue(
        self,
        path: Path,
        issue: dict[str, Any],
        image_report: dict[str, Any],
        *,
        folder_text: str,
        folder_current: int,
        folder_total: int,
        source_file_count: int,
    ) -> None:
        relative = _relative_path_text(path, self.target_dir)
        category = str(issue.get("category") or CATEGORY_BAD_VIDEO_FILE)
        if category not in {CATEGORY_LARGE_VIDEO_FILE, CATEGORY_BAD_VIDEO_FILE}:
            category = CATEGORY_BAD_VIDEO_FILE
        size = (
            int(issue["size"])
            if isinstance(issue.get("size"), int)
            else self._safe_stat_size(relative)
        )
        entry = {
            "path": relative,
            "size": size,
            "reason": str(issue.get("reason") or "unknown"),
        }
        error = issue.get("error")
        if error:
            entry["error"] = str(error)

        if category == CATEGORY_LARGE_VIDEO_FILE:
            image_report["large_video_files"].append(entry)
            image_report["large_video_file_count"] += 1
            message = "Found large video"
        else:
            image_report["bad_video_files"].append(entry)
            image_report["bad_video_file_count"] += 1
            message = "Found bad video"

        self._emit_found_file(
            {
                "path": relative,
                "categories": [category],
                "size": size,
                "video_issue": entry["reason"],
            },
            message,
            current_folder=folder_text,
            folder_current=folder_current,
            folder_total=folder_total,
            source_file_count=source_file_count,
        )

    def _record_image_classification(
        self,
        path: Path,
        classification: dict[str, Any],
        image_report: dict[str, Any],
        *,
        folder_text: str,
        folder_current: int,
        folder_total: int,
        source_file_count: int,
    ) -> None:
        relative = _relative_path_text(path, self.target_dir)
        width = int(classification["width"])
        height = int(classification["height"])
        aspect_ratio = float(classification["aspect_ratio"])
        is_landscape = bool(classification["is_landscape"])
        is_non_portrait = bool(classification["is_non_portrait"])
        has_face = classification["has_face"]
        has_person = classification["has_person"]
        landscape_features = classification["landscape_features"]

        if is_landscape:
            image_report["landscape_images"].append(relative)
            image_report["landscape_image_count"] += 1
        if is_non_portrait:
            image_report["non_portrait_images"].append(relative)
            image_report["non_portrait_image_count"] += 1

        categories = [
            category
            for category, matched in (
                (CATEGORY_LANDSCAPE_IMAGE, is_landscape),
                (CATEGORY_NON_PORTRAIT_IMAGE, is_non_portrait),
            )
            if matched
        ]
        if categories:
            found_file = {
                "path": relative,
                "categories": categories,
                "size": self._safe_stat_size(relative),
            }
            found_file.update(
                self._found_file_metadata_from_classification(classification),
            )
            self._emit_found_file(
                found_file,
                "Found image candidate",
                current_folder=folder_text,
                folder_current=folder_current,
                folder_total=folder_total,
                source_file_count=source_file_count,
            )

        image_report["image_details"].append(
            {
                "path": relative,
                "width": width,
                "height": height,
                "aspect_ratio": round(aspect_ratio, 4),
                "is_landscape": is_landscape,
                "is_non_portrait": is_non_portrait,
                "has_face": has_face,
                "has_person": has_person,
                "perceptual_hash": classification.get("perceptual_hash"),
                "classification_reason": classification["reason"],
                "classification_confidence": classification["confidence"],
                "shape_landscape": classification["shape_landscape"],
                "ai_landscape_detected": landscape_features["detected"],
                "ai_landscape_score": landscape_features["score"],
                "landscape_features": landscape_features["features"],
                "dominant_landscape_features": landscape_features[
                    "dominant_features"
                ],
                "ai_landscape_features": landscape_features,
            }
        )

    def _process_image_candidate(
        self,
        path: Path,
        image_report: dict[str, Any],
        image_helpers: dict[str, Any],
        *,
        folder_text: str,
        folder_current: int,
        folder_total: int,
        source_file_count: int,
    ) -> None:
        if path.suffix.casefold() not in IMAGE_EXTENSIONS:
            return

        image_report["image_file_count"] += 1
        image_index = int(image_report["image_file_count"])
        if not self.analyze_images or image_helpers.get("Image") is None:
            return

        relative = _relative_path_text(path, self.target_dir)
        self._emit_progress(
            "image_analysis",
            f"Scanning image {image_index}: {relative}",
            image_current=image_index,
            image_total=image_index,
            current_file=relative,
            current_folder=folder_text,
            folder_current=folder_current,
            folder_total=folder_total,
            source_file_count=source_file_count,
            found_file_count=len(self._progress_found_paths),
        )

        try:
            with image_helpers["Image"].open(path) as opened_image:
                image = image_helpers["ImageOps"].exif_transpose(opened_image)
                classification = self._classify_image(
                    image,
                    image_helpers.get("face_detector"),
                    image_helpers.get("person_detector"),
                )
        except Exception as exc:
            image_report["warnings"].append(f"{relative}: {exc}")
            return

        self._record_image_classification(
            path,
            classification,
            image_report,
            folder_text=folder_text,
            folder_current=folder_current,
            folder_total=folder_total,
            source_file_count=source_file_count,
        )
        return

        width = int(classification["width"])
        height = int(classification["height"])
        aspect_ratio = float(classification["aspect_ratio"])
        is_landscape = bool(classification["is_landscape"])
        is_non_portrait = bool(classification["is_non_portrait"])
        has_face = classification["has_face"]
        has_person = classification["has_person"]
        landscape_features = classification["landscape_features"]

        if is_landscape:
            image_report["landscape_images"].append(relative)
            image_report["landscape_image_count"] += 1
        if is_non_portrait:
            image_report["non_portrait_images"].append(relative)
            image_report["non_portrait_image_count"] += 1

        categories = [
            category
            for category, matched in (
                (CATEGORY_LANDSCAPE_IMAGE, is_landscape),
                (CATEGORY_NON_PORTRAIT_IMAGE, is_non_portrait),
            )
            if matched
        ]
        if categories:
            found_file = {
                "path": relative,
                "categories": categories,
                "size": self._safe_stat_size(relative),
            }
            found_file.update(
                self._found_file_metadata_from_classification(classification),
            )
            self._emit_found_file(
                found_file,
                "Found image candidate",
                current_folder=folder_text,
                folder_current=folder_current,
                folder_total=folder_total,
                source_file_count=source_file_count,
            )

        image_report["image_details"].append(
            {
                "path": relative,
                "width": width,
                "height": height,
                "aspect_ratio": round(aspect_ratio, 4),
                "is_landscape": is_landscape,
                "is_non_portrait": is_non_portrait,
                "has_face": has_face,
                "has_person": has_person,
                "classification_reason": classification["reason"],
                "classification_confidence": classification["confidence"],
                "shape_landscape": classification["shape_landscape"],
                "ai_landscape_detected": landscape_features["detected"],
                "ai_landscape_score": landscape_features["score"],
                "landscape_features": landscape_features["features"],
                "dominant_landscape_features": landscape_features[
                    "dominant_features"
                ],
                "ai_landscape_features": landscape_features,
            }
        )

    def _find_duplicate_groups(self, files: list[Path]) -> list[DuplicateGroup]:
        files_by_size: dict[int, list[Path]] = {}
        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files_by_size.setdefault(size, []).append(path)

        duplicate_groups: list[DuplicateGroup] = []
        hashable_groups = [
            paths for paths in files_by_size.values() if len(paths) > 1
        ]
        self._log(f"Hashing {len(hashable_groups)} same-size file groups.")
        self._emit_progress(
            "hash_started",
            "Hash comparison started",
            hash_group_current=0,
            hash_group_total=len(hashable_groups),
            source_file_count=len(files),
            found_file_count=len(self._progress_found_paths),
        )

        for hash_group_index, paths in enumerate(hashable_groups, start=1):
            self._emit_progress(
                "hash_group",
                f"Hashing same-size group {hash_group_index}/{len(hashable_groups)}",
                hash_group_current=hash_group_index,
                hash_group_total=len(hashable_groups),
                source_file_count=len(files),
                found_file_count=len(self._progress_found_paths),
            )
            paths_by_hash: dict[str, list[Path]] = {}
            for path in paths:
                digest = self._hash_file(path)
                if digest:
                    paths_by_hash.setdefault(digest, []).append(path)
            for digest, matching_paths in paths_by_hash.items():
                if len(matching_paths) < 2:
                    continue
                ordered = sorted(
                    matching_paths,
                    key=lambda item: _relative_path_text(
                        item, self.target_dir
                    ).casefold(),
                )
                duplicate_groups.append(
                    DuplicateGroup(
                        sha256=digest,
                        size=ordered[0].stat().st_size,
                        keep=ordered[0],
                        duplicates=tuple(ordered[1:]),
                    )
                )
                for duplicate in ordered[1:]:
                    self._emit_found_file(
                        {
                            "path": _relative_path_text(duplicate, self.target_dir),
                            "categories": [CATEGORY_EXACT_DUPLICATE],
                            "size": ordered[0].stat().st_size,
                        },
                        "Found exact duplicate",
                    )

        return sorted(
            duplicate_groups,
            key=lambda group: _relative_path_text(group.keep, self.target_dir).casefold(),
        )

    def _hash_file(self, path: Path) -> str:
        hasher = hashlib.sha256()
        try:
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(self.hash_chunk_size), b""):
                    hasher.update(chunk)
        except OSError as exc:
            self._log(f"Could not read file: {path} ({exc})")
            return ""
        return hasher.hexdigest()

    def _analyze_images(self, files: list[Path]) -> dict[str, Any]:
        image_files = [
            path for path in files if path.suffix.casefold() in IMAGE_EXTENSIONS
        ]
        if not self.analyze_images:
            return self._empty_image_report(
                enabled=False,
                image_count=len(image_files),
                method="disabled",
            )

        try:
            from PIL import Image, ImageOps
        except Exception as exc:
            report = self._empty_image_report(
                enabled=True,
                image_count=len(image_files),
                method="unavailable",
            )
            report["warnings"].append(f"Pillow is not available: {exc}")
            return report

        face_detector, face_method = self._load_face_detector()
        person_detector, person_method = (
            self._load_person_detector()
            if self._person_detection_enabled()
            else (None, "disabled")
        )
        method = f"{face_method}+{person_method}" if person_detector is not None else face_method
        landscape_images: list[str] = []
        non_portrait_images: list[str] = []
        image_details: list[dict[str, Any]] = []
        warnings: list[str] = []
        self._emit_progress(
            "image_analysis_started",
            "Image analysis started",
            image_current=0,
            image_total=len(image_files),
            found_file_count=len(self._progress_found_paths),
        )

        for image_index, path in enumerate(image_files, start=1):
            relative = _relative_path_text(path, self.target_dir)
            self._emit_progress(
                "image_analysis",
                f"Scanning image {image_index}/{len(image_files)}: {relative}",
                image_current=image_index,
                image_total=len(image_files),
                current_file=relative,
                found_file_count=len(self._progress_found_paths),
            )
            try:
                with Image.open(path) as opened_image:
                    image = ImageOps.exif_transpose(opened_image)
                    classification = self._classify_image(
                        image,
                        face_detector,
                        person_detector,
                    )
            except Exception as exc:
                warnings.append(f"{relative}: {exc}")
                continue

            width = int(classification["width"])
            height = int(classification["height"])
            aspect_ratio = float(classification["aspect_ratio"])
            is_landscape = bool(classification["is_landscape"])
            is_non_portrait = bool(classification["is_non_portrait"])
            has_face = classification["has_face"]
            has_person = classification["has_person"]
            landscape_features = classification["landscape_features"]

            if is_landscape:
                landscape_images.append(relative)
            if is_non_portrait:
                non_portrait_images.append(relative)
            categories = [
                category
                for category, matched in (
                    (CATEGORY_LANDSCAPE_IMAGE, is_landscape),
                    (CATEGORY_NON_PORTRAIT_IMAGE, is_non_portrait),
                )
                if matched
            ]
            if categories:
                found_file = {
                    "path": relative,
                    "categories": categories,
                    "size": self._safe_stat_size(relative),
                }
                found_file.update(
                    self._found_file_metadata_from_classification(classification),
                )
                self._emit_found_file(
                    found_file,
                    "Found image candidate",
                )
            image_details.append(
                {
                    "path": relative,
                    "width": width,
                    "height": height,
                    "aspect_ratio": round(aspect_ratio, 4),
                    "is_landscape": is_landscape,
                    "is_non_portrait": is_non_portrait,
                    "has_face": has_face,
                    "has_person": has_person,
                    "classification_reason": classification["reason"],
                    "classification_confidence": classification["confidence"],
                    "shape_landscape": classification["shape_landscape"],
                    "ai_landscape_detected": landscape_features["detected"],
                    "ai_landscape_score": landscape_features["score"],
                    "landscape_features": landscape_features["features"],
                    "dominant_landscape_features": landscape_features[
                        "dominant_features"
                    ],
                    "ai_landscape_features": landscape_features,
                }
            )

        report = {
            "image_analysis_enabled": True,
            "image_analysis_method": method,
            "image_file_count": len(image_files),
            "landscape_image_count": len(landscape_images),
            "non_portrait_image_count": len(non_portrait_images),
            "landscape_images": landscape_images,
            "non_portrait_images": non_portrait_images,
            "image_details": image_details,
            "person_detection_method": person_method,
            "warnings": warnings,
        }
        if self.analyze_landscape_features:
            report["ai_landscape_features_enabled"] = True
            report["ai_landscape_method"] = AI_LANDSCAPE_FEATURE_METHOD
            report["ai_landscape_threshold"] = round(
                self.ai_landscape_threshold,
                4,
            )
        else:
            report["ai_landscape_features_enabled"] = False
            report["ai_landscape_method"] = "disabled"
            report["ai_landscape_threshold"] = round(
                self.ai_landscape_threshold,
                4,
            )
        return report

    @staticmethod
    def _empty_image_report(
        *,
        enabled: bool,
        image_count: int,
        method: str,
    ) -> dict[str, Any]:
        return {
            "image_analysis_enabled": enabled,
            "image_analysis_method": method,
            "image_file_count": image_count,
            "landscape_image_count": 0,
            "non_portrait_image_count": 0,
            "landscape_images": [],
            "non_portrait_images": [],
            "image_details": [],
            "similar_image_analysis_enabled": False,
            "similar_image_threshold": DEFAULT_SIMILAR_IMAGE_THRESHOLD,
            "similar_image_duplicate_count": 0,
            "similar_image_groups": [],
            "similar_image_duplicates": [],
            "similar_video_analysis_enabled": False,
            "similar_video_threshold": DEFAULT_SIMILAR_VIDEO_THRESHOLD,
            "similar_video_duplicate_count": 0,
            "similar_video_groups": [],
            "similar_video_duplicates": [],
            "video_analysis_method": "disabled",
            "video_file_count": 0,
            "large_video_file_count": 0,
            "large_video_files": [],
            "bad_video_file_count": 0,
            "bad_video_files": [],
            "video_details": [],
            "ai_landscape_features_enabled": False,
            "ai_landscape_method": "disabled",
            "ai_landscape_threshold": None,
            "person_detection_method": "disabled",
            "warnings": [],
        }

    @staticmethod
    def _looks_like_landscape(width: int, height: int) -> bool:
        if min(width, height) < MIN_CLASSIFIABLE_IMAGE_EDGE:
            return False
        return width > height and (width / max(height, 1)) >= LANDSCAPE_ASPECT_RATIO

    @staticmethod
    def _looks_non_portrait_by_shape(width: int, height: int) -> bool:
        if min(width, height) < MIN_CLASSIFIABLE_IMAGE_EDGE:
            return False
        return (width / max(height, 1)) >= NON_PORTRAIT_ASPECT_RATIO

    @staticmethod
    def _score(value: float) -> float:
        return round(max(0.0, min(1.0, float(value))), 4)

    def _empty_landscape_feature_result(
        self,
        *,
        enabled: bool,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "enabled": enabled,
            "method": AI_LANDSCAPE_FEATURE_METHOD if enabled else "disabled",
            "score": 0.0,
            "threshold": round(self.ai_landscape_threshold, 4),
            "detected": False,
            "confidence": "low",
            "features": [],
            "dominant_features": [],
            "reason": reason,
        }

    def _analyze_landscape_features(self, image: Any) -> dict[str, Any]:
        if not self.analyze_landscape_features:
            return self._empty_landscape_feature_result(
                enabled=False,
                reason="disabled",
            )

        width, height = image.size
        if min(width, height) < MIN_CLASSIFIABLE_IMAGE_EDGE:
            return self._empty_landscape_feature_result(
                enabled=True,
                reason="image_too_small_for_reliable_filter",
            )

        try:
            sample = image.convert("RGB").resize(
                (
                    AI_LANDSCAPE_FEATURE_SAMPLE_SIZE,
                    AI_LANDSCAPE_FEATURE_SAMPLE_SIZE,
                ),
            )
            sample_width, sample_height = sample.size
            rows: list[list[tuple[int, int, int]]] = [
                [sample.getpixel((x, y)) for x in range(sample_width)]
                for y in range(sample_height)
            ]
        except Exception:
            return self._empty_landscape_feature_result(
                enabled=True,
                reason="feature_extraction_failed",
            )

        top_cut = max(1, int(sample_height * 0.42))
        mid_cut = max(top_cut + 1, int(sample_height * 0.66))
        top_rows = rows[:top_cut]
        mid_rows = rows[top_cut:mid_cut]
        lower_rows = rows[mid_cut:]
        lower_and_mid_rows = rows[top_cut:]
        top_pixels = [pixel for row in top_rows for pixel in row]
        mid_pixels = [pixel for row in mid_rows for pixel in row]
        lower_pixels = [pixel for row in lower_rows for pixel in row]
        lower_and_mid_pixels = [*mid_pixels, *lower_pixels]
        all_pixels = [pixel for row in rows for pixel in row]

        def fraction(
            pixels: list[tuple[int, int, int]],
            predicate: Callable[[float, float, float], bool],
        ) -> float:
            if not pixels:
                return 0.0
            matches = 0
            for red, green, blue in pixels:
                hue, saturation, value = colorsys.rgb_to_hsv(
                    red / 255,
                    green / 255,
                    blue / 255,
                )
                if predicate(hue, saturation, value):
                    matches += 1
            return matches / len(pixels)

        def average_rgb(
            pixels: list[tuple[int, int, int]],
        ) -> tuple[float, float, float]:
            if not pixels:
                return (0.0, 0.0, 0.0)
            count = len(pixels)
            return (
                sum(pixel[0] for pixel in pixels) / count,
                sum(pixel[1] for pixel in pixels) / count,
                sum(pixel[2] for pixel in pixels) / count,
            )

        def color_distance(
            left: tuple[float, float, float],
            right: tuple[float, float, float],
        ) -> float:
            return (
                abs(left[0] - right[0])
                + abs(left[1] - right[1])
                + abs(left[2] - right[2])
            ) / 765

        def pixel_hsv(pixel: tuple[int, int, int]) -> tuple[float, float, float]:
            red, green, blue = pixel
            return colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)

        def is_blue_sky(hue: float, saturation: float, value: float) -> bool:
            return 0.52 <= hue <= 0.70 and saturation >= 0.16 and value >= 0.38

        def is_cloud_or_open_sky(
            _hue: float,
            saturation: float,
            value: float,
        ) -> bool:
            return saturation <= 0.18 and value >= 0.78

        def is_vegetation(hue: float, saturation: float, value: float) -> bool:
            return 0.17 <= hue <= 0.45 and saturation >= 0.18 and value >= 0.12

        def is_water(hue: float, saturation: float, value: float) -> bool:
            return 0.45 <= hue <= 0.68 and saturation >= 0.16 and value >= 0.18

        def is_mountain_or_earth(
            hue: float,
            saturation: float,
            value: float,
        ) -> bool:
            return (
                (
                    0.04 <= hue <= 0.16
                    and saturation >= 0.12
                    and 0.10 <= value <= 0.70
                )
                or (
                    0.16 < hue <= 0.45
                    and 0.12 <= saturation <= 0.55
                    and 0.10 <= value <= 0.52
                )
            )

        def matches_pixel(
            pixel: tuple[int, int, int],
            predicate: Callable[[float, float, float], bool],
        ) -> bool:
            return predicate(*pixel_hsv(pixel))

        def column_coverage(
            region_rows: list[list[tuple[int, int, int]]],
            predicate: Callable[[float, float, float], bool],
            *,
            minimum_column_fraction: float,
        ) -> float:
            if not region_rows:
                return 0.0
            covered_columns = 0
            for column in range(sample_width):
                matches = sum(
                    1
                    for row in region_rows
                    if matches_pixel(row[column], predicate)
                )
                if matches / len(region_rows) >= minimum_column_fraction:
                    covered_columns += 1
            return covered_columns / sample_width

        def region_texture(
            region_rows: list[list[tuple[int, int, int]]],
        ) -> float:
            distances: list[float] = []
            for y, row in enumerate(region_rows):
                for x, pixel in enumerate(row):
                    if x + 1 < sample_width:
                        distances.append(color_distance(pixel, row[x + 1]))
                    if y + 1 < len(region_rows):
                        distances.append(
                            color_distance(pixel, region_rows[y + 1][x])
                        )
            if not distances:
                return 0.0
            return self._score((sum(distances) / len(distances)) / 0.045)

        def transition_count(
            averages: list[tuple[float, float, float]],
            *,
            minimum_distance: float,
        ) -> int:
            return sum(
                1
                for index in range(len(averages) - 1)
                if color_distance(averages[index], averages[index + 1])
                >= minimum_distance
            )

        def row_averages_for(
            region_rows: list[list[tuple[int, int, int]]],
        ) -> list[tuple[float, float, float]]:
            return [average_rgb(row) for row in region_rows]

        def column_averages_for(
            region_rows: list[list[tuple[int, int, int]]],
        ) -> list[tuple[float, float, float]]:
            if not region_rows:
                return []
            return [
                average_rgb([row[column] for row in region_rows])
                for column in range(sample_width)
            ]

        top_average = average_rgb(top_pixels)
        lower_average = average_rgb(lower_pixels)
        region_contrast = color_distance(top_average, lower_average)
        row_averages = [average_rgb(row) for row in rows]
        max_row_transition = max(
            (
                color_distance(row_averages[index], row_averages[index + 1])
                for index in range(len(row_averages) - 1)
            ),
            default=0.0,
        )
        horizon_score = self._score(max_row_transition / 0.18)
        color_diversity = self._score(
            len(
                {
                    (red // 48, green // 48, blue // 48)
                    for red, green, blue in all_pixels
                },
            )
            / 18,
        )

        blue_sky_top = fraction(top_pixels, is_blue_sky)
        open_sky_top = fraction(top_pixels, is_cloud_or_open_sky)
        sky_mid = fraction(mid_pixels, is_blue_sky)
        vegetation = fraction(lower_and_mid_pixels, is_vegetation)
        lower_blue = fraction(lower_and_mid_pixels, is_water)
        dark_natural = fraction(lower_and_mid_pixels, is_mountain_or_earth)

        sky_score = self._score(
            (blue_sky_top + min(open_sky_top, 0.32) * 0.35 + sky_mid * 0.18)
            / 0.45
        )
        vegetation_score = self._score(vegetation / 0.34)
        water_score = self._score(
            (lower_blue / 0.42) * min(1.0, region_contrast / 0.15),
        )
        mountain_score = self._score(
            (dark_natural / 0.32) * (0.35 + 0.65 * sky_score),
        )
        layout_score = self._score(
            (region_contrast / 0.22) * 0.55 + horizon_score * 0.45,
        )
        sky_width_score = self._score(
            column_coverage(
                top_rows,
                is_blue_sky,
                minimum_column_fraction=0.20,
            )
            / 0.72
        )
        lower_nature_width_score = self._score(
            max(
                column_coverage(
                    lower_and_mid_rows,
                    is_vegetation,
                    minimum_column_fraction=0.16,
                ),
                column_coverage(
                    lower_and_mid_rows,
                    is_water,
                    minimum_column_fraction=0.16,
                ),
                column_coverage(
                    lower_and_mid_rows,
                    is_mountain_or_earth,
                    minimum_column_fraction=0.16,
                ),
            )
            / 0.70
        )
        natural_lower_pixels = [
            pixel
            for pixel in lower_and_mid_pixels
            if matches_pixel(pixel, is_vegetation)
            or matches_pixel(pixel, is_water)
            or matches_pixel(pixel, is_mountain_or_earth)
        ]
        natural_variety_score = self._score(
            len(
                {
                    (red // 40, green // 40, blue // 40)
                    for red, green, blue in natural_lower_pixels
                },
            )
            / 10
        )
        lower_texture_score = region_texture(lower_and_mid_rows)
        lower_row_layer_score = self._score(
            transition_count(
                row_averages_for(lower_and_mid_rows),
                minimum_distance=0.025,
            )
            / 5
        )
        lower_column_edge_score = self._score(
            transition_count(
                column_averages_for(lower_and_mid_rows),
                minimum_distance=0.035,
            )
            / 5
        )
        natural_structure_score = max(
            natural_variety_score,
            lower_texture_score,
            lower_row_layer_score,
        )
        flat_graphic_penalty = (
            1.0
            if (
                sky_width_score >= 0.70
                and lower_nature_width_score >= 0.70
                and natural_structure_score < 0.26
                and color_diversity < 0.22
            )
            else 0.0
        )
        block_layout_penalty = (
            self._score((lower_column_edge_score - lower_row_layer_score) / 0.75)
            if lower_nature_width_score < 0.62
            else 0.0
        )
        artificial_layout_penalty = max(flat_graphic_penalty, block_layout_penalty)
        lower_nature_score = max(vegetation_score, water_score, mountain_score)
        paired_landscape_score = (
            min(sky_score, lower_nature_score) * 0.34
            + sky_width_score * 0.12
            + lower_nature_width_score * 0.16
            + natural_structure_score * 0.22
            + horizon_score * 0.10
            + color_diversity * 0.06
        )
        nature_only_score = (
            lower_nature_score * 0.28
            + lower_nature_width_score * 0.22
            + natural_structure_score * 0.28
            + layout_score * 0.14
            + color_diversity * 0.08
        )
        landscape_score = self._score(
            max(paired_landscape_score, nature_only_score)
            - artificial_layout_penalty * 0.25
        )

        raw_features = [
            ("sky", sky_score),
            ("vegetation", vegetation_score),
            ("water", water_score),
            ("mountain_or_ridge", mountain_score),
            ("horizon_or_layering", horizon_score),
            ("natural_color_layout", layout_score),
            ("natural_area_coverage", lower_nature_width_score),
            ("spatial_structure", natural_structure_score),
            ("graphic_layout_penalty", artificial_layout_penalty),
        ]
        features = [
            {"name": name, "score": score}
            for name, score in sorted(
                raw_features,
                key=lambda item: item[1],
                reverse=True,
            )
            if score >= 0.08
        ]
        dominant_features = [
            name
            for name, score in raw_features
            if score >= AI_LANDSCAPE_ACTIVE_FEATURE_SCORE
        ]
        lower_feature_detected = lower_nature_score >= AI_LANDSCAPE_ACTIVE_FEATURE_SCORE
        sky_detected = sky_score >= AI_LANDSCAPE_ACTIVE_FEATURE_SCORE
        broad_scene_layout = (
            sky_width_score >= 0.45
            and lower_nature_width_score >= 0.45
            and natural_structure_score >= 0.26
        )
        paired_scene_detected = (
            sky_detected
            and lower_feature_detected
            and broad_scene_layout
        )
        layered_scene_detected = (
            horizon_score >= 0.55
            and lower_feature_detected
            and lower_nature_width_score >= 0.58
            and natural_structure_score >= 0.24
        )
        detected = (
            landscape_score >= self.ai_landscape_threshold
            and (paired_scene_detected or layered_scene_detected)
            and artificial_layout_penalty < 0.50
        )

        if detected and landscape_score >= 0.76 and paired_scene_detected:
            confidence = "high"
        elif detected:
            confidence = "medium"
        else:
            confidence = "low"

        if detected:
            reason = "semantic_landscape_features"
        elif artificial_layout_penalty >= 0.50:
            reason = "graphic_or_flat_layout_rejected"
        elif dominant_features:
            reason = "partial_landscape_features"
        elif natural_structure_score < 0.18:
            reason = "insufficient_spatial_structure"
        else:
            reason = "no_landscape_feature_pattern"

        return {
            "enabled": True,
            "method": AI_LANDSCAPE_FEATURE_METHOD,
            "score": landscape_score,
            "threshold": round(self.ai_landscape_threshold, 4),
            "detected": detected,
            "confidence": confidence,
            "features": features,
            "dominant_features": dominant_features,
            "reason": reason,
        }

    def _selected_exact_duplicate_delete_keys(
        self,
        selected_keys: set[str],
    ) -> set[str]:
        if not selected_keys:
            return set()

        files_by_size: dict[int, list[Path]] = {}
        for path in sorted(
            self._iter_candidate_files(),
            key=lambda item: _relative_path_text(
                item,
                self.target_dir,
            ).casefold(),
        ):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files_by_size.setdefault(size, []).append(path)

        deletable_keys: set[str] = set()
        for paths in files_by_size.values():
            if len(paths) < 2:
                continue
            paths_by_hash: dict[str, list[Path]] = {}
            for path in paths:
                digest = self._hash_file(path)
                if digest:
                    paths_by_hash.setdefault(digest, []).append(path)

            for matching_paths in paths_by_hash.values():
                if len(matching_paths) < 2:
                    continue
                group_keys = [
                    _relative_path_text(path, self.target_dir).casefold()
                    for path in matching_paths
                ]
                selected_group_keys = [
                    key for key in group_keys if key in selected_keys
                ]
                if selected_group_keys and len(selected_group_keys) < len(group_keys):
                    deletable_keys.update(selected_group_keys)

        return deletable_keys

    def _classify_image(
        self,
        image: Any,
        face_detector: Any | None,
        person_detector: Any | None = None,
    ) -> dict[str, Any]:
        width, height = image.size
        aspect_ratio = width / max(height, 1)
        landscape_features = self._analyze_landscape_features(image)
        has_face = (
            self._image_has_face(image, face_detector, self.face_detection)
            if face_detector is not None
            else None
        )
        has_person = (
            self._image_has_person(image, person_detector, self.face_detection)
            if person_detector is not None
            else None
        )
        perceptual_hash = (
            self._image_perceptual_hash(image) if self.analyze_similar_images else None
        )
        is_classifiable_size = min(width, height) >= MIN_CLASSIFIABLE_IMAGE_EDGE
        shape_non_portrait = self._looks_non_portrait_by_shape(width, height)
        shape_landscape = self._looks_like_landscape(width, height)
        landscape_detected = bool(landscape_features["detected"])
        if landscape_detected and has_face is True:
            landscape_features = {
                **landscape_features,
                "raw_detected": True,
                "detected": False,
                "reason": "face_detected",
            }
        elif landscape_detected and has_person is True:
            landscape_features = {
                **landscape_features,
                "raw_detected": True,
                "detected": False,
                "reason": "person_detected",
            }
        is_landscape = bool(landscape_features["detected"])
        is_non_portrait = is_classifiable_size and (
            (
                has_face is False
                and has_person is not True
                and self.face_detection.allow_no_face_non_portrait
            )
            or (
                shape_non_portrait
                and has_face is None
                and has_person is not True
                and self.face_detection.allow_shape_only_non_portrait
            )
        )

        if not is_classifiable_size:
            reason = "image_too_small_for_reliable_filter"
            confidence = "low"
        elif has_face is True:
            reason = "face_detected"
            confidence = "high"
        elif has_person is True:
            reason = "person_detected"
            confidence = "high"
        elif is_landscape:
            reason = "semantic_landscape_features"
            confidence = landscape_features["confidence"]
        elif is_non_portrait and has_face is False:
            reason = "no_human_detected" if has_person is False else "no_face_detected"
            confidence = "medium" if shape_non_portrait else "low"
        elif is_non_portrait:
            reason = "wide_image_shape_only_allowed"
            confidence = "low"
        elif shape_non_portrait and has_face is False:
            reason = "wide_image_no_face_untrusted"
            confidence = "low"
        elif shape_non_portrait and has_face is None:
            reason = "wide_image_face_detection_uncertain"
            confidence = "low"
        elif shape_landscape:
            reason = "wide_image_without_landscape_features"
            confidence = "low"
        elif has_face is False:
            reason = "no_face_detected_but_shape_is_portrait_like"
            confidence = "low"
        else:
            reason = "portrait_or_unknown"
            confidence = "low"

        return {
            "width": width,
            "height": height,
            "aspect_ratio": round(aspect_ratio, 4),
            "is_landscape": is_landscape,
            "shape_landscape": shape_landscape,
            "is_non_portrait": is_non_portrait,
            "has_face": has_face,
            "has_person": has_person,
            "perceptual_hash": perceptual_hash,
            "reason": reason,
            "confidence": confidence,
            "landscape_features": landscape_features,
        }

    @staticmethod
    def _found_file_metadata_from_classification(
        classification: dict[str, Any],
    ) -> dict[str, Any]:
        landscape_features = classification.get("landscape_features")
        metadata: dict[str, Any] = {
            "classification_confidence": classification.get("confidence"),
            "has_face": classification.get("has_face"),
            "has_person": classification.get("has_person"),
        }
        if isinstance(landscape_features, dict):
            metadata.update(
                {
                    "ai_landscape_detected": landscape_features.get("detected"),
                    "ai_landscape_score": landscape_features.get("score"),
                    "landscape_features": landscape_features.get("features", []),
                    "dominant_landscape_features": landscape_features.get(
                        "dominant_features",
                        [],
                    ),
                },
            )
        return metadata

    @staticmethod
    def _load_face_detector() -> tuple[Any | None, str]:
        try:
            import cv2
        except Exception:
            return None, "pillow+orientation-heuristic"

        try:
            cascade_names = (
                "haarcascade_frontalface_default.xml",
                "haarcascade_frontalface_alt.xml",
                "haarcascade_frontalface_alt2.xml",
                "haarcascade_frontalface_alt_tree.xml",
                "haarcascade_profileface.xml",
            )
            detectors = []
            for cascade_name in cascade_names:
                cascade_path = Path(cv2.data.haarcascades) / cascade_name
                detector = cv2.CascadeClassifier(str(cascade_path))
                if not detector.empty():
                    detectors.append(detector)
            if not detectors:
                return None, "pillow+orientation-heuristic"
        except Exception:
            return None, "pillow+orientation-heuristic"
        return tuple(detectors), "pillow+opencv-multi-face-v2"

    @staticmethod
    def _load_person_detector() -> tuple[Any | None, str]:
        try:
            import cv2
        except Exception:
            return None, "person-detection-unavailable"

        hog = None
        cascades = []
        try:
            hog = cv2.HOGDescriptor()
            hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        except Exception:
            hog = None

        try:
            cascade_names = (
                "haarcascade_fullbody.xml",
                "haarcascade_upperbody.xml",
                "haarcascade_lowerbody.xml",
            )
            for cascade_name in cascade_names:
                cascade_path = Path(cv2.data.haarcascades) / cascade_name
                detector = cv2.CascadeClassifier(str(cascade_path))
                if not detector.empty():
                    cascades.append(detector)
        except Exception:
            cascades = []

        if hog is None and not cascades:
            return None, "person-detection-unavailable"
        return {"hog": hog, "cascades": tuple(cascades)}, "opencv-hog-body-cascade"

    @staticmethod
    def _image_has_face(
        image: Any,
        detector: Any,
        config: FaceDetectionConfig | None = None,
    ) -> bool | None:
        try:
            import cv2
            import numpy as np

            cfg = config or FaceDetectionConfig()
            detectors = (
                detector
                if isinstance(detector, (list, tuple))
                else (detector,)
            )
            rgb_image = image.convert("RGB")
            rgb_array = np.array(rgb_image)
            height, width = rgb_array.shape[:2]
            max_edge = max(width, height)
            if max_edge > cfg.face_max_edge:
                scale = cfg.face_max_edge / max_edge
                rgb_array = cv2.resize(
                    rgb_array,
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    interpolation=cv2.INTER_AREA,
                )
                height, width = rgb_array.shape[:2]
            gray_image = cv2.cvtColor(rgb_array, cv2.COLOR_RGB2GRAY)
            equalized_gray = cv2.equalizeHist(gray_image)
            clahe_gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(
                gray_image
            )
            min_face_size = max(
                cfg.min_size_px,
                min(width, height) // cfg.min_size_ratio,
            )
            detection_passes = (
                (cfg.min_neighbors, min_face_size),
                (
                    cfg.relaxed_neighbors,
                    max(
                        cfg.relaxed_min_size_px,
                        min(width, height) // cfg.relaxed_min_size_ratio,
                    ),
                ),
            )
            for active_detector in detectors:
                for candidate_gray in (
                    gray_image,
                    equalized_gray,
                    clahe_gray,
                    cv2.flip(gray_image, 1),
                    cv2.flip(equalized_gray, 1),
                    cv2.flip(clahe_gray, 1),
                ):
                    for min_neighbors, pass_min_face_size in detection_passes:
                        faces = active_detector.detectMultiScale(
                            candidate_gray,
                            scaleFactor=cfg.scale_factor,
                            minNeighbors=min_neighbors,
                            minSize=(pass_min_face_size, pass_min_face_size),
                        )
                        if len(faces) > 0:
                            return True
            return False
        except Exception:
            return None

    @staticmethod
    def _image_has_person(
        image: Any,
        detector: Any,
        config: FaceDetectionConfig | None = None,
    ) -> bool | None:
        try:
            import cv2
            import numpy as np

            cfg = config or FaceDetectionConfig()
            rgb_image = image.convert("RGB")
            rgb_array = np.array(rgb_image)
            height, width = rgb_array.shape[:2]
            max_edge = max(width, height)
            if max_edge > cfg.person_max_edge:
                scale = cfg.person_max_edge / max_edge
                rgb_array = cv2.resize(
                    rgb_array,
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    interpolation=cv2.INTER_AREA,
                )
                height, width = rgb_array.shape[:2]

            hog = detector.get("hog") if isinstance(detector, dict) else None
            cascades = detector.get("cascades", ()) if isinstance(detector, dict) else ()

            if cascades:
                gray_image = cv2.cvtColor(rgb_array, cv2.COLOR_RGB2GRAY)
                equalized_gray = cv2.equalizeHist(gray_image)
                min_body_size = max(36, min(width, height) // 8)
                for active_detector in cascades:
                    for candidate_gray in (
                        gray_image,
                        equalized_gray,
                        cv2.flip(gray_image, 1),
                        cv2.flip(equalized_gray, 1),
                    ):
                        bodies = active_detector.detectMultiScale(
                            candidate_gray,
                            scaleFactor=cfg.person_scale,
                            minNeighbors=cfg.person_min_neighbors,
                            minSize=(min_body_size, min_body_size),
                        )
                        if len(bodies) > 0:
                            return True

            if hog is not None and min(width, height) >= 96:
                rectangles, weights = hog.detectMultiScale(
                    rgb_array,
                    hitThreshold=0,
                    winStride=(8, 8),
                    padding=(8, 8),
                    scale=cfg.person_scale,
                )
                if len(rectangles) > 0:
                    if len(weights) == 0:
                        return True
                    if any(float(weight) >= cfg.person_hog_weight for weight in weights):
                        return True

            return False
        except Exception:
            return None

    @staticmethod
    def _flattened_image_data(image: Any) -> Iterable[Any]:
        get_flattened_data = getattr(image, "get_flattened_data", None)
        if callable(get_flattened_data):
            return get_flattened_data()
        return image.getdata()

    @staticmethod
    def _image_perceptual_hash(image: Any) -> str | None:
        try:
            sample = image.convert("L").resize((9, 8))
            pixels = list(DuplicateCleaner._flattened_image_data(sample))
            value = 0
            for row in range(8):
                offset = row * 9
                for column in range(8):
                    value = (value << 1) | int(
                        pixels[offset + column] > pixels[offset + column + 1]
                    )
            return f"{value:016x}"
        except Exception:
            return None

    def _video_sample_count(self) -> int:
        speed = max(1, min(100, int(self.face_detection.analysis_speed)))
        return max(3, min(9, round(10 - 6 * (speed / 100))))

    @staticmethod
    def _video_issue(
        category: str,
        reason: str,
        *,
        size: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        issue: dict[str, Any] = {
            "category": category,
            "reason": reason,
        }
        if size is not None:
            issue["size"] = size
        if error:
            issue["error"] = error
        return issue

    def _video_direct_issue(
        self,
        path: Path,
        size: int | None = None,
    ) -> dict[str, Any] | None:
        try:
            actual_size = path.stat().st_size if size is None else int(size)
        except OSError as exc:
            return self._video_issue(
                CATEGORY_BAD_VIDEO_FILE,
                "stat_failed",
                error=str(exc),
            )

        if actual_size > VIDEO_FINGERPRINT_MAX_BYTES:
            return self._video_issue(
                CATEGORY_LARGE_VIDEO_FILE,
                "file_too_large",
                size=actual_size,
            )
        if actual_size <= 0:
            return self._video_issue(
                CATEGORY_BAD_VIDEO_FILE,
                "empty_file",
                size=actual_size,
            )
        if actual_size < VIDEO_FINGERPRINT_MIN_BYTES:
            return self._video_issue(
                CATEGORY_BAD_VIDEO_FILE,
                "file_too_small",
                size=actual_size,
            )
        return None

    def _video_fingerprint(self, path: Path) -> dict[str, Any] | None:
        size = path.stat().st_size
        if size <= 0:
            return None

        chunk_size = min(VIDEO_FINGERPRINT_CHUNK_SIZE, size)
        positions = self._video_sample_positions(
            size,
            chunk_size,
            self._video_sample_count(),
        )
        hashes: list[str] = []
        sampled_byte_count = 0
        try:
            with path.open("rb") as handle:
                for position in positions:
                    handle.seek(position)
                    chunk = handle.read(chunk_size)
                    if not chunk:
                        continue
                    sampled_byte_count += len(chunk)
                    chunk_hash = self._bytes_fingerprint_hash(chunk)
                    if chunk_hash:
                        hashes.append(chunk_hash)
        except OSError:
            return None

        if not hashes:
            return None

        return {
            "method": VIDEO_FINGERPRINT_METHOD,
            "byte_size": size,
            "sampled_byte_count": sampled_byte_count,
            "perceptual_hashes": hashes,
            "sample_count": len(hashes),
        }

    @staticmethod
    def _video_sample_positions(
        size: int,
        chunk_size: int,
        sample_count: int,
    ) -> list[int]:
        if size <= 0 or chunk_size <= 0 or sample_count <= 0:
            return []
        max_position = max(0, size - chunk_size)
        if max_position == 0:
            return [0]

        positions = {0, max_position}
        middle_count = max(0, sample_count - len(positions))
        for index in range(middle_count):
            fraction = (index + 1) / (middle_count + 1)
            positions.add(int(max_position * fraction))
        return sorted(positions)

    @staticmethod
    def _bytes_fingerprint_hash(data: bytes) -> str | None:
        if not data:
            return None

        mask = (1 << 64) - 1
        weights = [0] * 64
        step = max(1, len(data) // 4096)
        for sample_index, byte in enumerate(data[::step]):
            mixed = (
                ((byte + 1) * 0x9E3779B185EBCA87)
                ^ ((sample_index + 1) * 0xC2B2AE3D27D4EB4F)
            ) & mask
            for bit in range(64):
                if mixed & (1 << bit):
                    weights[bit] += 1
                else:
                    weights[bit] -= 1

        value = 0
        for bit_weight in weights:
            value = (value << 1) | int(bit_weight >= 0)
        return f"{value:016x}"

    def _build_found_files(
        self,
        groups: list[DuplicateGroup],
        image_report: dict[str, Any],
    ) -> list[dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        details_by_path = {
            str(detail.get("path")): detail
            for detail in image_report.get("image_details", [])
            if isinstance(detail, dict) and detail.get("path")
        }

        def image_metadata(path_text: str) -> dict[str, Any]:
            detail = details_by_path.get(path_text)
            if not isinstance(detail, dict):
                return {}
            metadata: dict[str, Any] = {}
            for key in (
                "classification_confidence",
                "has_face",
                "has_person",
                "ai_landscape_detected",
                "ai_landscape_score",
                "landscape_features",
                "dominant_landscape_features",
            ):
                if key in detail:
                    metadata[key] = detail[key]
            return metadata

        def add(
            path_text: str,
            category: str,
            size: int | None = None,
            metadata: dict[str, Any] | None = None,
        ) -> None:
            key = path_text.casefold()
            entry = entries.setdefault(
                key,
                {
                    "path": path_text,
                    "categories": [],
                    "size": size,
                    "thumbnail": self._thumbnail_data_uri(path_text),
                },
            )
            if category not in entry["categories"]:
                entry["categories"].append(category)
            if entry.get("size") is None and size is not None:
                entry["size"] = size
            for metadata_key, metadata_value in (metadata or {}).items():
                if metadata_value is not None:
                    entry[metadata_key] = metadata_value

        for group in groups:
            for duplicate in group.duplicates:
                add(
                    _relative_path_text(duplicate, self.target_dir),
                    CATEGORY_EXACT_DUPLICATE,
                    group.size,
                )

        for deleted in self._deleted_during_scan:
            if not isinstance(deleted, dict) or not deleted.get("path"):
                continue
            path_text = str(deleted["path"])
            add(
                path_text,
                CATEGORY_EXACT_DUPLICATE,
                deleted.get("size") if isinstance(deleted.get("size"), int) else None,
                {
                    "deleted_during_scan": True,
                    "deleted_at": deleted.get("deleted_at"),
                    "kept_path": deleted.get("kept_path"),
                },
            )

        for path_text in image_report.get("landscape_images", []):
            path_text = str(path_text)
            add(
                path_text,
                CATEGORY_LANDSCAPE_IMAGE,
                self._safe_stat_size(path_text),
                image_metadata(path_text),
            )

        for path_text in image_report.get("non_portrait_images", []):
            path_text = str(path_text)
            add(
                path_text,
                CATEGORY_NON_PORTRAIT_IMAGE,
                self._safe_stat_size(path_text),
                image_metadata(path_text),
            )

        for duplicate in image_report.get("similar_image_duplicates", []):
            if not isinstance(duplicate, dict) or not duplicate.get("path"):
                continue
            path_text = str(duplicate["path"])
            add(
                path_text,
                CATEGORY_SIMILAR_IMAGE_DUPLICATE,
                self._safe_stat_size(path_text),
                {
                    "similar_to": duplicate.get("similar_to"),
                    "image_similarity": duplicate.get("image_similarity"),
                    "perceptual_distance": duplicate.get("perceptual_distance"),
                    **image_metadata(path_text),
                },
            )

        for duplicate in image_report.get("similar_video_duplicates", []):
            if not isinstance(duplicate, dict) or not duplicate.get("path"):
                continue
            path_text = str(duplicate["path"])
            add(
                path_text,
                CATEGORY_SIMILAR_VIDEO_DUPLICATE,
                self._safe_stat_size(path_text),
                {
                    "similar_to": duplicate.get("similar_to"),
                    "video_similarity": duplicate.get("video_similarity"),
                    "perceptual_distance": duplicate.get("perceptual_distance"),
                },
            )

        for issue in image_report.get("large_video_files", []):
            if not isinstance(issue, dict) or not issue.get("path"):
                continue
            path_text = str(issue["path"])
            add(
                path_text,
                CATEGORY_LARGE_VIDEO_FILE,
                issue.get("size")
                if isinstance(issue.get("size"), int)
                else self._safe_stat_size(path_text),
                {"video_issue": issue.get("reason")},
            )

        for issue in image_report.get("bad_video_files", []):
            if not isinstance(issue, dict) or not issue.get("path"):
                continue
            path_text = str(issue["path"])
            add(
                path_text,
                CATEGORY_BAD_VIDEO_FILE,
                issue.get("size")
                if isinstance(issue.get("size"), int)
                else self._safe_stat_size(path_text),
                {"video_issue": issue.get("reason")},
            )

        return sorted(
            entries.values(),
            key=lambda entry: str(entry["path"]).casefold(),
        )

    def _safe_stat_size(self, relative_path: str) -> int | None:
        normalized = self._normalize_selected_path(relative_path)
        if normalized is None:
            return None
        target = (self.target_dir / normalized).resolve()
        if not self._is_inside_target(target) or not target.is_file():
            return None
        try:
            return target.stat().st_size
        except OSError:
            return None

    def _thumbnail_data_uri(self, relative_path: str) -> str | None:
        normalized = self._normalize_selected_path(relative_path)
        if normalized is None:
            return None
        target = (self.target_dir / normalized).resolve()
        if (
            not self._is_inside_target(target)
            or not target.is_file()
            or target.suffix.casefold() not in IMAGE_EXTENSIONS
        ):
            return None

        try:
            from PIL import Image, ImageOps

            with Image.open(target) as opened_image:
                image = ImageOps.exif_transpose(opened_image).convert("RGB")
                image.thumbnail((THUMBNAIL_MAX_EDGE, THUMBNAIL_MAX_EDGE))
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=72, optimize=True)
        except Exception:
            return None

        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    def _move_duplicate_files(
        self,
        groups: list[DuplicateGroup],
    ) -> list[dict[str, str]]:
        moved_files: list[dict[str, str]] = []
        backup_dir = self.backup_root / "exact_duplicates" / self._backup_batch_name()
        backup_dir.mkdir(parents=True, exist_ok=True)

        for group in groups:
            for source in group.duplicates:
                if not source.exists() or self._is_excluded(source):
                    continue
                destination = self._unique_backup_destination(source, backup_dir)
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(source), str(destination))
                except OSError as exc:
                    self._log(f"Could not move file: {source} ({exc})")
                    continue
                moved_files.append(
                    {
                        "source": _relative_path_text(source, self.target_dir),
                        "destination": str(destination),
                        "destination_backup_path": _relative_path_text(
                            destination,
                            self.backup_root,
                        ),
                    }
                )
        return moved_files

    def _unique_backup_destination(self, source: Path, backup_dir: Path) -> Path:
        relative = _safe_relative_path(source, self.target_dir)
        destination = backup_dir / relative
        if not destination.exists():
            return destination

        stem = destination.stem
        suffix = destination.suffix
        parent = destination.parent
        counter = 1
        while True:
            candidate = parent / f"{stem}_{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    @staticmethod
    def _backup_batch_name() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")

    def _build_report(
        self,
        started_at: str,
        files: list[Path],
        groups: list[DuplicateGroup],
        moved_files: list[dict[str, str]],
        image_report: dict[str, Any],
        found_files: list[dict[str, Any]],
        scan_folder_count: int,
    ) -> dict[str, Any]:
        duplicate_copy_count = sum(len(group.duplicates) for group in groups)
        deleted_during_scan_bytes = sum(
            int(item.get("size", 0))
            for item in self._deleted_during_scan
            if isinstance(item.get("size"), int)
        )
        reclaimable_bytes = sum(
            group.size * len(group.duplicates) for group in groups
        )
        report = {
            "ok": True,
            "tool": "duplicate-cleaner",
            "action": "scan",
            "target_dir": str(self.target_dir),
            "started_at": started_at,
            "finished_at": _utc_now(),
            "source_file_count": len(files),
            "scan_folder_count": scan_folder_count,
            "auto_move": self.auto_move,
            "delete_exact_during_scan": self.delete_exact_during_scan,
            "duplicate_analysis_enabled": self.analyze_duplicates,
            "parallel_analysis_enabled": self.parallel_analysis,
            "analysis_worker_count": self.image_analysis_workers,
            "face_detection": self._face_detection_report(),
            "backup_dir": str(self.backup_root),
            "report_file": str(self.report_path),
            "duplicate_group_count": len(groups),
            "duplicate_copy_count": duplicate_copy_count,
            "deleted_during_scan_count": len(self._deleted_during_scan),
            "deleted_during_scan_bytes": deleted_during_scan_bytes,
            "deleted_during_scan": self._deleted_during_scan,
            "moved_file_count": len(moved_files),
            "reclaimable_bytes": reclaimable_bytes,
            "duplicate_groups": [group.to_report(self.target_dir) for group in groups],
            "moved_files": moved_files,
            "found_file_count": len(found_files),
            "found_files": found_files,
        }
        report.update(image_report)
        return report

    def _face_detection_report(self) -> dict[str, Any]:
        return {
            "profile": self.face_detection.profile,
            "sensitivity": self.face_detection.sensitivity,
            "face_sensitivity": self.face_detection.face_sensitivity,
            "person_sensitivity": self.face_detection.person_sensitivity,
            "landscape_sensitivity": self.landscape_sensitivity,
            "analysis_speed": self.face_detection.analysis_speed,
            "image_workers": self.image_analysis_workers,
            "face_max_edge": self.face_detection.face_max_edge,
            "person_max_edge": self.face_detection.person_max_edge,
            "scale_factor": self.face_detection.scale_factor,
            "min_neighbors": self.face_detection.min_neighbors,
            "min_size_ratio": self.face_detection.min_size_ratio,
            "min_size_px": self.face_detection.min_size_px,
            "relaxed_neighbors": self.face_detection.relaxed_neighbors,
            "relaxed_min_size_ratio": self.face_detection.relaxed_min_size_ratio,
            "relaxed_min_size_px": self.face_detection.relaxed_min_size_px,
            "person_hog_weight": self.face_detection.person_hog_weight,
            "person_scale": self.face_detection.person_scale,
            "person_min_neighbors": self.face_detection.person_min_neighbors,
            "allow_shape_only_non_portrait": (
                self.face_detection.allow_shape_only_non_portrait
            ),
            "allow_no_face_non_portrait": (
                self.face_detection.allow_no_face_non_portrait
            ),
        }

    def _write_report(self, report: dict[str, Any]) -> None:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _emit_found_file(
        self,
        found_file: dict[str, Any],
        message: str,
        **payload: Any,
    ) -> None:
        path = str(found_file.get("path", "")).strip()
        if not path:
            return
        path_key = path.casefold()
        if "thumbnail" not in found_file:
            found_file = {**found_file, "thumbnail": self._thumbnail_data_uri(path)}
        categories = [
            str(category)
            for category in found_file.get("categories", [])
            if str(category).strip()
        ]
        existing_categories = self._progress_found_categories.setdefault(
            path_key,
            set(),
        )
        new_categories = [
            category for category in categories if category not in existing_categories
        ]
        if not new_categories and path_key in self._progress_found_paths:
            return
        existing_categories.update(new_categories)
        self._progress_found_paths.add(path_key)
        emitted_file = {
            **found_file,
            "categories": [
                category
                for category in categories
                if category in existing_categories
            ],
        }
        self._emit_progress(
            "found_file",
            message,
            found_file=emitted_file,
            found_file_count=len(self._progress_found_paths),
            **payload,
        )

    def _emit_progress(self, phase: str, message: str, **payload: Any) -> None:
        if self.progress_event_callback is None:
            return
        event = {
            "phase": phase,
            "message": message,
            "target_dir": str(self.target_dir),
            "timestamp": _utc_now(),
            **payload,
        }
        self.progress_event_callback(event)

    def _log(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)
        else:
            print(message)


def _print_progress_event(event: dict[str, Any]) -> None:
    print(
        PROGRESS_JSON_PREFIX + json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan exact duplicates and optional media candidates.",
    )
    parser.add_argument("target_dir", help="Folder to scan or clean.")
    parser.add_argument(
        "--auto-move",
        action="store_true",
        help="Move exact duplicate copies to the app data folder.",
    )
    parser.add_argument(
        "--delete-exact-during-scan",
        "--auto-delete-exact",
        action="store_true",
        help="Delete exact duplicate copies as soon as they are verified.",
    )
    parser.add_argument(
        "--no-duplicate-analysis",
        action="store_true",
        help="Skip exact duplicate analysis.",
    )
    parser.add_argument(
        "--image-analysis",
        action="store_true",
        help="Enable image analysis.",
    )
    parser.add_argument(
        "--no-image-analysis",
        action="store_true",
        help="Disable image analysis.",
    )
    parser.add_argument(
        "--no-ai-landscape-features",
        action="store_true",
        help="Disable local landscape feature scoring.",
    )
    parser.add_argument(
        "--ai-landscape-threshold",
        type=float,
        default=None,
        help="Landscape score threshold. Higher is stricter.",
    )
    parser.add_argument(
        "--face-sensitivity",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--person-sensitivity",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--landscape-sensitivity",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--analysis-speed",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--image-workers",
        "--media-workers",
        type=int,
        default=None,
        dest="image_workers",
    )
    parser.add_argument("--no-parallel-analysis", action="store_true")
    parser.add_argument(
        "--face-profile",
        choices=sorted(FACE_DETECTION_PROFILES),
        default="safe",
        help="Legacy face detection profile.",
    )
    parser.add_argument(
        "--face-scale-factor",
        type=float,
        default=None,
        help="OpenCV detectMultiScale scaleFactor override.",
    )
    parser.add_argument(
        "--face-min-neighbors",
        type=int,
        default=None,
        help="OpenCV detectMultiScale minNeighbors override.",
    )
    parser.add_argument(
        "--face-min-size-ratio",
        type=int,
        default=None,
        help="Minimum face size ratio override.",
    )
    parser.add_argument(
        "--face-min-size-px",
        type=int,
        default=None,
        help="Minimum face size in pixels override.",
    )
    parser.add_argument(
        "--allow-shape-only-non-portrait",
        action="store_true",
        help="Allow shape-only non-portrait candidates.",
    )
    parser.add_argument(
        "--allow-no-face-non-portrait",
        "--allow-no-face-images",
        action="store_true",
        dest="allow_no_face_non_portrait",
        help="Allow no-face image candidates.",
    )
    parser.add_argument(
        "--similar-image-analysis",
        action="store_true",
    )
    parser.add_argument(
        "--similar-image-threshold",
        type=int,
        default=DEFAULT_SIMILAR_IMAGE_THRESHOLD,
    )
    parser.add_argument(
        "--similar-video-analysis",
        action="store_true",
    )
    parser.add_argument(
        "--similar-video-threshold",
        type=int,
        default=DEFAULT_SIMILAR_VIDEO_THRESHOLD,
    )
    parser.add_argument(
        "--delete-selected",
        action="append",
        default=[],
        help="Delete a selected relative path. May be repeated.",
    )
    parser.add_argument(
        "--delete-selected-json",
        default="",
        help="Delete selected relative paths from a JSON array.",
    )
    parser.add_argument(
        "--allow-unverified-delete",
        action="store_true",
        help="Allow deleting files not verified as exact duplicates.",
    )
    parser.add_argument(
        "--report",
        default="",
        help="Custom report output path.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the JSON report.",
    )
    parser.add_argument(
        "--progress-jsonl",
        action="store_true",
        help="Print progress events as JSON lines.",
    )
    return parser.parse_args(argv)


def _parse_selected_paths(args: argparse.Namespace) -> list[str]:
    selected_paths = [str(path) for path in args.delete_selected]
    if not args.delete_selected_json:
        return selected_paths

    parsed = json.loads(args.delete_selected_json)
    if not isinstance(parsed, list):
        raise ValueError("--delete-selected-json must be a JSON array")
    selected_paths.extend(str(path) for path in parsed)
    return selected_paths


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = parse_args(argv)
    try:
        selected_paths = _parse_selected_paths(args)
        cleaner = DuplicateCleaner(
            args.target_dir,
            auto_move=bool(args.auto_move),
            delete_exact_during_scan=bool(args.delete_exact_during_scan),
            analyze_duplicates=not bool(args.no_duplicate_analysis),
            analyze_images=(
                bool(args.image_analysis) or bool(args.similar_image_analysis)
            )
            and not bool(args.no_image_analysis),
            analyze_landscape_features=not bool(args.no_ai_landscape_features),
            ai_landscape_threshold=args.ai_landscape_threshold,
            face_sensitivity=args.face_sensitivity,
            person_sensitivity=args.person_sensitivity,
            landscape_sensitivity=args.landscape_sensitivity,
            analysis_speed=args.analysis_speed,
            analyze_similar_images=bool(args.similar_image_analysis)
            and not bool(args.no_image_analysis),
            similar_image_threshold=args.similar_image_threshold,
            analyze_similar_videos=bool(args.similar_video_analysis),
            similar_video_threshold=args.similar_video_threshold,
            parallel_analysis=not bool(args.no_parallel_analysis),
            face_detection_profile=args.face_profile,
            face_scale_factor=args.face_scale_factor,
            face_min_neighbors=args.face_min_neighbors,
            face_min_size_ratio=args.face_min_size_ratio,
            face_min_size_px=args.face_min_size_px,
            image_analysis_workers=args.image_workers,
            allow_shape_only_non_portrait=bool(args.allow_shape_only_non_portrait),
            allow_no_face_non_portrait=bool(args.allow_no_face_non_portrait),
            report_path=args.report or None,
            progress_callback=(lambda _message: None) if args.json else print,
            progress_event_callback=(
                _print_progress_event if args.progress_jsonl else None
            ),
        )
        report = (
            cleaner.delete_selected(
                selected_paths,
                allow_unverified=bool(args.allow_unverified_delete),
            )
            if selected_paths
            else cleaner.run()
        )
    except Exception as exc:
        error_report = {"ok": False, "message": str(exc)}
        print(json.dumps(error_report, ensure_ascii=False))
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2 if args.json else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
