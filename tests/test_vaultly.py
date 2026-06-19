from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.getcwd(), "platform_tools", "vaultly", "src", "backend", "services"))

from vaultly.adapters import (
    AUTO_SCAN_CONTEXT_SCRIPT,
    DISCOVER_POSTS_SCRIPT,
    FOLLOWING_SCAN_SCRIPT,
    INSTAGRAM_PAGE_UNAVAILABLE_SCRIPT,
    INSTAGRAM_PROFILE_NAVIGATION_SCRIPT,
    INSTAGRAM_PROFILE_SETTINGS_SCRIPT,
    INSPECT_POST_SCRIPT,
    OPEN_FOLLOWING_LIST_SCRIPT,
    RESET_FOLLOWING_SCROLL_SCRIPT,
    SCROLL_SCRIPT,
    PLATFORMS,
    PlatformAdapter,
    is_star_candidate_account,
)
from vaultly.repository import VaultlyRepository
from vaultly.rules import (
    build_download_filename,
    is_allowed_media_url,
    is_valid_media_file,
    is_valid_media_payload,
    normalize_conditions,
    parse_metric,
    post_matches_conditions,
    safe_filename,
)
from vaultly.service import VaultlyService


def mp4_box(box_type: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


def sample_mp4() -> bytes:
    return (
        mp4_box(b"ftyp", b"isom0000")
        + mp4_box(b"moov", b"vide")
        + mp4_box(b"mdat", b"video")
    )


def sample_jpeg() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"JFIF\x00" + b"\x00" * 16 + b"\xff\xd9"


def sample_account() -> dict[str, str]:
    return {
        "account_id": "instagram:sample.user",
        "platform": "instagram",
        "handle": "sample.user",
        "display_name": "Sample User",
        "profile_url": "https://www.instagram.com/sample.user/",
        "avatar_url": "",
    }


def verified_account() -> dict[str, object]:
    return {
        **sample_account(),
        "account_id": "instagram:verified.star",
        "handle": "verified.star",
        "display_name": "Verified Star",
        "profile_url": "https://www.instagram.com/verified.star/",
        "verified": True,
    }


def test_repository_persists_accounts_selection_and_jobs(tmp_path: Path) -> None:
    repository = VaultlyRepository(tmp_path)
    repository.upsert_accounts([sample_account()])
    repository.save_selection(["instagram:sample.user"])
    accounts = repository.list_accounts(selected_only=True)

    assert len(accounts) == 1
    assert accounts[0]["selected"] is True

    conditions = normalize_conditions({"media_types": ["photo"], "max_items_per_account": 7})
    repository.create_job("job-1", [accounts[0]["account_id"]], conditions, "", True)
    job = repository.get_job("job-1")

    assert job is not None
    assert job["preview_only"] is True
    assert job["conditions"]["max_items_per_account"] == 7


def test_repository_manages_filter_terms_removed_accounts_and_restore(
    tmp_path: Path,
) -> None:
    repository = VaultlyRepository(tmp_path)
    repository.upsert_accounts([sample_account(), verified_account()])

    assert repository.add_filter_terms(["shop", "@sample.user", "shop"]) == 2
    assert repository.list_filter_terms() == ["@sample.user", "shop"]
    assert repository.remove_filter_terms(["shop"]) == 1
    assert repository.add_retained_accounts(["instagram:sample.user"]) == 1
    assert repository.list_retained_account_ids() == ["instagram:sample.user"]
    assert repository.remove_retained_accounts(["instagram:sample.user"]) == 1

    account = repository.get_accounts(["instagram:sample.user"])[0]
    account["filter_reason"] = "手動移除"
    account["filter_source"] = "manual"
    assert repository.record_removed_accounts([account, verified_account()]) == 1
    assert repository.delete_accounts(["instagram:sample.user"]) == 1

    removed = repository.list_removed_accounts()
    assert len(removed) == 1
    assert removed[0]["reason"] == "手動移除"
    assert removed[0]["source"] == "manual"
    assert removed[0]["verified"] is False

    restored = repository.restore_removed_accounts(["instagram:sample.user"])
    assert len(restored) == 1
    assert repository.list_removed_accounts() == []
    assert repository.get_accounts(["instagram:sample.user"])[0]["handle"] == "sample.user"
    assert repository.get_accounts(["instagram:verified.star"])[0]["verified"] is True


def test_repository_preserves_verified_state_and_cached_avatar(tmp_path: Path) -> None:
    repository = VaultlyRepository(tmp_path)
    account = {
        **verified_account(),
        "avatar_url": "data:image/jpeg;base64,YXZhdGFy",
    }
    repository.upsert_accounts([account])
    repository.upsert_accounts(
        [
            {
                **account,
                "display_name": "",
                "avatar_url": "",
                "verified": False,
            }
        ]
    )

    saved = repository.get_accounts(["instagram:verified.star"])[0]
    assert saved["display_name"] == "Verified Star"
    assert saved["avatar_url"] == "data:image/jpeg;base64,YXZhdGFy"
    assert saved["verified"] is True


def test_download_conditions_and_url_guard() -> None:
    conditions = normalize_conditions(
        {
            "include_keywords": "music, live",
            "exclude_keywords": ["ad"],
            "min_likes": 1_200,
            "min_views": 10_000,
        }
    )
    matches, reason = post_matches_conditions(
        {
            "text": "Live music performance",
            "likes": "1.5K Likes",
            "views": "12K Views",
        },
        conditions,
    )

    assert matches is True
    assert reason == ""
    assert parse_metric("1.5萬") == 15_000
    assert is_allowed_media_url("https://pbs.twimg.com/media/test.jpg", ("twimg.com",))
    assert not is_allowed_media_url("https://twimg.com.example.org/test.jpg", ("twimg.com",))
    assert not is_allowed_media_url("http://pbs.twimg.com/media/test.jpg", ("twimg.com",))
    assert safe_filename('bad:name / media') == "bad_name_media"
    assert build_download_filename(
        "sample.user",
        ".jpg",
        datetime(2026, 6, 4, 12, 34, 56, 789012, tzinfo=timezone.utc),
    ) == "sample.user_20260604_123456_789012.jpg"
    assert is_star_candidate_account(
        {
            "handle": "official_news_company",
            "display_name": "Official News Company",
            "verified": True,
        }
    ) == (True, "已認證帳號")
    assert is_star_candidate_account(
        {
            "handle": "official_news_company",
            "display_name": "Official News Company",
            "verified": False,
        }
    )[0] is False
    assert is_star_candidate_account(
        {
            "handle": "idol_artist",
            "display_name": "KPOP Idol Singer",
            "context_text": "韓團 偶像 歌手",
            "verified": False,
        }
    ) == (True, "偶像明星線索")
    assert is_star_candidate_account(
        {
            "handle": "idol_fanclub",
            "display_name": "Idol Fanclub",
            "context_text": "偶像粉絲頁",
            "verified": False,
        }
    )[0] is False
    assert is_star_candidate_account(
        {
            "handle": "music_shop",
            "display_name": "Music Shop",
            "verified": True,
        },
        ["@music_shop", "music"],
    ) == (True, "已認證帳號")
    assert is_star_candidate_account(
        {
            "handle": "music_person",
            "display_name": "Music Person",
            "verified": False,
        },
        ["@music_person"],
    )[1].startswith("自訂篩選帳號")
    assert is_valid_media_payload(
        sample_mp4(),
        "video",
        "video/mp4",
    )
    assert is_valid_media_payload(
        sample_jpeg(),
        "photo",
        "image/jpeg",
    )
    assert not is_valid_media_payload(
        b"<html>access denied</html>",
        "photo",
        "application/octet-stream",
    )
    assert not is_valid_media_payload(
        b"<html>access denied</html>",
        "video",
        "text/html",
    )
    assert not is_valid_media_payload(
        b"\x00\x00\x00\x18stypmsdh0000moof0000mdatsegment",
        "video",
        "video/mp4",
    )
    assert not is_valid_media_payload(
        mp4_box(b"ftyp", b"isom0000") + mp4_box(b"moov", b"vide"),
        "video",
        "video/mp4",
    )
    assert not is_valid_media_payload(
        sample_mp4()[:-1],
        "video",
        "video/mp4",
    )


def test_inspect_script_supports_instagram_and_x_hls_without_init_segments() -> None:
    assert "networkPlaylists" in INSPECT_POST_SCRIPT
    assert "m3u8" in INSPECT_POST_SCRIPT
    assert "isInitSegment" in INSPECT_POST_SCRIPT
    assert "videoIds" in INSPECT_POST_SCRIPT
    assert "platform === 'x'" in INSPECT_POST_SCRIPT
    assert "fallback_urls" in INSPECT_POST_SCRIPT


def test_following_scan_script_combines_collection_and_scrolling() -> None:
    assert "return { accounts, scroll:" in FOLLOWING_SCAN_SCRIPT
    assert "at_end:" in FOLLOWING_SCAN_SCRIPT
    assert "siguiendo" in FOLLOWING_SCAN_SCRIPT
    assert "target.scrollTop = 0" in RESET_FOLLOWING_SCROLL_SCRIPT


def test_vaultly_evaluate_scripts_are_parseable_by_browser_js() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to parse browser evaluate scripts")

    scripts = {
        "FOLLOWING_SCAN_SCRIPT": FOLLOWING_SCAN_SCRIPT,
        "SCROLL_SCRIPT": SCROLL_SCRIPT,
        "RESET_FOLLOWING_SCROLL_SCRIPT": RESET_FOLLOWING_SCROLL_SCRIPT,
        "AUTO_SCAN_CONTEXT_SCRIPT": AUTO_SCAN_CONTEXT_SCRIPT,
        "OPEN_FOLLOWING_LIST_SCRIPT": OPEN_FOLLOWING_LIST_SCRIPT,
        "INSTAGRAM_PROFILE_SETTINGS_SCRIPT": INSTAGRAM_PROFILE_SETTINGS_SCRIPT,
        "INSTAGRAM_PAGE_UNAVAILABLE_SCRIPT": INSTAGRAM_PAGE_UNAVAILABLE_SCRIPT,
        "INSTAGRAM_PROFILE_NAVIGATION_SCRIPT": INSTAGRAM_PROFILE_NAVIGATION_SCRIPT,
        "DISCOVER_POSTS_SCRIPT": DISCOVER_POSTS_SCRIPT,
        "INSPECT_POST_SCRIPT": INSPECT_POST_SCRIPT,
    }
    for name, script in scripts.items():
        runner = (
            f"const script = {json.dumps(script)};\n"
            "try { new Function('return (' + script + ')'); }\n"
            "catch (error) { console.error(error.stack || error.message); process.exit(1); }\n"
        )
        result = subprocess.run(
            [node, "-e", runner],
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, f"{name} is invalid JavaScript:\n{result.stderr}"


def test_vaultly_ui_supports_search_selected_priority_and_larger_window() -> None:
    ui_source = Path(
        "platform_tools/vaultly/src/ui/VaultlyDownloadCenter.tsx"
    ).read_text(encoding="utf-8")
    tool_window_source = Path("src-ui/main/index.ts").read_text(encoding="utf-8")

    assert "matchesAccountSearch" in ui_source
    assert "Number(selectedIds.has(right.account_id))" in ui_source
    assert "搜尋帳號、顯示名稱或平台" in ui_source
    assert "帳號搜尋" in ui_source
    assert "多選模式" in ui_source
    assert "全選顯示" in ui_source
    assert "認證不自動篩選" in ui_source
    assert "篩選名單只會加入你手動輸入的內容" in ui_source
    assert "被移除帳號紀錄（包含自動篩選）" in ui_source
    assert "removedSourceLabel" in ui_source
    assert "readPlatformToolWindowConfig" in tool_window_source
    assert "readChildToolWindowConfig" not in tool_window_source
    assert "isVaultlyWindow" not in tool_window_source
    tool_manifest = json.loads(
        Path("platform_tools/vaultly/manifest.json").read_text(encoding="utf-8")
    )
    assert tool_manifest["window"]["width"] == 1280
    assert tool_manifest["window"]["height"] == 900
    assert tool_manifest["window"]["minWidth"] == 1020
    assert tool_manifest["window"]["minHeight"] == 720
    assert Path("platform_tools/vaultly/src/backend/services/vaultly/service.py").exists()


@pytest.mark.asyncio
async def test_account_scan_normalizes_deduplicates_and_stops_at_end() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.scan_count = 0
            self.waits: list[int] = []

        async def evaluate(self, script: str, _platform: str) -> object:
            if script == RESET_FOLLOWING_SCROLL_SCRIPT:
                return True
            if script == FOLLOWING_SCAN_SCRIPT:
                self.scan_count += 1
                return {
                    "accounts": [
                        {
                            "handle": "@Good_User",
                            "display_name": "",
                            "profile_url": "https://example.invalid/ignored",
                            "avatar_url": "",
                        },
                        {
                            "handle": "good_user",
                            "display_name": "Good User",
                            "profile_url": "https://example.invalid/ignored",
                            "avatar_url": "https://example.invalid/avatar.jpg",
                        },
                        {"handle": "bad.name", "display_name": "Invalid X Handle"},
                        {"handle": "home", "display_name": "Reserved"},
                        {
                            "handle": "verified_news",
                            "display_name": "Official News Company",
                            "verified": False,
                        },
                        {
                            "handle": "verified_news",
                            "display_name": "Official News Company",
                            "verified": True,
                        },
                        {
                            "handle": "unverified_shop",
                            "display_name": "Official Shop",
                            "verified": False,
                        },
                    ],
                    "scroll": {
                        "moved": False,
                        "position": 0,
                        "maximum": 0,
                        "at_end": True,
                    },
                }
            if script == SCROLL_SCRIPT:
                raise AssertionError("combined scan should not use fallback scrolling")
            raise AssertionError("unexpected script")

        async def wait_for_timeout(self, timeout: int) -> None:
            self.waits.append(timeout)

    page = FakePage()
    adapter = PlatformAdapter(PLATFORMS["x"])
    accounts = await adapter.scan_following(page)

    assert page.scan_count == 4
    assert len(accounts) == 2
    assert accounts[0]["account_id"] == "x:good_user"
    assert accounts[0]["handle"] == "Good_User"
    assert accounts[0]["display_name"] == "Good User"
    assert accounts[0]["profile_url"] == "https://x.com/Good_User"
    assert accounts[0]["avatar_url"] == "https://example.invalid/avatar.jpg"
    assert accounts[1]["account_id"] == "x:verified_news"
    assert accounts[1]["verified"] is True
    assert adapter.last_scan_stats["filtered_account_ids"] == ["x:unverified_shop"]
    assert adapter.last_scan_stats["filtered_accounts"][0]["filter_source"] == "automatic"
    assert adapter.last_scan_stats["completed"] is True
    assert adapter.last_scan_stats["rounds"] == 4
    assert adapter.last_scan_stats["reset_to_start"] is True
    assert page.waits[0] == 350


@pytest.mark.asyncio
async def test_account_scan_tracks_filtered_accounts_as_observed_progress() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.scan_count = 0

        async def evaluate(self, script: str, _platform: str) -> object:
            if script == RESET_FOLLOWING_SCROLL_SCRIPT:
                return False
            assert script == FOLLOWING_SCAN_SCRIPT
            self.scan_count += 1
            handle = "shop_one" if self.scan_count == 1 else "shop_two"
            return {
                "accounts": [
                    {
                        "handle": handle,
                        "display_name": "Official Shop",
                        "verified": False,
                    }
                ],
                "scroll": {
                    "moved": False,
                    "position": 0,
                    "maximum": 0,
                    "at_end": True,
                },
            }

        async def wait_for_timeout(self, _timeout: int) -> None:
            return None

    adapter = PlatformAdapter(PLATFORMS["x"])
    accounts = await adapter.scan_following(FakePage())

    assert accounts == []
    assert adapter.last_scan_stats["observed"] == 2
    assert adapter.last_scan_stats["filtered"] == 2
    assert adapter.last_scan_stats["completed"] is True


@pytest.mark.asyncio
async def test_account_scan_applies_manual_filter_terms() -> None:
    class FakePage:
        async def evaluate(self, script: str, _platform: str) -> object:
            if script == RESET_FOLLOWING_SCROLL_SCRIPT:
                return False
            if script == FOLLOWING_SCAN_SCRIPT:
                return [
                    {
                        "handle": "music_person",
                        "display_name": "Music Person",
                        "verified": False,
                    },
                    {
                        "handle": "verified_music",
                        "display_name": "Music Official",
                        "verified": True,
                    },
                ]
            if script == SCROLL_SCRIPT:
                return {"moved": False, "position": 0, "maximum": 0}
            raise AssertionError("unexpected script")

        async def wait_for_timeout(self, _timeout: int) -> None:
            return None

    adapter = PlatformAdapter(PLATFORMS["x"])
    accounts = await adapter.scan_following(FakePage(), filter_terms=["music"])

    assert [account["account_id"] for account in accounts] == ["x:verified_music"]
    assert adapter.last_scan_stats["filtered_account_ids"] == ["x:music_person"]
    assert adapter.last_scan_stats["filtered_accounts"][0]["filter_source"] == "manual"

    restored_accounts = await adapter.scan_following(
        FakePage(),
        filter_terms=["music"],
        retained_account_ids=["x:music_person"],
    )
    assert [account["account_id"] for account in restored_accounts] == [
        "x:music_person",
        "x:verified_music",
    ]


@pytest.mark.asyncio
async def test_prepare_following_scan_navigates_after_login() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://x.com/home"

        async def evaluate(self, script: str, _platform: str) -> object:
            assert script == AUTO_SCAN_CONTEXT_SCRIPT
            if self.url == "https://x.com/home":
                return {
                    "logged_in": True,
                    "ready": False,
                    "target_url": "https://x.com/sample/following/",
                    "following_url": "https://x.com/sample/following/",
                }
            return {
                "logged_in": True,
                "ready": True,
                "target_url": "https://x.com/sample/following/",
                "following_url": "https://x.com/sample/following/",
            }

        async def goto(self, url: str, wait_until: str, timeout: int) -> None:
            assert wait_until == "domcontentloaded"
            assert timeout == 60_000
            self.url = url

        async def wait_for_timeout(self, timeout: int) -> None:
            assert timeout in {750, 1_000}

    page = FakePage()
    ready = await PlatformAdapter(PLATFORMS["x"]).prepare_following_scan(page)

    assert ready is True
    assert page.url == "https://x.com/sample/following/"


@pytest.mark.asyncio
async def test_prepare_instagram_scan_opens_following_dialog_fallback() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://www.instagram.com/"
            self.following_open = False
            self.open_attempts = 0

        async def evaluate(self, script: str, _platform: str | None = None) -> object:
            if script == AUTO_SCAN_CONTEXT_SCRIPT:
                if self.following_open:
                    return {
                        "logged_in": True,
                        "ready": True,
                        "target_url": self.url,
                        "following_url": "https://www.instagram.com/sample/following/",
                    }
                return {
                    "logged_in": True,
                    "ready": False,
                    "target_url": "https://www.instagram.com/sample/",
                    "following_url": "https://www.instagram.com/sample/following/",
                }
            if script == INSTAGRAM_PROFILE_SETTINGS_SCRIPT:
                assert self.url == "https://www.instagram.com/accounts/edit/"
                return "sample"
            if script == OPEN_FOLLOWING_LIST_SCRIPT:
                self.open_attempts += 1
                self.following_open = True
                return True
            raise AssertionError("unexpected script")

        async def goto(self, url: str, wait_until: str, timeout: int) -> None:
            assert wait_until == "domcontentloaded"
            assert timeout == 60_000
            self.url = url

        async def wait_for_timeout(self, timeout: int) -> None:
            assert timeout in {750, 1_000, 1_500}

    page = FakePage()
    adapter = PlatformAdapter(PLATFORMS["instagram"])
    ready = await adapter.prepare_following_scan(page)

    assert ready is True
    assert page.open_attempts == 1
    assert page.url == "https://www.instagram.com/sample/"
    assert adapter.last_prepare_status["status"] == "ready"


@pytest.mark.asyncio
async def test_prepare_instagram_scan_accepts_cookie_session_without_dialog() -> None:
    class FakeContext:
        async def cookies(self, urls: list[str]) -> list[dict[str, str]]:
            assert urls == ["https://www.instagram.com/"]
            return [
                {"name": "sessionid", "value": "signed-cookie"},
                {"name": "ds_user_id", "value": "42"},
            ]

    class FakePage:
        context = FakeContext()

        async def evaluate(self, _script: str, _platform: str | None = None) -> object:
            raise AssertionError("cookie-ready Instagram scan should not inspect DOM")

        async def goto(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("cookie-ready Instagram scan should not navigate")

        async def wait_for_timeout(self, _timeout: int) -> None:
            raise AssertionError("cookie-ready Instagram scan should not wait for UI")

    adapter = PlatformAdapter(PLATFORMS["instagram"])
    ready = await adapter.prepare_following_scan(FakePage())

    assert ready is True
    assert adapter.last_prepare_status == {
        "status": "ready",
        "message": "Instagram Cookie 登入可用，使用 Cookie 掃描追蹤名單",
    }


@pytest.mark.asyncio
async def test_instagram_scan_prefers_cookie_api_over_following_dialog() -> None:
    class FakeResponse:
        ok = True

        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        async def json(self) -> dict[str, object]:
            return self.payload

    class FakeRequest:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str], int]] = []

        async def get(
            self,
            url: str,
            headers: dict[str, str],
            timeout: int,
        ) -> FakeResponse:
            self.calls.append((url, headers, timeout))
            assert timeout == 45_000
            assert headers["X-IG-App-ID"]
            assert headers["X-CSRFToken"] == "csrf-token"
            assert "sessionid=signed-cookie" in headers["Cookie"]
            assert "ds_user_id=42" in headers["Cookie"]
            if "max_id=cursor-2" in url:
                return FakeResponse(
                    {
                        "users": [
                            {
                                "username": "verified.star",
                                "full_name": "Verified Star",
                                "profile_pic_url": "https://example.invalid/v.jpg",
                                "is_verified": True,
                            }
                        ],
                        "next_max_id": "",
                    }
                )
            return FakeResponse(
                {
                    "users": [
                        {
                            "username": "sample.user",
                            "full_name": "Singer",
                            "profile_pic_url": "https://example.invalid/a.jpg",
                            "is_verified": False,
                        },
                        {
                            "username": "shop_account",
                            "full_name": "Official Shop",
                            "profile_pic_url": "",
                            "is_verified": False,
                        },
                    ],
                    "next_max_id": "cursor-2",
                }
            )

    class FakeContext:
        def __init__(self) -> None:
            self.request = FakeRequest()

        async def cookies(self, urls: list[str]) -> list[dict[str, str]]:
            assert urls == ["https://www.instagram.com/"]
            return [
                {"name": "sessionid", "value": "signed-cookie"},
                {"name": "ds_user_id", "value": "42"},
                {"name": "csrftoken", "value": "csrf-token"},
            ]

    class FakePage:
        def __init__(self) -> None:
            self.context = FakeContext()

        async def evaluate(self, _script: str, _platform: str | None = None) -> object:
            raise AssertionError("cookie API scan should not inspect following dialog")

        async def wait_for_timeout(self, _timeout: int) -> None:
            raise AssertionError("cookie API scan should not wait for scrolling")

    page = FakePage()
    adapter = PlatformAdapter(PLATFORMS["instagram"])
    accounts = await adapter.scan_following(page)

    assert [account["account_id"] for account in accounts] == [
        "instagram:sample.user",
        "instagram:verified.star",
    ]
    assert adapter.last_scan_stats["method"] == "cookie_api"
    assert adapter.last_scan_stats["observed"] == 3
    assert adapter.last_scan_stats["filtered_account_ids"] == [
        "instagram:shop_account"
    ]
    assert adapter.last_scan_stats["completed"] is True
    assert len(page.context.request.calls) == 2


