"""Media cleanup scanning for the file sorter tool.

This module intentionally does not include exact duplicate file detection or
similar image detection. The retained cleanup feature is similar video scanning,
plus lightweight image/video issue reporting.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Sequence


PROGRESS_JSON_PREFIX = "FILE_SORTER_CLEANUP_PROGRESS_JSON="
VIDEO_FINGERPRINT_METHOD = "file-sampled-video-fingerprint-v1"
VIDEO_FINGERPRINT_MIN_BYTES = 1024
VIDEO_FINGERPRINT_CHUNK_SIZE = 64 * 1024
VIDEO_FINGERPRINT_MAX_BYTES = 4 * 1024 * 1024 * 1024
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
CATEGORY_LANDSCAPE_IMAGE = "landscape_image"
CATEGORY_NON_PORTRAIT_IMAGE = "non_portrait_image"
CATEGORY_LARGE_VIDEO_FILE = "large_video_file"
CATEGORY_BAD_VIDEO_FILE = "bad_video_file"
CATEGORY_SIMILAR_VIDEO_DUPLICATE = "similar_video_duplicate"
LANDSCAPE_ASPECT_RATIO = 1.45
NON_PORTRAIT_ASPECT_RATIO = 1.35
MIN_CLASSIFIABLE_IMAGE_EDGE = 256
DEFAULT_SIMILAR_VIDEO_THRESHOLD = 96
DEFAULT_ANALYSIS_SPEED = 50

ProgressEventCallback = Callable[[dict[str, Any]], None]


class CleanupError(Exception):
    """Raised when cleanup scanning cannot be completed."""


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            continue


def _clamp_percent(value: int | float | None, default: int) -> int:
    if value is None:
        return default
    return max(1, min(100, int(round(float(value)))))


def _safe_relative_path(path: Path, root: Path) -> Path:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return Path(path.name)
    return Path(*[part for part in relative.parts if part not in {"", ".", ".."}])


def _relative_path_text(path: Path, root: Path) -> str:
    return str(_safe_relative_path(path, root))


def _resolve_target_dir(target_dir: str | Path) -> Path:
    target = Path(target_dir).expanduser().resolve()
    if not target.is_dir():
        raise CleanupError(f"Target folder does not exist: {target}")
    return target


def print_progress_event(event: dict[str, Any]) -> None:
    print(
        PROGRESS_JSON_PREFIX
        + json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )


class CleanupScanner:
    def __init__(
        self,
        target_dir: str | Path,
        *,
        image_cleanup: bool = False,
        video_cleanup: bool = True,
        similar_video_analysis: bool = True,
        similar_video_threshold: int | None = None,
        analysis_speed: int | None = None,
        parallel_analysis: bool = True,
        progress_event_callback: ProgressEventCallback | None = None,
    ) -> None:
        self.target_dir = _resolve_target_dir(target_dir)
        self.image_cleanup = bool(image_cleanup)
        self.video_cleanup = bool(video_cleanup or similar_video_analysis)
        self.similar_video_analysis = bool(similar_video_analysis)
        self.similar_video_threshold = _clamp_percent(
            similar_video_threshold,
            DEFAULT_SIMILAR_VIDEO_THRESHOLD,
        )
        self.analysis_speed = _clamp_percent(analysis_speed, DEFAULT_ANALYSIS_SPEED)
        self.parallel_analysis = bool(parallel_analysis)
        self.progress_event_callback = progress_event_callback
        self._found_files: dict[str, dict[str, Any]] = {}

    def run(self) -> dict[str, Any]:
        folders = self._list_scan_folders()
        report = self._empty_report(len(folders))
        image_helpers = self._load_image_helpers() if self.image_cleanup else {}
        video_jobs: list[tuple[concurrent.futures.Future[dict[str, Any]], Path]] = []
        executor = self._create_executor() if self.similar_video_analysis else None

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
                    if not path.is_file() or self._is_excluded(path):
                        continue
                    source_file_count += 1
                    report["source_file_count"] = source_file_count

                    if self.image_cleanup and self._is_image_candidate(path):
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
                                if executor is not None:
                                    video_jobs.append((executor.submit(self._video_fingerprint, path), path))
                                else:
                                    detail = self._video_fingerprint(path)
                                    if detail is not None:
                                        self._record_video_detail(path, detail, report)

                self._emit_progress(
                    "folder_done",
                    f"Finished folder {folder_index}/{len(folders)}: {folder_text}",
                    folder_current=folder_index,
                    folder_total=len(folders),
                    current_folder=folder_text,
                    source_file_count=source_file_count,
                    found_file_count=len(self._found_files),
                )

            self._collect_video_jobs(video_jobs, report)
        finally:
            if executor is not None:
                executor.shutdown(wait=True)

        if self.similar_video_analysis:
            self._append_similar_video_results(report)

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
            "image_file_count": 0,
            "landscape_image_count": 0,
            "landscape_images": [],
            "non_portrait_image_count": 0,
            "non_portrait_images": [],
            "video_cleanup_enabled": self.video_cleanup,
            "video_file_count": 0,
            "large_video_file_count": 0,
            "large_video_files": [],
            "bad_video_file_count": 0,
            "bad_video_files": [],
            "video_details": [],
            "similar_video_analysis_enabled": self.similar_video_analysis,
            "similar_video_threshold": self.similar_video_threshold,
            "similar_video_duplicate_count": 0,
            "similar_video_groups": [],
            "similar_video_duplicates": [],
            "video_analysis_method": (
                VIDEO_FINGERPRINT_METHOD if self.similar_video_analysis else "disabled"
            ),
            "found_file_count": 0,
            "found_files": [],
            "warnings": [],
        }

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

    @staticmethod
    def _is_image_candidate(path: Path) -> bool:
        return path.suffix.casefold() in IMAGE_EXTENSIONS

    @staticmethod
    def _is_video_candidate(path: Path) -> bool:
        return path.suffix.casefold() in VIDEO_EXTENSIONS

    def _is_excluded_folder(self, path: Path) -> bool:
        try:
            relative = path.resolve().relative_to(self.target_dir)
        except ValueError:
            return True
        excluded_names = {".git", "__pycache__", "_cleaner_backup", ".GPTBridge_CleanerQuarantine"}
        return bool(relative.parts and relative.parts[0] in excluded_names)

    def _is_excluded(self, path: Path) -> bool:
        return self._is_excluded_folder(path.parent)

    def _create_executor(self) -> concurrent.futures.ThreadPoolExecutor | None:
        if not self.parallel_analysis:
            return None
        workers = min(4, max(1, os.cpu_count() or 1))
        if workers <= 1:
            return None
        return concurrent.futures.ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="file-sorter-cleanup-video",
        )

    @staticmethod
    def _load_image_helpers() -> dict[str, Any]:
        try:
            from PIL import Image, ImageOps
        except Exception as exc:
            return {"error": f"Pillow is not available: {exc}"}
        return {"Image": Image, "ImageOps": ImageOps}

    def _process_image_candidate(
        self,
        path: Path,
        report: dict[str, Any],
        image_helpers: dict[str, Any],
        *,
        folder_index: int,
        folder_total: int,
        folder_text: str,
        source_file_count: int,
    ) -> None:
        report["image_file_count"] += 1
        relative = _relative_path_text(path, self.target_dir)
        self._emit_progress(
            "image_analysis",
            f"Scanning image: {relative}",
            current_file=relative,
            current_folder=folder_text,
            folder_current=folder_index,
            folder_total=folder_total,
            source_file_count=source_file_count,
            found_file_count=len(self._found_files),
        )
        if image_helpers.get("error"):
            warning = str(image_helpers["error"])
            if warning not in report["warnings"]:
                report["warnings"].append(warning)
            return

        try:
            with image_helpers["Image"].open(path) as opened_image:
                image = image_helpers["ImageOps"].exif_transpose(opened_image)
                width, height = image.size
        except Exception as exc:
            report["warnings"].append(f"{relative}: {exc}")
            return

        if width <= 0 or height <= 0:
            return

        aspect_ratio = width / max(height, 1)
        is_classifiable = min(width, height) >= MIN_CLASSIFIABLE_IMAGE_EDGE
        is_landscape = is_classifiable and aspect_ratio >= LANDSCAPE_ASPECT_RATIO
        is_non_portrait = is_classifiable and aspect_ratio >= NON_PORTRAIT_ASPECT_RATIO
        categories = [
            category
            for category, matched in (
                (CATEGORY_LANDSCAPE_IMAGE, is_landscape),
                (CATEGORY_NON_PORTRAIT_IMAGE, is_non_portrait),
            )
            if matched
        ]
        if not categories:
            return

        if is_landscape:
            report["landscape_images"].append(relative)
            report["landscape_image_count"] += 1
        if is_non_portrait:
            report["non_portrait_images"].append(relative)
            report["non_portrait_image_count"] += 1

        self._add_found_file(
            relative,
            categories,
            size=self._safe_stat_size(path),
            metadata={
                "width": width,
                "height": height,
                "aspect_ratio": round(aspect_ratio, 4),
            },
        )

    def _process_video_issue_candidate(
        self,
        path: Path,
        report: dict[str, Any],
        *,
        folder_index: int,
        folder_total: int,
        folder_text: str,
        source_file_count: int,
    ) -> None:
        report["video_file_count"] += 1
        relative = _relative_path_text(path, self.target_dir)
        self._emit_progress(
            "video_analysis",
            f"Scanning video: {relative}",
            current_file=relative,
            current_folder=folder_text,
            folder_current=folder_index,
            folder_total=folder_total,
            source_file_count=source_file_count,
            found_file_count=len(self._found_files),
        )
        issue = self._video_direct_issue(path)
        if issue is None:
            return

        category = str(issue.get("category") or CATEGORY_BAD_VIDEO_FILE)
        size = (
            int(issue["size"])
            if isinstance(issue.get("size"), int)
            else self._safe_stat_size(path)
        )
        entry = {
            "path": relative,
            "size": size,
            "reason": str(issue.get("reason") or "unknown"),
        }
        if issue.get("error"):
            entry["error"] = str(issue["error"])

        if category == CATEGORY_LARGE_VIDEO_FILE:
            report["large_video_files"].append(entry)
            report["large_video_file_count"] += 1
        else:
            category = CATEGORY_BAD_VIDEO_FILE
            report["bad_video_files"].append(entry)
            report["bad_video_file_count"] += 1

        self._add_found_file(
            relative,
            [category],
            size=size,
            metadata={"video_issue": entry["reason"]},
        )

    def _collect_video_jobs(
        self,
        video_jobs: list[tuple[concurrent.futures.Future[dict[str, Any]], Path]],
        report: dict[str, Any],
    ) -> None:
        for future, path in video_jobs:
            relative = _relative_path_text(path, self.target_dir)
            try:
                detail = future.result()
            except Exception as exc:
                report["warnings"].append(f"{relative}: {exc}")
                continue
            if detail:
                self._record_video_detail(path, detail, report)

    def _record_video_detail(
        self,
        path: Path,
        detail: dict[str, Any],
        report: dict[str, Any],
    ) -> None:
        report["video_details"].append(
            {
                "path": _relative_path_text(path, self.target_dir),
                "size": self._safe_stat_size(path),
                **detail,
            }
        )

    def _append_similar_video_results(self, report: dict[str, Any]) -> None:
        candidates = [
            {
                "path": str(detail["path"]),
                "hashes": [
                    str(value)
                    for value in detail.get("perceptual_hashes", [])
                    if value
                ],
            }
            for detail in report.get("video_details", [])
            if isinstance(detail, dict) and detail.get("path")
        ]
        candidates.sort(key=lambda item: item["path"].casefold())

        used_duplicates: set[str] = set()
        groups: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []

        for base in candidates:
            base_path = base["path"]
            if base_path.casefold() in used_duplicates:
                continue
            matches: list[dict[str, Any]] = []
            for candidate in candidates:
                candidate_path = candidate["path"]
                if candidate_path == base_path:
                    continue
                if candidate_path.casefold() in used_duplicates:
                    continue
                similarity, distance = self._hash_sequence_similarity(
                    base["hashes"],
                    candidate["hashes"],
                )
                if similarity < self.similar_video_threshold:
                    continue
                match = {
                    "path": candidate_path,
                    "similar_to": base_path,
                    "video_similarity": similarity,
                    "perceptual_distance": distance,
                }
                matches.append(match)
            if not matches:
                continue
            groups.append({"keep": base_path, "matches": matches})
            for match in matches:
                used_duplicates.add(str(match["path"]).casefold())
                duplicates.append(match)
                self._add_found_file(
                    str(match["path"]),
                    [CATEGORY_SIMILAR_VIDEO_DUPLICATE],
                    size=self._safe_stat_size(self.target_dir / str(match["path"])),
                    metadata={
                        "similar_to": match["similar_to"],
                        "video_similarity": match["video_similarity"],
                        "perceptual_distance": match["perceptual_distance"],
                    },
                )

        report["similar_video_groups"] = groups
        report["similar_video_duplicates"] = duplicates
        report["similar_video_duplicate_count"] = len(duplicates)

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
            CleanupScanner._hex_hamming_distance(left, right)
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

    def _video_sample_count(self) -> int:
        speed = max(1, min(100, int(self.analysis_speed)))
        return max(3, min(9, round(10 - 6 * (speed / 100))))

    @staticmethod
    def _video_issue(
        category: str,
        reason: str,
        *,
        size: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        issue: dict[str, Any] = {"category": category, "reason": reason}
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
            return self._video_issue(CATEGORY_BAD_VIDEO_FILE, "stat_failed", error=str(exc))

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

    def _video_fingerprint(self, path: Path) -> dict[str, Any]:
        size = path.stat().st_size
        chunk_size = min(VIDEO_FINGERPRINT_CHUNK_SIZE, size)
        positions = self._video_sample_positions(
            size,
            chunk_size,
            self._video_sample_count(),
        )
        hashes: list[str] = []
        sampled_byte_count = 0
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
    video_cleanup: bool = True,
    similar_video_analysis: bool = True,
    similar_video_threshold: int | None = None,
    analysis_speed: int | None = None,
    parallel_analysis: bool = True,
    progress_event_callback: ProgressEventCallback | None = None,
) -> dict[str, Any]:
    scanner = CleanupScanner(
        target_dir,
        image_cleanup=image_cleanup,
        video_cleanup=video_cleanup,
        similar_video_analysis=similar_video_analysis,
        similar_video_threshold=similar_video_threshold,
        analysis_speed=analysis_speed,
        parallel_analysis=parallel_analysis,
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
    parser.add_argument("--video-cleanup", action="store_true")
    parser.add_argument("--similar-video-analysis", action="store_true")
    parser.add_argument(
        "--similar-video-threshold",
        type=int,
        default=DEFAULT_SIMILAR_VIDEO_THRESHOLD,
    )
    parser.add_argument("--analysis-speed", type=int, default=DEFAULT_ANALYSIS_SPEED)
    parser.add_argument("--no-parallel-analysis", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--progress-jsonl", action="store_true")
    args = parser.parse_args(argv)

    selected = bool(args.image_cleanup or args.video_cleanup or args.similar_video_analysis)
    report = run_cleanup_scan(
        args.target_dir,
        image_cleanup=bool(args.image_cleanup),
        video_cleanup=bool(args.video_cleanup or args.similar_video_analysis or not selected),
        similar_video_analysis=bool(args.similar_video_analysis or not selected),
        similar_video_threshold=args.similar_video_threshold,
        analysis_speed=args.analysis_speed,
        parallel_analysis=not bool(args.no_parallel_analysis),
        progress_event_callback=print_progress_event if args.progress_jsonl else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.json else None))
    return 0 if report.get("ok") is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
