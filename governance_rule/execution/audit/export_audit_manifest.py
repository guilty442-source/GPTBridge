"""Export the governed audit-check manifest for the native audit engine.

``star-audit-manifest/v1`` — the *data* driving
``native/audit/audit_engine.cpp`` (§1.1 權限核心 C/C++ 審計路徑；P0-9).

The governed Python tooling remains the **source of truth for what must
be checked**: protected-source lists come from the live policy/directory
snapshots, forbidden paths from the audit registry, pollution targets
from the codex tree.  The engine only executes — it never decides which
files are protected.

Shadow semantics (same dual-track as the E1 execution prototypes):

- checks reducible to static file operations are emitted with native
  ``kind`` (``file-exists`` / ``file-not-exists`` / ``file-readonly`` /
  ``file-contains`` / ``file-not-contains`` / ``text-no-pollution`` /
  ``json-parses`` / ``json-has-keys`` / ``json-key-value`` /
  ``json-key-absent`` / ``json-array-min-count`` /
  ``glob-min-count`` / ``glob-contains`` / ``glob-not-contains`` /
  ``glob-absent`` / ``file-not-contains-unless`` / ``fail``) —
  the engine verifies them directly;
- every Python ``check_*`` function not fully reducible is emitted as a
  ``delegated`` record — explicit, counted, never silently dropped;
- regenerating after any governance-data change is the cache-invalidation
  policy (法典變更 → manifest 重算，P0-9 ④).

Usage::

    python -m governance_rule.execution.audit.export_audit_manifest \
        --root E:/GPTBridge [--out <path>]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from governance_rule.execution.audit.export_manifest_emit import ManifestEmit
from governance_rule.execution.audit.export_manifest_reducers import (
    emit_reducible,
)
from governance_rule.execution.audit.export_manifest_static import (
    emit_foundation,
    emit_static_axes,
    emit_static_governance,
    emit_static_metadata_browser,
    emit_static_native_boundary,
    emit_static_retirement,
)
from governance_rule.execution.audit.export_manifest_tables import (
    emit_activation_states,
    emit_codex_consistency,
    emit_contract_axes,
    emit_directory_audit,
    emit_formal_rules,
    emit_implementation_obligations,
)
from governance_rule.execution.audit.export_manifest_policy import (
    emit_authority_policies,
    emit_git_tiers_remainder,
    emit_identity_bindings,
    emit_shared_layer_policies,
)
from governance_rule.execution.audit.export_manifest_scans import (
    emit_architecture_registry,
    emit_delegated,
    emit_gpu_renderer,
    emit_owned_sources,
    emit_pkg_layers,
    emit_protected_remainder,
    emit_sql_patterns,
    emit_tm_parity,
    emit_tm_rows,
    emit_tool_manifests,
    emit_tree_policies,
    emit_worker_pools,
)


def build_manifest(root: Path) -> dict[str, object]:
    sys.path.insert(0, str(root))
    checks: list[dict[str, object]] = []
    ctx = ManifestEmit(root, checks)

    emit_foundation(ctx)
    emit_reducible(ctx)
    emit_static_metadata_browser(ctx)
    emit_static_governance(ctx)
    emit_static_native_boundary(ctx)
    emit_static_retirement(ctx)
    emit_static_axes(ctx)
    emit_tool_manifests(ctx)
    emit_contract_axes(ctx)
    emit_activation_states(ctx)
    emit_formal_rules(ctx)
    emit_implementation_obligations(ctx)
    emit_authority_policies(ctx)
    emit_shared_layer_policies(ctx)
    emit_identity_bindings(ctx)
    emit_git_tiers_remainder(ctx)
    emit_codex_consistency(ctx)
    emit_directory_audit(ctx)
    emit_architecture_registry(ctx)
    emit_gpu_renderer(ctx)
    emit_worker_pools(ctx)
    emit_sql_patterns(ctx)
    emit_protected_remainder(ctx)
    emit_tm_parity(ctx, emit_tm_rows(ctx))
    emit_owned_sources(ctx)
    emit_pkg_layers(ctx)
    emit_tree_policies(ctx)
    emit_delegated(ctx)

    return {
        "schema": "star-audit-manifest/v1",
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generator": "governance_rule.execution.audit.export_audit_manifest",
        "coverage": {
            "native_checks": sum(
                1 for c in checks if c["kind"] != "delegated"),
            "delegated_checks": sum(
                1 for c in checks if c["kind"] == "delegated"),
            "native_kinds": [
                "file-exists", "file-not-exists", "file-readonly",
                "dir-exists", "file-contains", "file-not-contains",
                "file-not-contains-unless",
                "text-no-pollution", "json-parses", "json-has-keys",
                "json-key-absent",
                "glob-min-count", "glob-not-contains", "glob-absent",
                "glob-contains", "json-key-value", "py-bucket-budget",
                "json-array-min-count", "fail",
            ],
        },
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--out",
        default=str(
            Path("governance_rule") / "execution" / "audit"
            / "audit_checks_manifest.json"
        ),
    )
    args = parser.parse_args()
    root = Path(args.root).resolve()
    manifest = build_manifest(root)
    out = Path(args.out)
    if not out.is_absolute():
        out = root / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    coverage = manifest["coverage"]
    print(
        f"manifest written: {out} "
        f"(native={coverage['native_checks']} "
        f"delegated={coverage['delegated_checks']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