@pytest.mark.asyncio
async def test_prepare_instagram_scan_ignores_stale_ready_dialog() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://www.instagram.com/other.user/"
            self.ready_calls = 0
            self.following_open = False
            self.gotos: list[str] = []
            self.open_urls: list[str] = []

        async def evaluate(self, script: str, _platform: str | None = None) -> object:
            if script == AUTO_SCAN_CONTEXT_SCRIPT:
                self.ready_calls += 1
                if self.ready_calls <= 3:
                    return {
                        "logged_in": True,
                        "ready": True,
                        "target_url": "https://www.instagram.com/other.user/",
                        "following_url": "https://www.instagram.com/other.user/following/",
                    }
                return {
                    "logged_in": True,
                    "ready": self.following_open,
                    "target_url": self.url,
                    "following_url": "https://www.instagram.com/sample/following/",
                }
            if script == INSTAGRAM_PROFILE_SETTINGS_SCRIPT:
                assert self.url == "https://www.instagram.com/accounts/edit/"
                return "sample"
            if script == INSTAGRAM_PAGE_UNAVAILABLE_SCRIPT:
                return False
            if script == OPEN_FOLLOWING_LIST_SCRIPT:
                self.open_urls.append(self.url)
                assert self.url == "https://www.instagram.com/sample/"
                self.following_open = True
                return True
            raise AssertionError("unexpected script")

        async def goto(self, url: str, wait_until: str, timeout: int) -> None:
            assert wait_until == "domcontentloaded"
            assert timeout == 60_000
            self.url = url
            self.gotos.append(url)

        async def wait_for_timeout(self, timeout: int) -> None:
            assert timeout in {500, 1_000, 1_500}

    page = FakePage()
    adapter = PlatformAdapter(PLATFORMS["instagram"])
    ready = await adapter.prepare_following_scan(page)

    assert ready is True
    assert page.gotos == [
        "https://www.instagram.com/accounts/edit/",
        "https://www.instagram.com/sample/",
    ]
    assert page.open_urls == ["https://www.instagram.com/sample/"]


