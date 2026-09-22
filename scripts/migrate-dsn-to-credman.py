"""G89: migrate DSN secrets from user env vars to Windows Credential Manager.

Reads each purpose's env DSN, stores it under ``GPTBridge/postgres/dsn/<purpose>``
in Credential Manager, then verifies the governed resolution path returns the
same value.  It never prints secret material and never clears the env vars —
removing ``GPTBRIDGE_POSTGRES_*_DSN`` from the user environment is a separate,
deliberate step left to the operator.

Usage:
    main-system/.venv/Scripts/python.exe scripts/migrate-dsn-to-credman.py
    main-system/.venv/Scripts/python.exe scripts/migrate-dsn-to-credman.py --dry-run
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(ROOT))

from shared_layer.security import credential_store, dsn_policy  # noqa: E402
from shared_layer.security.dsn_policy import DsnPurpose  # noqa: E402


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    results = []
    for purpose in DsnPurpose:
        suffix = credential_store.DSN_TARGET_TEMPLATE.format(purpose=purpose.value)
        target = credential_store.credential_target(suffix)
        existing_store = credential_store.read_secret(suffix)

        # find the env value for this purpose
        env_value = ""
        env_name = ""
        for name in dsn_policy._PURPOSE_ENV[purpose]:
            candidate = str(os.environ.get(name, "")).strip()
            if candidate:
                env_value, env_name = candidate, name
                break

        entry = {
            "purpose": purpose.value,
            "target": target,
            "store_already_populated": existing_store is not None,
            "env_source": env_name or None,
        }
        if not env_value:
            entry["action"] = "skipped-no-env-value"
        elif existing_store == env_value:
            entry["action"] = "already-migrated"
        elif dry_run:
            entry["action"] = "would-migrate"
        else:
            credential_store.store_secret(
                suffix, env_value, comment=f"GPTBridge {purpose.value} DSN (G89)"
            )
            stored = credential_store.read_secret(suffix)
            entry["action"] = "migrated" if stored == env_value else "VERIFY_FAILED"
        results.append(entry)

    # verify resolution end-to-end (env masked so the store path is exercised)
    if not dry_run:
        masked = {k: v for k, v in os.environ.items() if not k.startswith("GPTBRIDGE_POSTGRES_")}
        for purpose in DsnPurpose:
            suffix = credential_store.DSN_TARGET_TEMPLATE.format(purpose=purpose.value)
            if credential_store.read_secret(suffix):
                try:
                    binding = dsn_policy.resolve_dsn(purpose, environ=masked)
                    ok = binding.env_name.startswith("credman:")
                except dsn_policy.DsnPolicyError as exc:
                    ok = False
                for entry in results:
                    if entry["purpose"] == purpose.value:
                        entry["resolved_via_store"] = ok

    print(json.dumps({"dry_run": dry_run, "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
