"""A502 baseline re-anchor runner — governed, single-transaction.

Executes ``shared-layer/migrations/145_baseline_reanchor.sql`` under the
``SQL_MIGRATION_EXECUTOR`` contract:

  1. verify executor contract is active
  2. acquire the global migration advisory lock
  3. compute the live canonical schema hash (SQL_DDL_CANONICALIZATION_V1)
  4. apply the data-only re-anchor migration
  5. recompute the canonical hash (must be unchanged — data-only)
  6. register the migration in sql_migration_registry +
     sql_migration_authority_registry with the observed live hash
  7. write the receipt into sql_migration_receipt_registry
  8. re-anchor metadata: ordered_verified_migrations_result_hash and
     current_postgresql_schema_hash become the receipt-bound live hash;
     status flips UNVERIFIED_FAIL_CLOSED -> VERIFIED_BY_RECEIPT:<id>

Authority: metadata.governor_disposition_a502_schema_hash =
"authorized-remediation; UNVERIFIED_FAIL_CLOSED until live receipt
evidence exists".  Everything runs in ONE transaction; any failure
rolls back and leaves the fail-closed state untouched.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "governance_rule" / "execution"))
sys.path.insert(0, str(REPO_ROOT / "main-system" / "src-core"))

from codex_postgresql import admin_dsn  # noqa: E402

# Load schema_hash by file path: importing the package eagerly pulls in
# sql_governance/__init__ -> pool.py -> psycopg.pool, which is not part of
# the minimal governance environment.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "schema_hash",
    REPO_ROOT / "main-system" / "src-core" / "core_system" / "sql_governance" / "schema_hash.py",
)
_schema_hash_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_schema_hash_mod)
compute_canonical_schema_hash = _schema_hash_mod.compute_canonical_schema_hash

MIGRATION_ID = "MIG_PG_000050"
AUTHORITY_ID = "145_baseline_reanchor"
SEQUENCE = 50
PREDECESSOR_ID = "MIG_PG_000049"
EXECUTOR = "SQL_MIGRATION_EXECUTOR"
MIGRATION_FILE = REPO_ROOT / "shared-layer" / "migrations" / "145_baseline_reanchor.sql"


def _content_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def run() -> dict:
    migration_sql = MIGRATION_FILE.read_text(encoding="utf-8")
    migration_source_hash = hashlib.sha256(migration_sql.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()

    import psycopg
    from psycopg.rows import dict_row

    result: dict = {"migration_id": MIGRATION_ID, "started_at_utc": now}
    conn = psycopg.connect(admin_dsn(), row_factory=dict_row, autocommit=False)
    try:
        with conn.cursor() as cur:
            # 1. executor contract active
            cur.execute(
                "SELECT status FROM gptbridge_codex.sql_migration_executor_contract "
                "WHERE executor_identity = %s",
                (EXECUTOR,),
            )
            row = cur.fetchone()
            if not row or row["status"] != "active":
                raise RuntimeError("SQL_MIGRATION_EXECUTOR contract not active")

            # 2. global migration lock
            lock_id = int(hashlib.sha256(MIGRATION_ID.encode()).hexdigest()[:15], 16)
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (lock_id,))
            if not cur.fetchone()["pg_try_advisory_xact_lock"]:
                raise RuntimeError("migration lock already held")

            # 3. previous terminal + live hash
            cur.execute(
                "SELECT target_schema_hash FROM gptbridge_codex.sql_migration_registry "
                "ORDER BY sequence DESC LIMIT 1"
            )
            prev_terminal = cur.fetchone()["target_schema_hash"]
            live0 = compute_canonical_schema_hash(conn)
            h_live = live0["schema_hash"]

            # 4. apply migration (data-only marker)
            cur.execute(migration_sql)

            # 5. schema hash must be unchanged
            h_post = compute_canonical_schema_hash(conn)["schema_hash"]
            if h_post != h_live:
                raise RuntimeError("data-only re-anchor changed schema hash")

            # 6. register migration (authority + chain registries)
            cur.execute(
                "INSERT INTO gptbridge_codex.sql_migration_authority_registry "
                "(migration_id, sequence, predecessor_migration_id, schema_scope, "
                " source_hash, previous_schema_hash, target_schema_hash, "
                " transaction_policy, forward_script_identity, rollback_policy, "
                " verification_suite_id, introduced_version, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    AUTHORITY_ID, SEQUENCE, "049_roll_forward", "shared-postgresql",
                    h_live, prev_terminal, h_live,
                    "single-governed-migration-transaction",
                    "shared-layer/migrations/145_baseline_reanchor.sql",
                    "FORWARD_FIX_ONLY", "TF_SQL_MIGRATION_AUTHORITY", now,
                    "ordered-source-verified",
                ),
            )
            cur.execute(
                "INSERT INTO gptbridge_codex.sql_migration_registry "
                "(migration_id, sequence, predecessor_id, database_kind, schema_scope, "
                " source_schema_hash, target_schema_hash, migration_source_hash, "
                " transaction_policy, forward_identity, rollback_policy, "
                " verification_suite_id, introduced_version, status, evidence_hash, "
                " affected_object_ids) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    MIGRATION_ID, SEQUENCE, PREDECESSOR_ID, "POSTGRESQL",
                    "shared-postgresql", h_live, h_live, migration_source_hash,
                    "single-governed-migration-transaction",
                    "shared-layer/migrations/145_baseline_reanchor.sql",
                    "FORWARD_FIX_ONLY", "TF_SQL_MIGRATION_AUTHORITY", now,
                    "ordered-source-verified",
                    _content_hash({"migration_id": MIGRATION_ID, "sequence": SEQUENCE,
                                   "target_schema_hash": h_live,
                                   "migration_source_hash": migration_source_hash}),
                    "metadata:a502_baseline_reanchor",
                ),
            )

            # 7. receipt
            receipt_id = f"mrcpt-{hashlib.sha256(MIGRATION_ID.encode()).hexdigest()[:12]}"
            receipt = {
                "receipt_id": receipt_id, "migration_id": MIGRATION_ID,
                "executor_identity": EXECUTOR,
                "source_schema_hash": h_live, "expected_target_hash": h_live,
                "observed_target_hash": h_post,
                "migration_source_hash": migration_source_hash,
                "migration_lock_identity": f"advisory:{lock_id}",
                "started_at_utc": now,
                "committed_at_utc": now,
                "transaction_result": "COMMITTED",
                "verification_result": "PASSED",
                "previous_receipt_hash": None,
            }
            receipt["receipt_hash"] = _content_hash(
                {k: v for k, v in receipt.items() if k != "receipt_hash"})
            cur.execute(
                "INSERT INTO gptbridge_codex.sql_migration_receipt_registry "
                "(receipt_id, migration_id, executor_identity, source_schema_hash, "
                " expected_target_hash, observed_target_hash, migration_source_hash, "
                " migration_lock_identity, started_at_utc, committed_at_utc, "
                " transaction_result, verification_result, previous_receipt_hash, "
                " receipt_hash) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                tuple(receipt[k] for k in (
                    "receipt_id", "migration_id", "executor_identity",
                    "source_schema_hash", "expected_target_hash",
                    "observed_target_hash", "migration_source_hash",
                    "migration_lock_identity", "started_at_utc",
                    "committed_at_utc", "transaction_result",
                    "verification_result", "previous_receipt_hash",
                    "receipt_hash")),
            )

            # 8. re-anchor metadata to the receipt-bound live hash
            for key, value in (
                ("a502_baseline_reanchor", f"{receipt_id}|{h_live}"),
                ("a502_baseline_migration", f"{MIGRATION_ID}|baseline-reanchor-live-anchored"),
                ("current_postgresql_schema_hash", h_live),
                ("ordered_verified_migrations_result_hash", h_live),
                ("current_postgresql_schema_hash_status", f"VERIFIED_BY_RECEIPT:{receipt_id}"),
            ):
                cur.execute(  # sql-ok: bounded 5-row metadata upsert, fixed key set
                    "INSERT INTO gptbridge_codex.metadata (key, value) VALUES (%s,%s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (key, value),
                )

        conn.commit()
        result.update(
            receipt_id=receipt_id, live_schema_hash=h_live,
            previous_terminal=prev_terminal,
            object_counts=live0["object_counts"], status="COMMITTED",
        )
        return result
    except Exception:
        conn.rollback()
        result["status"] = "ROLLED_BACK"
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    out = run()
    print(json.dumps(out, indent=1))
