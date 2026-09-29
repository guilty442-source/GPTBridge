"""Scan/registry driven check sections (split from export_audit_manifest)."""

from __future__ import annotations

import json
from pathlib import Path

from governance_rule.execution.audit.export_manifest_emit import _protected_sources
from governance_rule.execution.audit.export_manifest_reducers import (
    _NATIVE_COVERED,
    _iter_python_check_names,
)


def emit_tool_manifests(ctx) -> None:
    emit = ctx.emit
    root = ctx.root
    _manifest_scanned = ctx.manifest_scanned
    _emit_manifest = ctx.emit_manifest
    # check_tool_manifests — 部分歸約：逐 manifest 檔案層級斷言原生。
    # 列舉規則對齊 oracle 的四層掃描（depth-1/2 頂層、depth-3/4 嵌套）；
    # manifest 為匯出期讀取的資料來源——跨檔 identity parity、label
    _standalone_dir = root / "Standalone tools"
    for manifest_path in sorted(root.glob("*/manifest.json")):
        if _manifest_scanned(manifest_path):
            _emit_manifest(manifest_path, top_level=True,
                           expected_owner=None, self_health=True)
    for manifest_path in sorted(
            _standalone_dir.glob("*/manifest.json")):
        _emit_manifest(manifest_path, top_level=True,
                       expected_owner=None, self_health=True)
    for manifest_path in sorted(
            _standalone_dir.glob("*/*/manifest.json")):
        _emit_manifest(manifest_path, top_level=False,
                       expected_owner=manifest_path.parent.parent.name,
                       self_health=True)
    for manifest_path in sorted(
            root.glob("*/*/*/*/manifest.json")):
        if _manifest_scanned(manifest_path):
            _emit_manifest(
                manifest_path, top_level=False,
                expected_owner=manifest_path.parent.parent.parent.name)


def emit_tm_rows(ctx) -> tuple:
    emit = ctx.emit
    root = ctx.root
    _manifest_scanned = ctx.manifest_scanned
    _tm_ids: set[str] = set()
    _tm_docs: list[tuple[Path, str, dict]] = []
    _tm_top_roots: set[str] = set()
    _tm_paths = [
        *sorted(p for p in root.glob("*/manifest.json") if _manifest_scanned(p)),
        *sorted((root / "Standalone tools").glob("*/manifest.json")),
        *sorted((root / "Standalone tools").glob("*/*/manifest.json")),
        *sorted(p for p in root.glob("*/*/*/*/manifest.json") if _manifest_scanned(p)),
    ]
    for mpath in _tm_paths:
        try:
            mdoc = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(mdoc, dict):
            continue
        mtid = str(mdoc.get("id") or "")
        mlc = mdoc.get("lifecycle")
        if (isinstance(mlc, dict) and str(mlc.get("status") or "").strip().casefold() == "retired"):
            if not mtid:
                emit(f"tm:{mpath.relative_to(root).as_posix()}:no-id",
                     "fail",
                     reason="retired tool manifest lacks an identifier")
            continue
        _tm_ids.add(mtid)
        por = str(mdoc.get("physical_owner_root") or "")
        _tm_docs.append((mpath, mtid, mdoc))
        if len(mpath.relative_to(root).parts) <= 3 and por:
            _tm_top_roots.add(por)
    return _tm_ids, _tm_docs, _tm_top_roots


