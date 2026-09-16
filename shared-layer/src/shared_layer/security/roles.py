"""Role layering and least-privilege certification.

Layering contract:

  * NOLOGIN group roles carry the privileges (``gptbridge_*_reader`` etc.);
  * LOGIN roles are per-module and inherit only the minimum groups;
  * runtime roles never hold SUPERUSER / CREATEDB / CREATEROLE / BYPASSRLS;
  * no PUBLIC grants on governed schemas.

:func:`least_privilege_report` inspects a live connection and returns a
certification report; :func:`certification_errors` turns it into failures so
a release gate can assert it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

FORBIDDEN_ROLE_FLAGS: Final[tuple[str, ...]] = (
    "rolsuper",
    "rolcreatedb",
    "rolcreaterole",
    "rolbypassrls",
)

GOVERNED_SCHEMAS: Final[tuple[str, ...]] = (
    "gptbridge_index",
    "gptbridge_transport",
    "gptbridge_audit",
    "gptbridge_security",
    "gptbridge_rag",
    "registry",
)

_ROLE_FLAGS_SQL: Final[str] = (
    "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolcanlogin "
    "FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname"
)

_PUBLIC_GRANTS_SQL: Final[str] = (
    "SELECT table_schema, table_name, privilege_type "
    "FROM information_schema.role_table_grants "
    "WHERE grantee = 'PUBLIC' AND table_schema = ANY(%s) "
    "ORDER BY 1, 2, 3 LIMIT 200"
)

_NOLOGIN_GROUPS_SQL: Final[str] = (
    "SELECT rolname FROM pg_roles WHERE NOT rolcanlogin AND rolname LIKE 'gptbridge%%' "
    "ORDER BY rolname"
)


@dataclass
class RoleFacts:
    role: str
    flags: dict[str, bool] = field(default_factory=dict)

    @property
    def violations(self) -> list[str]:
        return [name for name in FORBIDDEN_ROLE_FLAGS if self.flags.get(name)]


@dataclass
class LeastPrivilegeReport:
    checked_roles: list[dict[str, Any]] = field(default_factory=list)
    public_grants: list[dict[str, Any]] = field(default_factory=list)
    nologin_groups: list[str] = field(default_factory=list)
    current_user: str = ""
    current_user_flags: dict[str, bool] = field(default_factory=dict)


def least_privilege_report(connection: Any, roles: tuple[str, ...]) -> LeastPrivilegeReport:  # pragma: no cover - needs PG
    report = LeastPrivilegeReport()
    rows = connection.execute(_ROLE_FLAGS_SQL, (list(roles),)).fetchall()
    for row in rows:
        facts = dict(zip(("role", *FORBIDDEN_ROLE_FLAGS, "can_login"), row))
        report.checked_roles.append(facts)
    report.public_grants = [
        dict(zip(("schema", "table", "privilege"), row))
        for row in connection.execute(_PUBLIC_GRANTS_SQL, (list(GOVERNED_SCHEMAS),)).fetchall()
    ]
    report.nologin_groups = [row[0] for row in connection.execute(_NOLOGIN_GROUPS_SQL).fetchall()]
    me = connection.execute(
        "SELECT current_user, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
        "FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if me:
        report.current_user = str(me[0])
        report.current_user_flags = dict(zip(FORBIDDEN_ROLE_FLAGS, (bool(flag) for flag in me[1:])))
    return report


def certification_errors(report: LeastPrivilegeReport) -> list[str]:
    errors: list[str] = []
    for facts in report.checked_roles:
        violations = [name for name in FORBIDDEN_ROLE_FLAGS if facts.get(name)]
        if violations:
            errors.append(f"role '{facts.get('role')}' has forbidden flags: {','.join(violations)}")
    if report.public_grants:
        errors.append(f"PUBLIC grants present on governed schemas: {len(report.public_grants)}")
    if not report.nologin_groups:
        errors.append("no NOLOGIN group roles found for the data platform")
    if any(report.current_user_flags.get(name) for name in FORBIDDEN_ROLE_FLAGS):
        violations = [name for name in FORBIDDEN_ROLE_FLAGS if report.current_user_flags.get(name)]
        errors.append(f"runtime login '{report.current_user}' has forbidden flags: {','.join(violations)}")
    return errors


__all__ = [
    "FORBIDDEN_ROLE_FLAGS",
    "GOVERNED_SCHEMAS",
    "LeastPrivilegeReport",
    "RoleFacts",
    "certification_errors",
    "least_privilege_report",
]
