from __future__ import annotations

import json
import os
import subprocess
import uuid
from typing import Any

from ..infrastructure.analytics_repository import InvestmentAnalyticsStore, utc_text
from ..infrastructure.privacy import protect_text, unprotect_text
from .automation_common import _background_subprocess_kwargs, _json


class NotificationManager:
    DEFAULT_CHANNELS = (
        ("mobile_outbox", "mobile", "行動裝置通知佇列", True),
        ("windows_local", "windows", "Windows 本機通知", False),
        ("email_smtp", "email", "電子郵件", False),
        ("teams_webhook", "teams", "Microsoft Teams", False),
        ("slack_webhook", "slack", "Slack", False),
    )

    def __init__(self, store: InvestmentAnalyticsStore) -> None:
        self.store = store
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        now = utc_text()
        with self.store.connect() as connection:
            for channel_id, channel_type, name, enabled in self.DEFAULT_CHANNELS:
                connection.execute(
                    """
                    INSERT INTO notification_channels(
                        channel_id, channel_type, name, enabled, config_encrypted, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id) DO NOTHING
                    """,
                    (channel_id, channel_type, name, int(enabled), protect_text("{}"), now, now),
                )

    def list_channels(self) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute("SELECT channel_id, channel_type, name, enabled, config_encrypted, created_at, updated_at FROM notification_channels ORDER BY channel_id").fetchall()
        output = []
        for source in rows:
            item = dict(source)
            config = json.loads(unprotect_text(item.pop("config_encrypted")) or "{}")
            item["enabled"] = bool(item["enabled"])
            item["consent_confirmed"] = bool(config.get("consent_confirmed"))
            item["configured"] = bool({key: value for key, value in config.items() if key != "consent_confirmed" and value})
            output.append(item)
        return output

    def configure(self, channel_id: str, payload: dict[str, Any], *, confirmed: bool) -> dict[str, Any]:
        with self.store.connect() as connection:
            row = connection.execute("SELECT channel_id, channel_type, name, enabled, config_encrypted, created_at, updated_at FROM notification_channels WHERE channel_id=?", (channel_id,)).fetchone()
        if not row:
            raise ValueError("找不到通知管道")
        enabled = bool(payload.get("enabled"))
        if enabled and not confirmed:
            raise ValueError("啟用外部通知前必須明確確認")
        config = dict(payload.get("config") or {})
        config["consent_confirmed"] = bool(confirmed and enabled)
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE notification_channels SET enabled=?, config_encrypted=?, updated_at=? WHERE channel_id=?",
                (int(enabled), protect_text(_json(config)), utc_text(), channel_id),
            )
        self.store.audit("notification_channel_configured", {"channel_id": channel_id, "enabled": enabled})
        return next(item for item in self.list_channels() if item["channel_id"] == channel_id)

    def queue(self, title: str, body: str, *, severity: str = "info", channel_id: str = "") -> list[str]:
        channels = self.list_channels()
        selected = [item for item in channels if item["enabled"] and (not channel_id or item["channel_id"] == channel_id)]
        identifiers = []
        with self.store.connect() as connection:
            for channel in selected:
                notification_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO notification_outbox VALUES(?, ?, ?, ?, ?, ?, 'pending', '', '')",
                    (notification_id, utc_text(), channel["channel_id"], severity, title[:240], protect_text(body)),
                )
                identifiers.append(notification_id)
        return identifiers

    def outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT notification_id, created_at, channel_id, severity, title, body_encrypted, status, sent_at, error_encrypted FROM notification_outbox ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        output = []
        for source in rows:
            item = dict(source)
            item["body"] = unprotect_text(item.pop("body_encrypted"))
            item["error"] = unprotect_text(item.pop("error_encrypted"))
            output.append(item)
        return output

    def _channel(self, channel_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.store.connect() as connection:
            row = connection.execute("SELECT channel_id, channel_type, name, enabled, config_encrypted, created_at, updated_at FROM notification_channels WHERE channel_id=?", (channel_id,)).fetchone()
        if not row:
            raise ValueError("找不到通知管道")
        channel = dict(row)
        config = json.loads(unprotect_text(channel.pop("config_encrypted")) or "{}")
        return channel, config

    def _send(self, channel: dict[str, Any], config: dict[str, Any], title: str, body: str) -> None:
        channel_type = channel["channel_type"]
        if channel_type == "mobile":
            return
        if not config.get("consent_confirmed"):
            raise ValueError("通知管道尚未取得明確同意")
        if channel_type == "windows":
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$n=New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon=[System.Drawing.SystemIcons]::Information; $n.Visible=$true; "
                "$n.ShowBalloonTip(8000,$env:GPTBRIDGE_NOTICE_TITLE,$env:GPTBRIDGE_NOTICE_BODY,[System.Windows.Forms.ToolTipIcon]::Info); "
                "Start-Sleep -Seconds 9; $n.Dispose()"
            )
            environment = dict(os.environ)
            environment["GPTBRIDGE_NOTICE_TITLE"] = title[:240]
            environment["GPTBRIDGE_NOTICE_BODY"] = body[:2000]
            subprocess.run(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                check=True,
                timeout=15,
                env=environment,
                **_background_subprocess_kwargs(),
            )
            return
        if channel_type == "email":
            raise PermissionError(
                "AI investment manager cannot send email directly; route external delivery through Xingcheng."
            )
        if channel_type in {"teams", "slack"}:
            raise PermissionError(
                "AI investment manager cannot call webhooks directly; route external delivery through Xingcheng."
            )
        raise ValueError("不支援的通知管道")

    def dispatch(self, *, limit: int = 20) -> dict[str, int]:
        with self.store.connect() as connection:
            pending_rows = connection.execute(
                "SELECT notification_id, created_at, channel_id, severity, title, body_encrypted, status, sent_at, error_encrypted FROM notification_outbox WHERE status = 'pending' ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        pending = []
        for source in pending_rows:
            item = dict(source)
            item["body"] = unprotect_text(item.pop("body_encrypted"))
            item["error"] = unprotect_text(item.pop("error_encrypted"))
            pending.append(item)
        sent = failed = 0
        for item in pending:
            try:
                channel, config = self._channel(item["channel_id"])
                if not channel["enabled"]:
                    raise ValueError("通知管道已停用")
                self._send(channel, config, item["title"], item["body"])
                status, error = "available" if channel["channel_type"] == "mobile" else "sent", ""
                sent += 1
            except Exception as exc:
                status, error = "failed", str(exc)
                failed += 1
            with self.store.connect() as connection:
                connection.execute(
                    "UPDATE notification_outbox SET status=?, sent_at=?, error_encrypted=? WHERE notification_id=?",
                    (status, utc_text(), protect_text(error), item["notification_id"]),
                )
        return {"processed": len(pending), "sent": sent, "failed": failed}
