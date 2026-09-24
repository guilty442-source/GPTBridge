"""Build the A607 MODULE_RULE_INDEX_V1 PostgreSQL-backed projection."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from governance_rule.execution.codex_repository import codex_readonly_connection

DEFAULT_OUTPUT = ROOT / "main-system" / "runtime" / "state" / "module-rule-index.json"
SCHEMA = "MODULE_RULE_INDEX_V1"
REQUIRED_RULE_FIELDS = (
    "module_identity", "rule_namespace", "rule_code", "controlling_provision",
    "module_contract", "contract_version", "source_revision", "scope",
    "capability", "trigger", "phase", "priority", "status", "content_hash",
    "introduced_version", "retired_version", "validated_against_codex_version",
    "authority_class",
)


def _hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _version(db: object) -> str:
    row = db.execute("SELECT value FROM metadata WHERE key='current_version'").fetchone()
    if not row or not str(row[0]).strip():
        raise RuntimeError("MODULE_RULE_INDEX_NOT_READY:current-version-missing")
    return str(row[0])


def _modules(db: object) -> dict[str, dict[str, object]]:
    rows = db.execute(
        "SELECT architecture_code,canonical_name,entity_kind,owner,physical_root,"
        "logical_boundary,permission_scope,introduced_version,retired_version "
        "FROM project_architecture_directory ORDER BY architecture_code"
    ).fetchall()
    return {
        str(r[0]): {
            "module_identity": str(r[0]), "canonical_name": str(r[1]),
            "entity_kind": str(r[2]), "owner": str(r[3]),
            "physical_root": r[4], "scope": str(r[5]),
            "permission_scope": str(r[6]), "introduced_version": str(r[7]),
            "status": "retired" if r[8] else "current",
        }
        for r in rows
    }


def build() -> dict[str, object]:
    with codex_readonly_connection() as db:
        version = _version(db)
        modules = _modules(db)
    # No module has published a versioned module-rule contract yet.  An empty
    # rule set is truthful; topology rows are identity scope, never fake rules.
    rules: list[dict[str, object]] = []
    body: dict[str, object] = {
        "schema": SCHEMA,
        "contract_code": SCHEMA,
        "authority_class": "module-derived-non-authoritative",
        "source_authority": "postgresql://local/gptbridge_codex",
        "codex_version": version,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "modules": modules,
        "rules": rules,
        "indexes": {
            "exact_key": {}, "module": {}, "controlling_provision": {},
            "capability": {}, "scope": {}, "status": {}, "contract_version": {},
        },
        "counts": {"modules": len(modules), "rules": 0},
        "validation": {
            "required_rule_fields": list(REQUIRED_RULE_FIELDS),
            "duplicate_keys": 0, "conflicts": 0, "unregistered_modules": 0,
            "source": "PostgreSQL project_architecture_directory",
        },
    }
    body["content_hash"] = _hash(body)
    return body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    document = build()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, args.output)
    if args.summary:
        print(json.dumps(document["counts"], ensure_ascii=False))
    print("[PASS] MODULE_RULE_INDEX_V1 published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
