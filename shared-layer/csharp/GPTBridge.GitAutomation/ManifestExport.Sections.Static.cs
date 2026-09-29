using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.GitAutomation;

internal static partial class ManifestExport
{
    private static void EmitStaticSection(Ctx ctx)
    {
        var e = ctx.E;
        var root = ctx.Root;

        e.Contains("metadata-contract:fields",
            "shared-layer/src/shared_layer/metadata_contract.py",
            new[]
            {
                "FIELD_MODULE_ID", "FIELD_RESOURCE_ID", "FIELD_LOCATOR_ID",
                "FIELD_VERSION", "FIELD_CONTENT_HASH", "FIELD_UPDATED_AT",
                "FIELD_STATUS", "ResourceMetadata",
                "validate_vector_payload",
            });
        e.Emit("metadata-contract:ownership-doc", "file-exists",
            "shared-layer/docs/DATA_OWNERSHIP_CONTRACT.md");

        var shared = ctx.PolicyKw("shared_layer");
        foreach (var relative in new[]
                 {
                     KwStr(shared, "module_root"),
                     KwStr(shared, "source_root"),
                     KwStr(shared, "data_root"),
                 })
            if (relative is not null)
                e.Emit($"shared-layer-dir:{relative}", "dir-exists",
                    relative);
        var sourceRoot = KwStr(shared, "source_root") ?? "shared-layer/src";
        foreach (var name in new[] { "__init__.py", "channel.py", "store.py" })
            e.Emit($"shared-layer-source:{name}", "file-exists",
                $"{sourceRoot}/shared_layer/{name}");

        foreach (var path in new[]
        {
            "Standalone tools/ai-collaboration/src/backend/services/" +
            "ai_collaboration/integration/browser_automation.py",
            "Standalone tools/ai-collaboration/src/backend/services/" +
            "ai_collaboration/integration/provider_session.py",
            "Standalone tools/vaultly/src/backend/services/" +
            "vaultly/integration/browser_session.py",
        })
        {
            e.NotContains($"embedded-browser:no-playwright:{path}", path,
                new[] { "from playwright", "import playwright" },
                optional: true);
            e.Checks.Add(new JsonObject
            {
                ["id"] = $"embedded-browser:no-async-playwright:{path}",
                ["kind"] = "file-not-contains-unless",
                ["path"] = path,
                ["markers"] = Emitter.Arr(new[] { "async_playwright" }),
                ["unless"] = Emitter.Arr(
                    new[] { "InProcessEmbeddedBrowser" }),
                ["optional"] = true,
            });
        }
        foreach (var path in new[]
                 { "main-system/requirements.txt",
                   "main-system/pyproject.toml" })
            e.Checks.Add(new JsonObject
            {
                ["id"] = $"embedded-browser:no-dep:{path}",
                ["kind"] = "file-not-contains",
                ["path"] = path,
                ["markers"] = Emitter.Arr(new[] { "playwright" }),
                ["optional"] = true,
                ["ignore_case"] = true,
            });
        e.Emit("embedded-browser:module", "file-exists",
            "main-system/src-tauri/src/webview_host/mod.rs");
        e.Emit("embedded-browser:client", "file-exists",
            "shared-layer/src/shared_layer/embedded_browser_client.py");

        e.Contains("orphan-scanner:module",
            "shared-layer/src/shared_layer/database/orphan_scanner.py",
            new[] { "scan_orphans" });

        e.Contains("git-tiers:module",
            "governance_rule/execution/git_tiers/__init__.py",
            new[]
            {
                "TIER1_OPS", "TIER2_OPS", "TIER3_OPS",
                "def classify", "def enforce", "def audit_log",
            });
        e.Contains("git-tiers:gate-wrapper", "scripts/git-gate.py",
            new[] { "from governance_rule.execution.git_tiers import" });
        e.Contains("git-tiers:pre-push",
            "governance_rule/git-hooks/pre-push",
            new[]
            {
                "GOVERNANCE_AUTHORITY_APPROVAL", "merge-base",
                "refs/tags/",
            });

        e.Checks.Add(new JsonObject
        {
            ["id"] = "release-manifest:keys",
            ["kind"] = "json-has-keys",
            ["path"] = "shared-layer/database-release.json",
            ["markers"] = Emitter.Arr(new[]
            {
                "release_id", "schema_version", "migration_head",
                "rls_version", "role_version",
                "reconcile_contract_version", "query_contract_version",
                "minimum_runtime_version", "compatibility_range",
                "state",
            }),
        });

        e.Contains("architecture:shared-database-canonical",
            "shared-layer/src/shared_layer/database/__init__.py",
            new[] { "POSTGRESQL_CANONICAL: bool = True" });
        e.Contains("architecture:local-vector-degraded",
            "shared-layer/src/shared_layer/local/vector_store.py",
            new[]
            {
                "\"engine\": \"rust-vectord-degraded\"",
                "\"canonical\": False",
            });
        foreach (var name in new[]
                 { "market_data.py", "xingcheng_tools/search/searchd.py" })
        {
            var path = "Standalone tools/local-model/src/backend/services/" +
                       $"xingcheng/infrastructure/{name}";
            e.Contains($"architecture:network-allowlist:{name}", path,
                new[] { "NETWORK_DESTINATION_ALLOWLIST" });
        }

        e.Contains("reconcile:state-store",
            "shared-layer/src/shared_layer/reconcile.py",
            new[] { "ReconcileStateStore" });
        e.NotContains("reconcile:no-decision-surface",
            "shared-layer/src/shared_layer/reconcile.py",
            new[]
            {
                "class ReconcileService", "def _push_to_central",
                "def _pull_from_central",
            });
        e.Contains("reconcile:decision-owner",
            "main-system/src-core/core_system/data_reconciliation.py",
            new[] { "class ReconcileService" });

        e.NotContains("main-system:no-legacy-enforcer",
            "main-system/src-core/main.py",
            new[] { "GovernanceEnforcer", "governance.enforcer" });
        e.Contains("main-system:launcher-attested",
            "main-system/src-core/main.py",
            new[] { "MainSystemGovernance.from_environment" });
        e.NotContains("main-system:no-persistent-logger",
            "main-system/src-core/main.py", new[] { "CoreLogger" });
        e.Contains("main-system:governance-denied",
            "main-system/src-core/core_system/governance_runtime.py",
            new[] { "if tool_id == \"governance_rule\"" });

        const string bootstrapDir =
            "main-system/launcher/src/GPTBridge.Bootstrap";
        e.Emit("bootstrap-entry:csproj", "file-exists",
            $"{bootstrapDir}/GPTBridge.Bootstrap.csproj");
        e.Emit("bootstrap-entry:program", "file-exists",
            $"{bootstrapDir}/Program.cs");
        e.Contains("bootstrap-entry:contract-marker",
            $"{bootstrapDir}/Program.cs", new[] { "--prepare-only" });

        const string channelLib =
            "shared-layer/csharp/GPTBridge.Channels/GPTBridge.Channels";
        foreach (var rel in new[]
        {
            $"{channelLib}/GPTBridge.Channels.csproj",
            "shared-layer/csharp/GPTBridge.Channels/" +
            "GPTBridge.Channels.Tests/GPTBridge.Channels.Tests.csproj",
            $"{channelLib}/A263Channel.cs",
        })
            e.Checks.Add(new JsonObject
            {
                ["id"] = $"channel-gateway:{rel.Split('/')[^1]}",
                ["kind"] = "file-exists",
                ["path"] = rel,
            });
        e.Contains("channel-gateway:port-invariants",
            $"{channelLib}/A263Channel.cs",
            new[] { "Stopwatch.GetTimestamp", "ReconnectAsync" });

        const string toolHost =
            "shared-layer/csharp/GPTBridge.ToolHost/GPTBridge.ToolHost";
        foreach (var rel in new[] { toolHost, $"{toolHost}.Tests" })
            e.Checks.Add(new JsonObject
            {
                ["id"] = $"tool-host:dir:{rel.Split('/')[^1]}",
                ["kind"] = "dir-exists",
                ["path"] = rel,
            });
        foreach (var marker in new[]
        {
            "HMACSHA", "issue_token", "launcher_key",
            "integrity_manifest", "identity_attestation",
            "gptbridge_transport", "Npgsql", "pg_notify",
        })
            e.Checks.Add(new JsonObject
            {
                ["id"] = $"tool-host:forbidden:{marker}",
                ["kind"] = "glob-not-contains",
                ["glob"] = $"{toolHost}/*.cs",
                ["markers"] = Emitter.Arr(new[] { marker }),
            });
        e.Emit("tool-host:proxy-client", "file-exists",
            $"{toolHost}/TransportProxyClient.cs");
        e.Contains("tool-host:proxy-ops",
            $"{toolHost}/TransportProxyClient.cs",
            new[]
            {
                "\"hello\"", "\"claim\"", "\"respond\"",
                "\"request_cancelled\"", "\"notification_stamp\"",
            });
        e.Contains("tool-host:spawn-exe-branch",
            "main-system/src-core/tasks/toolbox_start_spawn_process.py",
            new[] { "source_entry.suffix.lower() == \".exe\"" });
        e.Contains("tool-host:resolver-native-entry",
            "main-system/src-core/tasks/tool_path_resolver.py",
            new[] { "native_entry" });

        var tsExclude = new[]
        {
            "venv", "node_modules", "__pycache__", "dist", "dist-ui",
            "build", "release", "releases", "runtime", "out",
        };
        foreach (var pattern in new[] { "*.ts", "*.tsx", "*.d.ts" })
            e.Checks.Add(new JsonObject
            {
                ["id"] = $"typescript-retirement:{pattern}",
                ["kind"] = "glob-absent",
                ["path"] = "",
                ["glob"] = pattern,
                ["exclude"] = Emitter.Arr(tsExclude),
            });

        foreach (var forbidden in new[]
        {
            "governance_rule/execution/" +
            "legacy_python_verification_adapter.py",
            "pytest.ini",
            "conftest.py",
            "native/test_suites/proxy_wire_agent.py",
            "scripts/devin-cli-p6-test-sla.py",
        })
            e.Checks.Add(new JsonObject
            {
                ["id"] = $"python-retirement:forbidden:{forbidden}",
                ["kind"] = "file-not-exists",
                ["path"] = forbidden,
            });
        const string retireInv =
            "governance_rule/execution/audit/" +
            "pytest_retirement_inventory.json";
        e.Checks.Add(new JsonObject
        {
            ["id"] = "python-retirement:inventory-parses",
            ["kind"] = "json-parses",
            ["path"] = retireInv,
        });
        e.Checks.Add(new JsonObject
        {
            ["id"] = "python-retirement:inventory-keys",
            ["kind"] = "json-has-keys",
            ["path"] = retireInv,
            ["markers"] = Emitter.Arr(new[]
            {
                "registry", "status_enum", "delete_gate",
                "rows", "fixtures", "bounded_consumers",
            }),
        });
        e.Checks.Add(new JsonObject
        {
            ["id"] = "python-minimization:bucket-budget",
            ["kind"] = "py-bucket-budget",
            ["path"] = "main-system/config/" +
                       "python-minimization-baseline.json",
        });

        e.Contains("global-cleaner-retired:identity",
            "governance_rule/permission_directory/registries/" +
            "permissions/identity_groups.py",
            new[]
            {
                "GLOBAL_CLEANER_IDENTITY",
                "bound_tool_id=\"global-cleaner\"",
                "lifecycle=\"retired\"",
            });
        e.Checks.Add(new JsonObject
        {
            ["id"] = "global-cleaner-retired:absent",
            ["kind"] = "file-not-exists",
            ["path"] = "Standalone tools/global-cleaner",
        });

        e.Contains("tool-isolation:controls",
            "main-system/src-core/core_system/tool_isolation.py",
            new[]
            {
                "_record_isolation_audit", "job_assigned",
                "job-assignment-failed",
            });
        foreach (var marker in new[]
                 { "stdin=subprocess.DEVNULL", "close_fds=True" })
            e.Checks.Add(new JsonObject
            {
                ["id"] = $"tool-isolation:spawn:{marker.Split('=')[^1]}",
                ["kind"] = "glob-contains",
                ["glob"] = "main-system/src-core/tasks/" +
                           "toolbox_start_spawn*.py",
                ["markers"] = Emitter.Arr(new[] { marker }),
            });

        const string inventory =
            "governance_rule/execution/third_party_management/" +
            "tool_inventory.json";
        e.Checks.Add(new JsonObject
        {
            ["id"] = "third-party-inventory:parse",
            ["kind"] = "json-parses",
            ["path"] = inventory,
        });
        e.Checks.Add(new JsonObject
        {
            ["id"] = "third-party-inventory:dependency-formality",
            ["kind"] = "json-key-value",
            ["path"] = inventory,
            ["markers"] = Emitter.Arr(new[]
            {
                "tools[pybind11].formal=false",
                "tools[pybind11].formality^=approved-implementation-",
                "tools[uv].formal=false",
                "tools[uv].formality^=approved-implementation-",
                "tools[local-rag].formal=false",
                "tools[local-rag].formality=bounded-degraded-fallback",
            }),
        });

        e.Contains("worker-pools:thread-budget-module",
            "shared-layer/src/shared_layer/performance/thread_budget.py",
            new[]
            {
                "CORE_BUDGET_CAP = 5", "bounded_workers",
                "bounded_threads", "allocation_within_budget",
            });

        foreach (var axis in new[]
        {
            "ai-connection", "backend-lifecycle", "data-architecture",
            "ipc", "sql-schema", "tool-runtime",
        })
        {
            var path = $"main-system/config/{axis}-contract.json";
            var markers = new List<string>();
            var axisData = ReadJsonObject(Rel(root, path));
            if (axisData is not null)
            {
                if (axisData["contract_version"] is not null)
                    markers.Add("contract_version>=1");
                if (axisData["minimum_supported_contract_version"]
                        is not null)
                    markers.Add(
                        "minimum_supported_contract_version>=0");
                var schema = axisData["schema"]?.GetValue<string>();
                if (axisData["contract_version"] is null
                    && schema is not null && schema.Contains("/v"))
                    markers.Add(
                        $"schema^={schema[..schema.LastIndexOf("/v", StringComparison.Ordinal)]}/v");
            }
            if (markers.Count > 0)
                e.Checks.Add(new JsonObject
                {
                    ["id"] = $"contract-axis:version:{axis}",
                    ["kind"] = "json-key-value",
                    ["path"] = path,
                    ["markers"] = Emitter.Arr(markers),
                });
        }
    }
}
