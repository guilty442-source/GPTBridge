#!/usr/bin/env python3
"""Build the canonical identity directory SQLite database.

This script creates a read-only SQLite database at
``governance_rule/permission_directory/data/identity_directory.db``
containing the fixed identity-group codes and names for every sovereign and
module.  The directory is derived from the Governance Codex sovereigns and the
existing capability/permission registries.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DB_PATH = ROOT / "governance_rule" / "permission_directory" / "data" / "identity_directory.db"
SCHEMA_PATH = ROOT / "governance_rule" / "permission_directory" / "database" / "identity_directory_schema.sql"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    return sqlite3.connect(DB_PATH)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS identity_group (
    group_id            TEXT PRIMARY KEY,
    group_name          TEXT NOT NULL,
    display_name_zh     TEXT NOT NULL,
    group_type          TEXT NOT NULL,
    management_authority TEXT NOT NULL,
    registry_mode       TEXT NOT NULL,
    legacy_identity_compatibility INTEGER NOT NULL DEFAULT 0,
    aliases_allowed     INTEGER NOT NULL DEFAULT 0,
    unknown_identity_access TEXT NOT NULL,
    authentication      TEXT NOT NULL,
    active              INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS identity (
    identity_id         TEXT PRIMARY KEY,
    identity_code       TEXT NOT NULL UNIQUE,
    identity_name       TEXT NOT NULL,
    display_name_zh     TEXT NOT NULL,
    identity_type       TEXT NOT NULL CHECK (identity_type IN ('sovereign', 'module', 'companion')),
    group_id            TEXT NOT NULL REFERENCES identity_group(group_id),
    actor               TEXT NOT NULL UNIQUE,
    bound_tool_id       TEXT NOT NULL,
    bound_roots         TEXT NOT NULL DEFAULT '[]',
    manifest_required   INTEGER NOT NULL DEFAULT 0,
    manifest_path_template TEXT NOT NULL DEFAULT '',
    manifest_tool_id_field TEXT NOT NULL DEFAULT '',
    manifest_max_bytes  INTEGER NOT NULL DEFAULT 0,
    authentication      TEXT NOT NULL DEFAULT 'governance-policy-issued-capability-token'
);

CREATE TABLE IF NOT EXISTS identity_required_capability (
    identity_id         TEXT NOT NULL REFERENCES identity(identity_id),
    capability          TEXT NOT NULL,
    PRIMARY KEY (identity_id, capability)
);

CREATE TABLE IF NOT EXISTS identity_manifest_requirement (
    identity_id         TEXT NOT NULL REFERENCES identity(identity_id),
    field_path          TEXT NOT NULL,
    expected_value      TEXT NOT NULL,
    PRIMARY KEY (identity_id, field_path)
);

CREATE TABLE IF NOT EXISTS identity_permission (
    identity_id         TEXT NOT NULL REFERENCES identity(identity_id),
    capability          TEXT NOT NULL,
    PRIMARY KEY (identity_id, capability)
);
"""


def _module_display_names() -> dict[str, tuple[str, str]]:
    return {
        "main-system": ("Main System", "主系統"),
        "governance_rule": ("Governance Rule", "治理規則"),
        "shared-layer": ("Shared Layer", "共用層"),
        "ai-assistant": ("AI Assistant", "AI 助手"),
        "ai-collaboration": ("AI Collaboration", "AI 協作"),
        "file-sorter": ("File Sorter", "檔案排序"),
        "global-cleaner": ("Global Cleaner", "全域清理"),
        "investment-mobile": ("Investment Mobile", "投資行動"),
        "xingcheng": ("Xingcheng", "星澄"),
        "star-chat": ("Star Chat", "星聊"),
        "vaultly": ("Vaultly", "Vaultly"),
    }


def _sovereign_actor(sov_id: str) -> str:
    return f"governance/sovereign/{sov_id}"


def _identity_type_for_tool(tool_id: str) -> str:
    if tool_id == "star-chat":
        return "companion"
    return "module"


