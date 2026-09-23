"""G92 normative classification & convergence consumer tests (A608/A609)."""
from __future__ import annotations

import sqlite3

import pytest

from governance_rule.execution.convergence import normative_classification as nc


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        create table codex_normative_category_directory(
            category_code text, category_kind text, authority_class text,
            owner text, status text, version_identity text, content_hash text);
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
        create table provision_lifecycle_status(
            provision_type text, provision_id text, lifecycle_state text,
            effective_version text, successor_identity text, evidence text,
            legacy_effective_version text, current_binding_version text);
        """
    )
    for cat in ("PRINCIPLE", "ARTICLE", "EDICT", "RESPONSIBILITY", "PROHIBITION"):
        conn.execute(
            "insert into codex_normative_category_directory values(?,?,?,?,?,?,?)",
            (cat, "x", "x", "permission-sovereign", "active", "v", "h"),
        )
    yield conn
    conn.close()


def _add_prov(db, ptype, pid, *, lifecycle="active", status="current", cat=None):
    db.execute(
        "insert into provision_normative_category values(?,?,?,?,?,?,?,?,?,?)",
        (ptype, pid, cat or nc.STRUCTURAL_CATEGORIES[ptype], 0, 0, lifecycle,
         "", status, "v", "h"),
    )
    db.execute(
        "insert into provision_lifecycle_status values(?,?,?,?,?,?,?,?)",
        (ptype, pid, lifecycle, "v", None, "", None, "v"),
    )


def test_five_category_parity_passes_on_live_codex():
    with nc.open_db() as conn:
        assert nc.validate_classification(conn) == []


def test_structural_category_must_match_provision_type(db):
    _add_prov(db, "article", "A1", cat="EDICT")
    errors = nc.validate_classification(db)
    assert any("structural category mismatch" in e for e in errors)


def test_restatement_resolves_to_unique_controller(db):
    _add_prov(db, "edict", "E1")
    _add_prov(db, "article", "A9")
    db.execute(
        "insert into codex_normative_convergence_registry values(?,?,?,?,?,?,?,?,?,?,?)",
        ("C1", "edict", "E1", "article", "A9", "restatement-non-independent",
         "supplemental", "edict-over-restatement", "current", "v", "h"),
    )
    conv = nc.load_convergence(db)
    assert nc.resolve_controller(conv, "article", "A9") == "edict:E1"
    assert nc.resolve_controller(conv, "edict", "E1") == "edict:E1"
    assert nc.is_subordinate_restatement(conv, "article", "A9")
    assert not nc.is_subordinate_restatement(conv, "edict", "E1")


def test_multiple_current_controllers_flagged(db):
    _add_prov(db, "edict", "E1")
    _add_prov(db, "edict", "E2")
    _add_prov(db, "article", "A9")
    for i, ctl in enumerate(("E1", "E2")):
        db.execute(
            "insert into codex_normative_convergence_registry values(?,?,?,?,?,?,?,?,?,?,?)",
            (f"C{i}", "edict", ctl, "article", "A9", "restatement-non-independent",
             "supplemental", "x", "current", "v", "h"),
        )
    errors = nc.validate_classification(db)
    assert any("multiple current controllers" in e for e in errors)


def test_controller_must_be_current_effective(db):
    _add_prov(db, "edict", "E1", lifecycle="superseded", status="historical")
    _add_prov(db, "article", "A9")
    db.execute(
        "insert into codex_normative_convergence_registry values(?,?,?,?,?,?,?,?,?,?,?)",
        ("C1", "edict", "E1", "article", "A9", "restatement-non-independent",
         "supplemental", "x", "current", "v", "h"),
    )
    errors = nc.validate_classification(db)
    assert any("controller not current-effective" in e for e in errors)


def test_restatements_excluded_from_controlling_set(db):
    _add_prov(db, "edict", "E1")
    _add_prov(db, "article", "A9")
    db.execute(
        "insert into codex_normative_convergence_registry values(?,?,?,?,?,?,?,?,?,?,?)",
        ("C1", "edict", "E1", "article", "A9", "restatement-non-independent",
         "supplemental", "x", "current", "v", "h"),
    )
    cls = nc.load_classifications(db)
    conv = nc.load_convergence(db)
    exposed = nc.controlling_provisions(cls, conv)
    assert set(exposed) == {"edict:E1"}
