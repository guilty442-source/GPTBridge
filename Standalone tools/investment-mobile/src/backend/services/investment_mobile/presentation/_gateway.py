from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..domain.platform_contract import mobile_platform_contract
from ._constants import (
    DEFAULT_PAIRING_TTL_HOURS,
    DEFAULT_PORT,
    MAX_REQUESTS_PER_WINDOW,
    PAIRING_ALPHABET,
    REQUEST_WINDOW_SECONDS,
    SESSION_IDLE_SECONDS,
    SESSION_TTL_HOURS,
    CommandScheduler,
    RemoteUrlProvider,
    SnapshotProvider,
)
from ._templates import _mobile_html
from ._utils import _json_bytes, local_ipv4_addresses, normalize_remote_url


class MobileSyncGateway:
    def __init__(
        self,
        tool_root: Path,
        snapshot_provider: SnapshotProvider,
        command_scheduler: CommandScheduler,
        remote_url_provider: RemoteUrlProvider,
    ) -> None:
        self.tool_root = tool_root
        self.snapshot_provider = snapshot_provider
        self.command_scheduler = command_scheduler
        self.remote_url_provider = remote_url_provider
        self.pairing_ttl_hours = DEFAULT_PAIRING_TTL_HOURS
        self.pairing_code = self._new_pairing_code()
        self.pairing_created_at = datetime.now(timezone.utc)
        self.pairing_expires_at = self.pairing_created_at + timedelta(
            hours=self.pairing_ttl_hours
        )
        self.pairing_revoked_at: datetime | None = None
        self._request_log: dict[str, list[float]] = {}
        self._sessions: dict[str, dict[str, Any]] = {}
        self._security_lock = threading.Lock()
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0
        self.bind_host = "127.0.0.1"

    def start(self, port: int | None = None, *, allow_lan: bool = False) -> None:
        requested_host = "0.0.0.0" if allow_lan else "127.0.0.1"
        if self.httpd is not None and self.bind_host == requested_host:
            return
        if self.httpd is not None:
            self.shutdown()
        self.bind_host = requested_host
        selected_port = DEFAULT_PORT if port is None else max(0, int(port))
        candidates = [selected_port] if selected_port == 0 else list(range(selected_port, selected_port + 20))
        last_error: OSError | None = None
        handler = self._handler_class()
        for candidate in candidates:
            try:
                self.httpd = ThreadingHTTPServer((self.bind_host, candidate), handler)
                self.port = int(self.httpd.server_address[1])
                break
            except OSError as exc:
                last_error = exc
        if self.httpd is None:
            raise OSError(f"手機同步服務啟動失敗：{last_error}")
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="AIInvestmentMobileSync",
            daemon=True,
        )
        self.thread.start()

    def shutdown(self) -> None:
        if self.httpd is None:
            return
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.thread = None

    def rotate_pairing_code(self) -> str:
        self.pairing_code = self._new_pairing_code()
        self.pairing_created_at = datetime.now(timezone.utc)
        self.pairing_expires_at = self.pairing_created_at + timedelta(
            hours=self.pairing_ttl_hours
        )
        self.pairing_revoked_at = None
        return self.pairing_code

    def revoke_pairing_code(self) -> None:
        self.pairing_revoked_at = datetime.now(timezone.utc)
        with self._security_lock:
            self._sessions.clear()

    def set_pairing_ttl_hours(self, hours: float) -> None:
        self.pairing_ttl_hours = max(0.25, min(24.0, float(hours)))
        self.pairing_expires_at = self.pairing_created_at + timedelta(
            hours=self.pairing_ttl_hours
        )

    def pairing_active(self) -> bool:
        return self.pairing_revoked_at is None and datetime.now(timezone.utc) < self.pairing_expires_at

    def _rate_limit_allows(self, remote_address: str) -> bool:
        now = time.monotonic()
        address = remote_address or "unknown"
        with self._security_lock:
            attempts = [
                stamp
                for stamp in self._request_log.get(address, [])
                if now - stamp < REQUEST_WINDOW_SECONDS
            ]
            if len(attempts) >= MAX_REQUESTS_PER_WINDOW:
                self._request_log[address] = attempts
                return False
            attempts.append(now)
            self._request_log[address] = attempts
            if len(self._request_log) > 512:
                self._request_log = {
                    key: value
                    for key, value in self._request_log.items()
                    if value and now - value[-1] < REQUEST_WINDOW_SECONDS
                }
        return True

    def pair(
        self,
        supplied: str,
        remote_address: str,
        *,
        client_platform: str = "",
        device_name: str = "",
    ) -> str:
        if not self._rate_limit_allows(remote_address):
            return ""
        if not (
            self.pairing_active()
            and bool(supplied)
            and secrets.compare_digest(supplied, self.pairing_code)
        ):
            return ""
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._security_lock:
            self._sessions[token] = {
                "created_at": now,
                "last_used_at": now,
                "expires_at": now + SESSION_TTL_HOURS * 3600,
                "remote_address": remote_address or "unknown",
                "client_platform": client_platform[:32],
                "device_name": device_name[:80],
            }
        # A desktop pairing code is one-time. Existing device sessions continue
        # until explicitly revoked or their idle/absolute expiry is reached.
        self.pairing_revoked_at = datetime.now(timezone.utc)
        return token

    def authorize_session(self, supplied: str, remote_address: str) -> bool:
        if not supplied or not self._rate_limit_allows(remote_address):
            return False
        now = time.monotonic()
        with self._security_lock:
            session = self._sessions.get(supplied)
            if not session:
                return False
            expired = now >= float(session.get("expires_at") or 0)
            idle = now - float(session.get("last_used_at") or 0) >= SESSION_IDLE_SECONDS
            wrong_device = str(session.get("remote_address") or "") != (
                remote_address or "unknown"
            )
            if expired or idle or wrong_device:
                self._sessions.pop(supplied, None)
                return False
            session["last_used_at"] = now
        return True

    def status(self, *, expose_pairing_code: bool = True) -> dict[str, Any]:
        running = self.httpd is not None and self.port > 0
        pairing_active = self.pairing_active()
        remote_base_url = ""
        remote_url = ""
        try:
            remote_base_url = normalize_remote_url(self.remote_url_provider())
        except ValueError:
            remote_base_url = ""
        if running and remote_base_url:
            remote_url = remote_base_url
        local_urls = (
            [
                f"http://{ip}:{self.port}"
                for ip in local_ipv4_addresses()
            ]
            if running and self.bind_host == "0.0.0.0"
            else []
        )
        loopback_url = (
            f"http://127.0.0.1:{self.port}"
            if running
            else ""
        )
        status = {
            "enabled": running,
            "running": running,
            "mode": (
                "remote_bridge"
                if remote_url
                else "local_network"
                if self.bind_host == "0.0.0.0"
                else "loopback"
            ),
            "mode_label": (
                "不同網路橋接"
                if remote_url
                else "本機/區網"
                if self.bind_host == "0.0.0.0"
                else "僅限本機"
            ),
            "remote_ready": bool(remote_url),
            "remote_status_label": "不同網路已啟用" if remote_url else "不同網路未設定",
            "relay_required": not bool(remote_url),
            "port": self.port,
            "bind_host": self.bind_host,
            "loopback_url": loopback_url,
            "local_urls": local_urls,
            "remote_base_url": remote_base_url,
            "remote_url": remote_url,
            "access_scope": "read_state_and_queue_xingcheng_command",
            "platform": mobile_platform_contract(),
            "session_count": len(self._sessions),
            "session_idle_minutes": int(SESSION_IDLE_SECONDS / 60),
            "pairing_active": pairing_active,
            "pairing_expired": datetime.now(timezone.utc) >= self.pairing_expires_at,
            "pairing_revoked": self.pairing_revoked_at is not None,
            "pairing_created_at": self.pairing_created_at.isoformat(),
            "pairing_expires_at": self.pairing_expires_at.isoformat(),
            "pairing_revoked_at": self.pairing_revoked_at.isoformat() if self.pairing_revoked_at else "",
            "pairing_ttl_hours": self.pairing_ttl_hours,
            "rate_limit": f"{MAX_REQUESTS_PER_WINDOW}/{int(REQUEST_WINDOW_SECONDS)}s per address",
        }
        if expose_pairing_code:
            status["pairing_code"] = self.pairing_code if pairing_active else ""
        return status

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def do_OPTIONS(self) -> None:
                self._send_empty(204)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path in {"", "/"}:
                    self._send_bytes(200, _mobile_html(), "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/platform":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "platform": mobile_platform_contract(),
                        },
                    )
                    return
                if parsed.path == "/api/state":
                    if not self._authorized():
                        self._send_json(403, {"ok": False, "message": "工作階段無效或已過期"})
                        return
                    try:
                        payload = gateway.snapshot_provider()
                        self._send_json(200, payload)
                    except Exception as exc:
                        self._send_json(500, {"ok": False, "message": str(exc)})
                    return
                self._send_json(404, {"ok": False, "message": "not found"})

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/api/pair":
                    payload = self._read_json()
                    session_token = gateway.pair(
                        str(payload.get("code") or "").strip().upper(),
                        str(self.client_address[0]),
                        client_platform=str(
                            payload.get("client_platform") or ""
                        ).strip(),
                        device_name=str(payload.get("device_name") or "").strip(),
                    )
                    if not session_token:
                        self._send_json(403, {"ok": False, "message": "配對碼不正確或已使用"})
                        return
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "session_token": session_token,
                            "expires_in_hours": SESSION_TTL_HOURS,
                            "idle_timeout_minutes": int(SESSION_IDLE_SECONDS / 60),
                            "platform": mobile_platform_contract(),
                        },
                    )
                    return
                if parsed.path != "/api/xingcheng-command":
                    self._send_json(404, {"ok": False, "message": "not found"})
                    return
                payload = self._read_json()
                if not self._authorized():
                    self._send_json(403, {"ok": False, "message": "工作階段無效或已過期"})
                    return
                instruction = str(payload.get("instruction") or "").strip()
                if not instruction:
                    self._send_json(400, {"ok": False, "message": "請輸入本地 AI 命令"})
                    return
                try:
                    result = gateway.command_scheduler(instruction)
                    self._send_json(200, result)
                except Exception as exc:
                    self._send_json(500, {"ok": False, "message": str(exc)})

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _authorized(self) -> bool:
                authorization = str(self.headers.get("Authorization") or "")
                supplied = (
                    authorization[7:].strip()
                    if authorization.casefold().startswith("bearer ")
                    else str(self.headers.get("X-GPTBridge-Mobile-Session") or "")
                )
                return gateway.authorize_session(
                    supplied,
                    str(self.client_address[0]),
                )

            def _read_json(self) -> dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length") or "0")
                except ValueError:
                    length = 0
                if length <= 0 or length > 32768:
                    return {}
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return {}
                return payload if isinstance(payload, dict) else {}

            def _send_empty(self, status: int) -> None:
                self.send_response(status)
                self._headers("text/plain; charset=utf-8")
                self.end_headers()

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                self._send_bytes(status, _json_bytes(payload), "application/json; charset=utf-8")

            def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self._headers(content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _headers(self, content_type: str) -> None:
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Pragma", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                )

        return Handler

    @staticmethod
    def _new_pairing_code() -> str:
        return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))
