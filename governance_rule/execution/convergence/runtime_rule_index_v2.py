"""G90 — RUNTIME_RULE_INDEX_V2 producer / validator / atomic generation loader.

Implements the codex contract ``runtime_rule_index_contract`` row
``RUNTIME_RULE_INDEX_V2`` (A605):

* content: current-effective principles/articles/edicts (+ their normative
  facets and formal rules) only — superseded/retired provisions and
  subordinate restatements (A609) are never indexed as independent entries;
* successor policy: ``resolve-before-index; unresolved=deny`` — references to
  non-current provisions resolve through ``provision_reference_resolution_v2``
  and ``provision_supersession_edges``; an unresolved reference denies the
  build (fail-closed);
* structure: compact ordinals, deduplicated dictionary, sorted posting lists;
* generation: ``validate-then-atomic-swap`` with a reader generation fence —
  readers observe whole generations only; a stale or failed generation is
  ``INDEX_NOT_READY`` and callers must fall back to the governed canonical
  query path, never to a stale index;
* SLO: exact warm p95 <= 1 ms, multifilter warm p95 <= 5 ms,
  build-and-verify <= 20 s.

The index is ``derived-rebuildable-non-authoritative``: deleting the output
is always safe; rebuild with ``python -m
governance_rule.execution.convergence.runtime_rule_index_v2``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .normative_classification import (
    controlling_provisions,
    load_classifications,
    load_convergence,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATABASE = (
    PROJECT_ROOT / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "main-system" / "runtime" / "state" / "runtime-rule-index-v2.json"
)

SCHEMA = "RUNTIME_RULE_INDEX_V2"
CONTRACT_CODE = "RUNTIME_RULE_INDEX_V2"

# Posting dimensions mandated by the contract row.
POSTING_DIMENSIONS = (
    "owner",
    "capability",
    "trigger",
    "phase",
    "severity",
    "effect",
    "deadline",
    "resource_class",
    "data_class",
    "dependency",
    "successor",
)

BUILD_VERIFY_BUDGET_S = 20.0
EXACT_WARM_P95_MS = 1.0
MULTIFILTER_WARM_P95_MS = 5.0


class IndexBuildError(RuntimeError):
    """Raised when the build must be denied (fail-closed)."""


class IndexNotReady(RuntimeError):
    """INDEX_NOT_READY — callers must use the governed canonical path."""


def _rows(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return db.execute(sql, params).fetchall()


def _codex_version(db: sqlite3.Connection) -> str:
    try:
        row = db.execute(
            "select version_identity from provision_lifecycle_status "
            "order by current_binding_version desc limit 1"
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row and row[0]:
        return str(row[0])
    try:
        row = db.execute("select max(current_binding_version) from provision_lifecycle_status").fetchone()
        return str(row[0]) if row and row[0] else "unknown"
    except sqlite3.Error:
        return "unknown"


def _sha256_json(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


_SUCCESSOR_PREFIXES: tuple[tuple[str, str], ...] = (
    ("ARTICLE_", "article"),
    ("EDICT_", "edict"),
    ("PRINCIPLE_", "principle"),
    ("CLOSURE_DEFINITION_", "closure-definition"),
    ("FORMAL_RULE_", "formal-rule"),
    ("REGISTRY_RULE_", "registry-rule"),
    ("SOVEREIGN_", "sovereign"),
)


def _successor_candidates(identity: str) -> list[str]:
    """Candidate ``<type>:<id>`` keys for a declared successor identity.

    ``provision_lifecycle_status.successor_identity`` uses declared names
    (``ARTICLE_A604``, ``CLOSURE_DEFINITION_A516_V1``, ``FORMAL_RULE_A498_V1``)
    that differ from live provision ids for delegated rows
    (``CLOSURE_DELEGATED_*`` / ``RULE_DELEGATED_*``) — emit both spellings.
    """
    for prefix, ptype in _SUCCESSOR_PREFIXES:
        if identity.startswith(prefix):
            pid = identity[len(prefix) :]
            candidates = [f"{ptype}:{pid}"]
            if ptype == "closure-definition":
                candidates.append(f"{ptype}:CLOSURE_DELEGATED_{pid}")
            if ptype == "formal-rule":
                candidates.append(f"{ptype}:RULE_DELEGATED_{pid}")
            return candidates
    return []


def _resolve_successor(
    key: str,
    resolution_v2: Mapping[str, tuple[str, str]],
    edges: Mapping[str, str],
    successor_identity: Mapping[str, str],
    active_keys: set[str],
) -> str:
    """Resolve a non-current provision key to its active successor.

    Returns the resolved ``<type>:<id>`` key, or raises ``IndexBuildError``
    when no active successor exists — the contract is
    ``resolve-before-index; unresolved=deny``.
    """
    status, target = resolution_v2.get(key, ("", ""))
    if status == "resolved-active" and target:
        return target
    if status == "retired-no-successor":
        raise IndexBuildError(f"UNRESOLVED_SUCCESSOR:{key}:retired-no-successor")
    edge = edges.get(key)
    if edge:
        return edge
    declared = successor_identity.get(key)
    if declared:
        for candidate in _successor_candidates(declared):
            if candidate in active_keys:
                return candidate
        raise IndexBuildError(
            f"UNRESOLVED_SUCCESSOR:{key}:declared:{declared}:not-active"
        )
    raise IndexBuildError(f"UNRESOLVED_SUCCESSOR:{key}:no-edge")


def build_index(db: sqlite3.Connection) -> dict[str, Any]:
    """Build the V2 index document from the codex database."""
    db.row_factory = sqlite3.Row
    started = time.monotonic()

    classifications = load_classifications(db)
    convergence = load_convergence(db)
    controlling = controlling_provisions(classifications, convergence)

    # Successor resolution tables (A605: resolve-before-index).
    resolution_v2: dict[str, tuple[str, str]] = {}
    for r in _rows(
        db,
        "select predecessor_type, predecessor_id, resolved_successor_type, "
        "resolved_active_successor_id, status from provision_reference_resolution_v2",
    ):
        key = f"{r['predecessor_type']}:{r['predecessor_id']}"
        target = (
            f"{r['resolved_successor_type']}:{r['resolved_active_successor_id']}"
            if r["resolved_active_successor_id"]
            else ""
        )
        resolution_v2[key] = (str(r["status"]), target)
    edges: dict[str, str] = {}
    for r in _rows(
        db,
        "select predecessor_type, predecessor_id, successor_type, successor_id "
        "from provision_supersession_edges where status='current'",
    ):
        edges[f"{r['predecessor_type']}:{r['predecessor_id']}"] = (
            f"{r['successor_type']}:{r['successor_id']}"
        )
    successor_identity: dict[str, str] = {}
    active_keys: set[str] = set()
    lifecycle_map: dict[str, str] = {}
    for r in _rows(
        db,
        "select provision_type, provision_id, lifecycle_state, successor_identity "
        "from provision_lifecycle_status",
    ):
        key = f"{r['provision_type']}:{r['provision_id']}"
        lifecycle_map[key] = str(r["lifecycle_state"])
        if r["successor_identity"]:
            successor_identity[key] = str(r["successor_identity"])
        if r["lifecycle_state"] == "active":
            active_keys.add(key)

    # Provision-level authoritative facets.
    law_class = {
        f"{r['provision_type']}:{r['provision_id']}": (str(r["tier"]), str(r["law_code"]))
        for r in _rows(db, "select provision_type, provision_id, tier, law_code from provision_law_classification")
    }
    normativity = {
        f"{r['provision_type']}:{r['provision_id']}": str(r["primary_class"])
        for r in _rows(
            db,
            "select provision_type, provision_id, primary_class "
            "from provision_normativity_classification where status='active'",
        )
    }
    membership = {
        f"{r['provision_type']}:{r['provision_id']}": str(r["module_code"])
        for r in _rows(
            db,
            "select provision_type, provision_id, module_code from codex_internal_module_membership "
            "where membership_kind='primary' and resolution_state='resolved'",
        )
    }
    module_owner = {
        str(r["module_code"]): str(r["owner"])
        for r in _rows(db, "select module_code, owner from codex_internal_module_directory")
    }
    module_deps: dict[str, list[str]] = {}
    for r in _rows(
        db,
        "select source_module_code, target_module_code from codex_internal_module_dependency",
    ):
        module_deps.setdefault(str(r["source_module_code"]), []).append(str(r["target_module_code"]))
    deadlines: dict[str, list[str]] = {}
    for r in _rows(
        db,
        "select declaration_provision, due_at_utc from implementation_obligations where due_at_utc is not null",
    ):
        deadlines.setdefault(f"article:{r['declaration_provision']}", []).append(str(r["due_at_utc"]))
        # Obligations declare against article ids; keep other types resolvable.
        deadlines.setdefault(str(r["declaration_provision"]), [])
    ownership: dict[str, set[str]] = {}
    for r in _rows(
        db,
        "select rule_code, owner_domain from formal_rule_ownership_map where status='current'",
    ):
        ownership.setdefault(str(r["rule_code"]), set()).add(str(r["owner_domain"]))
    capabilities: dict[str, set[str]] = {}
    for r in _rows(
        db,
        "select capability_code, formal_rule_id from mature_capability_registry "
        "where formal_rule_id is not null and formal_rule_id != 'None'",
    ):
        capabilities.setdefault(str(r["formal_rule_id"]), set()).add(str(r["capability_code"]))

    # --- provision entries (exact key: provision_identity) ---
    provision_entries: list[dict[str, Any]] = []
    for key in sorted(controlling):
        rec = controlling[key]
        tier, law_code = law_class.get(key, ("", ""))
        module = membership.get(key, "")
        entry = {
            "provision_identity": key,
            "lifecycle_state": rec.lifecycle_state,
            "structural_category": rec.structural_category,
            "has_responsibility": rec.has_responsibility,
            "has_prohibition": rec.has_prohibition,
            "successor": rec.current_successor or "",
            "owner": module_owner.get(module, ""),
            "module": module,
            "law_tier": tier,
            "law_code": law_code,
            "data_class": normativity.get(key, ""),
            "deadlines": sorted(set(deadlines.get(key, ()))),
            "dependency": sorted(module_deps.get(module, ())),
        }
        provision_entries.append(entry)

    # --- rule entries (exact key: rule_code) ---
    rule_rows = _rows(
        db,
        "select rule_code, controlling_provision_id, required_inputs, severity, "
        "pass_decision, fail_decision, low_cost_path, precedence_scope, status, "
        "parity_status, semantic_hash, version_identity from formal_rule_registry",
    )
    rule_entries: list[dict[str, Any]] = []
    for r in rule_rows:
        pid = str(r["controlling_provision_id"])
        # Rule's controlling provision must resolve to a current-effective
        # identity before indexing (resolve-before-index; unresolved=deny).
        candidates = [f"article:{pid}", f"edict:{pid}", f"principle:{pid}"]
        pkey = next((c for c in candidates if c in classifications), None)
        if pkey is None:
            raise IndexBuildError(f"UNKNOWN_CONTROLLING_PROVISION:{r['rule_code']}:{pid}")
        prec = classifications[pkey]
        successor_key = ""
        if pkey not in controlling:
            if pkey in convergence:
                # Restatement as controller — resolve to the unique controller.
                successor_key = convergence[pkey].controlling_key
            else:
                successor_key = _resolve_successor(
                    pkey, resolution_v2, edges, successor_identity, active_keys
                )
            if successor_key not in controlling and successor_key not in active_keys:
                raise IndexBuildError(
                    f"SUCCESSOR_NOT_CURRENT_EFFECTIVE:{pkey}->{successor_key}"
                )
        required = [t for t in str(r["required_inputs"] or "").split("|") if t]
        caps = set(capabilities.get(str(r["rule_code"]), ()))
        caps.add(str(r["precedence_scope"]))  # governed domain is the capability class
        prov_key = successor_key or pkey
        # The resolved controller may be a non-normative provision kind
        # (closure-definition / formal-rule) that lives outside the classified
        # domain — its lifecycle state comes from the lifecycle map then.
        prov_state = (
            controlling[prov_key].lifecycle_state
            if prov_key in controlling
            else lifecycle_map.get(prov_key, "active")
        )
        module = membership.get(prov_key, "")
        entry = {
            "rule_code": str(r["rule_code"]),
            "provision_identity": prov_key,
            "declared_provision": pkey,
            "lifecycle_state": prov_state,
            "controlling_successor": successor_key,
            "severity": str(r["severity"]),
            "status": str(r["status"]),
            "parity_status": str(r["parity_status"]),
            "owner": sorted(ownership.get(str(r["rule_code"]), ())),
            "capability": sorted(caps),
            "trigger": sorted(set(required)),
            "phase": str(r["low_cost_path"]),
            "effect": str(r["fail_decision"]),
            "pass_decision": str(r["pass_decision"]),
            "deadline": sorted(set(deadlines.get(prov_key, ()))),
            "resource_class": law_class.get(prov_key, ("", ""))[0],
            "data_class": normativity.get(prov_key, ""),
            "dependency": sorted(module_deps.get(module, ())),
            "successor": successor_identity.get(prov_key, ""),
            "semantic_hash": str(r["semantic_hash"]),
        }
        rule_entries.append(entry)
    rule_entries.sort(key=lambda e: e["rule_code"])

    # --- compact-ordinal + deduplicated dictionary + sorted postings ---
    dictionary: list[str] = []
    dict_id: dict[str, int] = {}

    def intern(value: str) -> int:
        if value not in dict_id:
            dict_id[value] = len(dictionary)
            dictionary.append(value)
        return dict_id[value]

    postings: dict[str, dict[str, list[int]]] = {d: {} for d in POSTING_DIMENSIONS}
    by_provision: dict[str, int] = {}
    by_rule: dict[str, int] = {}

    def post(dim: str, value: str, ordinal: int) -> None:
        if not value:
            return
        postings[dim].setdefault(value, []).append(ordinal)

    for ordinal, entry in enumerate(provision_entries):
        intern(entry["provision_identity"])
        by_provision[entry["provision_identity"]] = ordinal
        post("owner", entry["owner"], ordinal)
        post("resource_class", entry["law_tier"], ordinal)
        post("data_class", entry["data_class"], ordinal)
        post("severity", "", ordinal)  # provisions carry no severity — skipped
        for dl in entry["deadlines"]:
            post("deadline", dl, ordinal)
        for dep in entry["dependency"]:
            post("dependency", dep, ordinal)
        if entry["has_prohibition"]:
            post("effect", "PROHIBITION", ordinal)
        if entry["has_responsibility"]:
            post("effect", "RESPONSIBILITY", ordinal)
        if entry["successor"]:
            post("successor", entry["successor"], ordinal)
        intern(entry["module"])

    rule_base = len(provision_entries)
    for i, entry in enumerate(rule_entries):
        ordinal = rule_base + i
        intern(entry["rule_code"])
        by_rule[entry["rule_code"]] = ordinal
        by_provision.setdefault(entry["provision_identity"], None)
        for o in entry["owner"]:
            post("owner", o, ordinal)
        for c in entry["capability"]:
            post("capability", c, ordinal)
        for tg in entry["trigger"]:
            post("trigger", tg, ordinal)
        post("phase", entry["phase"], ordinal)
        post("severity", entry["severity"], ordinal)
        post("effect", entry["effect"], ordinal)
        for dl in entry["deadline"]:
            post("deadline", dl, ordinal)
        post("resource_class", entry["resource_class"], ordinal)
        post("data_class", entry["data_class"], ordinal)
        for dep in entry["dependency"]:
            post("dependency", dep, ordinal)
        if entry["controlling_successor"]:
            post("successor", entry["controlling_successor"], ordinal)

    for dim in postings.values():
        for value in dim:
            dim[value] = sorted(set(dim[value]))

    # Element output sets required by A605 (active / prohibition /
    # fail-closed / hot-path expressed as ordinal sets).
    elements = {
        "active": sorted(
            i
            for i, e in enumerate(provision_entries)
            if e["lifecycle_state"] == "active"
        )
        + sorted(
            rule_base + i
            for i, e in enumerate(rule_entries)
            if e["lifecycle_state"] == "active"
        ),
        "prohibition": sorted(
            i for i, e in enumerate(provision_entries) if e["has_prohibition"]
        ),
        "fail_closed": sorted(
            rule_base + i
            for i, e in enumerate(rule_entries)
            if "fail" in e["effect"].lower() or "deny" in e["effect"].lower() or e["severity"] == "critical"
        ),
        "hot_path": sorted(
            rule_base + i
            for i, e in enumerate(rule_entries)
            if e["phase"] in {"in-process-governed-dispatch", "direct-call", "cached exact lookup"}
        ),
    }

    codex_version = _codex_version(db)
    body = {
        "schema": SCHEMA,
        "contract_code": CONTRACT_CODE,
        "authority_class": "derived-rebuildable-non-authoritative",
        "codex_version": codex_version,
        "built_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dictionary": dictionary,
        "provisions": provision_entries,
        "rules": rule_entries,
        "indexes": {
            "by_provision_identity": by_provision,
            "by_rule_code": by_rule,
            "postings": postings,
            "elements": elements,
        },
        "counts": {
            "provisions": len(provision_entries),
            "rules": len(rule_entries),
            "dictionary": len(dictionary),
            "posting_dimensions": {d: len(v) for d, v in postings.items()},
        },
    }
    body["content_hash"] = _sha256_json(body)
    body["build_seconds"] = round(time.monotonic() - started, 3)
    return body


def validate_index(index: Mapping[str, Any], db: sqlite3.Connection) -> list[str]:
    """Mandatory post-build verification (count/identity/hash/successor/acyclic)."""
    errors: list[str] = []
    if index.get("schema") != SCHEMA:
        errors.append(f"schema mismatch: {index.get('schema')}")
    expected_version = _codex_version(db)
    if index.get("codex_version") != expected_version:
        errors.append(
            f"source version mismatch: {index.get('codex_version')} != {expected_version}"
        )

    # Recompute content hash over everything except the stamped fields.
    body = {k: v for k, v in index.items() if k not in {"content_hash", "build_seconds"}}
    if _sha256_json(body) != index.get("content_hash"):
        errors.append("content hash mismatch")

    classifications = load_classifications(db)
    convergence = load_convergence(db)
    controlling = controlling_provisions(classifications, convergence)

    # Identity-set parity: indexed provisions == current-effective controllers.
    indexed = {e["provision_identity"] for e in index.get("provisions", ())}
    if indexed != set(controlling):
        missing = set(controlling) - indexed
        extra = indexed - set(controlling)
        if missing:
            errors.append(f"missing current-effective provisions: {sorted(missing)[:6]}")
        if extra:
            errors.append(f"indexed non-current provisions: {sorted(extra)[:6]}")

    # No restatement or superseded provision may appear as an index identity.
    for bad in indexed & set(convergence):
        errors.append(f"subordinate restatement indexed: {bad}")

    # Rule parity: every registry rule resolves to a current-effective provision.
    registry = {
        str(r["rule_code"]) for r in _rows(db, "select rule_code from formal_rule_registry")
    }
    indexed_rules = {e["rule_code"] for e in index.get("rules", ())}
    if indexed_rules != registry:
        errors.append(
            f"rule set mismatch: -{sorted(registry - indexed_rules)[:4]} +{sorted(indexed_rules - registry)[:4]}"
        )

    # Posting integrity: sorted unique ordinals, in-range.
    total = len(index.get("provisions", ())) + len(index.get("rules", ()))
    postings = index.get("indexes", {}).get("postings", {})
    for dim, values in postings.items():
        for value, ordinals in values.items():
            if ordinals != sorted(set(ordinals)):
                errors.append(f"posting list not sorted-unique: {dim}:{value}")
            if ordinals and (ordinals[0] < 0 or ordinals[-1] >= total):
                errors.append(f"posting ordinal out of range: {dim}:{value}")

    # Dependency acyclic at module level (contract: dependency acyclic).
    deps: dict[str, list[str]] = {}
    for r in _rows(
        db, "select source_module_code, target_module_code from codex_internal_module_dependency"
    ):
        deps.setdefault(str(r["source_module_code"]), []).append(str(r["target_module_code"]))
    state: dict[str, int] = {}
    def _cycle(node: str, stack: tuple[str, ...]) -> str | None:
        state[node] = 1
        for nxt in deps.get(node, ()):
            if state.get(nxt) == 1:
                return ">".join(stack + (node, nxt))
            if state.get(nxt, 0) == 0:
                hit = _cycle(nxt, stack + (node,))
                if hit:
                    return hit
        state[node] = 2
        return None
    for node in deps:
        if state.get(node, 0) == 0:
            hit = _cycle(node, ())
            if hit:
                errors.append(f"dependency cycle: {hit}")
                break
    return errors


class RuntimeRuleIndex:
    """Loaded generation with a reader generation fence.

    Readers only ever observe a complete published generation; a generation
    whose codex version no longer matches the live codex raises
    ``IndexNotReady`` (stale_policy: governed canonical query, never a stale
    index).
    """

    def __init__(self, document: Mapping[str, Any]) -> None:
        self._doc = document
        self._by_provision = document["indexes"]["by_provision_identity"]
        self._by_rule = document["indexes"]["by_rule_code"]
        self._postings = document["indexes"]["postings"]
        self._provisions = document["provisions"]
        self._rules = document["rules"]

    @property
    def generation(self) -> str:
        return str(self._doc.get("built_at_utc", ""))

    @property
    def codex_version(self) -> str:
        return str(self._doc.get("codex_version", ""))

    def check_fresh(self, db: sqlite3.Connection) -> None:
        if _codex_version(db) != self.codex_version:
            raise IndexNotReady(
                f"INDEX_NOT_READY: generation {self.codex_version} != codex {_codex_version(db)}"
            )

    def _entry(self, ordinal: int) -> dict[str, Any]:
        if ordinal < len(self._provisions):
            return self._provisions[ordinal]
        return self._rules[ordinal - len(self._provisions)]

    def exact_provision(self, identity: str) -> dict[str, Any] | None:
        ordinal = self._by_provision.get(identity)
        return self._provisions[ordinal] if ordinal is not None else None

    def exact_rule(self, rule_code: str) -> dict[str, Any] | None:
        ordinal = self._by_rule.get(rule_code)
        return self._rules[ordinal] if ordinal is not None else None

    def query(self, **dimension_filters: str | Iterable[str]) -> list[dict[str, Any]]:
        """Smallest-posting-first intersection (contract query_strategy).

        Unknown dimensions or absent values yield an empty result — never a
        full scan and never a guess.
        """
        sets: list[set[int]] = []
        for dim, values in dimension_filters.items():
            if dim not in self._postings:
                return []
            if isinstance(values, str):
                values = (values,)
            postings = self._postings[dim]
            hit: set[int] = set()
            for v in values:
                hit |= set(postings.get(v, ()))
            sets.append(hit)
        if not sets:
            return []
        sets.sort(key=len)  # smallest posting first
        result = sets[0]
        for s in sets[1:]:
            result &= s
            if not result:
                return []
        return [self._entry(i) for i in sorted(result)]

    def element_set(self, name: str) -> list[int]:
        return list(self._doc["indexes"]["elements"].get(name, ()))


def _validate_then_swap(document: dict[str, Any], db: sqlite3.Connection, output: Path) -> None:
    errors = validate_index(document, db)
    if errors:
        raise IndexBuildError("VALIDATE_FAILED:" + "; ".join(errors[:6]))
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, output)  # atomic swap; readers see whole generations only


def build_and_publish(
    database: Path | None = None, output: Path | None = None
) -> dict[str, Any]:
    """Build → verify → atomic swap.  Enforces the 20 s contract gate."""
    db_path = Path(database) if database else DEFAULT_DATABASE
    out = Path(output) if output else DEFAULT_OUTPUT
    started = time.monotonic()
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        document = build_index(db)
        _validate_then_swap(document, db, out)
    finally:
        db.close()
    elapsed = time.monotonic() - started
    document["publish_seconds"] = round(elapsed, 3)
    if elapsed > BUILD_VERIFY_BUDGET_S:
        # The swap already happened; report the breach so callers can deny.
        raise IndexBuildError(
            f"BUILD_VERIFY_BUDGET_EXCEEDED:{elapsed:.1f}s>{BUILD_VERIFY_BUDGET_S}s"
        )
    return document


def load_published(path: Path | None = None) -> RuntimeRuleIndex:
    p = Path(path) if path else DEFAULT_OUTPUT
    try:
        document = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IndexNotReady(f"INDEX_NOT_READY:{exc}") from exc
    body = {k: v for k, v in document.items() if k not in {"content_hash", "build_seconds", "publish_seconds"}}
    if _sha256_json(body) != document.get("content_hash"):
        raise IndexNotReady("INDEX_NOT_READY:content-hash-mismatch")
    return RuntimeRuleIndex(document)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--database", type=Path, default=None)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    try:
        document = build_and_publish(args.database, args.output)
    except IndexBuildError as exc:
        print(f"[FAIL] {exc}")
        return 1
    if args.summary:
        print(json.dumps(document["counts"], ensure_ascii=False, indent=2))
    print(f"[PASS] RUNTIME_RULE_INDEX_V2 published ({document['counts']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