@pytest.mark.asyncio
async def test_prepare_instagram_scan_avoids_unavailable_following_route() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://www.instagram.com/"
            self.following_open = False
            self.open_urls: list[str] = []
            self.gotos: list[str] = []
            self.waits: list[int] = []

        async def evaluate(self, script: str, _platform: str | None = None) -> object:
            if script == AUTO_SCAN_CONTEXT_SCRIPT:
                if self.following_open:
                    return {
                        "logged_in": True,
                        "ready": True,
                        "target_url": self.url,
                        "following_url": "https://www.instagram.com/sample/following/",
                    }
                return {
                    "logged_in": True,
                    "ready": False,
                    "target_url": "https://www.instagram.com/sample/",
                    "following_url": "https://www.instagram.com/sample/following/",
                }
            if script == INSTAGRAM_PROFILE_SETTINGS_SCRIPT:
                assert self.url == "https://www.instagram.com/accounts/edit/"
                return "sample"
            if script == OPEN_FOLLOWING_LIST_SCRIPT:
                self.open_urls.append(self.url)
                if len(self.open_urls) >= 5:
                    self.following_open = True
                    return True
                return False
            raise AssertionError("unexpected script")

        async def goto(self, url: str, wait_until: str, timeout: int) -> None:
            assert wait_until == "domcontentloaded"
            assert timeout == 60_000
            self.url = url
            self.gotos.append(url)

        async def wait_for_timeout(self, timeout: int) -> None:
            self.waits.append(timeout)

    page = FakePage()
    adapter = PlatformAdapter(PLATFORMS["instagram"])
    ready = await adapter.prepare_following_scan(page)

    assert ready is True
    assert page.gotos == [
        "https://www.instagram.com/accounts/edit/",
        "https://www.instagram.com/sample/",
    ]
    assert all(url == "https://www.instagram.com/sample/" for url in page.open_urls)
    assert adapter.last_prepare_status["status"] == "ready"


