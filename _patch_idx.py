"""One-shot patch: add build-time identity cross-validation to build-module-rule-index.py."""
from pathlib import Path

path = Path("scripts/build-module-rule-index.py")
text = path.read_text(encoding="utf-8")

anchor = "def _validate_zero_mixing(payload: dict, runtime_path: Path | None = None) -> list[str]:"
assert anchor in text, "anchor missing"

new_block = '''IDENTITY_DIRECTORY = (
    PROJECT_ROOT / "governance_rule" / "permission_directory" / "data" / "identity_directory.db"
)


def _load_directory_identities() -> dict:
    """Read-only projection of the canonical permission directory (A334/A607).

    The directory is the machine authority for module/sovereign identities;
    the build only reads it (``mode=ro``).  When it is unreachable the
    projection reports ``available=False`` so callers fail closed instead of
    silently skipping identity cross-validation.
    """
    if not IDENTITY_DIRECTORY.is_file():
        return {"available": False, "identities": [], "bound_roots": {}}
    try:
        import sqlite3

        db = sqlite3.connect(f"file:{IDENTITY_DIRECTORY}?mode=ro", uri=True)
        try:
            rows = list(
                db.execute(
                    "SELECT identity_id, identity_type, bound_tool_id, bound_roots"
                    " FROM identity"
                )
            )
        finally:
            db.close()
    except Exception:
        return {"available": False, "identities": [], "bound_roots": {}}
    identities = []
    bound_roots: dict[str, list[str]] = {}
    for iid, itype, tool_id, roots in rows:
        try:
            parsed = json.loads(roots) if roots else []
        except (TypeError, ValueError):
            parsed = []
        identities.append({"id": iid, "type": itype, "bound_tool_id": tool_id})
        bound_roots[iid] = [str(r) for r in parsed if isinstance(r, str)]
    return {"available": True, "identities": identities, "bound_roots": bound_roots}


def _norm_path(value: str) -> str:
    return str(value or "").replace("\\\\", "/").rstrip("/")


def _path_under(path: str, root: str) -> bool:
    p, r = _norm_path(path), _norm_path(root)
    return bool(p) and bool(r) and (p == r or p.startswith(r + "/"))


def _validate_directory_identity(payload: dict) -> tuple[list[str], dict]:
    """Cross-validate index module identities against the permission directory.

    Hard errors are *contradictions*: a component presenting a module-type
    directory identity (``component_id``/``execution_identity`` equal to an
    ``identity_id`` or ``bound_tool_id``) whose ``physical_path`` escapes that
    identity's ``bound_roots``.  Unregistered components, paths outside every
    bound root, and owner domains without a sovereign anchor are recorded in
    the report -- the index is module-derived and non-authoritative, so those
    degrade evidence rather than block generation.
    """
    report: dict = {
        "checked_at_utc": utc_now(),
        "directory": "governance_rule/permission_directory/data/identity_directory.db",
    }
    directory = _load_directory_identities()
    report["directory_available"] = bool(directory.get("available"))
    if not directory.get("available"):
        return ["identity directory unreachable -- cross-validation cannot run"], report

    identities = directory["identities"]
    bound_roots = directory["bound_roots"]
    by_id = {i["id"]: i for i in identities}
    by_tool = {i["bound_tool_id"]: i for i in identities if i["bound_tool_id"]}
    module_ids = {i["id"] for i in identities if i["type"] == "module"}
    sovereign_ids = {i["id"] for i in identities if i["type"] == "sovereign"}

    errors: list[str] = []
    backed: list[str] = []
    unregistered: list[str] = []
    retired: list[str] = []
    unbound_paths: list[str] = []
    unresolved_owners: set[str] = set()
    module_roots = [roots for iid, roots in bound_roots.items() if iid in module_ids]

    for mid, entry in (payload.get("modules") or {}).items():
        if not isinstance(entry, dict):
            continue
        exec_id = str(entry.get("execution_identity") or "")
        identity = next(
            (by_id[c] for c in (mid, exec_id) if c in by_id), None
        ) or next((by_tool[c] for c in (mid, exec_id) if c in by_tool), None)
        path = str(entry.get("physical_path") or "")
        is_retired = exec_id.startswith("retired-")

        if identity is None:
            unregistered.append(mid)
        elif is_retired:
            retired.append(mid)
        elif identity["type"] == "module":
            roots = bound_roots.get(identity["id"], [])
            if not path or not any(_path_under(path, r) for r in roots):
                errors.append(
                    f"{mid}: physical_path {path or '<empty>'!r} escapes "
                    f"bound_roots of directory identity {identity['id']!r}"
                )
            else:
                backed.append(mid)
        else:
            # sovereign/companion bound_roots are logical namespaces, not paths
            backed.append(mid)

        if path and not any(
            _path_under(path, r) for roots in module_roots for r in roots
        ):
            unbound_paths.append(mid)

        owner = str(entry.get("owner") or "")
        if (
            owner
            and f"{owner}-sovereign" not in sovereign_ids
            and owner not in sovereign_ids
        ):
            unresolved_owners.add(owner)

    report.update(
        {
            "directory_backed": sorted(backed),
            "unregistered_components": sorted(unregistered),
            "retired_components": sorted(retired),
            "unbound_paths": sorted(set(unbound_paths)),
            "unresolved_owner_domains": sorted(unresolved_owners),
        }
    )
    return errors, report


'''

