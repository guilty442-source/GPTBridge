"""Video fingerprinting mixin for CleanupScanner."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .cleanup_constants import (
    CATEGORY_BAD_VIDEO_FILE,
    CATEGORY_LARGE_VIDEO_FILE,
    VIDEO_FINGERPRINT_CHUNK_SIZE,
    VIDEO_FINGERPRINT_FALLBACK_METHOD,
    VIDEO_FINGERPRINT_MAX_BYTES,
    VIDEO_FINGERPRINT_METHOD,
    VIDEO_FINGERPRINT_MIN_BYTES,
)
from .cleanup_errors import CleanupError


class VideoFingerprintMixin:
    """Video fingerprinting and issue-detection methods for CleanupScanner."""

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
        if not self._is_contained_regular_file(path):
            return self._video_issue(
                CATEGORY_BAD_VIDEO_FILE,
                "escaped_scan_target",
            )
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
        if not self._is_contained_regular_file(path):
            raise CleanupError(f"File escaped the scan target: {path}")
        stat = path.stat()
        size = stat.st_size
        cached = self._fingerprint_cache.get(
            path,
            size=size,
            mtime_ns=stat.st_mtime_ns,
        )
        if cached is not None:
            return {**cached, "cache_hit": True}

        frame_fingerprint = self._ffmpeg_frame_fingerprint(path, size=size)
        if frame_fingerprint is not None:
            self._fingerprint_cache.put(
                path,
                size=size,
                mtime_ns=stat.st_mtime_ns,
                payload=frame_fingerprint,
            )
            return frame_fingerprint

        fallback = self._sampled_file_fingerprint(path, size=size)
        self._fingerprint_cache.put(
            path,
            size=size,
            mtime_ns=stat.st_mtime_ns,
            payload=fallback,
        )
        return fallback

    def _ffmpeg_frame_fingerprint(
        self,
        path: Path,
        *,
        size: int,
    ) -> dict[str, Any] | None:
        if not self._is_contained_regular_file(path):
            return None
        try:
            import imageio_ffmpeg

            ffmpeg_executable = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None

        sample_count = self._video_sample_count()
        frame_width = 9
        frame_height = 8
        frame_size = frame_width * frame_height
        command = [
            str(ffmpeg_executable),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-an",
            "-vf",
            f"fps=1/15,scale={frame_width}:{frame_height}:flags=area,format=gray",
            "-frames:v",
            str(sample_count),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=90,
                **({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0 or len(completed.stdout) < frame_size:
            return None

        hashes = [
            self._frame_difference_hash(completed.stdout[offset : offset + frame_size])
            for offset in range(0, len(completed.stdout) - frame_size + 1, frame_size)
        ]
        hashes = [value for value in hashes if value]
        if not hashes:
            return None
        return {
            "method": VIDEO_FINGERPRINT_METHOD,
            "byte_size": size,
            "sampled_byte_count": len(completed.stdout),
            "perceptual_hashes": hashes,
            "sample_count": len(hashes),
            "cache_hit": False,
        }

    @staticmethod
    def _frame_difference_hash(frame: bytes) -> str | None:
        if len(frame) != 72:
            return None
        value = 0
        for row in range(8):
            offset = row * 9
            for column in range(8):
                value = (value << 1) | int(
                    frame[offset + column] >= frame[offset + column + 1]
                )
        return f"{value:016x}"

    def _sampled_file_fingerprint(
        self,
        path: Path,
        *,
        size: int,
    ) -> dict[str, Any]:
        if not self._is_contained_regular_file(path):
            raise CleanupError(f"File escaped the scan target: {path}")
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
            "method": VIDEO_FINGERPRINT_FALLBACK_METHOD,
            "byte_size": size,
            "sampled_byte_count": sampled_byte_count,
            "perceptual_hashes": hashes,
            "sample_count": len(hashes),
            "cache_hit": False,
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