@pytest.mark.asyncio
async def test_prepare_instagram_scan_recovers_from_unavailable_profile_page() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://www.instagram.com/"
            self.following_open = False
            self.gotos: list[str] = []
            self.open_urls: list[str] = []
            self.settings_calls = 0

        async def evaluate(self, script: str, _platform: str | None = None) -> object:
            if script == AUTO_SCAN_CONTEXT_SCRIPT:
                if self.following_open:
                    return {
                        "logged_in": True,
                        "ready": True,
                        "target_url": self.url,
                        "following_url": "https://www.instagram.com/sample/following/",
                    }
                return {
                    "logged_in": True,
                    "ready": False,
                    "target_url": "https://www.instagram.com/wrong.user/",
                    "following_url": "https://www.instagram.com/wrong.user/following/",
                }
            if script == INSTAGRAM_PAGE_UNAVAILABLE_SCRIPT:
                return self.url == "https://www.instagram.com/wrong.user/"
            if script == INSTAGRAM_PROFILE_SETTINGS_SCRIPT:
                assert self.url == "https://www.instagram.com/accounts/edit/"
                self.settings_calls += 1
                return "wrong.user" if self.settings_calls == 1 else "sample"
            if script == OPEN_FOLLOWING_LIST_SCRIPT:
                self.open_urls.append(self.url)
                if self.url == "https://www.instagram.com/sample/":
                    self.following_open = True
                    return True
                return False
            raise AssertionError("unexpected script")

        async def goto(self, url: str, wait_until: str, timeout: int) -> None:
            assert wait_until == "domcontentloaded"
            assert timeout == 60_000
            self.url = url
            self.gotos.append(url)

        async def wait_for_timeout(self, timeout: int) -> None:
            assert timeout in {500, 1_000, 1_500}

    page = FakePage()
    adapter = PlatformAdapter(PLATFORMS["instagram"])
    ready = await adapter.prepare_following_scan(page)

    assert ready is True
    assert page.gotos[:4] == [
        "https://www.instagram.com/accounts/edit/",
        "https://www.instagram.com/wrong.user/",
        "https://www.instagram.com/accounts/edit/",
        "https://www.instagram.com/sample/",
    ]
    assert page.open_urls == ["https://www.instagram.com/sample/"]
    assert adapter.last_prepare_status["status"] == "ready"