def emit_tm_parity(ctx, tm_data) -> None:
    _tm_ids, _tm_docs, _tm_top_roots = tm_data
    import re as _rem
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    root = ctx.root
    _policy = ctx.policy
    _code_rules = ctx.code_rules
    _identity_group = ctx.identity_group
    _NON_INDEPENDENT = frozenset({ "governance_rule", "shared-layer", "star-chat", "xingcheng-assistant"})
    for mpath, mtid, mdoc in _tm_docs:
        mrel = mpath.relative_to(root).as_posix()
        parts = mpath.relative_to(root).parts
        por = str(mdoc.get("physical_owner_root") or "")
        if len(parts) >= 4:
            if not por or por != parts[1] or por not in _tm_top_roots:
                emit(f"tm:{mtid}:owner-parity", "fail", reason=f"nested physical_owner_root invalid: {mrel}")
        elif mtid != mpath.parent.name and por != mpath.parent.name:
            emit(f"tm:{mtid}:dir-parity", "fail", reason=f"tool identity mismatch: {mrel}")
        emit(f"tm:{mtid}:id", "json-key-value", mrel, markers=[f"id={mtid}"])
        if _rem.fullmatch( _policy.identifier_labels.tool_id_pattern, mtid) is None:
            emit(f"tm:{mtid}:label", "fail", reason=f"tool identifier is not standardized: {mtid}")
        caps = mdoc.get("capabilities")
        if not isinstance(caps, dict):
            emit(f"tm:{mtid}:caps", "fail", reason=f"tool capabilities are missing: {mtid}")
        for cname in caps if isinstance(caps, dict) else ():
            if (cname not in _code_rules.approved_capability_names
                    or _rem.fullmatch( _policy.identifier_labels.capability_pattern, cname) is None):
                emit(f"tm:{mtid}:cap:{cname}", "fail", reason="capability label not standardized: " f"{mtid}:{cname}")
            else:
                contains(f"tm:{mtid}:cap:{cname}", mrel, [f'"{cname}"'])
        try:
            locale = json.loads( (mpath.parent / "locales" / "zh-TW.json") .read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue  # file-exists/json-parses rows fail natively
        if not isinstance(locale, dict) or not all(
                isinstance(key, str)
                and _rem.fullmatch( _policy.identifier_labels.locale_key_pattern, key)
                and isinstance(value, str)
                for key, value in locale.items()):
            emit(f"tm:{mtid}:locale-schema", "fail", reason=f"zh-TW locale schema invalid: {mtid}")
    _tm_retired_identity = {
        i.bound_tool_id for i in _identity_group.identities
        if i.lifecycle == "retired"}
    _tm_sets = {
        "registered": {
            i.bound_tool_id for i in _identity_group.identities
            if i.bound_tool_id != "main-system"
            and i.bound_tool_id not in _NON_INDEPENDENT
            and i.lifecycle != "retired"},
        "manifest": _tm_ids - _NON_INDEPENDENT - _tm_retired_identity,
        "approved": set(_code_rules.approved_tool_ids)
                    - _NON_INDEPENDENT - _tm_retired_identity,
    }
    for left, right in (("registered", "manifest"),
                        ("manifest", "registered"),
                        ("manifest", "approved"),
                        ("approved", "manifest")):
        for tid in sorted(_tm_sets[left] - _tm_sets[right]):
            emit(f"tm-parity:{left}-not-{right}:{tid}", "fail", reason=f"{left} tool id not in {right} set: {tid}")


def emit_architecture_registry(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- check_architecture_registry ------------------------------------
    emit("architecture-registry:parse", "json-parses", "governance_rule/execution/audit/architecture_registry.json")
    for rel_mod in ("main-system/governance/sovereigns/__init__.py",):
        emit(f"sovereign-module:{rel_mod}", "file-exists", rel_mod)
    _routes_src = ( "governance_rule/permission_directory/registries/" "permissions/tool_routes.py")
    try:
        _routes_text = (root / _routes_src).read_text( encoding="utf-8", errors="replace")
    except OSError:
        _routes_text = ""
    _route_ids: set[str] = set()
    for marker in ('"tool_id"', "'tool_id'"):
        _idx = 0
        while True:
            _idx = _routes_text.find(marker, _idx)
            if _idx < 0:
                break
            _tail = _routes_text[_idx + len(marker):]
            for quote in ('"', "'"):
                _start = _tail.find(quote)
                if _start < 0:
                    continue
                _end = _tail.find(quote, _start + 1)
                if _end > 0:
                    _cand = _tail[_start + 1:_end].strip()
                    if _cand:
                        _route_ids.add(_cand)
                    break
            _idx += 1
    for rid in sorted(_route_ids):
        emit(f"architecture-registry:route:{rid}", "file-contains",
             "governance_rule/execution/audit/architecture_registry.json",
             markers=[f'"{rid}"'])



def emit_gpu_renderer(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- check_gpu_coordinator_torch_free (B167/B38) --------------------
    _gpu_src = ("shared-layer/src/shared_layer/adaptive/" "gpu_coordinator.py")
    contains("gpu-torch-free:probe", _gpu_src,
             ["_query_via_nvidia_smi", "def query_gpu"])
    not_contains("gpu-torch-free:no-retired-framework", _gpu_src,
                 ["import torch", "from torch", "_query_via_torch",
                  "torch.cuda", "def _torch("])

    # --- check_jax_sft_retrace_bound ------------------------------------
    # RETIRED (B167): jax_backend/sft.py removed with the JAX framework.

    # --- check_renderer_idle_gating --------------------------------------
    _renderer_base = root / "main-system" / "src-ui" / "renderer"
    if _renderer_base.is_dir():
        for _rfile in sorted(_renderer_base.rglob("*")):
            if (_rfile.suffix not in (".js", ".jsx", ".mjs")
                    or not _rfile.is_file()
                    or "node_modules" in _rfile.parts
                    or "dist" in _rfile.parts):
                continue
            try:
                _rtext = _rfile.read_text( encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "setInterval(" in _rtext:
                checks.append({
                    "id": "renderer-idle-gating:"
                          f"{_rfile.relative_to(root).as_posix()}",
                    "kind": "file-not-contains-unless",
                    "path": _rfile.relative_to(root).as_posix(),
                    "markers": ["setInterval("],
                    "unless": ["visibilityState", "idle-ok", "navigator.onLine", "visibilitychange"],
                })



def emit_worker_pools(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- check_bounded_worker_pools --------------------------------------
    _pools_src = ("shared-layer/src/shared_layer/performance/" "thread_budget.py")
    contains("worker-pools:budget-module", _pools_src,
             ["CORE_BUDGET_CAP = 5", "bounded_workers", "bounded_threads", "allocation_within_budget"])
    for _pool_root in ("main-system/src-core", "main-system/governance",
                       "shared-layer/src", "governance_rule",
                       "Standalone tools"):
        _pbase = root / _pool_root
        if not _pbase.is_dir():
            continue
        for _pfile in sorted(_pbase.rglob("*.py")):
            _parts = set(_pfile.parts)
            if (_parts & {"__pycache__", ".venv", "bin", "build",
                          "dist", "node_modules", "releases",
                          "runtime", "test", "tests"}
                    or _pfile.name.startswith("test_")):
                continue
            try:
                if b"PoolExecutor" not in _pfile.read_bytes():
                    continue
            except OSError:
                continue
            emit(f"worker-pool:{_pfile.relative_to(root).as_posix()}",
                 "file-contains",
                 _pfile.relative_to(root).as_posix(),
                 markers=["max_workers"])



def emit_sql_patterns(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- check_sql_anti_patterns ------------------------------------------
    emit("sql-patterns:baseline-exists", "file-exists", "governance_rule/execution/audit/sql_patterns_baseline.json")
    emit("sql-patterns:baseline-keys", "json-has-keys",
         "governance_rule/execution/audit/sql_patterns_baseline.json",
         markers=["findings"])
    contains("sql-patterns:scanner-machinery",
             "governance_rule/execution/audit/audit_sql_patterns.py",
             ["collect_finding_keys", "baseline_path", "_SELECT_STAR", "_OFFSET"])
    from governance_rule.execution.audit.audit_sql_patterns import ( collect_finding_keys as _collect_findings, )
    for _violation in sorted(set(_collect_findings(root))):
        emit(f"sql-patterns:finding:{_violation}", "fail",
             reason="sql anti-pattern finding at manifest export: "
                    f"{_violation}")



def emit_protected_remainder(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- protected-source-semantic remainder ------------------------------
    _protected = _protected_sources()
    if len(_protected) != len(set(_protected)):
        emit("protected-source:duplicates", "fail", reason="protected governance sources contain duplicates")



def emit_owned_sources(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- oracle-only: source_ownership_errors -------------------------
    from governance_rule.permission_directory.registries.permissions import ( source_ownership as _so)
    for _owner, _srcs in _so.REQUIRED_OWNED_SOURCES.items():
        for _rel in sorted(_srcs):
            emit(f"owned-source:{_rel}", "file-exists", _rel)
    _retired = set(json.loads(
        (root / "governance_rule/execution/audit/retired_sources.json")
        .read_text(encoding="utf-8"))["paths"])
    for _rel in sorted(_so.FORBIDDEN_LEGACY_BUSINESS_SOURCES | _retired):
        emit(f"forbidden-source:{_rel}", "file-not-exists", _rel)
    emit("owned-source:visual-smoke", "file-exists", "Standalone tools/ai-assistant/scripts/visual_smoke.py")
    not_contains("main-system:ipc-symbols", "main-system/src-core/ipc/server.py",
                 ["_investment_watch_result_log_payload", "_INVESTMENT_WATCH_LOG_"])


def emit_pkg_layers(ctx) -> None:
    emit = ctx.emit
    root = ctx.root
    from governance_rule.permission_directory.registries.permissions import source_ownership as _so
    for _pkg, _layers in (
            (_so.AI_ASSISTANT_PACKAGE_ROOT, _so.AI_ASSISTANT_REQUIRED_LAYERS),
            (_so.XINGCHENG_PACKAGE_ROOT, _so.XINGCHENG_REQUIRED_LAYERS),
            (_so.AI_COLLABORATION_PACKAGE_ROOT, _so.AI_COLLABORATION_REQUIRED_LAYERS),
            (_so.INVESTMENT_MOBILE_PACKAGE_ROOT, _so.INVESTMENT_MOBILE_REQUIRED_LAYERS),
            (_so.FILE_SORTER_PACKAGE_ROOT, _so.FILE_SORTER_REQUIRED_LAYERS),
            (_so.VAULTLY_PACKAGE_ROOT, _so.VAULTLY_REQUIRED_LAYERS),
            (_so.STAR_CHAT_PACKAGE_ROOT, _so.STAR_CHAT_REQUIRED_LAYERS)):
        for _layer in sorted(_layers):
            emit(f"pkg-layer:{_pkg}:{_layer}", "file-exists", f"{_pkg}/{_layer}/__init__.py")
        for _stray in sorted((root / _pkg).glob("*.py")):
            if _stray.name != "__init__.py":
                emit(f"pkg-stray:{_stray.relative_to(root).as_posix()}", "fail",
                     reason="source outside owned layer")


def emit_tree_policies(ctx) -> None:
    emit = ctx.emit
    root = ctx.root
    _relativize = ctx.relativize
    from governance_rule.permission_directory.registries.permissions import source_ownership as _so
    _trees = sorted(
        {p.as_posix() for p in root.glob("*/src") if p.is_dir()}
        | {p.as_posix() for p in (root / "Standalone tools").glob("*/src") if p.is_dir()})
    for _prefix, _owner_root in _so.OWNED_IMPORT_PREFIXES.items():
        for _tree in _trees:
            _trel = _relativize(_tree)
            if not _trel.startswith(_owner_root + "/"):
                emit(f"cross-import:{_prefix}:{_trel}", "tree-not-contains",
                     _trel, glob="*.py",
                     markers=[f"import {_prefix}", f"from {_prefix}"])
    emit("shared-layer:forbidden-terms", "tree-not-contains",
         "shared-layer/src", glob="*.py", ignore_case=True,
         markers=sorted(_so.SHARED_LAYER_FORBIDDEN_TERMS))
    emit("main-system:forbidden-business", "tree-not-contains",
         "main-system/src-core", glob="*.py", ignore_case=True,
         markers=sorted(_so.MAIN_SYSTEM_FORBIDDEN_BUSINESS_TERMS))
    emit("ai-assistant:forbidden-network", "tree-not-contains",
         _so.AI_ASSISTANT_PACKAGE_ROOT, glob="*.py",
         markers=[f"{v} {m}" for v in ("import", "from")
                  for m in ("aiohttp", "httpx", "requests", "smtplib")]
         + ["urllib.request", "urlopen("])
    _shared_src = root / _so.SHARED_LAYER_ROOT
    if _shared_src.is_dir():
        for _sf in sorted(_shared_src.rglob("*.py")):
            _srel = _sf.relative_to(_shared_src).as_posix()
            if (_srel not in _so.SHARED_LAYER_ALLOWED_SOURCES
                    and not _srel.startswith(_so.SHARED_LAYER_ALLOWED_PREFIXES)):
                emit(f"unowned-shared:{_srel}", "fail", reason="unowned shared-layer source")



def emit_delegated(ctx) -> None:
    checks = ctx.checks
    reducible = ctx.reducible
    filelist = ctx.filelist
    # --- delegated: every Python check not natively covered -----------
    # Each delegated row carries an explicit ``python`` target so the
    # delegated lane (same-request execution, G96) can resolve it without
    # a second mapping table — unresolvable rows fail closed.
    covered = _NATIVE_COVERED | set(reducible) | set(filelist)
    delegated_names = [
        name for name in _iter_python_check_names(None)
        if name not in covered
    ]
    for name in delegated_names:
        checks.append({
            "id": f"python-check:{name}",
            "kind": "delegated",
            "reason": "python oracle (transition)",
            "python": name,
        })
