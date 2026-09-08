"""vaultly consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "global-cleaner" / "src"),
    str(_ROOT / "ai-assistant" / "src"),
    str(_ROOT / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "vaultly" / "src" / "backend" / "services"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p


# -- CONSOLIDATED TEST SUITE --

########################################################################
# source: restored_vaultly.py
########################################################################
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


TOOL_ROOT = Path(__file__).resolve().parents[1]
SERVICES_ROOT = TOOL_ROOT / "src" / "backend" / "services"
if str(SERVICES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICES_ROOT))

from vaultly.domain.rules import (  # noqa: E402
    build_download_filename,
    extension_for_media,
    is_allowed_media_url,
    is_valid_media_payload,
    media_matches_conditions,
    normalize_conditions,
    parse_metric,
    post_matches_conditions,
    safe_filename,
    split_keywords,
)
from vaultly.infrastructure.repository import VaultlyRepository  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 0),
        ("", 0),
        ("invalid", 0),
        ("1,234", 1_234),
        ("1.5k", 1_500),
        ("2M", 2_000_000),
        ("3.2b", 3_200_000_000),
        ("4萬", 40_000),
        ("1.2億", 120_000_000),
        ("觀看 987 次", 987),
    ],
)
def test_parse_metric_supports_platform_notation(raw: object, expected: int) -> None:
    assert parse_metric(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("alpha,beta，gamma\nALPHA", ["alpha", "beta", "gamma"]),
        (["One", " one ", "Two", ""], ["One", "Two"]),
        (None, []),
        (123, ["123"]),
    ],
)
def test_split_keywords_normalizes_and_deduplicates(
    raw: object,
    expected: list[str],
) -> None:
    assert split_keywords(raw) == expected


def test_normalize_conditions_defaults_and_bounds_values() -> None:
    defaults = normalize_conditions(None)
    assert defaults["media_types"] == ["photo", "video"]
    assert defaults["max_items_per_account"] == 20
    assert defaults["skip_downloaded"] is True

    normalized = normalize_conditions(
        {
            "media_types": ["video", "video", "invalid"],
            "include_keywords": "Alpha, beta",
            "exclude_keywords": ["spam", "SPAM"],
            "min_likes": -5,
            "min_views": 3_000_000_000,
            "max_items_per_account": 999,
            "skip_downloaded": False,
        }
    )
    assert normalized["media_types"] == ["video"]
    assert normalized["include_keywords"] == ["Alpha", "beta"]
    assert normalized["exclude_keywords"] == ["spam"]
    assert normalized["min_likes"] == 0
    assert normalized["min_views"] == 2_000_000_000
    assert normalized["max_items_per_account"] == 200
    assert normalized["skip_downloaded"] is False


@pytest.mark.parametrize(
    ("post", "conditions", "allowed", "reason"),
    [
        ({"text": "alpha launch"}, {"include_keywords": ["alpha"]}, True, ""),
        ({"text": "other"}, {"include_keywords": ["alpha"]}, False, "未包含指定關鍵字"),
        ({"text": "contains SPAM"}, {"exclude_keywords": ["spam"]}, False, "包含排除關鍵字"),
        ({"published_at": "2026-01-01"}, {"date_since": "2026-02-01"}, False, "早於起始日期"),
        ({"published_at": "2026-03-01"}, {"date_until": "2026-02-28"}, False, "晚於結束日期"),
        ({"likes": "9"}, {"min_likes": 10}, False, "按讚數不足"),
        ({"likes": "10", "views": "999"}, {"min_likes": 10, "min_views": 1_000}, False, "觀看數不足"),
        ({"likes": "1.2k", "views": "2萬"}, {"min_likes": 1_000, "min_views": 10_000}, True, ""),
    ],
)
def test_post_conditions_fail_closed_with_specific_reason(
    post: dict[str, object],
    conditions: dict[str, object],
    allowed: bool,
    reason: str,
) -> None:
    assert post_matches_conditions(post, conditions) == (allowed, reason)


@pytest.mark.parametrize(
    ("media_type", "requested", "expected"),
    [
        ("photo", ["photo"], True),
        ("video", ["photo"], False),
        ("video", ["photo", "video"], True),
        ("audio", ["photo", "video"], False),
    ],
)
def test_media_type_filter_is_explicit(
    media_type: str,
    requested: list[str],
    expected: bool,
) -> None:
    assert media_matches_conditions(
        {"media_type": media_type},
        {"media_types": requested},
    ) is expected


@pytest.mark.parametrize(
    ("url", "hosts", "expected"),
    [
        ("https://example.com/media.jpg", ["example.com"], True),
        ("https://cdn.example.com/media.jpg", ["example.com"], True),
        ("https://EXAMPLE.COM/media.jpg", ["example.com"], True),
        ("http://example.com/media.jpg", ["example.com"], False),
        ("https://badexample.com/media.jpg", ["example.com"], False),
        ("https://example.com.evil.test/media.jpg", ["example.com"], False),
        ("file:///tmp/media.jpg", ["example.com"], False),
        ("not a url", ["example.com"], False),
    ],
)
def test_media_url_requires_https_and_exact_allowed_host(
    url: str,
    hosts: list[str],
    expected: bool,
) -> None:
    assert is_allowed_media_url(url, hosts) is expected


@pytest.mark.parametrize(
    ("raw", "fallback", "expected"),
    [
        ("normal-name", "media", "normal-name"),
        (" a/b:c* ", "media", "a_b_c"),
        ("two   words", "media", "two_words"),
        ("...", "fallback", "fallback"),
        ("", "fallback", "fallback"),
        ("x" * 150, "media", "x" * 120),
    ],
)
def test_safe_filename_removes_unsafe_characters(
    raw: str,
    fallback: str,
    expected: str,
) -> None:
    assert safe_filename(raw, fallback) == expected


def test_download_filename_is_deterministic_for_supplied_time() -> None:
    timestamp = datetime(2026, 8, 27, 12, 34, 56, 123456, tzinfo=timezone.utc)
    assert build_download_filename("account/name", ".mp4", timestamp) == (
        "account_name_20260827_123456_123456.mp4"
    )


@pytest.mark.parametrize(
    ("url", "media_type", "content_type", "expected"),
    [
        ("https://x.test/a.jpeg", "photo", "", ".jpeg"),
        ("https://x.test/a.unknown", "photo", "image/png", ".png"),
        ("https://x.test/a.unknown", "photo", "image/webp", ".webp"),
        ("https://x.test/a.unknown", "photo", "image/gif", ".gif"),
        ("https://x.test/a.mov", "video", "", ".mov"),
        ("https://x.test/a.unknown", "video", "video/mp4", ".mp4"),
        ("https://x.test/a.unknown", "video", "video/webm", ".webm"),
        ("https://x.test/a.unknown", "photo", "", ".jpg"),
    ],
)
def test_media_extension_uses_verified_type_then_safe_url_suffix(
    url: str,
    media_type: str,
    content_type: str,
    expected: str,
) -> None:
    assert extension_for_media(url, media_type, content_type) == expected


def _mp4_box(box_type: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


@pytest.mark.parametrize(
    ("content", "media_type", "content_type", "expected"),
    [
        (b"\xff\xd8\xff\xe0image", "photo", "image/jpeg", True),
        (b"\x89PNG\r\n\x1a\nimage", "photo", "image/png", True),
        (b"GIF89aimage", "photo", "image/gif", True),
        (b"RIFF\x04\x00\x00\x00WEBP", "photo", "image/webp", True),
        (b"<html>error</html>", "photo", "text/html", False),
        (b"{}", "photo", "application/json", False),
        (b"random", "photo", "image/jpeg", False),
        (b"#EXTM3U\nsegment.ts", "video", "application/vnd.apple.mpegurl", False),
        (b"\x1aE\xdf\xa3webm", "video", "video/webm", True),
        (b"", "video", "video/mp4", False),
    ],
)
def test_media_payload_requires_real_supported_signature(
    content: bytes,
    media_type: str,
    content_type: str,
    expected: bool,
) -> None:
    assert is_valid_media_payload(content, media_type, content_type) is expected


def test_mp4_payload_requires_complete_boxes_and_video_track() -> None:
    valid = b"".join(
        [
            _mp4_box(b"ftyp", b"isom"),
            _mp4_box(b"moov", b"track-vide"),
            _mp4_box(b"mdat", b"payload"),
        ]
    )
    assert is_valid_media_payload(valid, "video", "video/mp4") is True
    assert is_valid_media_payload(valid[:-1], "video", "video/mp4") is False
    no_video = valid.replace(b"vide", b"soun")
    assert is_valid_media_payload(no_video, "video", "video/mp4") is False


def test_repository_uses_tool_owned_database_and_round_trips_settings(
    tmp_path: Path,
) -> None:
    repository = VaultlyRepository(tmp_path)
    assert repository.db_path == tmp_path / "runtime" / "state" / "vaultly.sqlite3"
    assert repository.db_path.is_file()
    assert repository.get_setting("missing", {"fallback": True}) == {"fallback": True}
    repository.set_setting("download", {"enabled": True, "limit": 5})
    assert repository.get_setting("download") == {"enabled": True, "limit": 5}


def test_repository_filter_terms_are_case_insensitive_and_reversible(
    tmp_path: Path,
) -> None:
    repository = VaultlyRepository(tmp_path)
    assert repository.add_filter_terms(["Alpha", "alpha", "Beta", ""]) == 2
    assert [term.casefold() for term in repository.list_filter_terms()] == ["alpha", "beta"]
    assert repository.remove_filter_terms(["ALPHA"]) == 1
    assert repository.list_filter_terms() == ["Beta"]