@pytest.mark.asyncio
async def test_prepare_instagram_scan_resolves_profile_from_settings_when_entry_missing() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://www.instagram.com/"
            self.following_open = False
            self.open_attempts = 0
            self.gotos: list[str] = []

        async def evaluate(self, script: str, _platform: str | None = None) -> object:
            if script == AUTO_SCAN_CONTEXT_SCRIPT:
                if self.following_open:
                    return {
                        "logged_in": True,
                        "ready": True,
                        "target_url": self.url,
                        "following_url": "https://www.instagram.com/sample/following/",
                    }
                return {
                    "logged_in": True,
                    "ready": False,
                    "target_url": "",
                    "following_url": "",
                    "message": "找不到個人檔案入口",
                }
            if script == INSTAGRAM_PROFILE_SETTINGS_SCRIPT:
                assert self.url == "https://www.instagram.com/accounts/edit/"
                return "sample"
            if script == OPEN_FOLLOWING_LIST_SCRIPT:
                assert self.url == "https://www.instagram.com/sample/"
                self.open_attempts += 1
                self.following_open = True
                return True
            raise AssertionError("unexpected script")

        async def goto(self, url: str, wait_until: str, timeout: int) -> None:
            assert wait_until == "domcontentloaded"
            assert timeout == 60_000
            self.url = url
            self.gotos.append(url)

        async def wait_for_timeout(self, timeout: int) -> None:
            assert timeout in {750, 1_000, 1_500}

    page = FakePage()
    adapter = PlatformAdapter(PLATFORMS["instagram"])
    ready = await adapter.prepare_following_scan(page)

    assert ready is True
    assert page.gotos[:2] == [
        "https://www.instagram.com/accounts/edit/",
        "https://www.instagram.com/sample/",
    ]
    assert page.open_attempts == 1
    assert adapter.last_prepare_status["status"] == "ready"


def test_instagram_scan_requires_real_following_dialog() -> None:
    assert "document.querySelector('[role=\"dialog\"]') || document.querySelector('main')" not in FOLLOWING_SCAN_SCRIPT
    assert "isInstagramFollowingDialog" in FOLLOWING_SCAN_SCRIPT
    assert "isInstagramFollowingDialog" in AUTO_SCAN_CONTEXT_SCRIPT
    assert "platform === 'instagram'\n      ? `${origin}/${handle}/`" in AUTO_SCAN_CONTEXT_SCRIPT
    assert "a[href], button, [role=\"button\"], [role=\"link\"]" in OPEN_FOLLOWING_LIST_SCRIPT
    assert "scrollIntoView" in OPEN_FOLLOWING_LIST_SCRIPT
    assert "PointerEvent" in OPEN_FOLLOWING_LIST_SCRIPT
    assert "pageUnavailable" in AUTO_SCAN_CONTEXT_SCRIPT


def test_instagram_auto_scan_profile_entry_has_resilient_fallbacks() -> None:
    assert "isLikelyInstagramProfileAnchor" in AUTO_SCAN_CONTEXT_SCRIPT
    assert "anchors.find(isLikelyInstagramProfileAnchor)" in AUTO_SCAN_CONTEXT_SCRIPT
    assert "profileTextPattern" in AUTO_SCAN_CONTEXT_SCRIPT
    assert "extractViewerHandleFromScripts" in AUTO_SCAN_CONTEXT_SCRIPT
    assert "pathParts.length === 1 && isAllowedHandle(pathParts[0])" in AUTO_SCAN_CONTEXT_SCRIPT


@pytest.mark.asyncio
async def test_instagram_avatar_is_embedded_for_renderer() -> None:
    class FakeResponse:
        ok = True
        headers = {"content-type": "image/jpeg"}

        async def body(self) -> bytes:
            return b"\xff\xd8\xffavatar"

    class FakeRequest:
        async def get(
            self,
            _url: str,
            headers: dict[str, str],
            timeout: int,
        ) -> FakeResponse:
            assert headers["Referer"] == "https://www.instagram.com/"
            assert timeout == 15_000
            return FakeResponse()

    class FakePage:
        class Context:
            request = FakeRequest()

        context = Context()

    accounts = [
        {
            "account_id": "instagram:sample.user",
            "platform": "instagram",
            "handle": "sample.user",
            "avatar_url": "https://scontent.cdninstagram.com/avatar.jpg",
        }
    ]

    await PlatformAdapter(PLATFORMS["instagram"]).hydrate_avatar_urls(FakePage(), accounts)

    assert accounts[0]["avatar_url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_service_requires_existing_destination_without_creating_folders(
    tmp_path: Path,
) -> None:
    service = VaultlyService(tmp_path, session=None)
    assert service.session.shared_profile_dir == (
        tmp_path / "runtime" / "browser-profiles" / "vaultly" / "shared"
    )
    service.repository.upsert_accounts([sample_account()])
    service._schedule_job = lambda _job_id: None  # type: ignore[method-assign]
    missing_destination = tmp_path / "missing" / "child"

    result = await service._create_job(
        {
            "account_ids": ["instagram:sample.user"],
            "destination": str(missing_destination),
            "preview_only": False,
            "conditions": {},
        }
    )
    preview_result = await service._create_job(
        {
            "account_ids": ["instagram:sample.user"],
            "preview_only": True,
            "conditions": {"max_items_per_account": 3},
        }
    )

    assert result["ok"] is False
    assert not missing_destination.exists()
    assert preview_result["ok"] is True


@pytest.mark.asyncio
async def test_service_start_enables_background_auto_scan(tmp_path: Path) -> None:
    service = VaultlyService(tmp_path, session=object())

    await service.start()

    assert service._auto_scan_task is not None
    assert service._auto_scan_task.done() is False

    await service.shutdown()

    assert service._auto_scan_task is None


@pytest.mark.asyncio
async def test_service_waits_for_vaultly_window_before_browser_work(
    tmp_path: Path,
) -> None:
    scheduled_jobs: list[str] = []
    service = VaultlyService(tmp_path, session=object())
    service.repository.create_job(
        "job-waiting",
        ["instagram:sample.user"],
        normalize_conditions({}),
        "",
        True,
    )
    service._schedule_job = scheduled_jobs.append  # type: ignore[method-assign]

    await service.start()

    assert service._user_opened is False
    assert scheduled_jobs == []
    assert service._pending_start_job_ids == {"job-waiting"}
    assert service._auto_scan_state["instagram"]["message"] == "等待開啟影音下載自動化"

    state = await service._get_state({})

    assert state["ok"] is True
    assert service._user_opened is True
    assert scheduled_jobs == ["job-waiting"]
    assert service._pending_start_job_ids == set()
    assert service._auto_scan_state["instagram"]["message"] == "等待登入後自動掃描"

    await service.shutdown()


