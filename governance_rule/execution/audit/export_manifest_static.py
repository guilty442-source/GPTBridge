"""Static native check emission sections (split from export_audit_manifest)."""

from __future__ import annotations

import json

from governance_rule.execution.audit.export_manifest_emit import _forbidden_legacy, _protected_sources

def emit_foundation(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- forbidden legacy paths (native: file-not-exists) -------------
    for relative in _forbidden_legacy():
        emit(f"forbidden-legacy:{relative}", "file-not-exists", relative)

    # --- protected governance sources (native: exists + readonly) -----
    # Codex amendment codex-readonly-minimization: the read-only attribute
    # applies to the generated zh-TW mirror parts only; all other
    # protected sources keep existence/integrity checks only.
    for relative in _protected_sources():
        emit(f"protected-source:{relative}", "file-exists", relative)
        if relative.startswith(
            "governance_rule/codex/governance_codex.zh-TW.part-"
        ):
            emit(f"protected-source-readonly:{relative}",
                 "file-readonly", relative)

    # --- codex / architecture text pollution (native scan) ------------
    codex_root = root / "governance_rule" / "codex"
    if codex_root.is_dir():
        for path in sorted(codex_root.glob("architecture-*.md")):
            emit(f"architecture-pollution:{path.name}",
                 "text-no-pollution", path.relative_to(root).as_posix())
        for path in sorted(codex_root.glob("*.zh-TW.part-*.txt")):
            emit(f"mirror-part-pollution:{path.name}",
                 "text-no-pollution", path.relative_to(root).as_posix())

    # --- governed JSON artifacts parse (native json-parses) -----------
    contracts = (
        "main-system/config/ai-connection-contract.json",
        "main-system/config/backend-lifecycle-contract.json",
        "main-system/config/cleanup-caps-policy.json",
        "main-system/config/data-architecture-contract.json",
        "main-system/config/ipc-contract.json",
        "main-system/config/ipc-surface-backend.json",
        "main-system/config/ipc-surface-frontend.json",
        "main-system/config/resident-core.json",
        "main-system/config/sleep-policy.json",
        "main-system/config/sql-schema-contract.json",
        "main-system/config/tool-isolation-policy.json",
        "main-system/config/tool-runtime-contract.json",
        "main-system/config/automation-flows.json",
    )
    for relative in contracts:
        emit(f"contract-parse:{relative}", "json-parses", relative)




def emit_static_metadata_browser(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- static contract / structure checks (native reducible) ---------
    def contains(check_id: str, path: str, markers: list[str],
                 optional: bool = False) -> None:
        emit(check_id, "file-contains", path, markers=markers,
             **({"optional": True} if optional else {}))

    def not_contains(check_id: str, path: str, markers: list[str],
                     optional: bool = False) -> None:
        emit(check_id, "file-not-contains", path, markers=markers,
             **({"optional": True} if optional else {}))

    # check_metadata_contract
    contains(
        "metadata-contract:fields",
        "shared-layer/src/shared_layer/metadata_contract.py",
        ["FIELD_MODULE_ID", "FIELD_RESOURCE_ID", "FIELD_LOCATOR_ID",
         "FIELD_VERSION", "FIELD_CONTENT_HASH", "FIELD_UPDATED_AT",
         "FIELD_STATUS", "ResourceMetadata", "validate_vector_payload"],
    )
    emit("metadata-contract:ownership-doc", "file-exists",
         "shared-layer/docs/DATA_OWNERSHIP_CONTRACT.md")

    # check_shared_layer_structure (dirs physical, sources readonly)
    from governance_rule.governance_policy import governance_policy_snapshot
    shared = governance_policy_snapshot().shared_layer
    for relative in (shared.module_root, shared.source_root,
                     shared.data_root):
        emit(f"shared-layer-dir:{relative}", "dir-exists", relative)
    for name in ("__init__.py", "channel.py", "store.py"):
        emit(f"shared-layer-source:{name}", "file-exists",
             f"{shared.source_root}/shared_layer/{name}")

    # check_embedded_browser (conditional file scans + required modules)
    for path in (
        "Standalone tools/ai-collaboration/src/backend/services/"
        "ai_collaboration/integration/browser_automation.py",
        "Standalone tools/ai-collaboration/src/backend/services/"
        "ai_collaboration/integration/provider_session.py",
        "Standalone tools/vaultly/src/backend/services/"
        "vaultly/integration/browser_session.py",
    ):
        not_contains(f"embedded-browser:no-playwright:{path}",
                     path, ["from playwright", "import playwright"],
                     optional=True)
        # async_playwright 僅在 InProcessEmbeddedBrowser 持有者檔內合法
        # （oracle: ``async_playwright ∧ ¬InProcessEmbeddedBrowser`` → 錯）
        checks.append({
            "id": f"embedded-browser:no-async-playwright:{path}",
            "kind": "file-not-contains-unless",
            "path": path,
            "markers": ["async_playwright"],
            "unless": ["InProcessEmbeddedBrowser"],
            "optional": True,
        })
    for path in (
        "main-system/requirements.txt",
        "main-system/pyproject.toml",
    ):
        checks.append({
            "id": f"embedded-browser:no-dep:{path}",
            "kind": "file-not-contains",
            "path": path,
            "markers": ["playwright"],
            "optional": True,
            "ignore_case": True,   # Python 側比對前 .lower()
        })
    checks.append({
        "id": "embedded-browser:module",
        "kind": "file-exists",
        "path": "main-system/src-tauri/src/webview_host/mod.rs",
    })
    checks.append({
        "id": "embedded-browser:client",
        "kind": "file-exists",
        "path": "shared-layer/src/shared_layer/embedded_browser_client.py",
    })

    # check_orphan_scanner
    contains("orphan-scanner:module",
             "shared-layer/src/shared_layer/database/orphan_scanner.py",
             ["scan_orphans"])



def emit_static_governance(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # check_git_tiers (classify()==3 semantic stays delegated)
    contains("git-tiers:module",
             "governance_rule/execution/git_tiers/__init__.py",
             ["TIER1_OPS", "TIER2_OPS", "TIER3_OPS",
              "def classify", "def enforce", "def audit_log"])
    contains("git-tiers:gate-wrapper",
             "scripts/git-gate.py",
             ["from governance_rule.execution.git_tiers import"])
    contains("git-tiers:pre-push",
             "governance_rule/git-hooks/pre-push",
             ["GOVERNANCE_AUTHORITY_APPROVAL", "merge-base", "refs/tags/"])

    # check_release_manifest_file (json + required keys)
    checks.append({
        "id": "release-manifest:keys",
        "kind": "json-has-keys",
        "path": "shared-layer/database-release.json",
        "markers": ["release_id", "schema_version", "migration_head",
                    "rls_version", "role_version",
                    "reconcile_contract_version",
                    "query_contract_version",
                    "minimum_runtime_version", "compatibility_range",
                    "state"],
    })

    # check_release_manifest_module — 已由 module-markers 自動歸約覆蓋。

    # check_architecture_sources
    contains("architecture:shared-database-canonical",
             "shared-layer/src/shared_layer/database/__init__.py",
             ["POSTGRESQL_CANONICAL: bool = True"])
    contains("architecture:local-vector-degraded",
             "shared-layer/src/shared_layer/local/vector_store.py",
             ['"engine": "rust-vectord-degraded"',
              '"canonical": False'])
    for name in ("market_data.py", "xingcheng_tools/search/searchd.py"):
        path = ("Standalone tools/local-model/src/backend/services/"
                f"xingcheng/infrastructure/{name}")
        contains(f"architecture:network-allowlist:{name}",
                 path, ["NETWORK_DESTINATION_ALLOWLIST"])

    # check_reconcile_modules — two-file contains/forbidden semantics.
    # shared_layer/reconcile.py: must hold ReconcileStateStore and must
    # NOT hold decision surfaces; core_system/data_reconciliation.py is
    # the sole decision owner (must hold class ReconcileService).
    contains("reconcile:state-store",
             "shared-layer/src/shared_layer/reconcile.py",
             ["ReconcileStateStore"])
    not_contains("reconcile:no-decision-surface",
                 "shared-layer/src/shared_layer/reconcile.py",
                 ["class ReconcileService",
                  "def _push_to_central",
                  "def _pull_from_central"])
    contains("reconcile:decision-owner",
             "main-system/src-core/core_system/data_reconciliation.py",
             ["class ReconcileService"])

    # check_main_system_source
    not_contains("main-system:no-legacy-enforcer",
                 "main-system/src-core/main.py",
                 ["GovernanceEnforcer", "governance.enforcer"])
    contains("main-system:launcher-attested",
             "main-system/src-core/main.py",
             ["MainSystemGovernance.from_environment"])
    not_contains("main-system:no-persistent-logger",
                 "main-system/src-core/main.py", ["CoreLogger"])
    contains("main-system:governance-denied",
             "main-system/src-core/core_system/governance_runtime.py",
             ['if tool_id == "governance_rule"'])



def emit_static_native_boundary(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # check_bootstrap_native_entry — csproj + Program.cs + contract marker.
    _bootstrap_dir = "main-system/launcher/src/GPTBridge.Bootstrap"
    checks.append({
        "id": "bootstrap-entry:csproj", "kind": "file-exists",
        "path": f"{_bootstrap_dir}/GPTBridge.Bootstrap.csproj",
    })
    checks.append({
        "id": "bootstrap-entry:program", "kind": "file-exists",
        "path": f"{_bootstrap_dir}/Program.cs",
    })
    contains("bootstrap-entry:contract-marker",
             f"{_bootstrap_dir}/Program.cs", ["--prepare-only"])

    # check_channel_gateway_csharp — library + test project + invariants.
    _channel_lib = (
        "shared-layer/csharp/GPTBridge.Channels/GPTBridge.Channels")
    for rel in (
        f"{_channel_lib}/GPTBridge.Channels.csproj",
        "shared-layer/csharp/GPTBridge.Channels/GPTBridge.Channels.Tests/"
        "GPTBridge.Channels.Tests.csproj",
        f"{_channel_lib}/A263Channel.cs",
    ):
        checks.append({
            "id": f"channel-gateway:{rel.rsplit('/', 1)[-1]}",
            "kind": "file-exists", "path": rel,
        })
    contains("channel-gateway:port-invariants",
             f"{_channel_lib}/A263Channel.cs",
             ["Stopwatch.GetTimestamp", "ReconnectAsync"])

    # check_tool_host_native_boundary — host/test dirs, forbidden
    # governance primitives over *.cs, proxy ops surface, spawn wiring.
    _tool_host = "shared-layer/csharp/GPTBridge.ToolHost/GPTBridge.ToolHost"
    for rel in (_tool_host, f"{_tool_host}.Tests"):
        checks.append({
            "id": f"tool-host:dir:{rel.rsplit('/', 1)[-1]}",
            "kind": "dir-exists", "path": rel,
        })
    for marker in (
        "HMACSHA", "issue_token", "launcher_key", "integrity_manifest",
        "identity_attestation", "gptbridge_transport", "Npgsql",
        "pg_notify",
    ):
        checks.append({
            "id": f"tool-host:forbidden:{marker}",
            "kind": "glob-not-contains",
            "glob": f"{_tool_host}/*.cs", "markers": [marker],
        })
    checks.append({
        "id": "tool-host:proxy-client", "kind": "file-exists",
        "path": f"{_tool_host}/TransportProxyClient.cs",
    })
    contains("tool-host:proxy-ops",
             f"{_tool_host}/TransportProxyClient.cs",
             ['"hello"', '"claim"', '"respond"', '"request_cancelled"',
              '"notification_stamp"'])
    contains("tool-host:spawn-exe-branch",
             "main-system/src-core/tasks/toolbox_start_spawn_process.py",
             ['source_entry.suffix.lower() == ".exe"'])
    contains("tool-host:resolver-native-entry",
             "main-system/src-core/tasks/tool_path_resolver.py",
             ["native_entry"])

    # check_typescript_retirement (A348) — no authored .ts/.tsx/.d.ts
    # outside the noise/exclusion set; dotdirs skipped implicitly.
    _ts_exclude = [
        "venv", "node_modules", "__pycache__", "dist", "dist-ui",
        "build", "release", "releases", "runtime", "out",
    ]
    for pattern in ("*.ts", "*.tsx", "*.d.ts"):
        checks.append({
            "id": f"typescript-retirement:{pattern}",
            "kind": "glob-absent", "path": "", "glob": pattern,
            "exclude": _ts_exclude,
        })



def emit_static_retirement(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # check_gpu_coordinator_torch_free — 部分歸約：nvidia-smi 探針
    # 存在性原生檢查＋退役框架引用缺席；AST import 判定與 query_gpu
    # 內部語義留 delegated。
    contains("gpu-coordinator:native-probe-markers",
             "shared-layer/src/shared_layer/adaptive/gpu_coordinator.py",
             ["_query_via_nvidia_smi", "def query_gpu"])
    not_contains("gpu-coordinator:torch-free",
             "shared-layer/src/shared_layer/adaptive/gpu_coordinator.py",
             ["import torch", "from torch", "_query_via_torch",
              "torch.cuda", "def _torch(", "_TORCH"])

    # check_jax_sft_retrace_bound — RETIRED (B167): JAX/XLA retired with
    # zero source/dependency/artifact role and no transitional period;
    # the former framework=jax verification is retired.

    # Python test-lane retirement (native-test-runner-register /
    # test-framework-final-ownership) — the forbidden artifacts must
    # stay absent; the migration worklist must exist and parse.
    for forbidden in (
        "governance_rule/execution/"
        "legacy_python_verification_adapter.py",
        "pytest.ini",
        "conftest.py",
        "native/test_suites/proxy_wire_agent.py",
        "scripts/devin-cli-p6-test-sla.py",
    ):
        checks.append({
            "id": f"python-retirement:forbidden:{forbidden}",
            "kind": "file-not-exists", "path": forbidden,
        })
    _retire = (
        "governance_rule/execution/audit/"
        "pytest_retirement_inventory.json")
    checks.append({
        "id": "python-retirement:inventory-parses",
        "kind": "json-parses", "path": _retire,
    })
    checks.append({
        "id": "python-retirement:inventory-keys",
        "kind": "json-has-keys", "path": _retire,
        "markers": ["registry", "status_enum", "delete_gate",
                    "rows", "fixtures", "bounded_consumers"],
    })

    # Python-minimization ratchet — native replacement for the retired
    # pytest gate (test_python_minimization_gates.py, inventory class H).
    # The baseline JSON carries the embedded measurement recipe; the
    # engine scans/classifies/counts and fails on any over-budget bucket.
    checks.append({
        "id": "python-minimization:bucket-budget",
        "kind": "py-bucket-budget",
        "path": "main-system/config/python-minimization-baseline.json",
    })

    # test_global_cleaner_retired.py (inventory H) — native migration.
    # lifecycle= appears once in identity_groups.py (inside
    # GLOBAL_CLEANER_IDENTITY); dir-absence covers the retired sources.
    contains("global-cleaner-retired:identity",
             "governance_rule/permission_directory/registries/permissions/"
             "identity_groups.py",
             ["GLOBAL_CLEANER_IDENTITY", 'bound_tool_id="global-cleaner"',
              'lifecycle="retired"'])
    checks.append({"id": "global-cleaner-retired:absent",
                   "kind": "file-not-exists",
                   "path": "Standalone tools/global-cleaner"})

    # check_tool_isolation_hardening (A266) — isolation controls 與
    # spawn 控制的 marker 檢查；spawn 兩檔 union 語義以 glob-contains
    # 表達（marker 落在任一命中檔即成立）。
    contains("tool-isolation:controls",
             "main-system/src-core/core_system/tool_isolation.py",
             ["_record_isolation_audit", "job_assigned",
              "job-assignment-failed"])
    for marker in ("stdin=subprocess.DEVNULL", "close_fds=True"):
        checks.append({
            "id": f"tool-isolation:spawn:{marker.split('=')[-1]}",
            "kind": "glob-contains",
            "glob": "main-system/src-core/tasks/toolbox_start_spawn*.py",
            "markers": [marker],
        })



def emit_static_axes(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # check_third_party_inventory — inventory JSON 的型別化值檢查；
    # formal=false 以 bool 等值、formality 前綴／等值以字串算子表達，
    # 與 Python ``is not False``/startswith/== 斷言逐一對齊。
    _inventory = (
        "governance_rule/execution/third_party_management/"
        "tool_inventory.json")
    checks.append({
        "id": "third-party-inventory:parse",
        "kind": "json-parses", "path": _inventory,
    })
    checks.append({
        "id": "third-party-inventory:dependency-formality",
        "kind": "json-key-value", "path": _inventory,
        # tools 為 id-keyed 陣列（Python dict-comp keyed by id）；
        # name[KEY] 選取 id==KEY 的元素。
        "markers": [
            "tools[pybind11].formal=false",
            "tools[pybind11].formality^=approved-implementation-",
            "tools[uv].formal=false",
            "tools[uv].formality^=approved-implementation-",
            "tools[local-rag].formal=false",
            "tools[local-rag].formality=bounded-degraded-fallback",
        ],
    })

    # check_bounded_worker_pools — 部分歸約：thread_budget 模組存在與
    # 必要入口 markers 原生；executor 邊界掃描（AST/regex）留 delegated。
    contains("worker-pools:thread-budget-module",
             "shared-layer/src/shared_layer/performance/thread_budget.py",
             ["CORE_BUDGET_CAP = 5", "bounded_workers",
              "bounded_threads", "allocation_within_budget"])

    # check_contract_axes — 部分歸約：各軸 contract_version>=1 與
    # minimum_supported>=0 原生；min<=version 跨鍵比較、schema
    # fallback 解析與非整數檢查留 delegated。Markers 依匯出當下檔案
    # 內容產生——鍵不存在時 Python 側本就略過，不憑空加嚴。
    for axis in ("ai-connection", "backend-lifecycle",
                 "data-architecture", "ipc", "sql-schema",
                 "tool-runtime"):
        path = f"main-system/config/{axis}-contract.json"
        markers: list[str] = []
        try:
            axis_data = json.loads(
                (root / path).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 交給引擎 unreadable FAIL
            axis_data = None
        if isinstance(axis_data, dict):
            if axis_data.get("contract_version") is not None:
                markers.append("contract_version>=1")
            if axis_data.get(
                    "minimum_supported_contract_version") is not None:
                markers.append(
                    "minimum_supported_contract_version>=0")
            schema = axis_data.get("schema")
            if axis_data.get("contract_version") is None and isinstance(
                    schema, str) and "/v" in schema:
                markers.append(
                    f"schema^={schema.rsplit('/v', 1)[0]}/v")
        if markers:
            checks.append({
                "id": f"contract-axis:version:{axis}",
                "kind": "json-key-value", "path": path,
                "markers": markers,
            })
