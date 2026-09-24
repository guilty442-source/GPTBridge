"""A607 MODULE_RULE_INDEX_V1 fail-closed reader and scoped query service."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX = ROOT / "main-system" / "runtime" / "state" / "module-rule-index.json"
SCHEMA = "MODULE_RULE_INDEX_V1"
REQUIRED = {
    "module_identity", "rule_namespace", "rule_code", "controlling_provision",
    "module_contract", "contract_version", "source_revision", "scope",
    "capability", "trigger", "phase", "priority", "status", "content_hash",
    "introduced_version", "retired_version", "validated_against_codex_version",
    "authority_class",
}


def _hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if data.get("schema") != SCHEMA or data.get("contract_code") != SCHEMA:
        errors.append("schema mismatch")
    if data.get("authority_class") != "module-derived-non-authoritative":
        errors.append("authority class mismatch")
    if data.get("source_authority") != "postgresql://local/gptbridge_codex":
        errors.append("non-PostgreSQL source")
    claimed = data.get("content_hash")
    body = {k: v for k, v in data.items() if k != "content_hash"}
    if claimed != _hash(body):
        errors.append("content hash mismatch")
    modules = data.get("modules") if isinstance(data.get("modules"), dict) else {}
    seen: set[tuple[str, str, str, str]] = set()
    for rule in data.get("rules") if isinstance(data.get("rules"), list) else []:
        if not isinstance(rule, dict) or not REQUIRED.issubset(rule):
            errors.append("rule schema incomplete")
            continue
        key = tuple(str(rule[x]) for x in ("module_identity", "rule_namespace", "rule_code", "contract_version"))
        if key in seen:
            errors.append("duplicate rule key")
        seen.add(key)
        if rule["module_identity"] not in modules:
            errors.append("unregistered module")
        if re.fullmatch(r"A\d+", str(rule.get("rule_code", ""))):
            errors.append("runtime-index identity mixed into module index")
        if rule["authority_class"] != "module-derived-non-authoritative":
            errors.append("rule authority mismatch")
    return errors


def load_index(*, validate: bool = True) -> dict[str, Any]:
    try:
        data = json.loads(DEFAULT_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"MODULE_RULE_INDEX_NOT_READY:{exc}") from exc
    errors = _validate(data) if validate else []
    if errors:
        raise RuntimeError("MODULE_RULE_INDEX_NOT_READY:" + ";".join(errors))
    return data


def query(*, caller_module: str | None = None, module_id: str | None = None, capability: str | None = None,
          scope: str | None = None, status: str | None = "current",
          contract_version: str | None = None, **_: object) -> list[dict[str, Any]]:
    try:
        data = load_index(validate=True)
    except RuntimeError:
        return []
    if not caller_module:
        return []
    requested_module = module_id or caller_module
    if requested_module != caller_module:
        return []
    result = []
    for rule in data.get("rules", []):
        if rule.get("module_identity") != requested_module: continue
        if capability is not None and rule.get("capability") != capability: continue
        if scope is not None and rule.get("scope") != scope: continue
        if status is not None and rule.get("status") != status: continue
        if contract_version is not None and rule.get("contract_version") != contract_version: continue
        result.append(rule)
    return result


__all__ = ["load_index", "query"]