@pytest.mark.asyncio
async def test_service_scan_records_filtered_accounts_and_can_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        pass

    class FakeSession:
        async def ensure_external_page(
            self,
            key: str,
            target_url: str,
            match_hosts: tuple[str, ...],
        ) -> FakePage:
            assert key == "vaultly:scan:instagram"
            assert target_url == "https://www.instagram.com/"
            assert match_hosts == ()
            return FakePage()

    class FakeAdapter:
        last_scan_stats = {
            "observed": 2,
            "filtered": 1,
            "filtered_accounts": [
                {
                    **sample_account(),
                    "filter_reason": "自訂篩選帳號：@sample.user",
                    "filter_source": "manual",
                    "verified": False,
                }
            ],
        }

        async def prepare_following_scan(self, _page: FakePage) -> bool:
            return True

        async def scan_following(
            self,
            _page: FakePage,
            filter_terms: list[str],
            retained_account_ids: list[str],
            reset_to_start: bool,
        ) -> list[dict[str, object]]:
            assert filter_terms == ["@sample.user"]
            assert retained_account_ids == []
            assert reset_to_start is True
            return [verified_account()]

        async def hydrate_avatar_urls(
            self,
            _page: FakePage,
            _accounts: list[dict[str, object]],
        ) -> None:
            return None

    monkeypatch.setattr("vaultly.service.get_adapter", lambda _platform: FakeAdapter())
    service = VaultlyService(tmp_path, session=FakeSession())
    service.repository.upsert_accounts([sample_account()])
    service.repository.add_filter_terms(["@sample.user"])

    result = await service._scan_platform("instagram", automatic=True)

    assert result["ok"] is True
    assert service.repository.get_accounts(["instagram:sample.user"]) == []
    assert service.repository.get_accounts(["instagram:verified.star"])[0]["verified"] is True
    assert service.repository.list_removed_accounts()[0]["account_id"] == "instagram:sample.user"

    restore_result = await service._restore_accounts(
        {"account_ids": ["instagram:sample.user"]}
    )
    assert restore_result["restored_count"] == 1
    assert service.repository.list_filter_terms() == ["@sample.user"]
    assert service.repository.list_removed_accounts() == []
    assert service.repository.list_retained_account_ids() == ["instagram:sample.user"]


@pytest.mark.asyncio
async def test_service_scan_records_automatically_filtered_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        async def ensure_external_page(self, *_args: object) -> object:
            return object()

    class FakeAdapter:
        last_scan_stats = {
            "observed": 2,
            "filtered": 1,
            "filtered_accounts": [
                {
                    **sample_account(),
                    "filter_reason": "明顯非明星帳號關鍵字：shop",
                    "filter_source": "automatic",
                    "verified": False,
                }
            ],
            "completed": True,
        }

        async def prepare_following_scan(self, _page: object) -> bool:
            return True

        async def scan_following(
            self,
            _page: object,
            filter_terms: list[str],
            retained_account_ids: list[str],
            reset_to_start: bool,
        ) -> list[dict[str, object]]:
            assert filter_terms == []
            assert retained_account_ids == []
            assert reset_to_start is True
            return [verified_account()]

        async def hydrate_avatar_urls(
            self,
            _page: object,
            _accounts: list[dict[str, object]],
        ) -> None:
            return None

    monkeypatch.setattr("vaultly.service.get_adapter", lambda _platform: FakeAdapter())
    service = VaultlyService(tmp_path, session=FakeSession())

    result = await service._scan_platform("instagram", automatic=True)

    removed = service.repository.list_removed_accounts()
    assert result["ok"] is True
    assert len(removed) == 1
    assert removed[0]["account_id"] == "instagram:sample.user"
    assert removed[0]["source"] == "automatic"
    assert removed[0]["reason"] == "明顯非明星帳號關鍵字：shop"


@pytest.mark.asyncio
async def test_service_scan_reuses_cached_instagram_avatar_and_protects_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached_avatar = "data:image/jpeg;base64,YXZhdGFy"

    class FakePage:
        pass

    class FakeSession:
        async def ensure_external_page(self, *_args: object) -> FakePage:
            return FakePage()

    class FakeAdapter:
        last_scan_stats = {
            "observed": 1,
            "filtered": 0,
            "filtered_accounts": [],
            "rounds": 2,
            "completed": True,
        }

        async def prepare_following_scan(self, _page: FakePage) -> bool:
            return True

        async def scan_following(
            self,
            _page: FakePage,
            filter_terms: list[str],
            retained_account_ids: list[str],
            reset_to_start: bool,
        ) -> list[dict[str, object]]:
            assert filter_terms == ["news"]
            assert retained_account_ids == ["instagram:verified.star"]
            assert reset_to_start is True
            return [
                {
                    **verified_account(),
                    "avatar_url": "https://scontent.cdninstagram.com/new-avatar.jpg",
                    "verified": False,
                }
            ]

        async def hydrate_avatar_urls(
            self,
            _page: FakePage,
            accounts: list[dict[str, object]],
        ) -> None:
            assert accounts[0]["avatar_url"] == cached_avatar

    monkeypatch.setattr("vaultly.service.get_adapter", lambda _platform: FakeAdapter())
    service = VaultlyService(tmp_path, session=FakeSession())
    service.repository.upsert_accounts(
        [
            {
                **verified_account(),
                "avatar_url": cached_avatar,
            }
        ]
    )
    service.repository.add_filter_terms(["news"])

    result = await service._scan_platform("instagram", automatic=True)

    saved = service.repository.get_accounts(["instagram:verified.star"])[0]
    assert result["ok"] is True
    assert result["reused_avatar_count"] == 1
    assert result["observed_count"] == 1
    assert saved["avatar_url"] == cached_avatar
    assert saved["verified"] is True


@pytest.mark.asyncio
async def test_service_marks_partial_scan_for_automatic_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        async def ensure_external_page(self, *_args: object) -> object:
            return object()

    class FakeAdapter:
        def __init__(self) -> None:
            self.scan_calls = 0
            self.last_scan_stats: dict[str, object] = {}

        async def prepare_following_scan(self, _page: object) -> bool:
            return True

        async def scan_following(
            self,
            _page: object,
            filter_terms: list[str],
            retained_account_ids: list[str],
            reset_to_start: bool,
        ) -> list[dict[str, object]]:
            self.scan_calls += 1
            assert filter_terms == []
            assert retained_account_ids == []
            assert reset_to_start is (self.scan_calls == 1)
            self.last_scan_stats = {
                "observed": 1,
                "filtered": 0,
                "filtered_accounts": [],
                "rounds": 160 if self.scan_calls == 1 else 2,
                "completed": self.scan_calls > 1,
            }
            return [sample_account()]

        async def hydrate_avatar_urls(
            self,
            _page: object,
            _accounts: list[dict[str, object]],
        ) -> None:
            return None

    adapter = FakeAdapter()
    monkeypatch.setattr("vaultly.service.get_adapter", lambda _platform: adapter)
    service = VaultlyService(tmp_path, session=FakeSession())

    result = await service._scan_platform("instagram", automatic=True)
    continued_result = await service._scan_platform("instagram", automatic=True)

    assert result["ok"] is True
    assert result["scan_complete"] is False
    assert "稍後會自動重試" in result["message"]
    assert continued_result["scan_complete"] is True
    assert service._auto_scan_state["instagram"]["status"] == "completed"
    assert service._scan_continuations["instagram"] is False


@pytest.mark.asyncio
async def test_service_retries_instagram_when_dialog_has_no_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        async def ensure_external_page(self, *_args: object) -> object:
            return object()

    class FakeAdapter:
        last_prepare_status = {"status": "ready", "message": "追蹤名單已開啟"}
        last_scan_stats = {
            "observed": 0,
            "filtered": 0,
            "filtered_accounts": [],
        }

        async def prepare_following_scan(self, _page: object) -> bool:
            return True

        async def scan_following(
            self,
            _page: object,
            filter_terms: list[str],
            retained_account_ids: list[str],
            reset_to_start: bool,
        ) -> list[dict[str, object]]:
            assert filter_terms == []
            assert retained_account_ids == []
            assert reset_to_start is True
            return []

        async def hydrate_avatar_urls(
            self,
            _page: object,
            _accounts: list[dict[str, object]],
        ) -> None:
            return None

    monkeypatch.setattr("vaultly.service.get_adapter", lambda _platform: FakeAdapter())
    service = VaultlyService(tmp_path, session=FakeSession())

    result = await service._scan_platform("instagram", automatic=True)

    assert result["ok"] is False
    assert service._auto_scan_state["instagram"]["status"] == "error"
    assert "稍後會自動重試" in service._auto_scan_state["instagram"]["message"]


