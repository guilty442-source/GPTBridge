"""Golden Git baseline freeze table (A185 split).

Extracted from ``baseline/__init__`` (source-size contract): evaluates the
sixteen article-536 freeze conditions — a condition without measured
evidence is UNVERIFIED, never PASS — and derives the overall verdict.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from . import static_gate
from .introspection import _component_map, _documentation_scan, _hook_evidence


def _gate_evidence(
    evidence: Optional[Mapping[str, Any]], key: str
) -> Optional[dict[str, Any]]:
    if not evidence:
        return None
    gates = evidence.get("gates")
    if not isinstance(gates, Mapping):
        return None
    entry = gates.get(key)
    return dict(entry) if isinstance(entry, Mapping) else None


def build_freeze_table(
    scan_result: Optional[dict[str, Any]] = None,
    gate_evidence: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Evaluate the sixteen article 536 conditions (never default PASS)."""
    scan_result = scan_result or static_gate.scan()
    counts = scan_result["counts"]

    def gate(key: str) -> Optional[dict[str, Any]]:
        return _gate_evidence(gate_evidence, key)

    def gate_status(key: str, *, required: bool = True) -> tuple[str, list[str]]:
        entry = gate(key)
        if entry is None:
            return ("UNVERIFIED" if required else "PASS"), [
                f"no evidence recorded for gate '{key}'"
            ]
        result = str(entry.get("result", "UNVERIFIED")).upper()
        evidence = [str(item) for item in entry.get("evidence", [])] or [
            f"gate '{key}' result={result}"
        ]
        return result, evidence

    rows: list[dict[str, Any]] = []
    row = rows.append

    # 1. all git writes through the gateway
    gateway_missing = "GitExecutionGateway" not in _component_map()["runtime"]
    status = "FAIL" if counts["confirmed_bool"] or gateway_missing else "PASS"
    row({
        "condition": "all_git_writes_via_gateway",
        "zh": "所有 Git write 經 Gateway",
        "status": status,
        "evidence": [
            f"GitExecutionGateway class: "
            f"{'MISSING' if gateway_missing else 'PRESENT'}",
            f"production confirmed=True call sites: {counts['confirmed_bool']}",
        ],
    })

    # 2. no illegal direct subprocess git callers
    illegal = counts.get("illegal_direct_git", 0)
    status = "FAIL" if illegal else "PASS"
    row({
        "condition": "no_illegal_direct_subprocess_git",
        "zh": "Direct subprocess Git 無非法 caller",
        "status": status,
        "evidence": [f"illegal call sites (A375 whitelist): {illegal}"]
        + scan_result["categories"].get("illegal_direct_git", [])[:10],
    })

    # 3. confirmed bool on production paths = 0
    status = "FAIL" if counts["confirmed_bool"] else "PASS"
    row({
        "condition": "confirmed_bool_production_zero",
        "zh": "confirmed bool 正式路徑 = 0",
        "status": status,
        "evidence": [f"production call sites: {counts['confirmed_bool']}"]
        + scan_result["categories"]["confirmed_bool"][:10],
    })

    # 4. AI tier-3 self approval = 0
    legacy_env = counts["legacy_env"]
    status = "FAIL" if legacy_env else "PASS"
    row({
        "condition": "ai_tier3_self_approval_zero",
        "zh": "AI Tier3 self approval = 0",
        "status": status,
        "evidence": [
            f"legacy approval env occurrences: {legacy_env}",
            "capability_gate refuses automation tier-3 legacy approval, but "
            "production paths still call GitRepository.run directly",
        ],
    })

    # 5. worker/queue/main invariants
    inv_status = "PASS"
    inv_evidence: list[str] = []
    for key in ("stress_50_workers", "chaos"):
        gs, ge = gate_status(key)
        inv_evidence.extend([f"{key}: {gs}"] + ge[:3])
        if gs != "PASS":
            inv_status = "UNVERIFIED" if gs == "UNVERIFIED" else "FAIL"
    row({
        "condition": "worker_queue_main_invariants",
        "zh": "worker/queue/main invariants PASS",
        "status": inv_status,
        "evidence": inv_evidence,
    })

    # 6. audit chain
    from .. import audit_chain

    try:
        health = audit_chain.chain_health()
    except Exception as exc:  # pragma: no cover - defensive
        health = {"chain_valid": False, "error": str(exc)}
    gs, ge = gate_status("audit_chain")
    status = "PASS" if health.get("chain_valid") and gs == "PASS" else (
        "UNVERIFIED" if gs == "UNVERIFIED" else "FAIL"
    )
    row({
        "condition": "audit_chain_pass",
        "zh": "audit chain PASS",
        "status": status,
        "evidence": [f"chain_health.chain_valid={health.get('chain_valid')}"] + ge[:3],
    })

    # 7. hooks integrity
    hooks = _hook_evidence()
    gs, ge = gate_status("hooks_integrity")
    status = "PASS" if hooks["templates_ok"] and gs == "PASS" else (
        "UNVERIFIED" if gs == "UNVERIFIED" else "FAIL"
    )
    row({
        "condition": "hooks_integrity_pass",
        "zh": "hooks integrity PASS",
        "status": status,
        "evidence": [
            f"governed templates ok={hooks['templates_ok']}",
            f"generation={hooks['live_generation'][:23]}",
        ] + ge[:3],
    })

    # 8. capability tests
    gs, ge = gate_status("capability")
    row({
        "condition": "capability_tests_pass",
        "zh": "capability tests PASS",
        "status": gs,
        "evidence": ge,
    })

    # 10-16. measured gates
    direct_gates = (
        ("stress_50_workers_pass", "50 worker stress PASS", "stress_50_workers"),
        ("chaos_pass", "chaos PASS", "chaos"),
        ("dr_pass", "DR PASS", "dr"),
        ("rolling_upgrade_pass", "rolling upgrade PASS", "rolling_upgrade"),
        ("performance_regression_gate_pass", "performance regression gate PASS",
         "performance"),
        ("architecture_tests_pass", "architecture tests PASS",
         "architecture"),
        ("documentation_sync_pass", "documentation sync PASS", "documentation"),
    )
    for condition, zh, key in direct_gates:
        gs, ge = gate_status(key)
        if key == "documentation":
            doc = _documentation_scan()
            gs = doc["verdict"] if not ge or ge[0].startswith("no evidence") else gs
            ge = [f"stale findings: {len(doc['stale_findings'])}"] + doc[
                "stale_findings"
            ][:5] + ge[:2]
        row({
            "condition": condition,
            "zh": zh,
            "status": gs if gs in {"PASS", "FAIL", "UNVERIFIED"} else "UNVERIFIED",
            "evidence": ge,
        })
    return rows


def overall_verdict(freeze_table: list[dict[str, Any]]) -> tuple[str, list[str]]:
    blockers = [
        f"{row['condition']}:{row['status']}"
        for row in freeze_table
        if row["status"] != "PASS"
    ]
    if blockers:
        return "GIT_BASELINE_NOT_FROZEN", blockers
    return "GIT_BASELINE_FROZEN", []


__all__ = ["build_freeze_table", "overall_verdict", "_gate_evidence"]
