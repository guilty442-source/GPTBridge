from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


MP4_BOX_HEADER_SIZE = 8
MP4_EXTENDED_BOX_HEADER_SIZE = 16
MP4_SCAN_CHUNK_SIZE = 1024 * 1024


DEFAULT_CONDITIONS: dict[str, Any] = {
    "media_types": ["photo", "video"],
    "date_since": "",
    "date_until": "",
    "include_keywords": [],
    "exclude_keywords": [],
    "min_likes": 0,
    "min_views": 0,
    "max_items_per_account": 20,
    "skip_downloaded": True,
}


def _unique_strings(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value).strip()
        marker = normalized.casefold()
        if not normalized or marker in seen:
            continue
        seen.add(marker)
        output.append(normalized)
    return output


def split_keywords(value: Any) -> list[str]:
    if isinstance(value, list):
        return _unique_strings(value)
    return _unique_strings(re.split(r"[\n,，]+", str(value or "")))


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def normalize_conditions(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    requested_types = payload.get("media_types", DEFAULT_CONDITIONS["media_types"])
    if not isinstance(requested_types, list):
        requested_types = DEFAULT_CONDITIONS["media_types"]
    media_types = [
        media_type
        for media_type in _unique_strings(requested_types)
        if media_type in {"photo", "video"}
    ]
    if not media_types:
        media_types = list(DEFAULT_CONDITIONS["media_types"])

    return {
        "media_types": media_types,
        "date_since": str(payload.get("date_since", "")).strip(),
        "date_until": str(payload.get("date_until", "")).strip(),
        "include_keywords": split_keywords(payload.get("include_keywords", [])),
        "exclude_keywords": split_keywords(payload.get("exclude_keywords", [])),
        "min_likes": _bounded_int(payload.get("min_likes"), 0, 0, 2_000_000_000),
        "min_views": _bounded_int(payload.get("min_views"), 0, 0, 2_000_000_000),
        "max_items_per_account": _bounded_int(
            payload.get("max_items_per_account"),
            int(DEFAULT_CONDITIONS["max_items_per_account"]),
            1,
            200,
        ),
        "skip_downloaded": payload.get("skip_downloaded", True) is not False,
    }


def parse_metric(value: Any) -> int:
    text = str(value or "").strip().lower().replace(",", "")
    if not text:
        return 0
    match = re.search(r"(\d+(?:\.\d+)?)\s*([kmb萬億]?)", text)
    if not match:
        return 0
    number = float(match.group(1))
    multiplier = {
        "": 1,
        "k": 1_000,
        "m": 1_000_000,
        "b": 1_000_000_000,
        "萬": 10_000,
        "億": 100_000_000,
    }.get(match.group(2), 1)
    return int(number * multiplier)


def _parse_date(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{text}T00:00:00+00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def post_matches_conditions(post: dict[str, Any], conditions: dict[str, Any]) -> tuple[bool, str]:
    normalized = normalize_conditions(conditions)
    text = str(post.get("text", "")).casefold()
    include_keywords = [item.casefold() for item in normalized["include_keywords"]]
    exclude_keywords = [item.casefold() for item in normalized["exclude_keywords"]]

    if include_keywords and not any(keyword in text for keyword in include_keywords):
        return False, "未包含指定關鍵字"
    if exclude_keywords and any(keyword in text for keyword in exclude_keywords):
        return False, "包含排除關鍵字"

    published_at = _parse_date(post.get("published_at"))
    date_since = _parse_date(normalized["date_since"])
    date_until = _parse_date(normalized["date_until"])
    if date_until is not None and len(normalized["date_until"]) == 10:
        date_until = date_until + timedelta(days=1) - timedelta(microseconds=1)
    if published_at is not None and date_since is not None and published_at < date_since:
        return False, "早於起始日期"
    if published_at is not None and date_until is not None and published_at > date_until:
        return False, "晚於結束日期"

    if parse_metric(post.get("likes")) < normalized["min_likes"]:
        return False, "按讚數不足"
    if parse_metric(post.get("views")) < normalized["min_views"]:
        return False, "觀看數不足"
    return True, ""


def media_matches_conditions(media: dict[str, Any], conditions: dict[str, Any]) -> bool:
    return str(media.get("media_type", "")) in normalize_conditions(conditions)["media_types"]


def is_allowed_media_url(url: str, allowed_hosts: Iterable[str]) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    hostname = parsed.hostname.casefold()
    return any(
        hostname == host.casefold() or hostname.endswith(f".{host.casefold()}")
        for host in allowed_hosts
    )


def safe_filename(value: str, fallback: str = "media") -> str:
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "").strip())
    normalized = re.sub(r"\s+", "_", normalized).strip(" ._")
    normalized = re.sub(r"_+", "_", normalized)
    return (normalized or fallback)[:120]


def build_download_filename(
    handle: str,
    extension: str,
    timestamp: datetime | None = None,
) -> str:
    captured_at = timestamp or datetime.now().astimezone()
    date_time = captured_at.strftime("%Y%m%d_%H%M%S_%f")
    return f"{safe_filename(handle, 'account')}_{date_time}{extension}"