@pytest.mark.asyncio
async def test_service_manual_remove_allows_verified_accounts(tmp_path: Path) -> None:
    service = VaultlyService(tmp_path, session=object())
    service.repository.upsert_accounts([sample_account(), verified_account()])
    service.repository.add_retained_accounts(["instagram:sample.user"])

    result = await service._remove_accounts(
        {
            "account_ids": [
                "instagram:sample.user",
                "instagram:verified.star",
            ]
        }
    )

    assert result["removed_count"] == 2
    assert result["protected_count"] == 0
    assert service.repository.get_accounts(["instagram:sample.user"]) == []
    assert service.repository.get_accounts(["instagram:verified.star"]) == []
    removed = service.repository.list_removed_accounts()
    assert service.repository.list_filter_terms() == []
    assert {account["account_id"] for account in removed} == {
        "instagram:sample.user",
        "instagram:verified.star",
    }
    assert all(account["source"] == "manual" for account in removed)
    assert service.repository.list_retained_account_ids() == []


@pytest.mark.asyncio
async def test_service_manual_removed_accounts_stay_hidden_without_filter_terms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        async def ensure_external_page(self, *_args: object) -> object:
            return object()

    class FakeAdapter:
        last_scan_stats = {
            "observed": 2,
            "filtered": 0,
            "filtered_accounts": [],
            "completed": True,
        }

        async def prepare_following_scan(self, _page: object) -> bool:
            return True

        async def scan_following(
            self,
            _page: object,
            filter_terms: list[str],
            retained_account_ids: list[str],
            reset_to_start: bool,
        ) -> list[dict[str, object]]:
            assert filter_terms == []
            assert retained_account_ids == []
            assert reset_to_start is True
            return [sample_account(), verified_account()]

        async def hydrate_avatar_urls(
            self,
            _page: object,
            _accounts: list[dict[str, object]],
        ) -> None:
            return None

    monkeypatch.setattr("vaultly.service.get_adapter", lambda _platform: FakeAdapter())
    service = VaultlyService(tmp_path, session=FakeSession())
    service.repository.upsert_accounts([sample_account(), verified_account()])

    await service._remove_accounts(
        {"account_ids": ["instagram:sample.user", "instagram:verified.star"]}
    )
    result = await service._scan_platform("instagram", automatic=True)

    assert result["filtered_count"] == 2
    assert service.repository.list_filter_terms() == []
    assert service.repository.get_accounts(["instagram:sample.user"]) == []
    assert service.repository.get_accounts(["instagram:verified.star"]) == []
    assert {account["account_id"] for account in service.repository.list_removed_accounts()} == {
        "instagram:sample.user",
        "instagram:verified.star",
    }


@pytest.mark.asyncio
async def test_service_download_job_writes_directly_to_selected_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_mp4 = sample_mp4()

    class FakeResponse:
        ok = True
        status = 200
        headers = {
            "content-type": "video/mp4",
            "content-length": str(len(valid_mp4)),
        }

        async def body(self) -> bytes:
            return valid_mp4

    class FakeRequest:
        async def get(
            self,
            _url: str,
            headers: dict[str, str],
            timeout: int,
        ) -> FakeResponse:
            assert headers["Referer"] == "https://www.instagram.com/p/post-1/"
            assert headers["Cookie"] == "sessionid=signed-cookie; ds_user_id=42"
            assert timeout == 60_000
            return FakeResponse()

    class FakePage:
        class Context:
            request = FakeRequest()

            async def cookies(self, urls: list[str]) -> list[dict[str, str]]:
                assert urls == ["https://scontent.cdninstagram.com/media/video.mp4"]
                return [
                    {"name": "sessionid", "value": "signed-cookie"},
                    {"name": "ds_user_id", "value": "42"},
                ]

        context = Context()

    class FakeSession:
        async def ensure_external_page(self, *_args: object) -> FakePage:
            return FakePage()

    class FakeAdapter:
        async def discover_posts(
            self,
            _page: FakePage,
            _profile_url: str,
            _limit: int,
        ) -> list[dict[str, str]]:
            return [{"post_url": "https://www.instagram.com/p/post-1/", "text": "music"}]

        async def inspect_post(
            self,
            _page: FakePage,
            post: dict[str, str],
        ) -> dict[str, object]:
            return {
                **post,
                "likes": "10K",
                "views": "20K",
                "media": [
                    {
                        "media_type": "video",
                        "source_url": "https://scontent.cdninstagram.com/media/video.mp4",
                    }
                ],
            }

    monkeypatch.setattr("vaultly.service.get_adapter", lambda _platform: FakeAdapter())
    destination = tmp_path / "downloads"
    destination.mkdir()
    service = VaultlyService(tmp_path, session=FakeSession())
    service.repository.upsert_accounts([sample_account()])
    service.repository.create_job(
        "job-download",
        ["instagram:sample.user"],
        normalize_conditions({"media_types": ["video"], "max_items_per_account": 1}),
        str(destination),
        False,
    )

    await service._run_job("job-download")

    job = service.repository.get_job("job-download")
    downloaded_files = list(destination.iterdir())
    assert job is not None
    assert job["status"] == "completed"
    assert job["downloaded"] == 1
    assert len(downloaded_files) == 1
    assert downloaded_files[0].parent == destination
    assert re.fullmatch(
        r"sample\.user_\d{8}_\d{6}_\d{6}\.mp4",
        downloaded_files[0].name,
    )
    assert "instagram" not in downloaded_files[0].name
    assert downloaded_files[0].read_bytes() == valid_mp4


@pytest.mark.asyncio
async def test_service_rejects_invalid_video_response(tmp_path: Path) -> None:
    class FakeResponse:
        ok = True
        status = 200
        headers = {"content-type": "text/html", "content-length": "26"}

        async def body(self) -> bytes:
            return b"<html>access denied</html>"

    class FakeRequest:
        async def get(
            self,
            _url: str,
            headers: dict[str, str],
            timeout: int,
        ) -> FakeResponse:
            assert headers["Referer"] == "https://www.instagram.com/reel/post-1/"
            assert timeout == 60_000
            return FakeResponse()

    class FakePage:
        class Context:
            request = FakeRequest()

        context = Context()

    destination = tmp_path / "downloads"
    destination.mkdir()
    service = VaultlyService(tmp_path, session=object())

    with pytest.raises(RuntimeError, match="完整媒體檔"):
        await service._download_media(
            FakePage(),
            {"destination": str(destination)},
            sample_account(),
            {"post_url": "https://www.instagram.com/reel/post-1/"},
            {
                "media_type": "video",
                "source_url": "https://scontent.cdninstagram.com/media/video.mp4",
            },
            "invalid-video",
        )

    assert list(destination.iterdir()) == []


