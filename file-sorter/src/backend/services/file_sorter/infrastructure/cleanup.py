"""Safe media review, exact-duplicate detection, and recycle-bin operations."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence


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


class CleanupError(Exception):
    """Raised when cleanup scanning cannot be completed."""


def _default_state_root() -> Path:
    explicit = str(os.environ.get("FILE_SORTER_STATE_ROOT") or "").strip()
    candidate = (
        Path(explicit).expanduser().resolve()
        if explicit
        else TOOL_ROOT / "data" / "business"
    )
    if not candidate.is_relative_to(TOOL_ROOT):
        raise PermissionError("FILE_SORTER_DATABASE_SCOPE_DENIED")
    return candidate


class VideoFingerprintCache:
    """Small SQLite cache keyed by a privacy-preserving canonical-path digest."""

    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path or (_default_state_root() / "video-fingerprints.sqlite3")
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        if not self._ready:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS video_fingerprints (
                    path_digest TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.commit()
            self._ready = True
        return connection

    @staticmethod
    def _path_digest(path: Path) -> str:
        canonical = os.path.normcase(str(path.expanduser().resolve(strict=False)))
        return hashlib.sha256(canonical.encode("utf-8", errors="surrogatepass")).hexdigest()

    def get(self, path: Path, *, size: int, mtime_ns: int) -> dict[str, Any] | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM video_fingerprints
                    WHERE path_digest = ? AND size = ? AND mtime_ns = ?
                      AND schema_version = ?
                    """,
                    (
                        self._path_digest(path),
                        int(size),
                        int(mtime_ns),
                        VIDEO_FINGERPRINT_CACHE_SCHEMA,
                    ),
                ).fetchone()
            if row is None:
                return None
            payload = json.loads(str(row[0]))
            return payload if isinstance(payload, dict) else None
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            return None

    def put(
        self,
        path: Path,
        *,
        size: int,
        mtime_ns: int,
        payload: dict[str, Any],
    ) -> None:
        try:
            payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO video_fingerprints (
                        path_digest, size, mtime_ns, schema_version, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(path_digest) DO UPDATE SET
                        size = excluded.size,
                        mtime_ns = excluded.mtime_ns,
                        schema_version = excluded.schema_version,
                        payload_json = excluded.payload_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        self._path_digest(path),
                        int(size),
                        int(mtime_ns),
                        VIDEO_FINGERPRINT_CACHE_SCHEMA,
                        payload_json,
                    ),
                )
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return


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
    except (OSError, ValueError):
        return Path(path.name)
    return Path(*[part for part in relative.parts if part not in {"", ".", ".."}])


def _relative_path_text(path: Path, root: Path) -> str:
    return str(_safe_relative_path(path, root))


