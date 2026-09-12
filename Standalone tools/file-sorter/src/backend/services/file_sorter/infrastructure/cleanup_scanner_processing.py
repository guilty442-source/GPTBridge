"""Media processing mixin for CleanupScanner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .cleanup_constants import (
    CATEGORY_BAD_VIDEO_FILE,
    CATEGORY_LARGE_VIDEO_FILE,
    CATEGORY_NON_PERSON_IMAGE,
)
from .cleanup_errors import CleanupError
from .cleanup_utils import _relative_path_text


class MediaProcessingMixin:
    """Image and video candidate processing methods for CleanupScanner."""

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
            if not self._is_contained_regular_file(path):
                raise CleanupError(f"File escaped the scan target: {path}")
            result = image_helpers["model"].predict(path)
        except Exception as exc:
            report["warnings"].append(f"{relative}: {exc}")
            report["indeterminate_image_count"] += 1
            return

        payload = result.to_dict()
        classification = str(payload.get("classification") or "indeterminate")
        if classification == "person":
            report["person_detected_image_count"] += 1
            return
        if classification != "non_person_candidate":
            report["indeterminate_image_count"] += 1
            return

        report["non_person_images"].append(relative)
        report["non_person_image_count"] += 1

        self._add_found_file(
            relative,
            [CATEGORY_NON_PERSON_IMAGE],
            size=self._safe_stat_size(path),
            metadata={
                "width": int(payload.get("width") or 0),
                "height": int(payload.get("height") or 0),
                "visual_recognition_confidence": float(payload.get("confidence") or 0.0),
                "visual_recognition_model": str(payload.get("model") or ""),
                "visual_recognition_model_version": str(payload.get("model_version") or ""),
                "visual_recognition_reason": str(payload.get("reason") or ""),
                "visual_recognition_tags": list(payload.get("tags") or []),
                "visual_recognition_summary": str(payload.get("summary") or ""),
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
        video_jobs: list[tuple[Any, Path]],
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
        if detail.get("cache_hit"):
            report["video_fingerprint_cache_hit_count"] += 1
        report["video_details"].append(
            {
                "path": _relative_path_text(path, self.target_dir),
                "size": self._safe_stat_size(path),
                **detail,
            }
        )
