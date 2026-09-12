"""Safe media review, exact-duplicate detection, and recycle-bin operations.

This module re-exports all public names from the refactored submodules so that
existing imports (``from .cleanup import ...``) continue to work unchanged.
"""

from __future__ import annotations

from .cleanup_constants import (
    CATEGORY_BAD_VIDEO_FILE,
    CATEGORY_LANDSCAPE_IMAGE,
    CATEGORY_LARGE_VIDEO_FILE,
    CATEGORY_NON_PERSON_IMAGE,
    CATEGORY_NON_PORTRAIT_IMAGE,
    CATEGORY_SIMILAR_IMAGE_DUPLICATE,
    CATEGORY_SIMILAR_VIDEO_DUPLICATE,
    DEFAULT_ANALYSIS_SPEED,
    DEFAULT_SIMILAR_VIDEO_THRESHOLD,
    IMAGE_EXTENSIONS,
    MIN_CLASSIFIABLE_IMAGE_EDGE,
    PROGRESS_JSON_PREFIX,
    ProgressEventCallback,
    TOOL_ROOT,
    VIDEO_EXTENSIONS,
    VIDEO_FINGERPRINT_CACHE_SCHEMA,
    VIDEO_FINGERPRINT_CHUNK_SIZE,
    VIDEO_FINGERPRINT_FALLBACK_METHOD,
    VIDEO_FINGERPRINT_MAX_BYTES,
    VIDEO_FINGERPRINT_METHOD,
    VIDEO_FINGERPRINT_MIN_BYTES,
)
from .cleanup_duplicates import (
    find_exact_duplicate_candidates,
    print_progress_event,
    recycle_exact_duplicate_candidates,
)
from .cleanup_errors import CleanupError
from .cleanup_scanner import CleanupScanner, main, run_cleanup_scan
from .video_fingerprint_cache import VideoFingerprintCache

__all__ = [
    "CATEGORY_BAD_VIDEO_FILE",
    "CATEGORY_LANDSCAPE_IMAGE",
    "CATEGORY_LARGE_VIDEO_FILE",
    "CATEGORY_NON_PERSON_IMAGE",
    "CATEGORY_NON_PORTRAIT_IMAGE",
    "CATEGORY_SIMILAR_IMAGE_DUPLICATE",
    "CATEGORY_SIMILAR_VIDEO_DUPLICATE",
    "CleanupError",
    "CleanupScanner",
    "DEFAULT_ANALYSIS_SPEED",
    "DEFAULT_SIMILAR_VIDEO_THRESHOLD",
    "IMAGE_EXTENSIONS",
    "MIN_CLASSIFIABLE_IMAGE_EDGE",
    "PROGRESS_JSON_PREFIX",
    "ProgressEventCallback",
    "TOOL_ROOT",
    "VIDEO_EXTENSIONS",
    "VIDEO_FINGERPRINT_CACHE_SCHEMA",
    "VIDEO_FINGERPRINT_CHUNK_SIZE",
    "VIDEO_FINGERPRINT_FALLBACK_METHOD",
    "VIDEO_FINGERPRINT_MAX_BYTES",
    "VIDEO_FINGERPRINT_METHOD",
    "VIDEO_FINGERPRINT_MIN_BYTES",
    "VideoFingerprintCache",
    "find_exact_duplicate_candidates",
    "main",
    "print_progress_event",
    "recycle_exact_duplicate_candidates",
    "run_cleanup_scan",
]
