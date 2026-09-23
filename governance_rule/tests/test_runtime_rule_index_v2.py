"""G90 RUNTIME_RULE_INDEX_V2 tests (A605 contract parity)."""
from __future__ import annotations

import json
import sqlite3
import statistics
import time

import pytest

from governance_rule.execution.convergence import runtime_rule_index_v2 as v2


@pytest.fixture(scope="module")
def live_db():
    # Post-cutover (A173): the live authority is PostgreSQL; open_db(None)
    # yields the governed dict-row shim. An explicit path still opens a
    # staged SQLite fixture.
    with v2.open_db(None) as conn:
        yield conn


@pytest.fixture(scope="module")
def document(live_db):
    return v2.build_index(live_db)


def test_build_validates_clean(document, live_db):
    assert v2.validate_index(document, live_db) == []


def test_only_current_effective_provisions(document):
    # A591/A592 are superseded -> must not be indexed identities.
    ids = {e["provision_identity"] for e in document["provisions"]}
    assert "article:A591" not in ids
    assert "article:A592" not in ids
    assert not any("sub-sovereign" in k for k in ids)


def test_restatements_never_indexed(document, live_db):
    from governance_rule.execution.convergence.normative_classification import (
        load_convergence,
    )

    conv = load_convergence(live_db)
    ids = {e["provision_identity"] for e in document["provisions"]}
    assert not (ids & set(conv))


def test_superseded_controllers_resolve(document):
    resolved = {e["declared_provision"]: e["provision_identity"] for e in document["rules"]}
    assert resolved.get("article:A591") == "article:A604"
    assert resolved.get("article:A592") == "article:A604"


def test_unresolved_successor_denies_build():
    # A rule bound to a superseded provision with no successor path -> deny.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        create table provision_normative_category(
            provision_type text, provision_id text, structural_category text,
            has_responsibility int, has_prohibition int, lifecycle_state text,
            current_successor text, status text, version_identity text,
            content_hash text);
        create table codex_normative_convergence_registry(
            statement_code text, controlling_type text, controlling_id text,
            restatement_type text, restatement_id text, relationship_kind text,
            facet_merge_policy text, resolution_basis text, status text,
            version_identity text, evidence_hash text);
        create table codex_normative_category_directory(
            category_code text, category_kind text, authority_class text,
            owner text, status text, version_identity text, content_hash text);
        create table provision_reference_resolution_v2(
            predecessor_type text, predecessor_id text,
            declared_successor_identity text, resolved_successor_type text,
            resolved_active_successor_id text, resolution_path text,
            status text, validated_at_utc text);
        create table provision_supersession_edges(
            predecessor_type text, predecessor_id text, successor_type text,
            successor_id text, edge_kind text, status text,
            version_identity text, evidence_hash text);
        create table provision_lifecycle_status(
            provision_type text, provision_id text, lifecycle_state text,
            effective_version text, successor_identity text, evidence text,
            legacy_effective_version text, current_binding_version text);
        create table provision_law_classification(
            provision_type text, provision_id text, tier text, law_code text,
            authority_basis text);
        create table provision_normativity_classification(
            provision_type text, provision_id text, primary_class text,
            verifier text, evidence_schema text, release_binding text,
            version_identity text, status text);
        create table codex_internal_module_membership(
            provision_type text, provision_id text, module_code text,
            membership_kind text, resolution_state text, version_identity text);
        create table codex_internal_module_directory(
            module_code text, canonical_name text, directory_code text,
            owner text, scope text, authority_kind text, status text,
            revision text, content_hash text, introduced_version text,
            retired_version text, responsibility text, authority_boundary text,
            parent_module_code text, version_identity text);
        create table codex_internal_module_dependency(
            source_module_code text, target_module_code text,
            dependency_kind text, allowed_reason text, cycle_policy text,
            version_identity text);
        create table implementation_obligations(
            obligation_code text, declaration_provision text,
            implementation_owner text, target_state text, current_state text,
            deadline_rule text, acceptance_evidence text,
            noncompliance_effect text, waiver_rule text,
            introduced_version text, created_at_utc text, due_at_utc text,
            previous_state text, verification_owner text, escalation_rule text);
        create table formal_rule_ownership_map(
            invariant_code text, rule_code text, owner_domain text,
            version_identity text, status text);
        create table mature_capability_registry(
            capability_code text, owner_domain text, implementation_scope text,
            controlling_law text, contract_id text, formal_rule_id text,
            test_suite_id text, evidence_reference text, status text,
            generation text);
        create table formal_rule_registry(
            rule_code text, controlling_provision_id text, required_inputs text,
            predicate text, pass_decision text, fail_decision text,
            severity text, evidence_schema text, low_cost_path text,
            precedence_scope text, version_identity text, status text,
            semantic_hash text, evaluator_hash text, test_contract_hash text,
            controlling_provision_hash text, parity_status text,
            parity_evidence_id text);
        """
    )
    conn.execute(
        "insert into provision_normative_category values(?,?,?,?,?,?,?,?,?,?)",
        ("article", "A1", "ARTICLE", 0, 0, "superseded", "", "current", "v", "h"),
    )
    conn.execute(
        "insert into provision_lifecycle_status values(?,?,?,?,?,?,?,?)",
        ("article", "A1", "superseded", "v", None, "", None, "v"),
    )
    conn.execute(
        "insert into formal_rule_registry values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("RULE_X", "A1", "", "", "PASS", "FAIL", "critical", "", "lane",
         "scope", "v", "current", "h", "", "", "", "VERIFIED", ""),
    )
    with pytest.raises(v2.IndexBuildError):
        v2.build_index(conn)
    conn.close()


def test_exact_and_multifilter_slo(document):
    idx = v2.RuntimeRuleIndex(document)
    ids = [e["provision_identity"] for e in document["provisions"][:200]]
    for k in ids[:20]:
        idx.exact_provision(k)
    ts = []
    for i in range(2000):
        t = time.perf_counter_ns()
        idx.exact_provision(ids[i % len(ids)])
        ts.append((time.perf_counter_ns() - t) / 1e6)
    assert statistics.quantiles(ts, n=100)[94] <= v2.EXACT_WARM_P95_MS
    ts = []
    for _ in range(500):
        t = time.perf_counter_ns()
        idx.query(owner="authority", severity="critical")
        ts.append((time.perf_counter_ns() - t) / 1e6)
    assert statistics.quantiles(ts, n=100)[94] <= v2.MULTIFILTER_WARM_P95_MS


def test_build_within_20s_gate(live_db):
    t = time.monotonic()
    doc = v2.build_index(live_db)
    assert v2.validate_index(doc, live_db) == []
    assert time.monotonic() - t <= v2.BUILD_VERIFY_BUDGET_S


def test_atomic_publish_and_freshness(document, live_db, tmp_path):
    out = tmp_path / "idx.json"
    v2._validate_then_swap(document, live_db, out)
    idx = v2.load_published(out)
    idx.check_fresh(live_db)
    doc = dict(document)
    doc["counts"] = dict(document["counts"], rules=999)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(v2.IndexNotReady):
        v2.load_published(bad)
