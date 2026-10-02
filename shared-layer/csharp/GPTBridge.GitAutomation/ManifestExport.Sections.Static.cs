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

        e.Emit("metadata-contract:retired", "file-not-exists",
            "shared-layer/src/shared_layer/metadata_contract.py");
        e.Emit("metadata-contract:ownership-doc", "file-exists",
            "shared-layer/docs/DATA_OWNERSHIP_CONTRACT.md");

        var shared = ctx.PolicyKw("shared_layer");
        //  source_root (shared-layer/src) is the retired Python root —
        //  it must NOT be required to exist; the never-reappear pins
        //  below keep governing it.
        foreach (var relative in new[]
                 {
                     KwStr(shared, "module_root"),
                     KwStr(shared, "data_root"),
                 })
            if (relative is not null)
                e.Emit($"shared-layer-dir:{relative}", "dir-exists",
                    relative);
        var sourceRoot = KwStr(shared, "source_root") ?? "shared-layer/src";
        //  Python sources retired (B167/B38): the owned-source contract is
        //  now the pin that they must never reappear.
        foreach (var name in new[] { "__init__.py", "channel.py", "store.py" })
            e.Emit($"shared-layer-source:{name}", "file-not-exists",
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
        e.Emit("embedded-browser:client", "file-not-exists",
            "shared-layer/src/shared_layer/embedded_browser_client.py");

        e.Emit("orphan-scanner:retired", "file-not-exists",
            "shared-layer/src/shared_layer/database/orphan_scanner.py");

        e.Contains("git-tiers:module",
            "shared-layer/csharp/GPTBridge.GitAutomation/Governance.cs",
            new[]
            {
                "TierGate", "Tier1", "Tier2", "Tier3",
                "Classify", "GateResult Execute",
            });
        e.Contains("git-tiers:gate-wrapper",
            "governance_rule/git-hooks/pre-commit",
            new[] { "GPTBridge.GitAutomation", "--hook" });
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
            "shared-layer/csharp/GPTBridge.CodexPipeline/PgDsn.cs",
            new[] { "postgresql://local/gptbridge_codex" });
        e.Contains("architecture:local-vector-degraded",
            "Standalone tools/vectord-rs/src/store.rs",
            new[]
            {
                "never the formal data authority",
                "PostgreSQL owns canonical",
            });
        e.Contains("architecture:ragd-retrieval-route",
            "Standalone tools/ragd-rs/src/main.rs",
            new[] { "/v1/retrieve", "/v1/rag/query" });
        e.Contains("architecture:ragd-dag-plane",
            "Standalone tools/ragd-rs/src/dag.rs",
            new[] { "HARD_MAX_STEPS", "dag-cycle", "node-timeout" });
        e.Contains("architecture:ragd-cag-gate",
            "Standalone tools/ragd-rs/src/cag.rs",
            new[] { "payload-integrity", "cache-denied:", "CagGate" });
        e.Contains("architecture:ragd-pg-authority",
            "Standalone tools/ragd-rs/src/retrieve.rs",
            new[] { "vectord never decides authority", "gptbridge_rag" });
        foreach (var name in new[]
                 { "market_data.json",
                   "xingcheng_tools/search/searchd.json" })
        {
            var path = "xingcheng/src/backend/services/" +
                       $"xingcheng/infrastructure/{name}";
            e.Contains($"architecture:network-allowlist:{name}", path,
                new[] { "NETWORK_DESTINATION_ALLOWLIST" });
        }

        //  Python shared-layer reconcile modules retired; the
        //  decision-surface invariants collapse to never-reappear pins.
        e.Emit("reconcile:state-store",
            "file-not-exists",
            "shared-layer/src/shared_layer/reconcile.py");
        e.Emit("reconcile:no-decision-surface",
            "file-not-exists",
            "shared-layer/src/shared_layer/reconcile.py");
        e.Emit("reconcile:decision-owner",
            "file-not-exists",
            "main-system/src-core/core_system/data_reconciliation.py");

        e.Emit("main-system:no-legacy-enforcer",
            "file-not-exists", "main-system/src-core/main.py");
        e.Emit("main-system:no-persistent-logger",
            "file-not-exists", "main-system/src-core/main.py");
        e.Emit("main-system:governance-denied",
            "file-not-exists",
            "main-system/src-core/core_system/governance_runtime.py");
        e.Contains("main-system:launcher-attested",
            "shared-layer/csharp/GPTBridge.MainSystem/StartupGate.cs",
            new[]
            {
                "gate_ok", "STARTUP_PHASE_MAX_WORKERS",
                "STARTUP_GATE_DEADLINE_SECONDS",
            });

        const string bootstrapDir =
            "main-system/launcher/src/GPTBridge.Bootstrap";
        e.Emit("bootstrap-entry:csproj", "file-exists",
            $"{bootstrapDir}/GPTBridge.Bootstrap.csproj");
        e.Emit("bootstrap-entry:program", "file-exists",
            $"{bootstrapDir}/Program.cs");
        e.Contains("bootstrap-entry:contract-marker",
            $"{bootstrapDir}/Program.cs", new[] { "--prepare-only" });

        const string ollamaSvc = "native/ollama_service";
        e.Emit("ollama-service:source", "file-exists",
            $"{ollamaSvc}/ollama_service.cpp");
        e.Emit("ollama-service:build", "file-exists",
            $"{ollamaSvc}/build.ps1");
        e.Contains("ollama-service:contract",
            $"{ollamaSvc}/ollama_service.cpp",
            new[]
            {
                "ollama-demand.jsonl", "ollama-demand-state.json",
                "spawned_image",
            });
        e.Contains("ollama-service:demand-start",
            $"{ollamaSvc}/ollama_service.cpp",
            new[]
            {
                "DETACHED_PROCESS", "CREATE_NO_WINDOW",
                "\" serve\"", "spawn-unavailable",
            });
        e.Contains("ollama-service:ownership",
            $"{ollamaSvc}/ollama_service.cpp",
            new[]
            {
                "UNLOAD_REFUSED_IMAGE_MISMATCH", "QueryFullProcessImageNameW",
            });

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
        e.Contains("tool-host:proxy-submit-ops",
            $"{toolHost}/TransportProxyClient.cs",
            new[]
            {
                "SubmitRequestAsync", "SubmitResponseAsync",
                "SubmitCancelAsync", "\"request\"", "\"response\"",
                "\"cancel\"", "SubmitBinding",
            });
        e.Contains("tool-host:spawn-exe-branch",
            "main-system/config/tool-runtime-contract.json",
            new[] { "\"allowed_modes\"", "\"executable\"" });
        e.Contains("tool-host:resolver-native-entry",
            "main-system/config/tool-runtime-contract.json",
            new[] { "\"special-unpackaged\"", "\"exe_required\"" });

        const string toolHostApp =
            "shared-layer/csharp/GPTBridge.ToolHost.App";
        e.Checks.Add(new JsonObject
        {
            ["id"] = "tool-host:dir:GPTBridge.ToolHost.App",
            ["kind"] = "dir-exists",
            ["path"] = toolHostApp,
        });
        // The generic host exe runs inside the same E4 boundary: no
        // credential minting, no transport-store access.
        foreach (var marker in new[]
        {
            "HMACSHA", "issue_token", "launcher_key",
            "integrity_manifest", "identity_attestation",
            "gptbridge_transport", "Npgsql", "pg_notify",
        })
            e.Checks.Add(new JsonObject
            {
                ["id"] = $"tool-host-app:forbidden:{marker}",
                ["kind"] = "glob-not-contains",
                ["glob"] = $"{toolHostApp}/*.cs",
                ["markers"] = Emitter.Arr(new[] { marker }),
            });

        const string permissionLib =
            "shared-layer/csharp/GPTBridge.Permission/" +
            "GPTBridge.Permission";
        foreach (var rel in new[]
        {
            $"{permissionLib}/GPTBridge.Permission.csproj",
            $"{permissionLib}/PermissionGrant.cs",
            $"{permissionLib}/PermissionGrantLedger.cs",
            $"{permissionLib}/PermissionLifecycleAutomation.cs",
            $"{permissionLib}/PermissionLifecycleAdjudicator.cs",
            $"{permissionLib}/RegistrySnapshot.cs",
            $"{permissionLib}/DirectorySyncAutomation.cs",
            $"{permissionLib}/ComplianceMonitorAutomation.cs",
            $"{permissionLib}/SelfHealingAutomation.cs",
            $"{permissionLib}/AuditSchedulerAutomation.cs",
            $"{permissionLib}/IdentityGroupManager.cs",
            $"{permissionLib}/IdentityGroupLifecycleAutomation.cs",
            "shared-layer/csharp/GPTBridge.Permission/" +
            "GPTBridge.Permission.Tests/" +
            "GPTBridge.Permission.Tests.csproj",
            "shared-layer/csharp/GPTBridge.Permission/" +
            "GPTBridge.Permission.Tests/" +
            "PermissionResidualTests.cs",
        })
            e.Checks.Add(new JsonObject
            {
                ["id"] = $"permission-core:{rel.Split('/')[^1]}",
                ["kind"] = "file-exists",
                ["path"] = rel,
            });
        e.Contains("permission-core:ledger-schema",
            $"{permissionLib}/PermissionGrantLedger.cs",
            new[]
            {
                "permission-grant-ledger.jsonl",
                "permission-violation-ledger.jsonl",
                "\"terminate\"", "\"revoke\"", "\"suspend\"",
            });
        e.Contains("permission-core:delegated-execution",
            $"{permissionLib}/PermissionLifecycleAdjudicator.cs",
            new[]
            {
                "delegated-to-governed-executor",
                "MISSING_PERMISSION_ID", "PERMISSION_NOT_ISSUED",
                "PERMISSION_ALREADY_TERMINATED",
            });
        e.Contains("permission-core:stop-triggers",
            $"{permissionLib}/PermissionLifecycleAutomation.cs",
            new[]
            {
                "MapStopTrigger", "PermissionStopTrigger.SecurityEvent",
                "LifecycleOperation.Suspend",
                "LifecycleOperation.Revoke",
                "LifecycleOperation.Terminate",
            });
        e.Contains("permission-core:directory-sync",
            $"{permissionLib}/DirectorySyncAutomation.cs",
            new[]
            {
                "RunOnceAsync", "SHA256.HashData",
                "InitialCodeVersion", "AuthorityCurrentVersion",
                "IdentityGroupIds.Count",
            });
        e.Contains("permission-core:compliance-monitor",
            $"{permissionLib}/ComplianceMonitorAutomation.cs",
            new[]
            {
                "RunOnceAsync", "ComplianceSeverity.Critical",
                "grant-actor-not-in-directory",
                "high_risk_actors", "unresolved_violations",
            });
        e.Contains("permission-core:self-healing",
            $"{permissionLib}/SelfHealingAutomation.cs",
            new[]
            {
                "HealingIssue.DirectoryAccess",
                "HealingIssue.GovernanceConnection",
                "HealingIssue.SovereignState",
                "HealingIssue.DirectoryPermissions",
                "RegisterRepair",
            });
        e.Contains("permission-core:audit-scheduler",
            $"{permissionLib}/AuditSchedulerAutomation.cs",
            new[]
            {
                "audit-engine.exe", "RunOnceAsync",
                "CancelAfter", "[PASS]",
            });
        e.Contains("permission-core:identity-groups",
            $"{permissionLib}/IdentityGroupManager.cs",
            new[]
            {
                "RegisterGroup", "ReconcileWithDirectory",
                "unregistered_in_directory", "duplicate_actor",
                "ResolveConflicts",
            });
        e.Contains("permission-core:identity-lifecycle",
            $"{permissionLib}/IdentityGroupLifecycleAutomation.cs",
            new[]
            {
                "RunOnceAsync", "GroupDeletionProposal",
                "PendingDeletions", "DeleteGroup",
                "identity-group-lifecycle.jsonl",
            });

        // 星澄 AI 投資管理與自動操盤系統 — investment-mobile native
        // service: sealed xingcheng relay, AI signal/proposal intake,
        // native risk+strategy gates, SHADOW/PAPER autotrade (LIVE
        // phase-locked). The retired Python channel_runtime lane is
        // replaced by src/InvestmentMobile.ToolHost.exe.
        const string invSvc =
            "Standalone tools/investment-mobile/src/" +
            "InvestmentMobile.Service";
        const string invHost =
            "Standalone tools/investment-mobile/src/" +
            "InvestmentMobile.ToolHost";
        const string invTests =
            "Standalone tools/investment-mobile/src/" +
            "InvestmentMobile.Service.Tests";
        foreach (var rel in new[]
        {
            $"{invSvc}/InvestmentMobile.Service.csproj",
            $"{invSvc}/XingchengChannel.cs",
            $"{invSvc}/InvestmentMobileService.cs",
            $"{invSvc}/SignalBook.cs",
            $"{invSvc}/AiSignalIntake.cs",
            $"{invSvc}/NativeRiskGate.cs",
            $"{invSvc}/AutoTradingEngine.cs",
            $"{invSvc}/TradingEngineCluster.cs",
            $"{invSvc}/ProxySubmitChannel.cs",
            $"{invHost}/InvestmentMobile.ToolHost.csproj",
            $"{invHost}/Program.cs",
            $"{invTests}/InvestmentMobile.Service.Tests.csproj",
            $"{invTests}/ServiceContractTests.cs",
            $"{invTests}/PipelineTests.cs",
            $"{invTests}/EngineClusterTests.cs",
            $"{invTests}/SubmitChannelTests.cs",
        })
            e.Checks.Add(new JsonObject
            {
                ["id"] = $"investment-mobile:{rel.Split('/')[^1]}",
                ["kind"] = "file-exists",
                ["path"] = rel,
            });
        e.Contains("investment-mobile:sealed-route",
            $"{invSvc}/XingchengChannel.cs",
            new[]
            {
                "xingcheng_mobile_get_investment_snapshot",
                "xingcheng_mobile_submit_investment_instruction",
                "AI_CHANNEL_NOT_CONNECTED",
                "governance/main-system",
                "governance/tool/investment-mobile",
            });
        e.Contains("investment-mobile:ai-boundary",
            $"{invSvc}/AiSignalIntake.cs",
            new[]
            {
                "INVALID_PROPOSAL", "SubmitSignal", "SubmitProposal",
                "DrainProposals",
            });
        e.Contains("investment-mobile:risk-gate",
            $"{invSvc}/NativeRiskGate.cs",
            new[]
            {
                "risk_evaluate_order", "RISK_ENGINE_UNAVAILABLE",
                "RiskOrderInput", "AllowedMarketMask",
            });
        e.Contains("investment-mobile:autotrade",
            $"{invSvc}/AutoTradingEngine.cs",
            new[]
            {
                "RunOnceAsync", "ModelBlocked", "RiskHalted",
                "LIVE_PHASE_LOCKED", "AiAssisted",
            });
        e.Contains("investment-mobile:cluster",
            $"{invSvc}/TradingEngineCluster.cs",
            new[]
            {
                "investment-mobile-signal-ingest",
                "investment-mobile-autotrade-recover",
                "RESUME_REQUIRES_GOVERNANCE",
                "investment-mobile-autotrade-risk-check",
            });
        e.Contains("investment-mobile:native-entry",
            "Standalone tools/investment-mobile/manifest.json",
            new[]
            {
                "dist/InvestmentMobile.ToolHost.exe",
                "\"native_entry\"",
            });
        e.Contains("investment-mobile:submit-channel",
            $"{invSvc}/ProxySubmitChannel.cs",
            new[]
            {
                "SubmitRequestAsync", "SubmitResponseAsync",
                "SubmitCancelAsync", "REQUEST_TIMEOUT",
                "AI_CHANNEL_NOT_CONNECTED",
            });
        // Same E4 boundary as the governed host: the service never
        // mints tokens or touches transport internals.
        foreach (var marker in new[]
        {
            "HMACSHA", "issue_token", "launcher_key",
            "integrity_manifest", "identity_attestation",
            "gptbridge_transport", "Npgsql", "pg_notify",
        })
            e.Checks.Add(new JsonObject
            {
                ["id"] = $"investment-mobile:forbidden:{marker}",
                ["kind"] = "glob-not-contains",
                ["glob"] = $"{invSvc}/*.cs",
                ["markers"] = Emitter.Arr(new[] { marker }),
            });

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
        // A132/B4/B73/B74 retire every Python source role, including
        // tests, verification adapters and training/tooling scripts.
        foreach (var pattern in new[] { "*.py", "*.pyw", "*.pyi" })
            e.Checks.Add(new JsonObject
            {
                ["id"] = $"python-retirement:source:{pattern}",
                ["kind"] = "glob-absent",
                ["path"] = "",
                ["glob"] = pattern,
                ["exclude"] = Emitter.Arr(new[]
                {
                    "node_modules", "target", "bin", "obj",
                }),
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
            "permissions/identity_groups.json",
            new[]
            {
                "GLOBAL_CLEANER_IDENTITY",
                "\"bound_tool_id\": \"global-cleaner\"",
                "\"lifecycle\": \"retired\"",
            });
        e.Checks.Add(new JsonObject
        {
            ["id"] = "global-cleaner-retired:absent",
            ["kind"] = "file-not-exists",
            ["path"] = "Standalone tools/global-cleaner",
        });

        e.Contains("tool-isolation:controls",
            "main-system/config/tool-isolation-policy.json",
            new[]
            {
                "\"isolate_on_crash\": true",
                "\"notify_main_system\": true",
                "\"kill_on_job_close\": true",
            });
        e.Contains("tool-isolation:spawn-containment",
            "main-system/config/tool-isolation-policy.json",
            new[]
            {
                "\"allow_breakaway\": false",
                "\"kill_on_job_close\": true",
                "\"wait_for_children\": true",
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
            "native/resource_governor/governor_budget.h",
            new[]
            {
                "concurrency-budget", "interactive_share",
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