def _read_mp4_box_header(content: bytes, offset: int) -> tuple[int, bytes, int] | None:
    if offset + MP4_BOX_HEADER_SIZE > len(content):
        return None
    size = int.from_bytes(content[offset : offset + 4], "big")
    box_type = content[offset + 4 : offset + 8]
    header_size = MP4_BOX_HEADER_SIZE
    if size == 1:
        if offset + MP4_EXTENDED_BOX_HEADER_SIZE > len(content):
            return None
        size = int.from_bytes(content[offset + 8 : offset + 16], "big")
        header_size = MP4_EXTENDED_BOX_HEADER_SIZE
    elif size == 0:
        size = len(content) - offset
    return size, box_type, header_size


def _is_complete_mp4_payload(content: bytes) -> bool:
    offset = 0
    found_boxes: set[bytes] = set()
    has_video_track = False
    while offset < len(content):
        header = _read_mp4_box_header(content, offset)
        if header is None:
            return False
        size, box_type, header_size = header
        end = offset + size
        if size < header_size or end > len(content):
            return False
        found_boxes.add(box_type)
        if box_type == b"moov" and b"vide" in content[offset + header_size : end]:
            has_video_track = True
        offset = end
    return (
        offset == len(content)
        and {b"ftyp", b"moov", b"mdat"}.issubset(found_boxes)
        and has_video_track
    )


def _stream_contains_marker(stream: Any, start: int, size: int, marker: bytes) -> bool:
    stream.seek(start)
    remaining = size
    carry = b""
    while remaining > 0:
        chunk = stream.read(min(MP4_SCAN_CHUNK_SIZE, remaining))
        if not chunk:
            return False
        searchable = carry + chunk
        if marker in searchable:
            return True
        carry = searchable[-(len(marker) - 1) :]
        remaining -= len(chunk)
    return False


def _is_complete_mp4_file(file_path: Path) -> bool:
    file_size = file_path.stat().st_size
    offset = 0
    found_boxes: set[bytes] = set()
    has_video_track = False
    with file_path.open("rb") as stream:
        while offset < file_size:
            stream.seek(offset)
            base_header = stream.read(MP4_BOX_HEADER_SIZE)
            if len(base_header) != MP4_BOX_HEADER_SIZE:
                return False
            size = int.from_bytes(base_header[:4], "big")
            box_type = base_header[4:8]
            header_size = MP4_BOX_HEADER_SIZE
            if size == 1:
                extended_size = stream.read(8)
                if len(extended_size) != 8:
                    return False
                size = int.from_bytes(extended_size, "big")
                header_size = MP4_EXTENDED_BOX_HEADER_SIZE
            elif size == 0:
                size = file_size - offset
            end = offset + size
            if size < header_size or end > file_size:
                return False
            found_boxes.add(box_type)
            if box_type == b"moov":
                has_video_track = _stream_contains_marker(
                    stream,
                    offset + header_size,
                    size - header_size,
                    b"vide",
                )
            offset = end
    return (
        offset == file_size
        and {b"ftyp", b"moov", b"mdat"}.issubset(found_boxes)
        and has_video_track
    )


def _is_supported_image_payload(content: bytes) -> bool:
    if len(content) < 4:
        return False
    if content.startswith(b"\xff\xd8\xff"):
        return True
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if content.startswith((b"GIF87a", b"GIF89a")):
        return True
    if (
        len(content) >= 12
        and content.startswith(b"RIFF")
        and content[8:12] == b"WEBP"
    ):
        return True
    return False


def is_valid_media_payload(
    content: bytes,
    media_type: str,
    content_type: str = "",
) -> bool:
    if not content:
        return False
    normalized_type = content_type.split(";", 1)[0].strip().casefold()
    if normalized_type.startswith(("text/", "application/json", "application/xml")):
        return False
    if media_type != "video":
        return _is_supported_image_payload(content)
    if content.startswith(b"#EXTM3U"):
        return False
    if content.startswith(b"\x1aE\xdf\xa3"):
        return True
    return _is_complete_mp4_payload(content)


def is_valid_media_file(file_path: Path, media_type: str) -> bool:
    try:
        if not file_path.is_file() or file_path.stat().st_size <= 0:
            return False
        if media_type != "video":
            with file_path.open("rb") as stream:
                return _is_supported_image_payload(stream.read(16))

        with file_path.open("rb") as stream:
            header = stream.read(4)
            if header.startswith(b"\x1aE\xdf\xa3"):
                return True
        return _is_complete_mp4_file(file_path)
    except OSError:
        return False


def extension_for_media(url: str, media_type: str, content_type: str = "") -> str:
    normalized_type = content_type.casefold()
    if "webm" in normalized_type:
        return ".webm"
    if "video" in normalized_type:
        return ".mp4"
    if "png" in normalized_type:
        return ".png"
    if "webp" in normalized_type:
        return ".webp"
    if "gif" in normalized_type:
        return ".gif"
    suffix = Path(urlparse(url).path).suffix.lower()
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".m4v", ".webm"}
    if suffix in allowed:
        return suffix
    if media_type == "video":
        return ".mp4"
    return ".jpg"
