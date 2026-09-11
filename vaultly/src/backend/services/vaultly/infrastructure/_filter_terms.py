from __future__ import annotations

from typing import Any, Iterable

from ._helpers import _utc_now


class FilterTermsMixin:
    @staticmethod
    def _normalize_filter_terms(terms: Iterable[str]) -> list[str]:
        return sorted(
            {
                str(term).strip()[:120]
                for term in terms
                if str(term).strip()
            },
            key=str.casefold,
        )

    def list_filter_terms(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT term
                FROM vaultly_filter_terms
                WHERE is_active = 1
                ORDER BY term COLLATE NOCASE
                """
            ).fetchall()
        return [str(row["term"]) for row in rows]

    def add_filter_terms(self, terms: Iterable[str]) -> int:
        normalized = self._normalize_filter_terms(terms)
        if not normalized:
            return 0
        changed = 0
        now = _utc_now()
        with self._connect() as connection:
            for term in normalized:
                existing = self._row_by_key(
                    connection,
                    "vaultly_filter_terms",
                    "term",
                    term,
                )
                if existing is not None and bool(existing["is_active"]):
                    continue
                self._record_row_history(
                    connection,
                    "filter_term",
                    term,
                    "superseded",
                    existing,
                )
                connection.execute(
                    """
                    INSERT INTO vaultly_filter_terms (
                        term, created_at, is_active, deactivated_at
                    )
                    VALUES (?, ?, 1, '')
                    ON CONFLICT(term) DO UPDATE SET
                        is_active = 1,
                        deactivated_at = ''
                    """,
                    (term, now),
                )
                self._record_row_history(
                    connection,
                    "filter_term",
                    term,
                    "created" if existing is None else "reactivated",
                    self._row_by_key(
                        connection,
                        "vaultly_filter_terms",
                        "term",
                        term,
                    ),
                )
                changed += 1
        return changed

    def remove_filter_terms(self, terms: Iterable[str]) -> int:
        normalized = self._normalize_filter_terms(terms)
        if not normalized:
            return 0
        placeholders = ",".join("?" for _ in normalized)
        now = _utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT term, created_at, is_active, deactivated_at FROM vaultly_filter_terms
                WHERE term IN ({placeholders}) AND is_active = 1
                """,
                tuple(normalized),
            ).fetchall()
            for row in rows:
                self._record_row_history(
                    connection,
                    "filter_term",
                    str(row["term"]),
                    "superseded",
                    row,
                )
            cursor = connection.execute(
                f"""
                UPDATE vaultly_filter_terms
                SET is_active = 0, deactivated_at = ?
                WHERE term IN ({placeholders}) AND is_active = 1
                """,
                (now, *normalized),
            )
            for row in rows:
                term = str(row["term"])
                self._record_row_history(
                    connection,
                    "filter_term",
                    term,
                    "deactivated",
                    self._row_by_key(
                        connection,
                        "vaultly_filter_terms",
                        "term",
                        term,
                    ),
                )
        return cursor.rowcount
