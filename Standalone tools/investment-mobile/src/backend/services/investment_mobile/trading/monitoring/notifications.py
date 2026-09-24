"""InvestmentNotificationService — dedup, cooldown, merge, history.

Channels: market | signal | fund | risk | allocation | system.

Anti-spam contract:

- Same event fingerprint within ``cooldown_s`` → the existing
  notification is updated (occurrences++, latest detail), no new row.
- State change (severity up/down, cleared) is a NEW fingerprint bucket
  → new notification allowed.
- Notifications never execute anything — clicking one cannot create a
  LIVE order; trading still needs the governed authorization pipeline.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

CHANNELS = frozenset({
    "market", "signal", "fund", "risk", "allocation", "system"})


class InvestmentNotificationService:
    def __init__(self, state_dir: Path,
                 cooldown_s: float = 3600.0) -> None:
        self._dir = state_dir / "monitoring"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "notifications.jsonl"
        self._fh = open(self._path, "a", encoding="utf-8")
        self._cooldown = cooldown_s
        self._notes: dict[str, dict[str, Any]] = {}   # key → row
        self._replay()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def _replay(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text(
                encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("op") == "update":
                cur = self._notes.get(e.get("key"))
                if cur is not None:
                    cur["occurrences"] += 1
                    cur["last_at"] = e.get("at", cur["last_at"])
                    cur["detail"] = e.get("detail", cur["detail"])
                continue
            self._notes[e["key"]] = e

    # ------------------------------------------------------------------
    def notify(self, channel: str, event: dict[str, Any], *,
               title: str = "", body: str = "",
               cooldown_s: float | None = None) -> dict[str, Any]:
        if channel not in CHANNELS:
            return {"ok": False, "error_code": "CHANNEL_UNKNOWN",
                    "channels": sorted(CHANNELS)}
        now = time.time()
        cd = cooldown_s if cooldown_s is not None else self._cooldown
        window = int(now // cd) if cd > 0 else int(now * 1000)
        key = f"{channel}|{event.get('event_type')}|" \
              f"{event.get('instrument_id')}|{event.get('account_id')}|" \
              f"{event.get('severity')}|{window}"
        existing = self._notes.get(key)
        if existing is not None and now - existing["last_at"] < cd:
            # merge into the existing notification — update, not spam
            upd = {"op": "update", "key": key, "at": now,
                   "detail": dict(event.get("detail") or {})}
            self._fh.write(json.dumps(upd, ensure_ascii=False) + "\n")
            self._fh.flush()
            existing["occurrences"] += 1
            existing["last_at"] = now
            return {"ok": True, "notification": dict(existing),
                    "merged": True}
        note = {
            "notification_id": f"ntf-{uuid.uuid4().hex[:10]}",
            "key": key, "channel": channel,
            "event_id": event.get("event_id", ""),
            "severity": event.get("severity", "INFO"),
            "title": title or event.get("event_type", ""),
            "body": body,
            "detail": dict(event.get("detail") or {}),
            "created_at": now, "last_at": now, "occurrences": 1,
            "status": "unread",
        }
        self._notes[key] = note
        self._fh.write(json.dumps(note, ensure_ascii=False) + "\n")
        self._fh.flush()
        return {"ok": True, "notification": dict(note), "merged": False}

    def mark_read(self, notification_id: str) -> dict[str, Any]:
        for n in self._notes.values():
            if n["notification_id"] == notification_id:
                n["status"] = "read"
                return {"ok": True}
        return {"ok": False, "error_code": "NOTIFICATION_NOT_FOUND"}

    def history(self, *, channel: str | None = None,
                severity: str | None = None,
                unread_only: bool = False,
                limit: int = 300) -> list[dict[str, Any]]:
        out = [n for n in self._notes.values()
               if (channel is None or n["channel"] == channel)
               and (severity is None or n["severity"] == severity)
               and (not unread_only or n["status"] == "unread")]
        return sorted(out, key=lambda n: n["last_at"],
                      reverse=True)[:limit]
