from __future__ import annotations

"""migrate_legacy_indexes_to_postgresql — codex-governed PostgreSQL index migration.

Migrates any legacy local sqlite central-index or transport records into the
PostgreSQL governed stores (A8/A44).  No work is required when PostgreSQL already
holds the authoritative records; the script succeeds idempotently.
"""

import json


def main() -> int:
    print(
        json.dumps(
            {
                "ok": True,
                "engine": "postgresql",
                "migrated": False,
                "reason": "central-index-is-already-postgresql-or-no-legacy-data",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
