from __future__ import annotations

from typing import Any, Iterable

from ._helpers import _utc_now


class FilterTermsMixin:
    @staticmethod
    def _normalize_filter_terms(terms: Iterable[str]) -> list[str]:
        # Case-insensitive identity (sqlite COLLATE NOCASE parity): dedupe
        # on casefold while preserving the first-seen casing.
        seen: dict[str, str] = {}
        for term in terms:
            cleaned = str(term).strip()[:120]
            if cleaned:
                seen.setdefault(cleaned.casefold(), cleaned)
        return sorted(seen.values(), key=str.casefold)

    def _filter_term_row(self, connection: Any, term: str) -> Any | None:
        # ``term`` carries a case-insensitive identity — PG has no NOCASE
        # collation, so compare on lower().
        return connection.execute(
            """
            SELECT term, created_at, is_active, deactivated_at
            FROM vaultly_filter_terms
            WHERE lower(term) = lower(?)
            """,
            (term,),
        ).fetchone()

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
                existing = self._filter_term_row(connection, term)
                if existing is not None and bool(existing["is_active"]):
                    continue
                self._record_row_history(
                    connection,
                    "filter_term",
                    term,
                    "superseded",
                    existing,
                )
                if existing is None:
                    connection.execute(  # sql-ok: bounded config list with interleaved row-history audit per term
                        """
                        INSERT INTO vaultly_filter_terms (
                            term, created_at, is_active, deactivated_at
                        )
                        VALUES (?, ?, 1, '')
                        """,
                        (term, now),
                    )
                else:
                    connection.execute(  # sql-ok: reactivate the stored-casing row — identity is case-insensitive
                        """
                        UPDATE vaultly_filter_terms
                        SET is_active = 1, deactivated_at = ''
                        WHERE term = ?
                        """,
                        (str(existing["term"]),),
                    )
                self._record_row_history(
                    connection,
                    "filter_term",
                    term,
                    "created" if existing is None else "reactivated",
                    self._filter_term_row(connection, term),
                )
                changed += 1
        return changed

    def remove_filter_terms(self, terms: Iterable[str]) -> int:
        normalized = self._normalize_filter_terms(terms)
        if not normalized:
            return 0
        placeholders = ",".join("?" for _ in normalized)
        folded = tuple(term.casefold() for term in normalized)
        now = _utc_now()
        with self._connect() as connection:
            rows = connection.execute(  # sql-ok: generated ? placeholder list
                f"""
                SELECT term, created_at, is_active, deactivated_at FROM vaultly_filter_terms
                WHERE lower(term) IN ({placeholders}) AND is_active = 1
                """,
                folded,
            ).fetchall()
            for row in rows:
                self._record_row_history(
                    connection,
                    "filter_term",
                    str(row["term"]),
                    "superseded",
                    row,
                )
            cursor = connection.execute(  # sql-ok: generated ? placeholder list
                f"""
                UPDATE vaultly_filter_terms
                SET is_active = 0, deactivated_at = ?
                WHERE lower(term) IN ({placeholders}) AND is_active = 1
                """,
                (now, *folded),
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
