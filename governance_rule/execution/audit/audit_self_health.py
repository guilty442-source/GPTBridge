"""Self-health test-coverage declaration verification for the audit.

Codex (test-framework-final-ownership / native-test-runner-register /
python-final-minimum-responsibility): pytest is forbidden and Python
files outside the two request-scoped responsibilities are
non-conforming source.  The legacy barrier — "every governed tool keeps
a collectable pytest file" — is therefore retired; the replacement
barrier verifies that every governed tool declares test coverage in a
conforming form:

- ``native-suite:<name>`` — resolves to ``native/test_suites/suite_<name>.cpp``
  (the unit-tier harness registered to the C# orchestrator).
- ``pending-native:<dir-prefix>`` — migration declaration: the tool's
  retired pytest surface must be tracked in
  ``pytest_retirement_inventory.json`` (the native-migration worklist);
  a prefix with no PENDING rows is a false declaration.
- an existing non-Python file path — a real on-disk test artifact.

A ``.py`` target or a missing file is a conformance error.  Tools whose
coverage is not yet natively expressed declare ``pending-native`` and
the claim is checked against the inventory instead of thin air.
"""

from __future__ import annotations

import json
from pathlib import Path


_INVENTORY_REL = Path(
    "governance_rule/execution/audit/pytest_retirement_inventory.json"
)


def _declared_self_health_test_targets(
    root: Path,
    errors: list[str],
) -> list[tuple[str, str]]:
    """Collect ``(tool_id, target)`` declarations from tool manifests."""
    declared: list[tuple[str, str]] = []
    manifest_paths = [
        *root.glob("*/manifest.json"),
        *root.glob("Standalone tools/*/manifest.json"),
        *root.glob("Standalone tools/*/*/manifest.json"),
    ]
    for manifest_path in sorted(set(manifest_paths)):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(
                f"self-health manifest is unreadable: "
                f"{manifest_path.relative_to(root).as_posix()}: {exc}"
            )
            continue
        tool_id = str(manifest.get("id") or "").strip()
        lifecycle = manifest.get("lifecycle")
        if manifest.get("enabled") is False or (
            isinstance(lifecycle, dict)
            and str(lifecycle.get("status") or "").strip().casefold()
            == "retired"
        ):
            # A retired owner is source lineage evidence, not a governed
            # executable; it declares no test-coverage barrier.
            continue
        targets = manifest.get("test_targets")
        if not tool_id or not isinstance(targets, list) or not targets:
            errors.append(
                "governed tool must declare test_targets: "
                f"{manifest_path.relative_to(root).as_posix()}"
            )
            continue
        tool_root = manifest_path.parent.resolve()
        for raw_target in targets:
            declared.append((tool_id, str(raw_target or "").strip(), tool_root))
    return declared


def _pending_native_prefixes(root: Path) -> frozenset[str]:
    """Directory prefixes that own PENDING native-migration rows."""
    try:
        inventory = json.loads(
            (root / _INVENTORY_REL).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return frozenset()
    prefixes: set[str] = set()
    for row in inventory.get("rows", []):
        if not isinstance(row, dict) or row.get("status") != "PENDING":
            continue
        source = str(row.get("source_test") or "")
        if source:
            prefixes.add(source.rsplit("/", 1)[0] + "/")
    return frozenset(prefixes)


def _verify_self_health_test_files(
    root: Path,
    errors: list[str],
) -> None:
    """Verify every declared test target is conforming coverage."""
    declared = _declared_self_health_test_targets(root, errors)
    pending_prefixes: frozenset[str] | None = None

    for tool_id, target, tool_root in declared:
        if target.startswith("pending-native:"):
            if pending_prefixes is None:
                pending_prefixes = _pending_native_prefixes(root)
            prefix = target.split(":", 1)[1].rstrip("/") + "/"
            if not any(
                p.startswith(prefix) or prefix.startswith(p)
                for p in pending_prefixes
            ):
                errors.append(
                    f"pending-native declaration has no PENDING inventory "
                    f"rows: {tool_id}: {target}"
                )
            continue
        if target.startswith("native-suite:"):
            name = target.split(":", 1)[1]
            suite = root / "native" / "test_suites" / f"suite_{name}.cpp"
            if not suite.is_file():
                errors.append(
                    f"native-suite target has no harness: "
                    f"{tool_id}: {target}"
                )
            continue
        candidate = (tool_root / target).resolve()
        try:
            relative = candidate.relative_to(root).as_posix()
            candidate.relative_to(tool_root)
        except ValueError:
            errors.append(
                f"self-health test target escaped tool root: "
                f"{tool_id}: {target}"
            )
            continue
        if candidate.suffix.casefold() == ".py":
            errors.append(
                f"non-conforming Python test target (FORBID:pytest): "
                f"{tool_id}: {target}"
            )
            continue
        if not candidate.is_file():
            errors.append(
                f"self-health test target is missing: {tool_id}: "
                f"{relative}"
            )
