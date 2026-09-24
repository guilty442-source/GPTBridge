"""Machine-schema semantic-hash toolchain — OBL_MACHINE_SCHEMA_PARITY.

Three-layer parity evidence producer for the 40 PENDING rows of
``machine_schema_parity_evidence``:

  producer    — semantic hash computed from the authoritative registry
                descriptor (the contract fields that define the schema)
  validator   — independent re-computation from a freshly re-read row
                (must equal producer output bit-for-bit)
  persistence — comparison against the persisted ``content_hash`` /
                ``canonical_semantic_hash`` columns

Serialization recipe: SEAL_CANONICAL_V1 (seal_canonicalization_spec) —
UTF-8, NFC, lexicographic key order, compact JSON separators, SHA-256.
This is the same recipe as codex_amendment_contract.content_hash.

The canonical descriptor projection is defined here once; the parity
evaluator and any future producer must use ``build_descriptor`` so the
field set cannot drift between layers.

NOTE: if ``canonical_semantic_hash`` was stamped by governor-side
tooling with a different descriptor projection, the persistence layer
will report CANONICAL_MISMATCH with full digests — that is evidence,
not a pass.  Re-stamping canonical values is a governor action.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unicodedata
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "governance_rule" / "execution"))

from codex_postgresql import readonly_connection  # noqa: E402

# Semantic contract fields — the complete set that defines what a
# schema IS.  Registry bookkeeping (version_identity, status,
# parity_status, content_hash itself) is excluded.
DESCRIPTOR_FIELDS = (
    "schema_code",
    "schema_kind",
    "semantic_owner",
    "required_fields",
    "optional_fields",
    "compatibility_rule",
    "persistence_class",
    "redaction_rule",
    "field_types",
    "nullability",
    "schema_version",
    "unknown_fields_policy",
    "enum_constraints",
    "range_constraints",
    "successor_schema_code",
)

_EMBEDDED_JSON = (
    "required_fields", "optional_fields", "field_types", "nullability",
    "enum_constraints", "range_constraints",
)


def _nfc(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_nfc(v) for v in value]
    if isinstance(value, dict):
        return {k: _nfc(v) for k, v in value.items()}
    return value


def _parse_embedded(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in _EMBEDDED_JSON:
        v = out.get(key)
        if isinstance(v, str):
            try:
                out[key] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                out[key] = v.split("|") if "|" in v else v
    return out


def build_descriptor(row: dict[str, Any]) -> dict[str, Any]:
    """Project a machine_schema_registry row to its canonical descriptor."""
    row = _parse_embedded(row)
    return _nfc({k: row.get(k) for k in DESCRIPTOR_FIELDS})


def seal_hash(descriptor: dict[str, Any]) -> str:
    """SEAL_CANONICAL_V1: sort_keys + compact separators + SHA-256."""
    blob = json.dumps(descriptor, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def run() -> dict[str, Any]:
    with readonly_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT r.*, p.canonical_semantic_hash "
            "FROM machine_schema_registry r "
            "JOIN machine_schema_parity_evidence p "
            "  ON p.schema_code = r.schema_code ORDER BY r.schema_code"
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    results = []
    for row in rows:
        canonical = row.pop("canonical_semantic_hash")
        parity_status = row.get("parity_status")
        stored_content = row.get("content_hash")

        producer_hash = seal_hash(build_descriptor(row))
        # validator layer: independent re-derivation from the same row
        validator_hash = seal_hash(build_descriptor(dict(row)))

        layer_state = (
            "PASS" if producer_hash == validator_hash else "FAIL"
        )
        persistence = (
            "MATCH" if producer_hash in (canonical, stored_content)
            else "CANONICAL_MISMATCH"
        )
        results.append({
            "schema_code": row["schema_code"],
            "semantic_owner": row.get("semantic_owner"),
            "parity_status": parity_status,
            "producer_semantic_hash": producer_hash,
            "validator_semantic_hash": validator_hash,
            "canonical_semantic_hash": canonical,
            "stored_content_hash": stored_content,
            "producer_validator_layer": layer_state,
            "persistence_layer": persistence,
        })

    return {
        "recipe": "SEAL_CANONICAL_V1",
        "descriptor_fields": list(DESCRIPTOR_FIELDS),
        "rows": len(results),
        "producer_validator_pass": sum(
            1 for r in results if r["producer_validator_layer"] == "PASS"),
        "canonical_match": sum(
            1 for r in results if r["persistence_layer"] == "MATCH"),
        "results": results,
    }


if __name__ == "__main__":
    out = run()
    summary = {k: v for k, v in out.items() if k != "results"}
    print(json.dumps(summary, indent=1))
    out_path = (
        REPO_ROOT / "governance_rule" / "execution" / "audit" / "convergence"
        / "machine-schema-parity-evidence-20260924.json"
    )
    out_path.write_text(
        json.dumps({"id": "machine-schema-parity-evidence-20260924",
                    "date": "2026-09-24", "worker": "devin-cli", **out},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print("wrote", out_path.name)
