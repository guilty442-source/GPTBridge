using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.GitAutomation;

internal static partial class ManifestExport
{
    // ------------------------------------------------------------------
    //  Remainder family — git-tiers / codex consistency / directory
    //  audit / architecture registry / GPU / renderer / worker pools /
    //  SQL anti-patterns / protected duplicates / tm semantic parity.
    // ------------------------------------------------------------------

    private static void EmitRemainder(Ctx ctx)
    {
        var e = ctx.E;
        var root = ctx.Root;

        e.Contains("git-tiers:classify-failclosed",
            "shared-layer/csharp/GPTBridge.GitAutomation/Governance.cs",
            new[]
            {
                "Classify", "Tier1", "Tier2",
                "Tier3", "return 3",
            });
        e.Emit("git-tiers:module-py", "file-not-exists",
            "governance_rule/execution/git_tiers/__init__.py");
        e.Contains("git-tiers:pre-push-gate",
            "governance_rule/git-hooks/pre-push",
            new[]
            {
                "GOVERNANCE_AUTHORITY_APPROVAL", "merge-base",
                "refs/tags/",
            });
        foreach (var hook in new[]
                 { "pre-commit", "pre-merge-commit", "pre-push" })
            e.Emit($"git-tiers:hook:{hook}", "file-exists",
                $"governance_rule/git-hooks/{hook}");

        // --- codex consistency semantic half ----------------------------
        const string p1 =
            "governance_rule/codex/governance_codex.zh-TW.part-1.txt";
        e.Checks.Add(new JsonObject
        {
            ["id"] = "codex-consistency:pin-keys",
            ["kind"] = "json-has-keys",
            ["path"] = "shared-layer/release-dependencies.json",
            ["markers"] = Emitter.Arr(
                new[] { "governance_references" }),
        });
        e.Checks.Add(new JsonObject
        {
            ["id"] = "codex-consistency:pin-values",
            ["kind"] = "json-key-value",
            ["path"] = "shared-layer/release-dependencies.json",
            ["markers"] = Emitter.Arr(new[]
            {
                "governance_references.codex_identity" +
                "=governance-codex://official",
                "governance_references.codex_authority" +
                "=postgresql://local/gptbridge_codex",
                "governance_references.codex_version^=20",
            }),
        });
        e.Checks.Add(new JsonObject
        {
            ["id"] = "codex-consistency:mirror-keys",
            ["kind"] = "json-has-keys",
            ["path"] = p1,
            ["markers"] = Emitter.Arr(new[]
            {
                "codex_version", "assembled_payload_hash",
                "part_hash", "mirror_id",
            }),
        });
        foreach (var requiredTable in new[]
        {
            "metadata", "revision_history", "seal_manifest",
            "certification_policy", "version_evolution_rules",
            "provision_identities", "provision_lineage",
        })
            TablePresent(ctx, requiredTable);
        foreach (var (table, key) in new (string, string)[]
        {
            ("sovereigns", "sovereign_id"),
            ("principles", "provision_id"),
            ("articles", "provision_id"),
            ("edicts", "provision_id"),
        })
            RowsOrCount(ctx, $"codex-id:{table}", table, key);

        // --- directory audit family -------------------------------------
        var dirTables =
            PyLit.StrDict(ModuleVar(root,
                "governance_rule/execution/audit/audit_directories.py",
                "DIRECTORY_TABLES"));
        var requiredSchema = new List<string>(dirTables.Keys);
        requiredSchema.AddRange(new[]
        {
            "directory_master_catalog",
            "directory_format_contract",
            "directory_coverage_requirements",
            "directory_relationship_requirements",
            "provision_law_classification",
            "seal_manifest",
            "metadata",
        });
        requiredSchema.Sort(StringComparer.Ordinal);
        foreach (var table in requiredSchema)
            TablePresent(ctx, table);

        var catalog = TableRows(ctx, "directory_master_catalog");
        var contractCodes = TableRows(ctx, "directory_format_contract")
            .Select(r => r["directory_code"]?.GetValue<string>() ?? "")
            .Where(s => s.Length > 0)
            .ToHashSet(StringComparer.Ordinal);
        var catalogCodes = catalog
            .Select(r => r["directory_code"]?.GetValue<string>() ?? "")
            .Where(s => s.Length > 0)
            .ToHashSet(StringComparer.Ordinal);
        foreach (var ccode in contractCodes.Union(catalogCodes)
                     .OrderBy(s => s, StringComparer.Ordinal))
        {
            TableAssert(ctx, $"catalog:contract-has:{ccode}",
                "directory_master_catalog",
                new[] { $"directory_code={ccode}" });
            TableAssert(ctx, $"catalog:catalog-has:{ccode}",
                "directory_format_contract",
                new[] { $"directory_code={ccode}" });
        }
        foreach (var crow in catalog)
        {
            var ccode = crow["directory_code"]?.GetValue<string>() ?? "";
            var expectedOwner = ccode == "DIR_MAINTENANCE_MANUAL"
                ? "learning-evidence-sync-sub-sovereign"
                : "permission-sovereign";
            TableAssert(ctx, $"catalog:owner:{ccode}",
                "directory_master_catalog",
                new[]
                {
                    $"directory_code={ccode}",
                    $"owner={expectedOwner}",
                });
            var physical = (crow["canonical_name"]
                ?.GetValue<string>() ?? "").Replace('-', '_');
            if (crow["implementation_state"]?.GetValue<string>()
                    == "active" && physical.Length > 0)
                TablePresent(ctx, physical);
        }
        foreach (var (table, idcol) in dirTables)
        {
            var rows = TableRows(ctx, table);
            foreach (var drow in rows)
                if (drow[idcol] is null)
                    e.Fail($"directory:identity-missing:{table}",
                        $"{table} row missing {idcol}");
            var owner = table == "maintenance_manual_directory"
                ? "learning-evidence-sync-sub-sovereign"
                : "permission-sovereign";
            RowsOrCount(ctx, $"directory:identity:{table}", table,
                idcol,
                _ => new List<string> { $"owner={owner}" });
        }
        var classRows = TableRows(ctx, "provision_law_classification");
        if (classRows.Count > 0)
        {
            TableAssert(ctx, "provision-class:count",
                "provision_law_classification",
                Array.Empty<string>(), count: classRows.Count);
            foreach (var law in classRows
                         .Select(r => r["law_code"]
                             ?.GetValue<string>() ?? "")
                         .Where(s => s.Length > 0)
                         .ToHashSet(StringComparer.Ordinal)
                         .OrderBy(s => s, StringComparer.Ordinal))
                TableAssert(ctx, $"provision-law:{law}",
                    "law_structure_directory",
                    new[] { $"law_code={law}" });
        }
        var seals = TableRows(ctx, "seal_manifest");
        if (seals.Count == 0)
        {
            e.Fail("directory:seal-empty", "seal manifest is empty");
        }
        else
        {
            var currentSeal = seals
                .OrderByDescending(r => r["version"]
                    ?.GetValue<string>() ?? "", StringComparer.Ordinal)
                .First();
            var sv = currentSeal["version"]?.GetValue<string>() ?? "";
            TableAssert(ctx, $"directory:seal:{sv}", "seal_manifest",
                new[] { $"version={sv}" }
                    .Concat(Bind("certification_state",
                        currentSeal["certification_state"]))
                    .Concat(Bind("content_root",
                        currentSeal["content_root"]))
                    .Concat(Bind("identity_root",
                        currentSeal["identity_root"]))
                    .Concat(Bind("full_root",
                        currentSeal["full_root"]))
                    .Concat(Bind("history_head",
                        currentSeal["history_head"])));
        }

        // --- architecture registry --------------------------------------
        const string archReg =
            "governance_rule/execution/audit/" +
            "architecture_registry.json";
        e.Emit("architecture-registry:parse", "json-parses", archReg);
        e.Emit(
            "sovereign-module:main-system/governance/" +
            "sovereigns/__init__.py",
            "file-not-exists",
            "main-system/governance/sovereigns/__init__.py");
        e.Contains("sovereign-module:transition-registry",
            "governance_rule/execution/audit/" +
            "sovereign_transition_registry.json",
            new[] { "sovereign-transition-registry", "A604" });
        var routeIds = new HashSet<string>(StringComparer.Ordinal);
        foreach (var listName in new[]
                 { "AUTHORIZED_TOOL_IDS", "AI_CHANNEL_TOOL_IDS" })
            foreach (var rid in ModuleStrings(root,
                "governance_rule/permission_directory/registries/" +
                "permissions/tool_routes.py", listName))
                routeIds.Add(rid);
        foreach (var scalarName in new[] { "MOBILE_TOOL_ID", "STAR_TOOL_ID" })
            if (PyLit.AsStr(ModuleVar(root,
                    "governance_rule/permission_directory/registries/" +
                    "permissions/tool_routes.py", scalarName))
                is { } scalarId && scalarId.Length > 0)
                routeIds.Add(scalarId);
        foreach (var rid in routeIds.OrderBy(s => s,
                     StringComparer.Ordinal))
            e.Contains($"architecture-registry:route:{rid}", archReg,
                new[] { $"\"{rid}\"" });

        // --- gpu torch-free (B167/B38) ----------------------------------
        // The Python gpu_coordinator lane is retired; the torch-free
        // invariant is now structural (no Python source may return).
        const string gpuSrc =
            "shared-layer/src/shared_layer/adaptive/" +
            "gpu_coordinator.py";
        e.Emit("gpu-coordinator:retired", "file-not-exists", gpuSrc);
        e.Emit("gpu-torch-free:retired", "file-not-exists", gpuSrc);

        // --- renderer idle gating ---------------------------------------
        var rendererBase = Rel(root, "main-system/src-ui/renderer");
        if (Directory.Exists(rendererBase))
        {
            foreach (var rfile in Directory
                         .EnumerateFiles(rendererBase, "*",
                             SearchOption.AllDirectories)
                         .Where(f =>
                         {
                             var ext = Path.GetExtension(f);
                             if (ext is not (".js" or ".jsx" or ".mjs"))
                                 return false;
                             var parts = f.Split(
                                 Path.DirectorySeparatorChar);
                             return !parts.Contains("node_modules")
                                    && !parts.Contains("dist");
                         })
                         .OrderBy(f => f, StringComparer.Ordinal))
            {
                var rtext = ReadText(rfile);
                if (!rtext.Contains("setInterval(",
                        StringComparison.Ordinal))
                    continue;
                var rrel = Path.GetRelativePath(root, rfile)
                    .Replace('\\', '/');
                e.Checks.Add(new JsonObject
                {
                    ["id"] = $"renderer-idle-gating:{rrel}",
                    ["kind"] = "file-not-contains-unless",
                    ["path"] = rrel,
                    ["markers"] = Emitter.Arr(
                        new[] { "setInterval(" }),
                    ["unless"] = Emitter.Arr(new[]
                    {
                        "visibilityState", "idle-ok",
                        "navigator.onLine", "visibilitychange",
                    }),
                });
            }
        }

        // --- bounded worker pools ---------------------------------------
        e.Contains("worker-pools:budget-module",
            "native/resource_governor/governor_budget.h",
            new[]
            {
                "BudgetPolicy", "ClassBudget",
                "ConcurrencyBudget",
            });
        e.Emit("worker-pools:budget-module-py", "file-not-exists",
            "shared-layer/src/shared_layer/performance/" +
            "thread_budget.py");
        var poolSkip = new HashSet<string>(StringComparer.Ordinal)
        {
            "__pycache__", ".venv", "bin", "build", "dist",
            "node_modules", "releases", "runtime", "test", "tests",
        };
        foreach (var poolRoot in new[]
        {
            "main-system/src-core", "main-system/governance",
            "shared-layer/src", "governance_rule",
            "Standalone tools",
        })
        {
            var pbase = Rel(root, poolRoot);
            if (!Directory.Exists(pbase)) continue;
            foreach (var pfile in Directory
                         .EnumerateFiles(pbase, "*.py",
                             SearchOption.AllDirectories)
                         .OrderBy(f => f, StringComparer.Ordinal))
            {
                var parts = pfile.Split(Path.DirectorySeparatorChar);
                if (parts.Any(poolSkip.Contains)
                    || Path.GetFileName(pfile).StartsWith("test_",
                        StringComparison.Ordinal))
                    continue;
                string ptext;
                try { ptext = File.ReadAllText(pfile); }
                catch (IOException) { continue; }
                catch (UnauthorizedAccessException) { continue; }
                if (!ptext.Contains("PoolExecutor",
                        StringComparison.Ordinal))
                    continue;
                e.Contains(
                    $"worker-pool:{Path.GetRelativePath(root, pfile)
                        .Replace('\\', '/')}",
                    Path.GetRelativePath(root, pfile)
                        .Replace('\\', '/'),
                    new[] { "max_workers" });
            }
        }

