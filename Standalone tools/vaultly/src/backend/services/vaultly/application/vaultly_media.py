from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

try:
    import imageio_ffmpeg
except ModuleNotFoundError:
    class _MissingImageioFfmpeg:
        @staticmethod
        def get_ffmpeg_exe() -> str:
            raise RuntimeError("缺少 imageio_ffmpeg，無法合併 HLS 影片。")

    imageio_ffmpeg = _MissingImageioFfmpeg()

from ..integration.adapters import PLATFORMS
from ..domain.rules import (
    build_download_filename,
    extension_for_media,
    is_allowed_media_url,
    is_valid_media_file,
    is_valid_media_payload,
)


class VaultlyMediaMixin:
    """Media download, HLS merging, retry logic, and download deduplication."""

    async def _download_media(
        self,
        page: Any,
        job: dict[str, Any],
        account: dict[str, Any],
        post: dict[str, Any],
        media: dict[str, Any],
        dedupe_key: str,
    ) -> None:
        media_type = str(media.get("media_type", "photo"))
        platform = str(account["platform"])
        definition = PLATFORMS[platform]
        source_urls = self._allowed_media_sources(media, definition.media_hosts)
        destination = Path(str(job["destination"]))
        errors: list[str] = []

        for source_url in source_urls:
            temp_path = destination / f".vaultly-{uuid.uuid4().hex}.part"
            try:
                content_type = await self._download_source_with_retries(
                    page,
                    source_url,
                    str(post["post_url"]),
                    media_type,
                    temp_path,
                )
                if not is_valid_media_file(temp_path, media_type):
                    raise RuntimeError("下載後的影片容器不完整或沒有可播放畫面")

                extension = extension_for_media(source_url, media_type, content_type)
                filename = build_download_filename(str(account["handle"]), extension)
                file_path = self._unique_download_path(destination / filename)
                temp_path.replace(file_path)
                digest = self._sha256_file(file_path)
                self.repository.record_download(
                    dedupe_key,
                    platform,
                    str(account["account_id"]),
                    str(post["post_url"]),
                    source_url,
                    str(file_path),
                    digest,
                )
                return
            except Exception as exc:
                errors.append(str(exc))
                with contextlib.suppress(OSError):
                    temp_path.unlink()

        message = errors[-1] if errors else "沒有可下載的完整媒體來源"
        raise RuntimeError(f"媒體下載失敗：{message}")

    async def _download_source_with_retries(
        self,
        page: Any,
        source_url: str,
        post_url: str,
        media_type: str,
        temp_path: Path,
    ) -> str:
        errors: list[str] = []
        for attempt in range(1, self.DOWNLOAD_RETRY_ATTEMPTS + 1):
            try:
                return (
                    await self._download_hls_media(page, source_url, post_url, temp_path)
                    if self._is_hls_url(source_url)
                    else await self._download_direct_media(
                        page,
                        source_url,
                        post_url,
                        media_type,
                        temp_path,
                    )
                )
            except Exception as exc:
                errors.append(f"第 {attempt} 次 {self._short_error(exc)}")
                with contextlib.suppress(OSError):
                    temp_path.unlink()
                if (
                    attempt >= self.DOWNLOAD_RETRY_ATTEMPTS
                    or not self._is_retryable_download_error(exc)
                ):
                    break
                await asyncio.sleep(self.DOWNLOAD_RETRY_BACKOFF_SECONDS * attempt)
        raise RuntimeError("；".join(errors) if errors else "沒有可下載的完整媒體來源")

    async def _download_direct_media(
        self,
        page: Any,
        source_url: str,
        post_url: str,
        media_type: str,
        temp_path: Path,
    ) -> str:
        headers = {
            "Accept": "video/*,*/*;q=0.8" if media_type == "video" else "image/*,*/*;q=0.8",
            "Referer": post_url,
        }
        cookie_header = await self._browser_cookie_header(page, source_url)
        if cookie_header:
            headers["Cookie"] = cookie_header

        response = await page.context.request.get(
            source_url,
            headers=headers,
            timeout=60_000,
        )
        if not response.ok:
            raise RuntimeError(f"媒體下載失敗：HTTP {response.status}")
        if response.status == 206:
            raise RuntimeError("平台只回傳部分媒體內容")
        content_length = int(response.headers.get("content-length", "0") or 0)
        if content_length > self.MAX_MEDIA_BYTES:
            raise RuntimeError("單一媒體超過 150 MB 安全限制")
        content = await response.body()
        if len(content) > self.MAX_MEDIA_BYTES:
            raise RuntimeError("單一媒體超過 150 MB 安全限制")
        content_type = response.headers.get("content-type", "")
        if not is_valid_media_payload(content, media_type, content_type):
            raise RuntimeError("下載內容不是可用的完整媒體檔")
        temp_path.write_bytes(content)
        return content_type

    async def _download_hls_media(
        self,
        page: Any,
        source_url: str,
        post_url: str,
        temp_path: Path,
    ) -> str:
        request_headers = f"Referer: {post_url}\r\n"
        cookie_header = await self._browser_cookie_header(page, source_url)
        if cookie_header:
            request_headers += f"Cookie: {cookie_header}\r\n"
        evaluate_method = getattr(page, "evaluate", None)
        if callable(evaluate_method):
            with contextlib.suppress(Exception):
                user_agent = str(await evaluate_method("() => navigator.userAgent")).strip()
                if user_agent:
                    request_headers += f"User-Agent: {user_agent}\r\n"

        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-headers",
            request_headers,
            "-i",
            source_url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-fs",
            str(self.MAX_MEDIA_BYTES),
            "-f",
            "mp4",
            "-y",
            str(temp_path),
        ]
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                capture_output=True,
                timeout=300,
                creationflags=creation_flags,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("串流影片合併超過 5 分鐘") from exc
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(stderr[-500:] or "串流影片合併失敗")
        if not temp_path.is_file() or temp_path.stat().st_size <= 0:
            raise RuntimeError("串流影片合併後沒有產生檔案")
        if temp_path.stat().st_size >= self.MAX_MEDIA_BYTES:
            raise RuntimeError("單一媒體超過 150 MB 安全限制")
        return "video/mp4"

    @staticmethod
    async def _browser_cookie_header(page: Any, source_url: str) -> str:
        context = getattr(page, "context", None)
        cookies_method = getattr(context, "cookies", None)
        if not callable(cookies_method):
            return ""

        with contextlib.suppress(Exception):
            cookies = await cookies_method([source_url])
            return "; ".join(
                f"{cookie['name']}={cookie['value']}"
                for cookie in cookies
                if isinstance(cookie, dict) and cookie.get("name") and cookie.get("value")
            )
        return ""

    @staticmethod
    def _allowed_media_sources(
        media: dict[str, Any],
        allowed_hosts: tuple[str, ...],
    ) -> list[str]:
        raw_urls = [media.get("source_url", "")]
        fallback_urls = media.get("fallback_urls", [])
        if isinstance(fallback_urls, list):
            raw_urls.extend(fallback_urls)
        output: list[str] = []
        for raw_url in raw_urls:
            source_url = str(raw_url).strip()
            if (
                source_url
                and source_url not in output
                and is_allowed_media_url(source_url, allowed_hosts)
            ):
                output.append(source_url)
        return output

    @staticmethod
    def _is_hls_url(source_url: str) -> bool:
        return source_url.split("?", 1)[0].casefold().endswith(".m3u8")

    @staticmethod
    def _is_retryable_download_error(error: BaseException) -> bool:
        if isinstance(error, (TimeoutError, OSError)):
            return True
        message = str(error)
        if re.search(r"HTTP\s+(429|5\d\d)", message):
            return True
        retry_markers = (
            "timeout",
            "timed out",
            "temporarily unavailable",
            "connection reset",
            "connection aborted",
        )
        folded = message.casefold()
        return any(marker in folded for marker in retry_markers)

    @staticmethod
    def _unique_download_path(file_path: Path) -> Path:
        if not file_path.exists():
            return file_path
        for index in range(1, 1000):
            candidate = file_path.with_name(
                f"{file_path.stem}-{index}{file_path.suffix}"
            )
            if not candidate.exists():
                return candidate
        return file_path.with_name(
            f"{file_path.stem}-{uuid.uuid4().hex[:8]}{file_path.suffix}"
        )

    @staticmethod
    def _sha256_file(file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _has_valid_download(self, dedupe_key: str, media_type: str) -> bool:
        record = self.repository.get_download(dedupe_key)
        if record is None:
            return False
        return is_valid_media_file(Path(str(record["file_path"])), media_type)

    @staticmethod
    def _dedupe_key(
        account: dict[str, Any],
        post: dict[str, Any],
        media_type: str,
        media_index: int,
    ) -> str:
        raw = "|".join(
            [
                str(account["platform"]),
                str(account["account_id"]),
                str(post["post_url"]),
                media_type,
                str(media_index),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