def _is_link_or_reparse(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    attributes = int(getattr(path_stat, "st_file_attributes", 0) or 0)
    return path.is_symlink() or bool(attributes & 0x400)


def _resolve_target_dir(target_dir: str | Path) -> Path:
    requested = Path(target_dir).expanduser()
    if _is_link_or_reparse(requested):
        raise CleanupError("Target folder cannot be a link or reparse point.")
    try:
        target = requested.resolve(strict=True)
    except OSError as error:
        raise CleanupError(
            f"Target folder cannot be resolved: {requested}: {error}"
        ) from error
    if _is_link_or_reparse(target):
        raise CleanupError("Target folder cannot be a link or reparse point.")
    if not target.is_dir():
        raise CleanupError(f"Target folder does not exist: {target}")
    return target


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_contained_regular_files(target: Path) -> list[Path]:
    files: list[Path] = []
    excluded_names = {
        ".git",
        "__pycache__",
        "_cleaner_backup",
        ".gptbridge_cleanerquarantine",
    }
    for current_root, directory_names, file_names in os.walk(
        target,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        directory_names[:] = [
            name
            for name in directory_names
            if name.casefold() not in excluded_names
            and not _is_link_or_reparse(current / name)
        ]
        for name in file_names:
            path = current / name
            if _is_link_or_reparse(path):
                continue
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(target)
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                files.append(resolved)
    return sorted(
        files,
        key=lambda path: str(path.relative_to(target)).casefold(),
    )


def find_exact_duplicate_candidates(
    target_dir: str | Path,
    *,
    quiet_seconds: float = 2.0,
) -> list[dict[str, Any]]:
    """Return exact duplicate candidates while preserving one canonical copy.

    Files must be non-empty, unchanged for the quiet period, and byte-for-byte
    identical by SHA-256. Hard links to the same file are not considered
    duplicates because recycling one would not reclaim storage.
    """

    target = _resolve_target_dir(target_dir)
    now_ns = time.time_ns()
    by_size: dict[int, list[tuple[Path, int]]] = {}
    for path in _iter_contained_regular_files(target):
        if path.name.casefold().endswith(
            (".crdownload", ".download", ".partial", ".part", ".tmp", ".temp")
        ):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size <= 0:
            continue
        if now_ns - stat.st_mtime_ns < max(0.0, quiet_seconds) * 1_000_000_000:
            continue
        by_size.setdefault(int(stat.st_size), []).append((path, int(stat.st_mtime_ns)))

    candidates: list[dict[str, Any]] = []
    for size, sized_files in by_size.items():
        if len(sized_files) < 2:
            continue
        by_digest: dict[str, list[tuple[Path, int]]] = {}
        for path, mtime_ns in sized_files:
            try:
                digest = _sha256_file(path)
            except OSError:
                continue
            by_digest.setdefault(digest, []).append((path, mtime_ns))
        for digest, matches in by_digest.items():
            if len(matches) < 2:
                continue
            ordered = sorted(
                matches,
                key=lambda item: (
                    item[1],
                    str(item[0].relative_to(target)).casefold(),
                ),
            )
            keep, _keep_mtime_ns = ordered[0]
            for duplicate, duplicate_mtime_ns in ordered[1:]:
                try:
                    if os.path.samefile(keep, duplicate):
                        continue
                except OSError:
                    continue
                candidates.append(
                    {
                        "path": str(duplicate),
                        "relative_path": str(duplicate.relative_to(target)),
                        "keep_path": str(keep),
                        "keep_relative_path": str(keep.relative_to(target)),
                        "size": size,
                        "mtime_ns": duplicate_mtime_ns,
                        "sha256": digest,
                    }
                )
    return candidates


def _send_to_windows_recycle_bin(path: Path) -> None:
    if os.name != "nt":
        raise CleanupError("Automatic duplicate recycling is supported only on Windows.")

    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 0x0003  # FO_DELETE
    operation.pFrom = str(path) + "\0\0"
    operation.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400
    result = int(ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation)))
    if result != 0 or operation.fAnyOperationsAborted:
        raise CleanupError(f"Windows Recycle Bin rejected the file (code {result}).")


def recycle_exact_duplicate_candidates(
    target_dir: str | Path,
    candidates: Sequence[dict[str, Any]],
    *,
    recycler: Callable[[Path], None] = _send_to_windows_recycle_bin,
) -> dict[str, Any]:
    """Revalidate exact duplicates and send only proven extras to Recycle Bin."""

    target = _resolve_target_dir(target_dir)
    recycled: list[dict[str, Any]] = []
    errors: list[str] = []
    for candidate in candidates:
        duplicate = Path(str(candidate.get("path") or ""))
        keep = Path(str(candidate.get("keep_path") or ""))
        expected_hash = str(candidate.get("sha256") or "")
        expected_size = int(candidate.get("size") or -1)
        expected_mtime_ns = int(candidate.get("mtime_ns") or -1)
        try:
            for path in (duplicate, keep):
                resolved = path.resolve(strict=True)
                resolved.relative_to(target)
                if _is_link_or_reparse(path) or not resolved.is_file():
                    raise CleanupError("Duplicate candidate is no longer a regular file.")
            duplicate_stat = duplicate.stat()
            if (
                duplicate_stat.st_size != expected_size
                or duplicate_stat.st_mtime_ns != expected_mtime_ns
            ):
                raise CleanupError("Duplicate candidate changed after observation.")
            if os.path.samefile(duplicate, keep):
                raise CleanupError("Duplicate candidate is a hard link to the retained file.")
            if not expected_hash or _sha256_file(keep) != expected_hash:
                raise CleanupError("Retained file changed after observation.")
            if _sha256_file(duplicate) != expected_hash:
                raise CleanupError("Duplicate content changed after observation.")
            recycler(duplicate)
            if duplicate.exists():
                raise CleanupError("Recycle Bin operation did not remove the source path.")
            recycled.append(
                {
                    "path": str(duplicate),
                    "keep_path": str(keep),
                    "size": expected_size,
                    "sha256": expected_hash,
                }
            )
        except (CleanupError, OSError, TypeError, ValueError) as error:
            errors.append(f"{duplicate.name or 'unknown'}: {error}")
    return {
        "ok": not errors,
        "type": "file-sorter-duplicate-recycle-result",
        "target_dir": str(target),
        "recycled_count": len(recycled),
        "recycled": recycled,
        "errors": errors,
    }


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
        if detail.get("cache_hit"):
            report["video_fingerprint_cache_hit_count"] += 1
        report["video_details"].append(
            {
                "path": _relative_path_text(path, self.target_dir),
                "size": self._safe_stat_size(path),
                **detail,
            }
        )

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