text = text.replace(anchor, new_block + anchor, 1)

# hook into build_index: identity check after zero-mixing, report into payload
old_build = """    # Validate zero mixing before writing
    runtime_path = PROJECT_ROOT / "main-system" / "runtime" / "state" / "runtime-rule-index.json"
    errs = _validate_zero_mixing(payload, runtime_path)
    if errs:
        raise RuntimeError("module index validation failed: " + "; ".join(errs))"""
new_build = """    # Build-time identity cross-validation against the canonical permission
    # directory (P3 residual): contradictions fail the build; divergences are
    # recorded inside content_sha256-covered payload.
    identity_errors, identity_report = _validate_directory_identity(payload)
    payload["identity_validation"] = identity_report
    payload["content_sha256"] = content_hash(
        {k: v for k, v in payload.items() if k != "content_sha256"}
    )

    # Validate zero mixing before writing
    runtime_path = PROJECT_ROOT / "main-system" / "runtime" / "state" / "runtime-rule-index.json"
    errs = _validate_zero_mixing(payload, runtime_path)
    if identity_errors:
        errs = errs + identity_errors
    if errs:
        raise RuntimeError("module index validation failed: " + "; ".join(errs))"""
assert old_build in text
text = text.replace(old_build, new_build, 1)

# remove the earlier content_sha256 computation (moved after identity report)
old_hash = """    payload["content_sha256"] = content_hash({k: v for k, v in payload.items() if k != "content_sha256"})

    # Build-time identity cross-validation"""
new_hash = """    # Build-time identity cross-validation"""
assert old_hash in text
text = text.replace(old_hash, new_hash, 1)

# validate_index: also run identity contradiction checks on existing files
old_val = """    # generation atomicity: content_sha256 must match
    expected = data.get("content_sha256")
    actual = content_hash({k: v for k, v in data.items() if k != "content_sha256"})
    if expected != actual:
        errors.append(f"content_sha256 mismatch: expected {expected}, actual {actual}")
    return errors"""
new_val = """    # generation atomicity: content_sha256 must match
    expected = data.get("content_sha256")
    actual = content_hash({k: v for k, v in data.items() if k != "content_sha256"})
    if expected != actual:
        errors.append(f"content_sha256 mismatch: expected {expected}, actual {actual}")
    # identity cross-validation (contradictions only are errors)
    id_errors, _ = _validate_directory_identity(data)
    errors.extend(id_errors)
    return errors"""
assert old_val in text
text = text.replace(old_val, new_val, 1)

path.write_text(text, encoding="utf-8")
print("patched", path)