@pytest.mark.asyncio
async def test_service_rejects_invalid_photo_response(tmp_path: Path) -> None:
    class FakeResponse:
        ok = True
        status = 200
        headers = {
            "content-type": "application/octet-stream",
            "content-length": "26",
        }

        async def body(self) -> bytes:
            return b"<html>access denied</html>"

    class FakeRequest:
        async def get(
            self,
            _url: str,
            headers: dict[str, str],
            timeout: int,
        ) -> FakeResponse:
            assert headers["Referer"] == "https://www.instagram.com/p/post-1/"
            assert timeout == 60_000
            return FakeResponse()

    class FakePage:
        class Context:
            request = FakeRequest()

        context = Context()

    destination = tmp_path / "downloads"
    destination.mkdir()
    service = VaultlyService(tmp_path, session=object())

    with pytest.raises(RuntimeError, match="完整媒體檔"):
        await service._download_media(
            FakePage(),
            {"destination": str(destination)},
            sample_account(),
            {"post_url": "https://www.instagram.com/p/post-1/"},
            {
                "media_type": "photo",
                "source_url": "https://scontent.cdninstagram.com/media/photo.jpg",
            },
            "invalid-photo",
        )

    assert list(destination.iterdir()) == []


def test_invalid_download_history_is_not_treated_as_completed(tmp_path: Path) -> None:
    service = VaultlyService(tmp_path, session=object())
    invalid_video = tmp_path / "invalid.mp4"
    invalid_video.write_bytes(b"<html>access denied</html>")
    valid_video = tmp_path / "valid.mp4"
    valid_video.write_bytes(sample_mp4())

    service.repository.record_download(
        "invalid",
        "instagram",
        "instagram:sample.user",
        "https://www.instagram.com/reel/invalid/",
        "https://scontent.cdninstagram.com/media/invalid.mp4",
        str(invalid_video),
        "bad",
    )
    service.repository.record_download(
        "valid",
        "instagram",
        "instagram:sample.user",
        "https://www.instagram.com/reel/valid/",
        "https://scontent.cdninstagram.com/media/valid.mp4",
        str(valid_video),
        "good",
    )

    assert not is_valid_media_file(invalid_video, "video")
    assert is_valid_media_file(valid_video, "video")
    assert service._has_valid_download("invalid", "video") is False
    assert service._has_valid_download("valid", "video") is True


@pytest.mark.asyncio
async def test_service_falls_back_to_hls_for_instagram_video(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        ok = True
        status = 200
        headers = {"content-type": "video/mp4", "content-length": "16"}

        async def body(self) -> bytes:
            return b"incomplete-video"

    class FakeRequest:
        async def get(
            self,
            _url: str,
            headers: dict[str, str],
            timeout: int,
        ) -> FakeResponse:
            assert headers["Referer"] == "https://www.instagram.com/reel/post-1/"
            assert timeout == 60_000
            return FakeResponse()

    class FakePage:
        class Context:
            request = FakeRequest()

        context = Context()

    hls_url = "https://scontent.cdninstagram.com/media/master.m3u8"

    async def fake_hls_download(
        _page: FakePage,
        source_url: str,
        post_url: str,
        temp_path: Path,
    ) -> str:
        assert source_url == hls_url
        assert post_url == "https://www.instagram.com/reel/post-1/"
        temp_path.write_bytes(sample_mp4())
        return "video/mp4"

    destination = tmp_path / "downloads"
    destination.mkdir()
    service = VaultlyService(tmp_path, session=object())
    monkeypatch.setattr(service, "_download_hls_media", fake_hls_download)

    await service._download_media(
        FakePage(),
        {"destination": str(destination)},
        sample_account(),
        {"post_url": "https://www.instagram.com/reel/post-1/"},
        {
            "media_type": "video",
            "source_url": "https://scontent.cdninstagram.com/media/init.mp4",
            "fallback_urls": [hls_url],
        },
        "instagram-hls",
    )

    downloaded_files = list(destination.iterdir())
    assert len(downloaded_files) == 1
    assert is_valid_media_file(downloaded_files[0], "video")
    assert service.repository.get_download("instagram-hls")["source_url"] == hls_url


@pytest.mark.asyncio
async def test_service_rejects_partial_video_response(tmp_path: Path) -> None:
    class FakeResponse:
        ok = True
        status = 206
        headers = {"content-type": "video/mp4", "content-length": "8"}

        async def body(self) -> bytes:
            return b"partial!"

    class FakeRequest:
        async def get(
            self,
            _url: str,
            headers: dict[str, str],
            timeout: int,
        ) -> FakeResponse:
            return FakeResponse()

    class FakePage:
        class Context:
            request = FakeRequest()

        context = Context()

    destination = tmp_path / "downloads"
    destination.mkdir()
    service = VaultlyService(tmp_path, session=object())

    with pytest.raises(RuntimeError, match="部分媒體內容"):
        await service._download_media(
            FakePage(),
            {"destination": str(destination)},
            sample_account(),
            {"post_url": "https://www.instagram.com/reel/post-1/"},
            {
                "media_type": "video",
                "source_url": "https://scontent.cdninstagram.com/media/video.mp4",
            },
            "partial-video",
        )

    assert list(destination.iterdir()) == []


@pytest.mark.asyncio
async def test_hls_download_merges_video_audio_and_forwards_browser_cookies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeContext:
        async def cookies(self, urls: list[str]) -> list[dict[str, str]]:
            assert urls == ["https://scontent.cdninstagram.com/media/master.m3u8"]
            return [{"name": "sessionid", "value": "signed-cookie"}]

    class FakePage:
        context = FakeContext()

    def fake_run(
        command: list[str],
        capture_output: bool,
        timeout: int,
        creationflags: int,
    ) -> object:
        assert capture_output is True
        assert timeout == 300
        assert creationflags >= 0
        assert command[command.index("-map") + 1] == "0:v:0"
        second_map = command.index("-map", command.index("-map") + 1)
        assert command[second_map + 1] == "0:a:0?"
        headers = command[command.index("-headers") + 1]
        assert "Referer: https://www.instagram.com/reel/post-1/" in headers
        assert "Cookie: sessionid=signed-cookie" in headers
        Path(command[-1]).write_bytes(sample_mp4())

        class Completed:
            returncode = 0
            stderr = b""

        return Completed()

    monkeypatch.setattr(
        "vaultly.service.imageio_ffmpeg.get_ffmpeg_exe",
        lambda: "ffmpeg",
    )
    monkeypatch.setattr("vaultly.service.subprocess.run", fake_run)

    output = tmp_path / "merged.part"
    service = VaultlyService(tmp_path, session=object())
    content_type = await service._download_hls_media(
        FakePage(),
        "https://scontent.cdninstagram.com/media/master.m3u8",
        "https://www.instagram.com/reel/post-1/",
        output,
    )

    assert content_type == "video/mp4"
    assert is_valid_media_file(output, "video")


@pytest.mark.asyncio
async def test_cancel_job_stops_running_task_without_requeue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        pass

    started = asyncio.Event()
    service = VaultlyService(tmp_path, session=FakeSession())
    service.repository.upsert_accounts([sample_account()])
    service.repository.create_job(
        "job-cancel",
        ["instagram:sample.user"],
        normalize_conditions({"media_types": ["photo"], "max_items_per_account": 1}),
        str(tmp_path),
        False,
    )

    async def slow_process_account(
        _job: dict[str, object],
        _account: dict[str, object],
        _counters: dict[str, int],
    ) -> None:
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(service, "_process_account", slow_process_account)
    task = asyncio.create_task(service._run_job("job-cancel"))
    service._job_tasks["job-cancel"] = task

    await asyncio.wait_for(started.wait(), timeout=1)
    result = await service._cancel_job({"job_id": "job-cancel"})
    await asyncio.wait_for(task, timeout=1)

    job = service.repository.get_job("job-cancel")
    assert result["ok"] is True
    assert task.cancelled() is False
    assert job is not None
    assert job["status"] == "cancelled"
    assert job["message"] == "使用者已取消"
