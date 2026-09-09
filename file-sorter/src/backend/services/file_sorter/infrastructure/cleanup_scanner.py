"""Cleanup scanner for media review and duplicate detection."""

from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
from typing import Any, Sequence

from .cleanup_constants import (
    DEFAULT_ANALYSIS_SPEED,
    DEFAULT_SIMILAR_VIDEO_THRESHOLD,
    IMAGE_EXTENSIONS,
    ProgressEventCallback,
    VIDEO_EXTENSIONS,
)
from .cleanup_duplicates import print_progress_event
from .cleanup_scanner_processing import MediaProcessingMixin
from .cleanup_scanner_similarity import VideoSimilarityMixin
from .cleanup_scanner_video import VideoFingerprintMixin
from .cleanup_utils import (
    _clamp_percent,
    _configure_utf8_stdio,
    _is_link_or_reparse,
    _relative_path_text,
    _resolve_target_dir,
)
from .video_fingerprint_cache import VideoFingerprintCache


class CleanupScanner(VideoFingerprintMixin, VideoSimilarityMixin, MediaProcessingMixin):
    def __init__(
        self,
        target_dir: str | Path,
        *,
        image_cleanup: bool = False,
        similar_image_analysis: bool = False,
        video_cleanup: bool = True,
        similar_video_analysis: bool = False,
        similar_video_threshold: int | None = None,
        analysis_speed: int | None = None,
        parallel_analysis: bool = True,
        model_temperature: float = 0.0,
        model_top_p: float = 0.9,
        model_context_window: int = 8192,
        model_max_output_tokens: int = 512,
        progress_event_callback: ProgressEventCallback | None = None,
    ) -> None:
        self.target_dir = _resolve_target_dir(target_dir)
        self.image_cleanup = bool(image_cleanup)
        self.similar_image_analysis = bool(similar_image_analysis)
        self.video_cleanup = bool(video_cleanup or similar_video_analysis)
        self.similar_video_analysis = bool(similar_video_analysis)
        self.similar_video_threshold = _clamp_percent(
            similar_video_threshold,
            DEFAULT_SIMILAR_VIDEO_THRESHOLD,
        )
        self.analysis_speed = _clamp_percent(analysis_speed, DEFAULT_ANALYSIS_SPEED)
        self.parallel_analysis = bool(parallel_analysis)
        self.model_temperature = model_temperature
        self.model_top_p = model_top_p
        self.model_context_window = model_context_window
        self.model_max_output_tokens = model_max_output_tokens
        self.progress_event_callback = progress_event_callback
        self._found_files: dict[str, dict[str, Any]] = {}
        self._fingerprint_cache = VideoFingerprintCache()

    def run(self) -> dict[str, Any]:
        folders = self._list_scan_folders()
        report = self._empty_report(len(folders))
        image_helpers = self._load_image_helpers() if (self.image_cleanup or self.similar_image_analysis or self.similar_video_analysis) else {}
        image_similarity_paths: list[Path] = []
        video_similarity_paths: list[Path] = []

        self._emit_progress(
            "scan_started",
            "Cleanup scan started",
            folder_current=0,
            folder_total=len(folders),
            source_file_count=0,
            found_file_count=0,
        )

        source_file_count = 0
        try:
            for folder_index, folder in enumerate(folders, start=1):
                folder_text = _relative_path_text(folder, self.target_dir)
                self._emit_progress(
                    "folder_scan",
                    f"Scanning folder {folder_index}/{len(folders)}: {folder_text}",
                    folder_current=folder_index,
                    folder_total=len(folders),
                    current_folder=folder_text,
                    source_file_count=source_file_count,
                    found_file_count=len(self._found_files),
                )
                try:
                    entries = sorted(folder.iterdir(), key=lambda item: item.name.casefold())
                except OSError as exc:
                    report["warnings"].append(f"{folder_text}: {exc}")
                    continue

                for path in entries:
                    if not self._is_contained_regular_file(path) or self._is_excluded(path):
                        continue
                    source_file_count += 1
                    report["source_file_count"] = source_file_count

                    if self.similar_image_analysis and self._is_image_candidate(path):
                        image_similarity_paths.append(path)
                        self._process_image_candidate(
                            path,
                            report,
                            image_helpers,
                            folder_index=folder_index,
                            folder_total=len(folders),
                            folder_text=folder_text,
                            source_file_count=source_file_count,
                        )

                    if self.video_cleanup and self._is_video_candidate(path):
                        self._process_video_issue_candidate(
                            path,
                            report,
                            folder_index=folder_index,
                            folder_total=len(folders),
                            folder_text=folder_text,
                            source_file_count=source_file_count,
                        )
                        if self.similar_video_analysis:
                            direct_issue = self._video_direct_issue(path)
                            if direct_issue is None:
                                video_similarity_paths.append(path)

                self._emit_progress(
                    "folder_done",
                    f"Finished folder {folder_index}/{len(folders)}: {folder_text}",
                    folder_current=folder_index,
                    folder_total=len(folders),
                    current_folder=folder_text,
                    source_file_count=source_file_count,
                    found_file_count=len(self._found_files),
                )

        finally:
            pass

        if self.similar_image_analysis:
            self._append_model_similarity_results(
                image_similarity_paths,
                report,
                image_helpers,
                media_type="image",
            )
        if self.similar_video_analysis:
            self._append_model_similarity_results(
                video_similarity_paths,
                report,
                image_helpers,
                media_type="video",
            )
        visual_model = image_helpers.get("model")
        if visual_model is not None:
            visual_model.close()

        report["found_files"] = sorted(
            self._found_files.values(),
            key=lambda item: str(item.get("path", "")).casefold(),
        )
        report["found_file_count"] = len(report["found_files"])
        self._emit_progress(
            "scan_completed",
            "Cleanup scan completed",
            folder_current=len(folders),
            folder_total=len(folders),
            source_file_count=source_file_count,
            found_file_count=report["found_file_count"],
        )
        return report

    def _empty_report(self, folder_count: int) -> dict[str, Any]:
        return {
            "ok": True,
            "tool": "file-sorter-cleanup",
            "target_dir": str(self.target_dir),
            "scan_folder_count": folder_count,
            "source_file_count": 0,
            "cleanup_action": "scan_only",
            "image_cleanup_enabled": self.image_cleanup,
            "similar_image_analysis_enabled": self.similar_image_analysis,
            "image_file_count": 0,
            "person_detected_image_count": 0,
            "non_person_image_count": 0,
            "non_person_images": [],
            "indeterminate_image_count": 0,
            "visual_recognition_service": "openbmb/minicpm-v4.6:q8_0",
            "visual_recognition_service_enabled": bool(
                self.image_cleanup
                or self.similar_image_analysis
                or self.similar_video_analysis
            ),
            "visual_model_parameters": {
                "temperature": max(0.0, min(1.0, float(self.model_temperature))),
                "top_p": max(0.1, min(1.0, float(self.model_top_p))),
                "context_window": max(2048, min(16384, int(self.model_context_window))),
                "max_output_tokens": max(128, min(1024, int(self.model_max_output_tokens))),
            },
            "video_cleanup_enabled": self.video_cleanup,
            "video_file_count": 0,
            "large_video_file_count": 0,
            "large_video_files": [],
            "bad_video_file_count": 0,
            "bad_video_files": [],
            "video_details": [],
            "similar_video_analysis_enabled": self.similar_video_analysis,
            "similar_image_duplicate_count": 0,
            "similar_image_groups": [],
            "similar_image_duplicates": [],
            "similar_video_threshold": self.similar_video_threshold,
            "similar_video_duplicate_count": 0,
            "similar_video_groups": [],
            "similar_video_duplicates": [],
            "similar_video_candidate_pair_count": 0,
            "similar_video_comparison_count": 0,
            "video_fingerprint_cache_hit_count": 0,
            "video_analysis_method": (
                "minicpm-v4.6-visual-similarity" if self.similar_image_analysis else "disabled"
            ),
            "image_similarity_method": (
                "minicpm-v4.6-visual-similarity" if self.similar_video_analysis else "disabled"
            ),
            "found_file_count": 0,
            "found_files": [],
            "warnings": [],
        }

    def _list_scan_folders(self) -> list[Path]:
        folders: list[Path] = []
        for current_root, directory_names, _file_names in os.walk(
            self.target_dir,
            topdown=True,
            followlinks=False,
        ):
            current = Path(current_root)
            if current != self.target_dir and _is_link_or_reparse(current):
                directory_names[:] = []
                continue
            if self._is_excluded_folder(current):
                directory_names[:] = []
                continue
            folders.append(current)
            directory_names[:] = [
                name
                for name in directory_names
                if not self._is_excluded_folder(current / name)
                and not _is_link_or_reparse(current / name)
            ]
        return sorted(
            folders,
            key=lambda item: _relative_path_text(item, self.target_dir).casefold(),
        )

    @staticmethod
    def _is_image_candidate(path: Path) -> bool:
        return path.suffix.casefold() in IMAGE_EXTENSIONS

    @staticmethod
    def _is_video_candidate(path: Path) -> bool:
        return path.suffix.casefold() in VIDEO_EXTENSIONS

    def _is_excluded_folder(self, path: Path) -> bool:
        try:
            relative = path.resolve().relative_to(self.target_dir)
        except (OSError, ValueError):
            return True
        excluded_names = {
            ".git",
            "__pycache__",
            "_cleaner_backup",
            ".gptbridge_cleanerquarantine",
        }
        return any(part.casefold() in excluded_names for part in relative.parts)

    def _is_excluded(self, path: Path) -> bool:
        return self._is_excluded_folder(path.parent)

    def _is_contained_regular_file(self, path: Path) -> bool:
        if _is_link_or_reparse(path):
            return False
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.target_dir)
        except (OSError, ValueError):
            return False
        return resolved.is_file()

    def _create_executor(self) -> concurrent.futures.ThreadPoolExecutor | None:
        if not self.parallel_analysis:
            return None
        workers = min(2, max(1, os.cpu_count() or 1))
        if workers <= 1:
            return None
        return concurrent.futures.ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="file-sorter-cleanup-video",
        )

    def _load_image_helpers(self) -> dict[str, Any]:
        try:
            from .minicpm_visual_service import MiniCPMVisualRecognitionService

            model = MiniCPMVisualRecognitionService(
                temperature=self.model_temperature,
                top_p=self.model_top_p,
                context_window=self.model_context_window,
                max_output_tokens=self.model_max_output_tokens,
            )
        except Exception as exc:
            return {"error": f"MiniCPM-V visual recognition service is not available: {exc}"}
        return {"model": model}

    def _add_found_file(
        self,
        path_text: str,
        categories: Sequence[str],
        *,
        size: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not categories:
            return
        key = path_text.casefold()
        entry = self._found_files.setdefault(
            key,
            {
                "path": path_text,
                "categories": [],
                "size": size,
            },
        )
        for category in categories:
            if category not in entry["categories"]:
                entry["categories"].append(category)
        if size is not None:
            entry["size"] = size
        if metadata:
            entry.update(metadata)

    @staticmethod
    def _safe_stat_size(path: Path) -> int | None:
        try:
            return path.stat().st_size
        except OSError:
            return None

    def _emit_progress(self, phase: str, message: str, **payload: Any) -> None:
        if self.progress_event_callback is None:
            return
        self.progress_event_callback(
            {
                "phase": phase,
                "message": message,
                "target_dir": str(self.target_dir),
                **payload,
            }
        )


def run_cleanup_scan(
    target_dir: str | Path,
    *,
    image_cleanup: bool = False,
    similar_image_analysis: bool = False,
    video_cleanup: bool = True,
    similar_video_analysis: bool = False,
    similar_video_threshold: int | None = None,
    analysis_speed: int | None = None,
    parallel_analysis: bool = True,
    model_temperature: float = 0.0,
    model_top_p: float = 0.9,
    model_context_window: int = 8192,
    model_max_output_tokens: int = 512,
    progress_event_callback: ProgressEventCallback | None = None,
) -> dict[str, Any]:
    scanner = CleanupScanner(
        target_dir,
        image_cleanup=image_cleanup,
        similar_image_analysis=similar_image_analysis,
        video_cleanup=video_cleanup,
        similar_video_analysis=similar_video_analysis,
        similar_video_threshold=similar_video_threshold,
        analysis_speed=analysis_speed,
        parallel_analysis=parallel_analysis,
        model_temperature=model_temperature,
        model_top_p=model_top_p,
        model_context_window=model_context_window,
        model_max_output_tokens=model_max_output_tokens,
        progress_event_callback=progress_event_callback,
    )
    return scanner.run()


def main(argv: list[str] | None = None) -> int:
    import argparse

    _configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Scan media cleanup candidates without exact duplicate or similar image detection."
    )
    parser.add_argument("target_dir")
    parser.add_argument("--image-cleanup", action="store_true")
    parser.add_argument("--similar-image-analysis", action="store_true")
    parser.add_argument("--video-cleanup", action="store_true")
    parser.add_argument("--similar-video-analysis", action="store_true")
    parser.add_argument(
        "--similar-video-threshold",
        type=int,
        default=DEFAULT_SIMILAR_VIDEO_THRESHOLD,
    )
    parser.add_argument("--analysis-speed", type=int, default=DEFAULT_ANALYSIS_SPEED)
    parser.add_argument("--no-parallel-analysis", action="store_true")
    parser.add_argument("--model-temperature", type=float, default=0.0)
    parser.add_argument("--model-top-p", type=float, default=0.9)
    parser.add_argument("--model-context-window", type=int, default=8192)
    parser.add_argument("--model-max-output-tokens", type=int, default=512)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--progress-jsonl", action="store_true")
    args = parser.parse_args(argv)

    selected = bool(args.image_cleanup or args.similar_image_analysis or args.video_cleanup or args.similar_video_analysis)
    report = run_cleanup_scan(
        args.target_dir,
        image_cleanup=bool(args.image_cleanup),
        similar_image_analysis=bool(args.similar_image_analysis),
        video_cleanup=bool(args.video_cleanup or args.similar_video_analysis or not selected),
        similar_video_analysis=bool(args.similar_video_analysis),
        similar_video_threshold=args.similar_video_threshold,
        analysis_speed=args.analysis_speed,
        parallel_analysis=not bool(args.no_parallel_analysis),
        model_temperature=args.model_temperature,
        model_top_p=args.model_top_p,
        model_context_window=args.model_context_window,
        model_max_output_tokens=args.model_max_output_tokens,
        progress_event_callback=print_progress_event if args.progress_jsonl else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.json else None))
    return 0 if report.get("ok") is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
