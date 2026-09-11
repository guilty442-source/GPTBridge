from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import uuid
from datetime import timedelta
from typing import Any, Awaitable, Callable

from ..infrastructure.analytics_repository import InvestmentAnalyticsStore, parse_datetime, utc_now, utc_text
from ..infrastructure.privacy import protect_text, unprotect_text


def _background_subprocess_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


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
            rows = connection.execute("SELECT * FROM notification_channels ORDER BY channel_id").fetchall()
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
            row = connection.execute("SELECT * FROM notification_channels WHERE channel_id=?", (channel_id,)).fetchone()
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


class ModelGovernance:
    MINIMUM_CALIBRATION_SAMPLES = 30
    MINIMUM_SLICE_SAMPLES = 20
    BRIER_READ_ONLY_THRESHOLD = 0.35
    SLICE_BRIER_READ_ONLY_THRESHOLD = 0.40

    def __init__(self, store: InvestmentAnalyticsStore) -> None:
        self.store = store

    def register(self, model_name: str, version: str, *, prompt: str = "", config: dict[str, Any] | None = None) -> str:
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT model_version_id FROM model_versions WHERE model_name=? AND version=? AND prompt_hash=?",
                (model_name, version, prompt_hash),
            ).fetchone()
            if row:
                return str(row["model_version_id"])
            model_version_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO model_versions VALUES(?, ?, ?, ?, ?, 'active', ?)",
                (model_version_id, model_name, version, prompt_hash, utc_text(), protect_text(_json(config or {}))),
            )
        return model_version_id

    def is_read_only(self, model_name: str) -> bool:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM model_versions WHERE model_name=? AND status='read_only' LIMIT 1",
                (model_name,),
            ).fetchone()
        return bool(row)

    def record_run(
        self,
        *,
        model_name: str,
        version: str,
        inputs: Any,
        outputs: Any,
        data_sources: list[str] | None = None,
        metrics: dict[str, Any] | None = None,
        prompt: str = "",
        analysis_run_id: str = "",
        decision_count: int = 0,
    ) -> str:
        model_id = self.register(model_name, version, prompt=prompt)
        run_id = uuid.uuid4().hex
        normalized_metrics = self._normalized_metrics(metrics)
        calibration = normalized_metrics.get("calibration")
        if isinstance(calibration, dict) and calibration.get("sample_version"):
            sample_version = str(calibration["sample_version"])
            if self._calibration_sample_recorded(model_id, sample_version):
                normalized_metrics.pop("calibration", None)
                normalized_metrics["calibration_reference"] = sample_version
                normalized_metrics["calibration_duplicate"] = True
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO model_governance_runs VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, model_id, analysis_run_id, utc_text(), _hash(inputs), _hash(outputs),
                    protect_text(_json(data_sources or [])), protect_text(_json(normalized_metrics)), int(decision_count),
                ),
            )
        return run_id

    def _normalized_metrics(self, metrics: dict[str, Any] | None) -> dict[str, Any]:
        output = dict(metrics or {})
        if isinstance(output.get("calibration"), dict):
            source = dict(output["calibration"])
        elif output.get("brier_score") is not None:
            source = {
                key: output.pop(key)
                for key in (
                    "status",
                    "sample_version",
                    "evaluated_count",
                    "brier_score",
                    "accuracy_percent",
                    "reliability_gap_percent",
                    "slices",
                )
                if key in output
            }
        else:
            source = self.store.calibration()
        evaluated_count = max(0, int(source.get("evaluated_count") or 0))
        brier = source.get("brier_score")
        sample_version = str(
            source.get("sample_version")
            or source.get("evaluation_sample_id")
            or ""
        )
        if not sample_version and evaluated_count and brier is not None:
            sample_version = _hash(
                {
                    "evaluated_count": evaluated_count,
                    "brier_score": brier,
                    "accuracy_percent": source.get("accuracy_percent"),
                    "slices": source.get("slices") or [],
                }
            )[:24]
        output["calibration"] = {
            "status": str(source.get("status") or ("ready" if brier is not None else "collecting")),
            "sample_version": sample_version,
            "evaluated_count": evaluated_count,
            "brier_score": float(brier) if brier is not None else None,
            "accuracy_percent": source.get("accuracy_percent"),
            "reliability_gap_percent": source.get("reliability_gap_percent"),
            "slices": [
                dict(item)
                for item in source.get("slices", [])
                if isinstance(item, dict)
            ],
        }
        return output

    def _calibration_sample_recorded(
        self,
        model_version_id: str,
        sample_version: str,
    ) -> bool:
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT metrics_encrypted
                FROM model_governance_runs
                WHERE model_version_id=?
                ORDER BY created_at DESC LIMIT 500
                """,
                (model_version_id,),
            ).fetchall()
        for row in rows:
            try:
                metrics = json.loads(
                    unprotect_text(str(row["metrics_encrypted"] or "")) or "{}"
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            calibration = metrics.get("calibration")
            if (
                isinstance(calibration, dict)
                and str(calibration.get("sample_version") or "") == sample_version
            ):
                return True
        return False

    def dashboard(self) -> dict[str, Any]:
        with self.store.connect() as connection:
            versions = connection.execute("SELECT model_version_id, model_name, version, prompt_hash, registered_at, status, config_encrypted FROM model_versions ORDER BY registered_at DESC LIMIT 500").fetchall()
            runs = connection.execute(
                """
                SELECT r.*, v.model_name, v.version, v.status AS model_status
                FROM model_governance_runs r JOIN model_versions v ON v.model_version_id=r.model_version_id
                ORDER BY r.created_at DESC LIMIT 200
                """
            ).fetchall()
        run_items = []
        brier_scores: list[float] = []
        calibration_samples: list[dict[str, Any]] = []
        seen_samples: set[tuple[str, str]] = set()
        for source in runs:
            item = dict(source)
            item["data_sources"] = json.loads(unprotect_text(item.pop("data_sources_encrypted")) or "[]")
            item["metrics"] = json.loads(unprotect_text(item.pop("metrics_encrypted")) or "{}")
            calibration = item["metrics"].get("calibration")
            if not isinstance(calibration, dict) and item["metrics"].get("brier_score") is not None:
                calibration = item["metrics"]
            if isinstance(calibration, dict) and calibration.get("brier_score") is not None:
                sample_version = str(
                    calibration.get("sample_version")
                    or item["governance_run_id"]
                )
                key = (str(item["model_version_id"]), sample_version)
                if key not in seen_samples:
                    seen_samples.add(key)
                    sample = {
                        "model_version_id": str(item["model_version_id"]),
                        "sample_version": sample_version,
                        "evaluated_count": max(
                            0, int(calibration.get("evaluated_count") or 0)
                        ),
                        "brier_score": float(calibration["brier_score"]),
                        "slices": [
                            dict(value)
                            for value in calibration.get("slices", [])
                            if isinstance(value, dict)
                        ],
                    }
                    calibration_samples.append(sample)
                    if sample["evaluated_count"] >= self.MINIMUM_CALIBRATION_SAMPLES:
                        brier_scores.append(sample["brier_score"])
            run_items.append(item)
        average_brier = sum(brier_scores) / len(brier_scores) if brier_scores else None
        affected: set[str] = set()
        for sample in calibration_samples:
            overall_bad = (
                sample["evaluated_count"] >= self.MINIMUM_CALIBRATION_SAMPLES
                and sample["brier_score"] > self.BRIER_READ_ONLY_THRESHOLD
            )
            slice_bad = any(
                str(item.get("dimension") or "") in {"market", "asset_type"}
                and int(item.get("evaluated_count") or 0) >= self.MINIMUM_SLICE_SAMPLES
                and float(item.get("brier_score") or 0)
                > self.SLICE_BRIER_READ_ONLY_THRESHOLD
                for item in sample["slices"]
            )
            if overall_bad or slice_bad:
                affected.add(sample["model_version_id"])
        degraded = bool(affected)
        if affected:
            with self.store.connect() as connection:
                connection.executemany("UPDATE model_versions SET status='read_only' WHERE model_version_id=?", [(value,) for value in affected])
            version_items = [
                dict(row) | {"config_encrypted": "", "status": "read_only" if row["model_version_id"] in affected else row["status"]}
                for row in versions
            ]
        else:
            version_items = [dict(row) | {"config_encrypted": ""} for row in versions]
        any_read_only = any(item.get("status") == "read_only" for item in version_items)
        return {
            "status": "read_only" if degraded or any_read_only else "ready",
            "version_count": len(versions),
            "run_count": len(runs),
            "evaluated_run_count": len(brier_scores),
            "calibration_sample_count": len(calibration_samples),
            "average_brier_score": average_brier,
            "guardrail": (
                "read-only when a distinct calibration sample has at least "
                f"{self.MINIMUM_CALIBRATION_SAMPLES} outcomes and Brier score exceeds "
                f"{self.BRIER_READ_ONLY_THRESHOLD}, or a market/asset slice has at least "
                f"{self.MINIMUM_SLICE_SAMPLES} outcomes and exceeds "
                f"{self.SLICE_BRIER_READ_ONLY_THRESHOLD}"
            ),
            "metrics_schema": {
                "calibration": {
                    "sample_version": "stable hash/version of the evaluated decision set",
                    "evaluation_sample_id": "accepted alias for sample_version",
                    "evaluated_count": "integer",
                    "brier_score": "0..1",
                    "accuracy_percent": "0..100 or null",
                    "reliability_gap_percent": "percentage points or null",
                    "slices": [
                        {
                            "dimension": "market|asset_type|direction",
                            "key": "slice identifier",
                            "evaluated_count": "integer",
                            "brier_score": "0..1",
                        }
                    ],
                }
            },
            "versions": version_items,
            "runs": run_items[:50],
        }


class InvestmentAutomation:
    def __init__(
        self,
        store: InvestmentAnalyticsStore,
        notifications: NotificationManager,
        cycle_callback: Callable[[], Awaitable[dict[str, Any]]],
        backup_callback: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.notifications = notifications
        self.cycle_callback = cycle_callback
        self.backup_callback = backup_callback
        self._task: asyncio.Task[Any] | None = None
        self._stopping = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._next_delay_seconds = self.interval_seconds
        self._recover_interrupted_runs()

    def _recover_interrupted_runs(self) -> int:
        recovered = 0
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT run_id, started_at FROM scheduler_runs WHERE status='running'"
            ).fetchall()
            for row in rows:
                detail = {
                    "error": "scheduler process stopped before the run completed",
                    "recovered_at": utc_text(),
                    "previous_started_at": str(row["started_at"]),
                    "retry_policy": "retry_on_next_scheduler_cycle",
                }
                connection.execute(
                    """
                    UPDATE scheduler_runs
                    SET finished_at=?, status='interrupted', detail_encrypted=?
                    WHERE run_id=?
                    """,
                    (utc_text(), protect_text(_json(detail)), row["run_id"]),
                )
                recovered += 1
        return recovered

    @property
    def interval_seconds(self) -> int:
        return max(60, min(86400, int(self.store.get_setting("scheduler_interval_seconds", 900) or 900)))

    def configure(self, interval_seconds: int) -> dict[str, Any]:
        interval = max(60, min(86400, int(interval_seconds)))
        self.store.set_setting("scheduler_interval_seconds", interval)
        self.store.audit("scheduler_configured", {"interval_seconds": interval})
        return self.status()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._recover_interrupted_runs()
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="investment-background-scheduler")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _loop(self) -> None:
        delay = self.interval_seconds
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            except asyncio.TimeoutError:
                try:
                    result = await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    result = {"status": "failed", "detail": {"error": str(exc)}}
                if result.get("status") == "failed":
                    self._consecutive_failures += 1
                    delay = min(
                        self.interval_seconds,
                        max(60, 60 * (2 ** min(6, self._consecutive_failures - 1))),
                    )
                else:
                    self._consecutive_failures = 0
                    delay = self.interval_seconds
                self._next_delay_seconds = delay

    async def run_once(self) -> dict[str, Any]:
        async with self._run_lock:
            run_id = uuid.uuid4().hex
            started = utc_text()
            with self.store.connect() as connection:
                connection.execute("INSERT INTO scheduler_runs VALUES(?, 'investment_intelligence', ?, '', 'running', '')", (run_id, started))
            try:
                detail = await self.cycle_callback()
                status = "completed"
            except Exception as exc:
                detail = {"error": str(exc)}
                status = "failed"
                try:
                    self.notifications.queue("投資管家排程失敗", str(exc), severity="warning")
                except Exception as notification_error:
                    detail["notification_queue_error"] = str(notification_error)
            try:
                notification_result = self.notifications.dispatch()
                if isinstance(detail, dict):
                    detail["notification_dispatch"] = notification_result
            except Exception as exc:
                if isinstance(detail, dict):
                    detail["notification_dispatch_error"] = str(exc)
                if status == "completed":
                    status = "completed_with_warnings"
            try:
                self._daily_backup()
            except Exception as exc:
                if isinstance(detail, dict):
                    detail["backup_error"] = str(exc)
                if status == "completed":
                    status = "completed_with_warnings"
            with self.store.connect() as connection:
                connection.execute(
                    "UPDATE scheduler_runs SET finished_at=?, status=?, detail_encrypted=? WHERE run_id=?",
                    (utc_text(), status, protect_text(_json(detail)), run_id),
                )
            self._prune_scheduler_runs()
            return {"run_id": run_id, "started_at": started, "status": status, "detail": detail}

    def _daily_backup(self) -> None:
        latest = self.store.get_setting("last_automatic_backup", "")
        parsed = parse_datetime(latest)
        if parsed and utc_now() - parsed < timedelta(hours=20):
            return
        if self.backup_callback is None:
            self.store.backup_database("automatic")
        else:
            self.backup_callback("automatic")
        self.store.set_setting("last_automatic_backup", utc_text())

    def _prune_scheduler_runs(self) -> int:
        """Retain every run; older rows form a logical append-only archive."""

        keep = max(
            20,
            min(
                2000,
                int(self.store.get_setting("scheduler_run_retention_count", 200) or 200),
            ),
        )
        with self.store.connect() as connection:
            total_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM scheduler_runs"
                ).fetchone()[0]
            )
        logical_archive_count = max(0, total_count - keep)
        pressure_threshold = max(5000, keep * 5)
        self.store.set_setting(
            "scheduler_run_storage",
            {
                "policy": "append_only_logical_archive",
                "active_window_count": min(total_count, keep),
                "archive_count": logical_archive_count,
                "total_count": total_count,
                "storage_pressure": total_count >= pressure_threshold,
                "pressure_threshold": pressure_threshold,
                "checked_at": utc_text(),
                "automatic_delete": False,
            },
        )
        return 0

    def status(self) -> dict[str, Any]:
        with self.store.connect() as connection:
            rows = connection.execute("SELECT * FROM scheduler_runs ORDER BY started_at DESC LIMIT 20").fetchall()
        runs = []
        for source in rows:
            item = dict(source)
            item["detail"] = json.loads(unprotect_text(item.pop("detail_encrypted")) or "{}")
            runs.append(item)
        return {
            "running": bool(self._task and not self._task.done()),
            "interval_seconds": self.interval_seconds,
            "consecutive_failures": self._consecutive_failures,
            "next_delay_seconds": self._next_delay_seconds,
            "retry_policy": "bounded exponential retry after failed scheduled cycles",
            "storage": self.store.get_setting(
                "scheduler_run_storage",
                {
                    "policy": "append_only_logical_archive",
                    "automatic_delete": False,
                },
            ),
            "last_run": runs[0] if runs else None,
            "runs": runs,
        }
