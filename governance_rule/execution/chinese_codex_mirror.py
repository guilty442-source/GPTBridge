"""Load the single Chinese Codex mirror from its three ordered physical parts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


PART_NAMES = tuple(f"governance_codex.zh-TW.part-{index}.txt" for index in range(1, 4))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def load_chinese_codex_parts(codex_root: Path) -> dict[str, Any]:
    """Validate and assemble the one logical mirror without creating a fourth copy."""
    parts = [
        json.loads((codex_root / name).read_text(encoding="utf-8"))
        for name in PART_NAMES
    ]
    version = str(parts[0]["codex_version"])
    mirror_id = str(parts[0]["mirror_id"])
    assembled_hash = str(parts[0]["assembled_payload_hash"])
    previous_hash = "0" * 64
    tables: dict[str, list[dict[str, object]]] = {}
    for expected_index, part in enumerate(parts, 1):
        if (
            part.get("part_index") != expected_index
            or part.get("part_count") != 3
            or str(part.get("codex_version")) != version
            or str(part.get("mirror_id")) != mirror_id
            or str(part.get("assembled_payload_hash")) != assembled_hash
            or str(part.get("previous_part_hash")) != previous_hash
        ):
            raise ValueError("Chinese Codex mirror part identity or chain mismatch")
        unsigned = {key: value for key, value in part.items() if key != "part_hash"}
        actual_part_hash = hashlib.sha256(_canonical(unsigned)).hexdigest()
        if actual_part_hash != str(part.get("part_hash")):
            raise ValueError("Chinese Codex mirror part hash mismatch")
        previous_hash = actual_part_hash
        for table, rows in part["tables"].items():
            tables.setdefault(str(table), []).extend(rows)
    assembled = {"codex_version": version, "tables": tables}
    if hashlib.sha256(_canonical(assembled)).hexdigest() != assembled_hash:
        raise ValueError("Chinese Codex assembled payload hash mismatch")
    return assembled


def render_chinese_codex(codex_root: Path) -> str:
    """Render an authorized session response from the assembled logical mirror."""
    return json.dumps(
        load_chinese_codex_parts(codex_root), ensure_ascii=False, indent=2
    ) + "\n"


__all__ = ["PART_NAMES", "load_chinese_codex_parts", "render_chinese_codex"]
