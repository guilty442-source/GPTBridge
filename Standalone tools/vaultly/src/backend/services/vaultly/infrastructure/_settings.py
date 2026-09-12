from __future__ import annotations

import json
from typing import Any

from ._helpers import _utc_now


class SettingsMixin:
    def set_setting(self, key: str, value: Any) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO vaultly_settings (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), _utc_now()),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM vaultly_settings WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:
            return default
