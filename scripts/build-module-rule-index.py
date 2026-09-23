"""Derived module rule index for governance — module-derived, non-authoritative.

Authority contract (A607):
* The permission core owns the index lifecycle; each owning module governs its
  own content slice.  The codex database remains the sole authority for
  normative provisions; this index is module-derived and never changes
  provision effect.
* Deleting the output is always safe; regenerate with this script.
* Zero mixing with the runtime rule index: module entries use module/component
  identifiers, never ``A<digits>`` provision codes.  A validator enforces it.

CLI::

    python scripts/build-module-rule-index.py [--output <path>] [--summary]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARCH_REGISTRY = PROJECT_ROOT / "governance_rule" / "execution" / "audit" / "architecture_registry.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "main-system" / "runtime" / "state" / "module-rule-index.json"

SCHEMA = "gptbridge-module-rule-index/v1"
CLASSIFIER_VERSION = "module-index/v1"
AUTHORITY = (
    "module-derived-non-authoritative; permission-core-owned; "
    "owning-module-governs-content; zero-mixing-with-runtime-index; "
    "governance_rule/codex remains sole normative authority"
)

# Lightweight classification for module-derived entries — kept index-only.


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def content_hash(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_registry() -> dict:
    try:
        data = json.loads(ARCH_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"architecture registry unavailable: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _extract_modules(registry: dict) -> list[dict]:
    """Extract module/component entries from the architecture registry.

    The registry is the single topology authority; each entry becomes an
    indexable module rule source.  Only the registry is consumed — no codex
    provision is ever indexed here, guaranteeing zero mixing.
    """
    components = registry.get("components") if isinstance(registry.get("components"), list) else registry.get("modules") if isinstance(registry.get("modules"), list) else []
    # Fallbacks for older shapes
    if not components:
        # Some versions store under 'architecture' or 'topology'
        for key in ("architecture", "topology", "elements"):
            val = registry.get(key)
            if isinstance(val, list) and val:
                components = val
                break
    out: list[dict] = []
    for entry in components:
        if not isinstance(entry, dict):
            continue
        comp_id = str(entry.get("component_id") or entry.get("module_id") or entry.get("id") or "").strip()
        if not comp_id:
            continue
        owner = str(entry.get("owner_sovereign") or entry.get("owner") or "").strip()
        role = str(entry.get("architectural_role") or entry.get("role") or "").strip()
        form = str(entry.get("runtime_form") or entry.get("form") or "").strip()
        lifecycle = str(entry.get("lifecycle") or "").strip()
        path = str(entry.get("physical_path") or entry.get("path") or "").strip()
        out.append(
            {
                "module_id": comp_id,
                "owner": owner,
                "architectural_role": role,
                "runtime_form": form,
                "lifecycle": lifecycle,
                "physical_path": path,
            }
        )
    # Deduplicate by module_id
    seen: dict[str, dict] = {}
    for m in out:
        seen.setdefault(m["module_id"], m)
    return sorted(seen.values(), key=lambda x: x["module_id"])


def _validate_zero_mixing(payload: dict, runtime_path: Path | None = None) -> list[str]:
    """Validate that no module entry collides with runtime provision codes.

    Runtime provisions are ``A<digits>`` (e.g. A605).  Module entries must not
    use that namespace.  When a runtime index file is present, also ensure no
    shared keys.
    """
    errors: list[str] = []
    modules = payload.get("modules") or {}
    for mid in modules.keys():
        if re.fullmatch(r"A\d{1,4}", mid):
            errors.append(f"module_id collides with runtime provision code: {mid}")
    if runtime_path and runtime_path.is_file():
        try:
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime_ids = set((runtime.get("provisions") or {}).keys())
            # rules are list of dicts with rule_code
            for r in runtime.get("rules") or []:
                if isinstance(r, dict) and r.get("rule_code"):
                    runtime_ids.add(str(r.get("rule_code")))
                elif isinstance(r, str):
                    runtime_ids.add(r)
            overlap = set(modules.keys()) & runtime_ids
            if overlap:
                errors.append(f"zero-mixing violated — overlap with runtime index: {sorted(overlap)[:5]}")
        except (OSError, ValueError):
            pass
    return errors


def build_index(output: Path) -> dict:
    registry = _load_registry()
    modules = _extract_modules(registry)

    # Build indexes
    by_owner: dict[str, list[str]] = {}
    by_role: dict[str, list[str]] = {}
    by_lifecycle: dict[str, list[str]] = {}
    module_map: dict[str, dict] = {}

    for m in modules:
        mid = m["module_id"]
        module_map[mid] = m
        if m["owner"]:
            by_owner.setdefault(m["owner"], []).append(mid)
        if m["architectural_role"]:
            by_role.setdefault(m["architectural_role"], []).append(mid)
        if m["lifecycle"]:
            by_lifecycle.setdefault(m["lifecycle"], []).append(mid)

    # Sort index lists
    for bucket in (by_owner, by_role, by_lifecycle):
        for k in bucket:
            bucket[k] = sorted(bucket[k])

    payload = {
        "schema": SCHEMA,
        "generated_at_utc": utc_now(),
        "generator": "scripts/build-module-rule-index.py",
        "authority": AUTHORITY,
        "source": {
            "architecture_registry": "governance_rule/execution/audit/architecture_registry.json",
            "registry_component_count": len(modules),
        },
        "classification": {
            "classifier_version": CLASSIFIER_VERSION,
            "index_only": True,
            "owner_governs_content": True,
        },
        "counts": {
            "modules": len(module_map),
            "owners": len(by_owner),
            "roles": len(by_role),
            "lifecycles": len(by_lifecycle),
        },
        "modules": module_map,
        "indexes": {
            "by_owner": {k: sorted(v) for k, v in sorted(by_owner.items())},
            "by_role": {k: sorted(v) for k, v in sorted(by_role.items())},
            "by_lifecycle": {k: sorted(v) for k, v in sorted(by_lifecycle.items())},
        },
    }
    payload["content_sha256"] = content_hash({k: v for k, v in payload.items() if k != "content_sha256"})

    # Validate zero mixing before writing
    runtime_path = PROJECT_ROOT / "main-system" / "runtime" / "state" / "runtime-rule-index.json"
    errs = _validate_zero_mixing(payload, runtime_path)
    if errs:
        raise RuntimeError("module index validation failed: " + "; ".join(errs))

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, output)
    return payload


def validate_index(path: Path) -> list[str]:
    """Validator for the module index file — returns error strings."""
    errors: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"unreadable: {exc}"]
    if data.get("schema") != SCHEMA:
        errors.append(f"schema mismatch: expected {SCHEMA}, got {data.get('schema')}")
    if not data.get("authority", "").startswith("module-derived"):
        errors.append("authority must be module-derived-non-authoritative")
    if not isinstance(data.get("modules"), dict):
        errors.append("modules must be a dict")
    # zero mixing
    errors.extend(_validate_zero_mixing(data, PROJECT_ROOT / "main-system" / "runtime" / "state" / "runtime-rule-index.json"))
    # generation atomicity: content_sha256 must match
    expected = data.get("content_sha256")
    actual = content_hash({k: v for k, v in data.items() if k != "content_sha256"})
    if expected != actual:
        errors.append(f"content_sha256 mismatch: expected {expected}, actual {actual}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--validate", action="store_true", help="validate existing index instead of building")
    args = parser.parse_args(argv)
    out = Path(args.output)
    if args.validate:
        errs = validate_index(out)
        if errs:
            for e in errs:
                print(f"FAIL: {e}")
            return 1
        print(f"PASS: {out} schema {SCHEMA} modules {(json.loads(out.read_text(encoding='utf-8')).get('counts') or {}).get('modules', '?')}")
        return 0
    payload = build_index(out)
    if args.summary:
        print(json.dumps({"output": str(out), "schema": payload["schema"], "content_sha256": payload["content_sha256"], "counts": payload["counts"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