def _sovereign_identities(conn: sqlite3.Connection) -> None:
    sys.path.insert(0, str(ROOT))
    from governance_rule.codex import GOVERNANCE_CODEX
    from governance_rule.codex.sovereigns_chinese import SOVEREIGNS_CHINESE
    from governance_rule.permission_directory.registries.permissions.identity_groups import (
        CAPABILITY_IDENTITIES,
    )

    tool_ids = {ci.bound_tool_id for ci in CAPABILITY_IDENTITIES}
    name_map = {s.id: (s.id.replace("-", " ").title(), s.name) for s in GOVERNANCE_CODEX.sovereigns}
    for sc in SOVEREIGNS_CHINESE:
        en, _ = name_map.get(sc.id, (sc.id, sc.id))
        name_map[sc.id] = (en, sc.name)

    for s in GOVERNANCE_CODEX.sovereigns:
        en_name, zh_name = name_map[s.id]
        identity_id = s.id if s.id not in tool_ids else f"{s.id}-sovereign"
        bound_roots = [s.id]
        if s.id == "system-sovereign":
            bound_roots = ["main-system"]
        elif s.id == "xingcheng":
            bound_roots = ["xingcheng"]
        actor = _sovereign_actor(s.id)
        conn.execute(
            """
            INSERT INTO identity
            (identity_id, identity_code, identity_name, display_name_zh,
             identity_type, group_id, actor, bound_tool_id, bound_roots,
             manifest_required, authentication)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identity_id,
                identity_id,
                en_name,
                zh_name,
                "sovereign",
                "governance-identity-v1",
                actor,
                s.id,
                json.dumps(bound_roots),
                0,
                "governance-policy-issued-capability-token",
            ),
        )


def _module_identities(conn: sqlite3.Connection) -> None:
    sys.path.insert(0, str(ROOT))
    from governance_rule.permission_directory.registries.permissions.identity_groups import (
        CAPABILITY_IDENTITIES,
    )
    from governance_rule.permission_directory.registries.permissions.identity_permissions import (
        IDENTITY_PERMISSION_BINDINGS,
    )

    display = _module_display_names()
    permission_by_actor: dict[str, list[str]] = {}
    for binding in IDENTITY_PERMISSION_BINDINGS:
        permission_by_actor.setdefault(binding.actor, []).extend(binding.capabilities)

    for ci in CAPABILITY_IDENTITIES:
        tool_id = ci.bound_tool_id
        en, zh = display.get(tool_id, (tool_id.replace("-", " ").title(), tool_id))
        identity_type = _identity_type_for_tool(tool_id)

        conn.execute(
            """
            INSERT INTO identity
            (identity_id, identity_code, identity_name, display_name_zh,
             identity_type, group_id, actor, bound_tool_id, bound_roots,
             manifest_required, manifest_path_template, manifest_tool_id_field,
             manifest_max_bytes, authentication)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tool_id,
                tool_id,
                en,
                zh,
                identity_type,
                "governance-identity-v1",
                ci.actor,
                tool_id,
                json.dumps(ci.bound_roots),
                1 if ci.manifest_binding.required else 0,
                ci.manifest_binding.path_template,
                ci.manifest_binding.tool_id_field,
                ci.manifest_binding.maximum_bytes,
                ci.authentication,
            ),
        )

        for cap in ci.manifest_binding.required_capabilities:
            conn.execute(
                "INSERT INTO identity_required_capability (identity_id, capability) VALUES (?, ?)",
                (tool_id, cap),
            )

        for req in ci.manifest_binding.requirements:
            conn.execute(
                "INSERT INTO identity_manifest_requirement (identity_id, field_path, expected_value) VALUES (?, ?, ?)",
                (tool_id, "/".join(req.field_path), req.expected_value),
            )

        for cap in permission_by_actor.get(ci.actor, []):
            conn.execute(
                "INSERT OR IGNORE INTO identity_permission (identity_id, capability) VALUES (?, ?)",
                (tool_id, cap),
            )


def _insert_group(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO identity_group
        (group_id, group_name, display_name_zh, group_type, management_authority,
         registry_mode, legacy_identity_compatibility, aliases_allowed,
         unknown_identity_access, authentication, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "governance-identity-v1",
            "Governance Identity Directory v1",
            "治理身份目錄 v1",
            "active",
            "directory-authority-only",
            "sqlite-canonical-read-only",
            0,
            0,
            "denied",
            "governance-policy-issued-capability-token-only",
            1,
        ),
    )


def main() -> None:
    conn = _connect()
    conn.executescript(SCHEMA_SQL)
    _insert_group(conn)
    _sovereign_identities(conn)
    _module_identities(conn)
    conn.commit()

    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(SCHEMA_SQL, encoding="utf-8")

    counts = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM identity_group) AS groups,
            (SELECT COUNT(*) FROM identity) AS identities,
            (SELECT COUNT(*) FROM identity_permission) AS permissions,
            (SELECT COUNT(*) FROM identity_required_capability) AS required_caps,
            (SELECT COUNT(*) FROM identity_manifest_requirement) AS requirements
        """
    ).fetchone()
    conn.close()

    print(f"Identity directory built at {DB_PATH}")
    print(f"Schema written to {SCHEMA_PATH}")
    print(f"  groups={counts[0]}, identities={counts[1]}, permissions={counts[2]}, required_caps={counts[3]}, requirements={counts[4]}")


if __name__ == "__main__":
    main()
