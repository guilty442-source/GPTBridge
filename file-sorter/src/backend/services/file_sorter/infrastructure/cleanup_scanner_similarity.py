"""Video similarity comparison mixin for CleanupScanner."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .cleanup_constants import (
    CATEGORY_SIMILAR_IMAGE_DUPLICATE,
    CATEGORY_SIMILAR_VIDEO_DUPLICATE,
)
from .cleanup_utils import _relative_path_text


class VideoSimilarityMixin:
    """Perceptual-hash and model-based similarity methods for CleanupScanner."""

    def _append_model_similarity_results(
        self,
        paths: Sequence[Path],
        report: dict[str, Any],
        helpers: dict[str, Any],
        *,
        media_type: str,
    ) -> None:
        if helpers.get("error"):
            warning = str(helpers["error"])
            if warning not in report["warnings"]:
                report["warnings"].append(warning)
            return
        model = helpers.get("model")
        ordered = sorted(paths, key=lambda item: str(item).casefold())
        used: set[str] = set()
        groups: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        comparisons = 0
        threshold = self.similar_video_threshold
        for index, base in enumerate(ordered):
            base_relative = _relative_path_text(base, self.target_dir)
            if base_relative.casefold() in used:
                continue
            matches: list[dict[str, Any]] = []
            for candidate in ordered[index + 1 :]:
                candidate_relative = _relative_path_text(candidate, self.target_dir)
                if candidate_relative.casefold() in used:
                    continue
                comparisons += 1
                try:
                    result = model.compare(
                        base,
                        candidate,
                        media_type=media_type,
                        analysis_speed=self.analysis_speed,
                    )
                except Exception as exc:
                    report["warnings"].append(
                        f"{base_relative} / {candidate_relative}: {exc}"
                    )
                    continue
                payload = result.to_dict()
                similarity = int(payload.get("similarity") or 0)
                if similarity < threshold or not payload.get("same_content"):
                    continue
                match = {
                    "path": candidate_relative,
                    "similar_to": base_relative,
                    f"{media_type}_similarity": similarity,
                    "visual_similarity_reason": str(payload.get("reason") or ""),
                    "visual_similarity_model": str(payload.get("model") or ""),
                }
                matches.append(match)
            if matches:
                groups.append({"keep": base_relative, "matches": matches})
                category = (
                    CATEGORY_SIMILAR_IMAGE_DUPLICATE
                    if media_type == "image"
                    else CATEGORY_SIMILAR_VIDEO_DUPLICATE
                )
                for match in matches:
                    used.add(str(match["path"]).casefold())
                    duplicates.append(match)
                    self._add_found_file(
                        str(match["path"]),
                        [category],
                        size=self._safe_stat_size(self.target_dir / str(match["path"])),
                        metadata=dict(match),
                    )
        prefix = "similar_image" if media_type == "image" else "similar_video"
        report[f"{prefix}_groups"] = groups
        report[f"{prefix}_duplicates"] = duplicates
        report[f"{prefix}_duplicate_count"] = len(duplicates)
        report[f"{prefix}_comparison_count"] = comparisons

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
        candidate_neighbors = self._video_candidate_neighbors(candidates)
        report["similar_video_candidate_pair_count"] = (
            sum(len(neighbors) for neighbors in candidate_neighbors) // 2
        )

        used_duplicates: set[str] = set()
        groups: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        comparison_count = 0

        for base_index, base in enumerate(candidates):
            base_path = base["path"]
            if base_path.casefold() in used_duplicates:
                continue
            matches: list[dict[str, Any]] = []
            for candidate_index in sorted(candidate_neighbors[base_index]):
                if candidate_index <= base_index:
                    continue
                candidate = candidates[candidate_index]
                candidate_path = candidate["path"]
                if candidate_path.casefold() in used_duplicates:
                    continue
                comparison_count += 1
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
        report["similar_video_comparison_count"] = comparison_count

    def _video_candidate_neighbors(
        self,
        candidates: Sequence[dict[str, Any]],
    ) -> list[set[int]]:
        """Return likely pairs using LSH bands for large, high-threshold scans."""
        candidate_count = len(candidates)
        neighbors = [set() for _ in range(candidate_count)]
        if candidate_count <= 64 or self.similar_video_threshold < 90:
            for left in range(candidate_count):
                for right in range(left + 1, candidate_count):
                    neighbors[left].add(right)
                    neighbors[right].add(left)
            return neighbors

        buckets: dict[tuple[int, int], set[int]] = {}
        for candidate_index, candidate in enumerate(candidates):
            for raw_hash in candidate.get("hashes", []):
                try:
                    hash_value = int(str(raw_hash), 16)
                except ValueError:
                    continue
                for band in range(4):
                    key = (band, (hash_value >> (band * 16)) & 0xFFFF)
                    buckets.setdefault(key, set()).add(candidate_index)

        for bucket in buckets.values():
            indexes = sorted(bucket)
            for position, left in enumerate(indexes):
                for right in indexes[position + 1 :]:
                    neighbors[left].add(right)
                    neighbors[right].add(left)
        return neighbors

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
            VideoSimilarityMixin._hex_hamming_distance(left, right)
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
