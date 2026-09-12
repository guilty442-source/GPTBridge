"""Constants and type aliases for cleanup scanning."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


PROGRESS_JSON_PREFIX = "FILE_SORTER_CLEANUP_PROGRESS_JSON="
VIDEO_FINGERPRINT_METHOD = "ffmpeg-frame-dhash-v1"
VIDEO_FINGERPRINT_FALLBACK_METHOD = "file-sampled-video-fingerprint-v1"
VIDEO_FINGERPRINT_CACHE_SCHEMA = 1
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
CATEGORY_SIMILAR_IMAGE_DUPLICATE = "similar_image_duplicate"
CATEGORY_NON_PERSON_IMAGE = "non_person_image_candidate"
MIN_CLASSIFIABLE_IMAGE_EDGE = 256
DEFAULT_SIMILAR_VIDEO_THRESHOLD = 96
DEFAULT_ANALYSIS_SPEED = 50

ProgressEventCallback = Callable[[dict[str, Any]], None]
TOOL_ROOT = Path(__file__).resolve().parents[5]
