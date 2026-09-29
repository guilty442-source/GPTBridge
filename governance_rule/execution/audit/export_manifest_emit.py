"""Emit context, emit primitives and codex-mirror table helpers (split from export_audit_manifest)."""

from __future__ import annotations

import json
from pathlib import Path


def _protected_sources() -> list[str]:
    """Live protected-source list from the governed snapshots."""
    from governance_rule.permission_directory.directory_authority import (
        directory_authority_snapshot,
    )
    from governance_rule.governance_policy import governance_policy_snapshot

    policy = governance_policy_snapshot()
    directory = directory_authority_snapshot()
    seen: list[str] = []
    for relative in (
        *policy.authority_files,
        *directory.managed_read_only_registry_paths,
    ):
        if relative not in seen:
            seen.append(relative)
    return seen


def _forbidden_legacy() -> list[str]:
    from governance_rule.execution.audit.audit_protected import (
        FORBIDDEN_LEGACY_SOURCES,
    )
    return [str(p) for p in FORBIDDEN_LEGACY_SOURCES]


_MANIFEST_ARTIFACT_ROOTS = frozenset({"worktrees", "backups"})

_MARKER_TOKENS = ("!=", "^=", ">=")
_ROW_BIND_CAP = 150


class MirrorTables:
    """Codex-mirror table access bound to an emit context."""

    def __init__(self, ctx) -> None:
        self._ctx = ctx

    def _table_absent(self, table: str) -> None:
        emit = self._ctx.emit
        emit(f"codex-table:absent:{table}", "fail", reason=f"codex mirror table missing: {table}")

    def _table_rows_for(self, table: str) -> list:
        _mirror = self._ctx.mirror
        entry = _mirror.get(table)
        return entry[1] if entry else []

    def _table_assert(self, cid: str, table: str, markers: list[str], count: int = 1) -> None:
        emit = self._ctx.emit
        _mirror = self._ctx.mirror
        _table_absent = self._table_absent
        """``>=count`` rows in the mirror table satisfy all markers."""
        entry = _mirror.get(table)
        if entry is None:
            _table_absent(table)
            return
        emit(cid, "json-array-min-count", entry[0], items=f"tables.{table}", markers=markers, min_count=count)

    def _table_present(self, table: str) -> None:
        emit = self._ctx.emit
        _seen_present = self._ctx._seen_present
        _mirror = self._ctx.mirror
        _table_absent = self._table_absent
        if table in _seen_present:
            return
        _seen_present.add(table)
        entry = _mirror.get(table)
        if entry is None:
            _table_absent(table)
        else:
            emit(f"codex-table:present:{table}", "json-array-min-count",
                 entry[0], items=f"tables.{table}", markers=[],
                 min_count=0)

    @staticmethod
    def _bind(field: str, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, bool):
            return [f"{field}={'true' if value else 'false'}"]
        text = str(value)
        first = min( (text.find(tok) for tok in _MARKER_TOKENS if tok in text), default=-1)
        if first < 0:
            return [f"{field}={text}"]
        return [f"{field}^={text[:first]}"] if first > 0 else []

    def _rows_or_count(self, cid_prefix: str, table: str, idcol: str, extra: object = None) -> None:
        _table_rows_for = self._table_rows_for
        _table_assert = self._table_assert
        _bind = MirrorTables._bind
        rows = _table_rows_for(table)
        if not rows:
            return
        _table_assert(f"{cid_prefix}:count:{table}", table, [], count=len(rows))
        if len(rows) > _ROW_BIND_CAP:
            return
        for row in rows:
            rid = row.get(idcol)
            if rid is None:
                continue
            markers = _bind(idcol, rid)
            if callable(extra):
                markers += extra(row)
            if markers:
                _table_assert(f"{cid_prefix}:{rid}", table, markers)