        // --- sql anti-patterns ------------------------------------------
        const string baselineRel =
            "governance_rule/execution/audit/" +
            "sql_patterns_baseline.json";
        e.Emit("sql-patterns:baseline-exists", "file-exists",
            baselineRel);
        e.Checks.Add(new JsonObject
        {
            ["id"] = "sql-patterns:baseline-keys",
            ["kind"] = "json-has-keys",
            ["path"] = baselineRel,
            ["markers"] = Emitter.Arr(new[] { "findings" }),
        });
        e.Contains("sql-patterns:scanner-machinery",
            "shared-layer/csharp/GPTBridge.GitAutomation/" +
            "ManifestExport.Remainder.SqlScan.cs",
            new[]
            {
                "CollectFindingKeys", "SqlScanRoots",
                "SelectStar", "OffsetRe",
            });
        e.Emit("sql-patterns:scanner-py", "file-not-exists",
            "governance_rule/execution/audit/audit_sql_patterns.py");
        foreach (var violation in CollectFindingKeys(root)
                     .Distinct(StringComparer.Ordinal)
                     .OrderBy(s => s, StringComparer.Ordinal))
            e.Fail($"sql-patterns:finding:{violation}",
                "sql anti-pattern finding at manifest export: " +
                violation);

        // --- protected duplicates ---------------------------------------
        var protectedList = ProtectedSources(ctx);
        if (protectedList.Count != protectedList
                .Distinct(StringComparer.Ordinal).Count())
            e.Fail("protected-source:duplicates",
                "protected governance sources contain duplicates");

        // --- tool-manifests semantic remainder --------------------------
        EmitTmSemantic(ctx);
    }
}