class ManifestEmit:
    """Emit context: root, checks sink, governed snapshots, mirror tables."""

    def __init__(self, root: Path, checks: list[dict[str, object]]) -> None:
        self.root = root
        self.checks = checks
        self.reducible: dict[str, tuple[str, list[str]]] = {}
        self.filelist: dict[str, tuple[str, list[str]]] = {}
        self._seen_present: set[str] = set()
        self.tables = MirrorTables(self)
        self.mirror = self._load_mirror()
        from governance_rule.governance_policy import (
            governance_policy_snapshot,
        )
        from governance_rule.permission_directory.directory_authority import (
            directory_authority_snapshot,
        )
        from governance_rule.code_rule_directory import (
            code_rule_directory_snapshot,
        )
        from governance_rule.permission_directory.registries.permissions.identity_groups import (
            identity_group_snapshot,
        )
        from governance_rule.permission_directory.registries.permissions.identity_permissions import (
            identity_permission_snapshot,
        )
        from governance_rule.permission_directory.registries.permissions.capability_boundaries import (
            capability_boundary_snapshot,
        )
        self.policy = governance_policy_snapshot()
        self.directory = directory_authority_snapshot()
        self.code_rules = code_rule_directory_snapshot()
        self.identity_group = identity_group_snapshot()
        self.bindings = identity_permission_snapshot()
        self.cap_bounds, self.repair_bounds = capability_boundary_snapshot()
        registry_dir = (self.root / "governance_rule"
                        / "permission_directory" / "registries")
        self.registry_files = [
            p for p in sorted(registry_dir.rglob("*.py"))
            if "__pycache__" not in p.parts]
        self.required_locale_keys = sorted(
            self.code_rules.required_locale_keys)

    def emit(self, cid: str, kind: str, path: str = "", **kw: object) -> None:
        row: dict[str, object] = {"id": cid, "kind": kind}
        if path:
            row["path"] = path
        row.update(kw)
        self.checks.append(row)

    def contains(self, check_id: str, path: str, markers: list[str],
                 optional: bool = False) -> None:
        emit = self.emit
        emit(check_id, "file-contains", path, markers=markers,
             **({"optional": True} if optional else {}))

    def not_contains(self, check_id: str, path: str, markers: list[str],
                     optional: bool = False) -> None:
        emit = self.emit
        emit(check_id, "file-not-contains", path, markers=markers,
             **({"optional": True} if optional else {}))

    def relativize(self, path_text: str) -> str:
        root = self.root
        norm = path_text.replace("\\", "/").rstrip("/")
        prefix = root.as_posix() + "/"
        if norm.startswith(prefix):
            return norm[len(prefix):]
        return "." if norm == root.as_posix() else norm

    def manifest_scanned(self, p: Path) -> bool:
        root = self.root
        _manifest_artifact_roots = _MANIFEST_ARTIFACT_ROOTS
        parts = p.relative_to(root).parts
        if parts[:3] in (
            ("main-system", "runtime", "releases"),
            ("main-system", "runtime", "temp"),
        ):
            return False
        return (not parts[0].startswith(".")
                and parts[0] not in _manifest_artifact_roots)

    def emit_manifest(self, manifest_path: Path, *, top_level: bool,
                      expected_owner: str | None,
                      self_health: bool = False) -> None:
        emit = self.emit
        root = self.root
        _required_locale_keys = self.required_locale_keys
        rel = manifest_path.relative_to(root).as_posix()
        emit(f"tool-manifest:parse:{rel}", "json-parses", rel)
        try:
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(manifest, dict):
            return
        lifecycle = manifest.get("lifecycle")
        retired = (
            isinstance(lifecycle, dict)
            and str(lifecycle.get("status") or "").strip().casefold()
            == "retired")
        tool_id = str(manifest.get("id") or "")
        if retired:
            emit(f"tool-manifest:retired:{rel}", "json-key-value", rel,
                 markers=[
                     "enabled=false",
                     "lifecycle.stoppable=false",
                     "status!=running",
                     "main_system_independent_tool!=true",
                 ])
            return
        markers = ["name_key=tool.name"]
        absent = ["name"]
        if isinstance(manifest.get("window"), dict):
            markers.append("window.title_key=tool.window_title")
            absent.append("window.title")
        if top_level:
            code_scope = (
                "project-source-excluding-governance-rule"
                if tool_id == "xingcheng" else "tool-root-only")
            db_scope = (
                "opaque-central-index-read-and-xingcheng-internal"
                "-read-write" if tool_id == "xingcheng"
                else "none" if tool_id == "governance_rule"
                else "tool-database-only")
            markers.append(f"permissions.code_scope={code_scope}")
            markers.append(f"permissions.database_scope={db_scope}")
            if tool_id == "governance_rule":
                markers.extend([
                    "status=running",
                    "lifecycle.startup=default-before-main-system",
                    "lifecycle.directLoad=true",
                    "lifecycle.encapsulated=false",
                    "lifecycle.optional=false",
                    "lifecycle.stoppable=false",
                    "lifecycle.disableable=false",
                    "lifecycle.unloadable=false",
                ])
                absent.append("executable")
        else:
            markers.append(f"physical_owner_root={expected_owner}")
        emit(f"tool-manifest:values:{rel}", "json-key-value", rel,
             markers=markers)
        emit(f"tool-manifest:absent:{rel}", "json-key-absent", rel,
             markers=absent)
        emit(f"tool-manifest:dicts:{rel}", "json-has-keys", rel,
             markers=["permissions", "capabilities"])
        locale_rel = (
            manifest_path.parent / "locales" / "zh-TW.json"
        ).relative_to(root).as_posix()
        emit(f"tool-locale:exists:{rel}", "file-exists", locale_rel)
        emit(f"tool-locale:parse:{rel}", "json-parses", locale_rel)
        emit(f"tool-locale:keys:{rel}", "json-has-keys", locale_rel,
             markers=list(_required_locale_keys))
        # self-health 覆蓋面（audit_self_health 三層 glob 掃到的
        # manifest 才走此列；depth-4 嵌套僅 tool-manifests 語義）。
        # retired / enabled=false 的擁有者不承擔覆蓋宣告屏障。
        if not self_health or manifest.get("enabled") is False:
            return
        self._emit_targets(manifest_path, manifest, tool_id, rel)

    def _emit_targets(self, manifest_path: Path, manifest: dict,
                  tool_id: str, rel: str) -> None:
        emit = self.emit
        root = self.root
        tool_root = manifest_path.parent.resolve()
        targets = manifest.get("test_targets")
        if not tool_id or not isinstance(targets, list) or not targets:
            emit(f"self-health:test-targets:{rel}", "fail",
                 reason="governed tool must declare test_targets")
            return
        for index, raw_target in enumerate(targets):
            target = str(raw_target or "").strip()
            base = f"self-health:test-target:{tool_id}:{index}"
            if target.startswith("pending-native:"):
                # 對齊 oracle 第一方向（PENDING row 位於宣告前綴之下）；
                # 反方向（宣告巢於 row dir 內）在 tool-root 宣告下不可能。
                prefix = target.split(":", 1)[1].rstrip("/") + "/"
                emit(f"{base}:pending-native", "json-array-min-count",
                     "governance_rule/execution/audit/"
                     "pytest_retirement_inventory.json",
                     items="rows", min_count=1,
                     markers=["status=PENDING", f"source_test^={prefix}"])
                continue
            if target.startswith("native-suite:"):
                emit(f"{base}:native-suite", "file-exists",
                     f"native/test_suites/suite_{target.split(':', 1)[1]}.cpp")
                continue
            candidate = (tool_root / target).resolve()
            try:
                rel_target = candidate.relative_to(root).as_posix()
                candidate.relative_to(tool_root)
            except ValueError:
                emit(f"{base}:escaped", "fail",
                     reason="test target escaped tool root: "
                            f"{tool_id}: {target}")
                continue
            if candidate.suffix.casefold() == ".py":
                emit(f"{base}:py", "fail",
                     reason="non-conforming Python test target "
                            f"(FORBID:pytest): {tool_id}: {target}")
                continue
            emit(f"{base}:exists", "file-exists", rel_target)

    def _load_mirror(self) -> dict[str, tuple[str, list]]:
        root = self.root
        tables: dict[str, tuple[str, list]] = {}
        codex_dir = root / "governance_rule" / "codex"
        for part in sorted( codex_dir.glob("governance_codex.zh-TW.part-*.txt")):
            try:
                doc = json.loads(part.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rel = part.relative_to(root).as_posix()
            for name, rows in (doc.get("tables") or {}).items():
                if isinstance(rows, list):
                    tables.setdefault(name, (rel, rows))
        return tables

    def literal_file(self, literal: str) -> str | None:
        root = self.root
        _registry_files = self.registry_files
        for reg in _registry_files:
            try:
                if literal in reg.read_text( encoding="utf-8", errors="replace"):
                    return reg.relative_to(root).as_posix()
            except OSError:
                continue
        return None

    def literal_assert(self, cid: str, literal: str) -> None:
        emit = self.emit
        _literal_file = self.literal_file
        found = _literal_file(literal)
        if found is None:
            emit(cid, "fail", reason=f"registry literal absent: {literal}")
        else:
            emit(cid, "file-contains", found, markers=[literal])
