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
            "governance_rule/execution/git_tiers/__init__.py",
            new[]
            {
                "def classify", "TIER1_OPS", "TIER2_OPS",
                "TIER3_OPS", "return 3",
            });
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
            "file-exists",
            "main-system/governance/sovereigns/__init__.py");
        var routesText = ReadText(Rel(root,
            "governance_rule/permission_directory/registries/" +
            "permissions/tool_routes.py"));
        var routeIds = new HashSet<string>(StringComparer.Ordinal);
        foreach (var marker in new[] { "\"tool_id\"", "'tool_id'" })
        {
            var idx = 0;
            for (;;)
            {
                idx = routesText.IndexOf(marker, idx,
                    StringComparison.Ordinal);
                if (idx < 0) break;
                var tail = routesText[(idx + marker.Length)..];
                foreach (var quote in new[] { '"', '\'' })
                {
                    var start = tail.IndexOf(quote);
                    if (start < 0) continue;
                    var end = tail.IndexOf(quote, start + 1);
                    if (end > 0)
                    {
                        var cand = tail[(start + 1)..end].Trim();
                        if (cand.Length > 0) routeIds.Add(cand);
                        break;
                    }
                }
                idx += 1;
            }
        }
        foreach (var rid in routeIds.OrderBy(s => s,
                     StringComparer.Ordinal))
            e.Contains($"architecture-registry:route:{rid}", archReg,
                new[] { $"\"{rid}\"" });

        // --- gpu lazy torch ---------------------------------------------
        const string gpuSrc =
            "shared-layer/src/shared_layer/adaptive/" +
            "gpu_coordinator.py";
        e.Contains("gpu-lazy-torch:probe", gpuSrc, new[]
        {
            "def _torch()", "_query_via_nvidia_smi",
            "def query_gpu",
        });
        e.NotContains("gpu-lazy-torch:top-import", gpuSrc, new[]
        {
            "\nimport torch\n", "\nimport torch ",
            "\nimport torch,", "\nimport torch.",
            "\nfrom torch ", "\nfrom torch.",
        });

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
            "shared-layer/src/shared_layer/performance/" +
            "thread_budget.py",
            new[]
            {
                "CORE_BUDGET_CAP = 5", "bounded_workers",
                "bounded_threads", "allocation_within_budget",
            });
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
            "governance_rule/execution/audit/audit_sql_patterns.py",
            new[]
            {
                "collect_finding_keys", "baseline_path",
                "_SELECT_STAR", "_OFFSET",
            });
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

    private static bool FullMatch(string pattern, string text)
    {
        var m = Regex.Match(text, pattern);
        return m.Success && m.Index == 0 && m.Length == text.Length;
    }

    private static void EmitTmSemantic(Ctx ctx)
    {
        var e = ctx.E;
        var root = ctx.Root;
        var tmIds = new HashSet<string>(StringComparer.Ordinal);
        var tmDocs =
            new List<(string Path, string ToolId, JsonObject Doc)>();
        var tmTopRoots = new HashSet<string>(StringComparer.Ordinal);
        var tmPaths = new List<string>();
        var standaloneDir = Rel(root, "Standalone tools");
        foreach (var dir in Directory.EnumerateDirectories(root))
        {
            var p = Path.Combine(dir, "manifest.json");
            if (File.Exists(p) && ManifestScanned(root, p))
                tmPaths.Add(p);
        }
        if (Directory.Exists(standaloneDir))
        {
            foreach (var dir in Directory
                         .EnumerateDirectories(standaloneDir))
            {
                var p = Path.Combine(dir, "manifest.json");
                if (File.Exists(p)) tmPaths.Add(p);
                foreach (var sub in Directory
                             .EnumerateDirectories(dir))
                {
                    var p2 = Path.Combine(sub, "manifest.json");
                    if (File.Exists(p2)) tmPaths.Add(p2);
                }
            }
        }
        foreach (var p in Directory
                     .EnumerateFiles(root, "manifest.json",
                         SearchOption.AllDirectories)
                     .Where(f => Path.GetRelativePath(root, f)
                         .Split(Path.DirectorySeparatorChar).Length == 5)
                     .Where(f => ManifestScanned(root, f)))
            tmPaths.Add(p);
        tmPaths.Sort(StringComparer.Ordinal);

        foreach (var mpath in tmPaths)
        {
            var mdoc = ReadJsonObject(mpath);
            if (mdoc is null) continue;
            var mtid = mdoc["id"]?.GetValue<string>() ?? "";
            var mlc = mdoc["lifecycle"] as JsonObject;
            if ((mlc?["status"]?.GetValue<string>() ?? "").Trim()
                    .Equals("retired", StringComparison.OrdinalIgnoreCase))
            {
                if (mtid.Length == 0)
                    e.Fail($"tm:{Path.GetRelativePath(root, mpath)
                        .Replace('\\', '/')}:no-id",
                        "retired tool manifest lacks an identifier");
                continue;
            }
            tmIds.Add(mtid);
            var por = mdoc["physical_owner_root"]
                ?.GetValue<string>() ?? "";
            tmDocs.Add((mpath, mtid, mdoc));
            if (Path.GetRelativePath(root, mpath)
                    .Split(Path.DirectorySeparatorChar).Length <= 3
                && por.Length > 0)
                tmTopRoots.Add(por);
        }

        var labels = ctx.PolicyKw("identifier_labels");
        var toolIdPattern =
            KwStr(labels, "tool_id_pattern") ?? "";
        var capPattern =
            KwStr(labels, "capability_pattern") ?? "";
        var localePattern =
            KwStr(labels, "locale_key_pattern") ?? "";
        var approvedCaps = new HashSet<string>(
            KwStrings(ctx.CodeRulesCall, "approved_capability_names"),
            StringComparer.Ordinal);

        foreach (var (mpath, mtid, mdoc) in tmDocs)
        {
            var mrel = Path.GetRelativePath(root, mpath)
                .Replace('\\', '/');
            var parts = Path.GetRelativePath(root, mpath)
                .Split(Path.DirectorySeparatorChar);
            var por = mdoc["physical_owner_root"]
                ?.GetValue<string>() ?? "";
            if (parts.Length >= 4)
            {
                if (por.Length == 0 || por != parts[1]
                    || !tmTopRoots.Contains(por))
                    e.Fail($"tm:{mtid}:owner-parity",
                        $"nested physical_owner_root invalid: {mrel}");
            }
            else if (mtid != Path.GetFileName(
                         Path.GetDirectoryName(mpath)!)
                     && por != Path.GetFileName(
                         Path.GetDirectoryName(mpath)!))
            {
                e.Fail($"tm:{mtid}:dir-parity",
                    $"tool identity mismatch: {mrel}");
            }
            e.Checks.Add(new JsonObject
            {
                ["id"] = $"tm:{mtid}:id",
                ["kind"] = "json-key-value",
                ["path"] = mrel,
                ["markers"] = Emitter.Arr(new[] { $"id={mtid}" }),
            });
            if (toolIdPattern.Length > 0
                && !FullMatch(toolIdPattern, mtid))
                e.Fail($"tm:{mtid}:label",
                    $"tool identifier is not standardized: {mtid}");
            if (mdoc["capabilities"] is JsonObject caps)
            {
                foreach (var (cname, _) in caps)
                {
                    if (!approvedCaps.Contains(cname)
                        || (capPattern.Length > 0
                            && !FullMatch(capPattern, cname)))
                        e.Fail($"tm:{mtid}:cap:{cname}",
                            "capability label not standardized: " +
                            $"{mtid}:{cname}");
                    else
                        e.Contains($"tm:{mtid}:cap:{cname}", mrel,
                            new[] { $"\"{cname}\"" });
                }
            }
            else
            {
                e.Fail($"tm:{mtid}:caps",
                    $"tool capabilities are missing: {mtid}");
            }
            var locale = ReadJsonObject(Path.Combine(
                Path.GetDirectoryName(mpath)!, "locales", "zh-TW.json"));
            if (locale is null) continue;
            var bad = locale.Any(kv =>
                (localePattern.Length > 0
                 && !FullMatch(localePattern, kv.Key))
                || kv.Value is not JsonValue lv
                || !lv.TryGetValue<string>(out _));
            if (bad)
                e.Fail($"tm:{mtid}:locale-schema",
                    $"zh-TW locale schema invalid: {mtid}");
        }

        var retiredIdent = ctx.Identities
            .Where(i => i.Lifecycle == "retired")
            .Select(i => i.BoundToolId)
            .ToHashSet(StringComparer.Ordinal);
        var registered = ctx.Identities
            .Where(i => i.BoundToolId != "main-system"
                        && !ctx.NonIndependent.Contains(i.BoundToolId)
                        && i.Lifecycle != "retired")
            .Select(i => i.BoundToolId)
            .ToHashSet(StringComparer.Ordinal);
        var manifestSet = tmIds;
        manifestSet.ExceptWith(ctx.NonIndependent);
        manifestSet.ExceptWith(retiredIdent);
        var approved = KwStrings(ctx.CodeRulesCall, "approved_tool_ids")
            .ToHashSet(StringComparer.Ordinal);
        approved.ExceptWith(ctx.NonIndependent);
        approved.ExceptWith(retiredIdent);
        var sets = new Dictionary<string, HashSet<string>>(
            StringComparer.Ordinal)
        {
            ["registered"] = registered,
            ["manifest"] = manifestSet,
            ["approved"] = approved,
        };
        foreach (var (left, right) in new (string, string)[]
        {
            ("registered", "manifest"), ("manifest", "registered"),
            ("manifest", "approved"), ("approved", "manifest"),
        })
            foreach (var tid in sets[left].Except(sets[right])
                         .OrderBy(s => s, StringComparer.Ordinal))
                e.Fail($"tm-parity:{left}-not-{right}:{tid}",
                    $"{left} tool id not in {right} set: {tid}");
    }

    // ------------------------------------------------------------------
    //  Oracle-only remainder: source_ownership_errors rows.
    // ------------------------------------------------------------------

    private static void EmitSourceOwnership(Ctx ctx)
    {
        var e = ctx.E;
        var root = ctx.Root;
        var so = Module(root,
            "governance_rule/permission_directory/registries/" +
            "permissions/source_ownership.py");

        string SoStr(string name) =>
            so.TryGetValue(name, out var v) ? PyLit.AsStr(v) ?? "" : "";
        List<string> SoStrings(string name) =>
            so.TryGetValue(name, out var v) ? PyLit.Strings(v)
                : new List<string>();

        if (so.TryGetValue("REQUIRED_OWNED_SOURCES", out var owned)
            && owned is PyLit.Dict ownedDict)
            foreach (var (key, val) in ownedDict.Entries)
                foreach (var rel in PyLit.Strings(val)
                             .OrderBy(s => s, StringComparer.Ordinal))
                    e.Emit($"owned-source:{rel}", "file-exists", rel);

        var retired = new HashSet<string>(StringComparer.Ordinal);
        var retiredDoc = ReadJsonObject(Rel(root,
            "governance_rule/execution/audit/retired_sources.json"));
        if (retiredDoc?["paths"] is JsonArray paths)
            foreach (var p in paths)
                if (p?.GetValue<string>() is { } s)
                    retired.Add(s);
        var forbidden = SoStrings("FORBIDDEN_LEGACY_BUSINESS_SOURCES")
            .Union(retired).OrderBy(s => s, StringComparer.Ordinal);
        foreach (var rel in forbidden)
            e.Emit($"forbidden-source:{rel}", "file-not-exists", rel);

        e.Emit("owned-source:visual-smoke", "file-exists",
            "Standalone tools/ai-assistant/scripts/visual_smoke.py");
        e.NotContains("main-system:ipc-symbols",
            "main-system/src-core/ipc/server.py",
            new[]
            {
                "_investment_watch_result_log_payload",
                "_INVESTMENT_WATCH_LOG_",
            });

        var packages = new (string Root, string Layers)[]
        {
            ("AI_ASSISTANT_PACKAGE_ROOT", "AI_ASSISTANT_REQUIRED_LAYERS"),
            ("XINGCHENG_PACKAGE_ROOT", "XINGCHENG_REQUIRED_LAYERS"),
            ("AI_COLLABORATION_PACKAGE_ROOT",
                "AI_COLLABORATION_REQUIRED_LAYERS"),
            ("INVESTMENT_MOBILE_PACKAGE_ROOT",
                "INVESTMENT_MOBILE_REQUIRED_LAYERS"),
            ("FILE_SORTER_PACKAGE_ROOT", "FILE_SORTER_REQUIRED_LAYERS"),
            ("VAULTLY_PACKAGE_ROOT", "VAULTLY_REQUIRED_LAYERS"),
            ("STAR_CHAT_PACKAGE_ROOT", "STAR_CHAT_REQUIRED_LAYERS"),
        };
        foreach (var (rootName, layerName) in packages)
        {
            var pkg = SoStr(rootName);
            if (pkg.Length == 0) continue;
            foreach (var layer in SoStrings(layerName)
                         .OrderBy(s => s, StringComparer.Ordinal))
                e.Emit($"pkg-layer:{pkg}:{layer}", "file-exists",
                    $"{pkg}/{layer}/__init__.py");
            var pkgDir = Rel(root, pkg);
            if (Directory.Exists(pkgDir))
                foreach (var stray in Directory
                             .EnumerateFiles(pkgDir, "*.py",
                                 SearchOption.TopDirectoryOnly)
                             .OrderBy(f => f, StringComparer.Ordinal))
                    if (Path.GetFileName(stray) != "__init__.py")
                        e.Fail(
                            $"pkg-stray:{Path.GetRelativePath(root, stray)
                                .Replace('\\', '/')}",
                            "source outside owned layer");
        }

        var trees = new SortedSet<string>(StringComparer.Ordinal);
        foreach (var dir in Directory.EnumerateDirectories(root))
        {
            var src = Path.Combine(dir, "src");
            if (Directory.Exists(src))
                trees.Add(Path.GetRelativePath(root, src)
                    .Replace('\\', '/'));
        }
        var standaloneDir = Rel(root, "Standalone tools");
        if (Directory.Exists(standaloneDir))
            foreach (var dir in Directory
                         .EnumerateDirectories(standaloneDir))
            {
                var src = Path.Combine(dir, "src");
                if (Directory.Exists(src))
                    trees.Add(Path.GetRelativePath(root, src)
                        .Replace('\\', '/'));
            }
        var prefixes = PyLit.StrDict(
            so.TryGetValue("OWNED_IMPORT_PREFIXES", out var oip)
                ? oip : null);
        foreach (var (prefix, ownerRoot) in prefixes)
            foreach (var tree in trees)
                if (!tree.StartsWith(ownerRoot + "/",
                        StringComparison.Ordinal))
                    e.Checks.Add(new JsonObject
                    {
                        ["id"] = $"cross-import:{prefix}:{tree}",
                        ["kind"] = "tree-not-contains",
                        ["path"] = tree,
                        ["glob"] = "*.py",
                        ["markers"] = Emitter.Arr(new[]
                        {
                            $"import {prefix}", $"from {prefix}",
                        }),
                    });
        e.Checks.Add(new JsonObject
        {
            ["id"] = "shared-layer:forbidden-terms",
            ["kind"] = "tree-not-contains",
            ["path"] = "shared-layer/src",
            ["glob"] = "*.py",
            ["ignore_case"] = true,
            ["markers"] = Emitter.Arr(
                SoStrings("SHARED_LAYER_FORBIDDEN_TERMS")
                    .OrderBy(s => s, StringComparer.Ordinal)),
        });
        e.Checks.Add(new JsonObject
        {
            ["id"] = "main-system:forbidden-business",
            ["kind"] = "tree-not-contains",
            ["path"] = "main-system/src-core",
            ["glob"] = "*.py",
            ["ignore_case"] = true,
            ["markers"] = Emitter.Arr(
                SoStrings("MAIN_SYSTEM_FORBIDDEN_BUSINESS_TERMS")
                    .OrderBy(s => s, StringComparer.Ordinal)),
        });
        var aiRoot = SoStr("AI_ASSISTANT_PACKAGE_ROOT");
        var netMarkers = new List<string>();
        foreach (var v in new[] { "import", "from" })
            foreach (var m in new[]
                     { "aiohttp", "httpx", "requests", "smtplib" })
                netMarkers.Add($"{v} {m}");
        netMarkers.Add("urllib.request");
        netMarkers.Add("urlopen(");
        e.Checks.Add(new JsonObject
        {
            ["id"] = "ai-assistant:forbidden-network",
            ["kind"] = "tree-not-contains",
            ["path"] = aiRoot,
            ["glob"] = "*.py",
            ["markers"] = Emitter.Arr(netMarkers),
        });
        var sharedRoot = SoStr("SHARED_LAYER_ROOT");
        var sharedDir = Rel(root, sharedRoot);
        if (Directory.Exists(sharedDir))
        {
            var allowed = SoStrings("SHARED_LAYER_ALLOWED_SOURCES")
                .ToHashSet(StringComparer.Ordinal);
            var allowedPrefixes = SoStrings(
                "SHARED_LAYER_ALLOWED_PREFIXES");
            foreach (var sf in Directory
                         .EnumerateFiles(sharedDir, "*.py",
                             SearchOption.AllDirectories)
                         .OrderBy(f => f, StringComparer.Ordinal))
            {
                var srel = Path.GetRelativePath(sharedDir, sf)
                    .Replace('\\', '/');
                if (!allowed.Contains(srel)
                    && !allowedPrefixes.Any(p => srel.StartsWith(
                        p, StringComparison.Ordinal)))
                    e.Fail($"unowned-shared:{srel}",
                        "unowned shared-layer source");
            }
        }
    }

    // ------------------------------------------------------------------
    //  check_sql_anti_patterns scanner — textual port of
    //  audit_sql_patterns.collect_finding_keys.  Position-tolerant
    //  ``category|relpath`` keys; over-approximation surfaces as visible
    //  fail rows (fail-closed), never silent acceptance.
    // ------------------------------------------------------------------

    private static readonly string[] SqlScanRoots =
    {
        "shared-layer/src", "main-system/src-core",
        "main-system/governance", "governance_rule",
        "Standalone tools",
    };

    private static readonly HashSet<string> SqlSkipDirs =
        new(StringComparer.Ordinal)
    {
        "__pycache__", ".venv", "node_modules", "runtime", "build",
        "dist", ".git", "bin", "obj", ".worktrees", "csharp",
        "site-packages",
    };

    private static readonly string[] SqlExemptPathParts =
    {
        "/tests/", "/test_", "regression_benchmarks.py", "/benchmark",
        "/perf/", "performance/", "native_core_benchmark.py",
        "transformer_benchmark.py", "vector_benchmark.py",
        "parser_benchmark.py", "/e2e/",
    };

    private static readonly HashSet<string> SqlExemptFiles =
        new(StringComparer.Ordinal)
    {
        "analytics_schema.py", "analytics_store_schema.py",
        "collab_repo_schema.py", "local_command_parser.py",
        "runtime_queue.py", "migrations.py", "bootstrap.py",
        "provenance.py", "repair_learning.py", "roles.py",
        "session.py", "transport_notify.py", "maintenance_postgres.py",
        "domain.py", "codex_repository.py", "codex_postgresql.py",
        "codex_update_validation.py", "audit_directories.py",
        "successor_framework.py", "store_async.py",
        "data_layer_contract.py", "lineage.py", "_entity_history.py",
    };

    private static readonly Regex StatementNeutral = new(
        @"^\s*(pragma|set\b|listen|unlisten|notify|begin|commit|rollback|"
        + @"savepoint|release|create|alter|drop|analyze|vacuum|attach|"
        + @"detach|explain|truncate|grant|revoke|reindex|checkpoint|"
        + @"cluster)",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex SelectStar = new(
        @"\bselect\s+\*", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex OffsetRe = new(
        @"\boffset\b", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex ConnReceiver = new(
        @"conn|cur|cursor|connection|session|admin|db\b|store",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex SqlWords = new(
        @"\b(select|insert|update|delete|from|where)\b",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex ExecuteCall = new(
        @"(?:(?<recv>[A-Za-z_][A-Za-z0-9_]*)\.)?"
        + @"(?<fn>execute|executemany)\s*\(",
        RegexOptions.Compiled);
    private static readonly Regex ConnectCall = new(
        @"\b(?:connect|_connect|_get_conn|get_connection|connection)"
        + @"\s*\(", RegexOptions.Compiled);
    private static readonly Regex FStringRe = new(
        @"[fF][""'][^""']*\{(?!\{)", RegexOptions.Compiled);
    private static readonly Regex QuotedStr = new(
        "\"\"\"(?s:.)*?\"\"\"|'''(?s:.)*?'''"
        + "|\"([^\"\\\\]|\\\\.)*\"|'([^'\\\\]|\\\\.)*'",
        RegexOptions.Compiled);
    private static readonly HashSet<string> DmlVerbs =
        new(StringComparer.Ordinal)
    {
        "select", "insert", "update", "delete", "replace", "upsert",
    };
    private static readonly string[] DmlPrefixes =
    {
        "select", "insert", "update", "delete", "replace", "upsert",
        "insert into", "insert or",
    };

    private static List<string> CollectFindingKeys(string root)
    {
        var keys = new List<string>();
        foreach (var scanRoot in SqlScanRoots)
        {
            var baseDir = Rel(root, scanRoot);
            if (!Directory.Exists(baseDir)) continue;
            foreach (var path in Directory
                         .EnumerateFiles(baseDir, "*.py",
                             SearchOption.AllDirectories))
            {
                var parts = path.Split(Path.DirectorySeparatorChar);
                if (parts.Any(SqlSkipDirs.Contains)) continue;
                var rel = Path.GetRelativePath(root, path)
                    .Replace('\\', '/');
                foreach (var finding in ScanSqlFile(path, rel))
                {
                    var space = finding.IndexOf(' ');
                    keys.Add(
                        $"{(space > 0 ? finding[..space] : finding)}" +
                        $"|{rel}");
                }
            }
        }
        keys.Sort(StringComparer.Ordinal);
        return keys;
    }

    private static List<string> ScanSqlFile(string path, string rel)
    {
        var findings = new List<string>();
        string source;
        try { source = File.ReadAllText(path); }
        catch (Exception ex) when (ex is IOException
                                 or UnauthorizedAccessException)
        {
            return new List<string>
                { $"sql-scan unreadable {rel}: {ex.Message}" };
        }
        if (!source.Contains("execute", StringComparison.Ordinal)
            && !source.Contains("connect", StringComparison.Ordinal))
            return findings;
        var lines = source.Replace("\r\n", "\n").Split('\n');

        var constants = new Dictionary<string, string>(
            StringComparer.Ordinal);
        foreach (var raw in lines)
        {
            var m = Regex.Match(raw,
                @"^\s*([A-Z_][A-Za-z0-9_]*)\s*=\s*"
                + @"(?:""([^""\\]*(?:\\.[^""\\]*)*)""|"
                + @"'([^'\\]*(?:\\.[^'\\]*)*)')");
            if (m.Success)
                constants[m.Groups[1].Value] =
                    m.Groups[2].Success ? m.Groups[2].Value
                                        : m.Groups[3].Value;
        }

        var exemptFile =
            SqlExemptPathParts.Any(p =>
                ("/" + rel).Contains(p, StringComparison.Ordinal))
            || SqlExemptFiles.Contains(Path.GetFileName(path));

        // Reconstruct physical line numbers for merged logical lines;
        // Pieces tracks (physicalLine, offsetInText) per appended
        // source line so findings can report the *call* line — Python
        // uses the call node's lineno, not the span's first line.
        var logicalSpans = new List<(int Line, string Text, int Depth,
            List<(int Line, int Off)> Pieces)>();
        {
            var phys = 0;
            var buf = new System.Text.StringBuilder();
            var pieces = new List<(int Line, int Off)>();
            var bracket = 0;
            var startLine = 0;
            var firstIndent = 0;
            foreach (var raw in lines)
            {
                phys++;
                var t = raw.Trim();
                if (buf.Length == 0 && t.Length == 0) continue;
                if (buf.Length == 0)
                {
                    startLine = phys;
                    var fi = 0;
                    while (fi < raw.Length && raw[fi] == ' ') fi++;
                    firstIndent = fi / 4;
                }
                pieces.Add((phys, buf.Length));
                buf.Append(t).Append(' ');
                var plain = StripStrings(raw);
                bracket += plain.Count(c => c is '(' or '[' or '{')
                           - plain.Count(c => c is ')' or ']' or '}');
                if (bracket <= 0)
                {
                    logicalSpans.Add((startLine, buf.ToString(),
                        firstIndent, pieces));
                    buf.Clear();
                    pieces = new List<(int, int)>();
                    bracket = 0;
                }
            }
        }

        bool Suppressed(int lineno) =>
            lineno > 0 && lineno <= lines.Length
            && lines[lineno - 1].Contains("# sql-ok",
                StringComparison.Ordinal);

        // inside-loop tracking via for-header depth stack
        var forDepths = new Stack<int>();
        for (var i = 0; i < logicalSpans.Count; i++)
        {
            var (lineno, text, depth, pieces) = logicalSpans[i];
            // physical line holding the text offset (call-site parity)
            int CallLine(int idx)
            {
                var line = lineno;
                foreach (var (pl, off) in pieces)
                {
                    if (off > idx) break;
                    line = pl;
                }
                return line;
            }
            var trimmed = text.Trim();
            var isFor = Regex.IsMatch(trimmed,
                @"^(async\s+)?for\b.*:$");
            if (isFor)
            {
                while (forDepths.Count > 0 && depth <= forDepths.Peek())
                    forDepths.Pop();
                forDepths.Push(depth);
                continue;
            }
            var isClause = Regex.IsMatch(trimmed,
                @"^(else|elif\b.*|except\b.*|finally)\s*:");
            if (!(isClause && forDepths.Count > 0
                  && depth == forDepths.Peek()))
                while (forDepths.Count > 0 && depth <= forDepths.Peek())
                    forDepths.Pop();
            var insideLoop = forDepths.Count > 0;

            // with ...connect() inside a loop
            if (insideLoop
                && Regex.IsMatch(trimmed, @"^(async\s+)?with\b")
                && ConnectCall.IsMatch(trimmed)
                && !Suppressed(lineno) && !exemptFile)
                findings.Add(
                    $"sql-conn-loop {rel}:{lineno} " +
                    "connect() inside for-loop " +
                    "(per-row commit/persist)");

            foreach (Match call in ExecuteCall.Matches(text))
            {
                var recv = call.Groups["recv"].Value;
                if (call.Groups["recv"].Success
                    && !ConnReceiver.IsMatch(recv))
                    continue;
                var callLine = CallLine(call.Index);
                var argsText = CallArgs(text,
                    call.Index + call.Length - 1);

                // f-string interpolation — Python parity: only args
                // that *are* JoinedStr (a bare f-string literal) are
                // checked; f-strings nested in tuples/calls do not
                // count. Each interpolated arg whose joined constant
                // fragments are not statement-neutral is flagged once.
                foreach (var rawArg in TopLevelArgs(argsText))
                {
                    var arg = rawArg.Trim();
                    if (arg.Length < 2 || arg[0] is not ('f' or 'F')
                        || arg[1] is not ('"' or '\''))
                        continue;
                    if (!Regex.IsMatch(arg, @"\{(?!\{)")) continue;
                    var constText = FStringConstant(arg, 0);
                    if (constText is null
                        || StatementNeutral.IsMatch(constText))
                        continue;
                    if (!Suppressed(callLine) && !exemptFile)
                        findings.Add(
                            $"sql-fstring {rel}:{callLine} " +
                            "execute() argument interpolates variables");
                }

                // literal SQL strings in args — Constant parity:
                // strings carrying an f/F prefix are JoinedStr
                // internals and are not counted by _sql_strings.
                foreach (Match sq in QuotedStr.Matches(argsText))
                {
                    if (sq.Index > 0
                        && argsText[sq.Index - 1] is 'f' or 'F')
                        continue;
                    var body = sq.Value.StartsWith("\"\"\"",
                            StringComparison.Ordinal)
                        || sq.Value.StartsWith("'''",
                            StringComparison.Ordinal)
                        ? sq.Value[3..^3]
                        : sq.Value[1..^1];
                    if (!SqlWords.IsMatch(body)) continue;
                    if (SelectStar.IsMatch(body)
                        && !Suppressed(callLine) && !exemptFile)
                        findings.Add(
                            $"sql-select-star {rel}:{callLine} " +
                            "SELECT * read");
                    if (OffsetRe.IsMatch(body) && !Suppressed(callLine))
                        findings.Add(
                            $"sql-offset {rel}:{callLine} " +
                            "OFFSET pagination");
                }

                // DML inside a for loop — first *positional* arg only;
                // a leading ``name=`` arg means no positional arg
                // exists (Python: node.args empty → check skipped).
                var firstArg = TopLevelArgs(argsText)[0].Trim();
                if (insideLoop && !exemptFile && !Suppressed(callLine)
                    && !Regex.IsMatch(firstArg, @"^[A-Za-z_]\w*\s*="))
                {
                    string? sqlText = null;
                    var qm = QuotedStr.Match(firstArg);
                    if (qm.Success && qm.Index == 0)
                        sqlText = qm.Value.StartsWith("\"\"\"",
                                StringComparison.Ordinal)
                            || qm.Value.StartsWith("'''",
                                StringComparison.Ordinal)
                            ? qm.Value[3..^3]
                            : qm.Value[1..^1];
                    else
                    {
                        var lookup = firstArg.Contains('.')
                            ? firstArg.Split('.').Last()
                            : firstArg;
                        if (constants.TryGetValue(lookup, out var cv))
                            sqlText = cv;
                    }
                    var isDml = sqlText is null
                        || (!StatementNeutral.IsMatch(sqlText)
                            && IsDml(sqlText));
                    if (isDml)
                        findings.Add(
                            $"sql-loop-exec {rel}:{callLine} " +
                            "execute() inside for-loop " +
                            "(N+1 candidate)");
                }
            }
        }
        return findings;
    }

    /// <summary>Split a call-argument span on depth-0 commas
    /// (paren/bracket/brace/quote aware).</summary>
    private static List<string> TopLevelArgs(string argsText)
    {
        var args = new List<string>();
        var depth = 0;
        var start = 0;
        char q = '\0';
        for (var i = 0; i < argsText.Length; i++)
        {
            var c = argsText[i];
            if (q != '\0')
            {
                if (c == '\\') { i++; continue; }
                if (c == q) q = '\0';
                continue;
            }
            if (c is '"' or '\'') { q = c; continue; }
            if (c is '(' or '[' or '{') depth++;
            else if (c is ')' or ']' or '}') depth--;
            else if (c == ',' && depth <= 0)
            {
                args.Add(argsText[start..i]);
                start = i + 1;
            }
        }
        args.Add(argsText[start..]);
        return args;
    }

    /// <summary>Join the constant fragments of the f-string literal
    /// starting at <paramref name="start"/> — Python parity for
    /// ``"".join(v.value for v in arg.values if Constant)``.</summary>
    private static string? FStringConstant(string text, int start)
    {
        var i = start;
        if (i >= text.Length || text[i] is not ('f' or 'F')) return null;
        i++;
        if (i >= text.Length || text[i] is not ('"' or '\''))
            return null;
        var quote = text[i++];
        var sb = new System.Text.StringBuilder();
        while (i < text.Length)
        {
            var c = text[i];
            if (c == '\\' && i + 1 < text.Length)
            { sb.Append(text[i + 1]); i += 2; continue; }
            if (c == quote) return sb.ToString();
            if (c == '{')
            {
                if (i + 1 < text.Length && text[i + 1] == '{')
                { sb.Append('{'); i += 2; continue; }
                var depth = 1;
                i++;
                while (i < text.Length && depth > 0)
                {
                    if (text[i] == '{') depth++;
                    else if (text[i] == '}') depth--;
                    i++;
                }
                continue;
            }
            if (c == '}')
            {
                if (i + 1 < text.Length && text[i + 1] == '}')
                { sb.Append('}'); i += 2; continue; }
                i++; continue;
            }
            sb.Append(c);
            i++;
        }
        return null;
    }

    private static bool IsDml(string sql)
    {
        var head = sql.TrimStart().ToLowerInvariant();
        var first = head.Split(new[] { ' ', '(' }, 2)[0];
        return DmlVerbs.Contains(first)
               || DmlPrefixes.Any(p => head.StartsWith(
                   p, StringComparison.Ordinal));
    }

    private static string StripStrings(string line)
    {
        var sb = new System.Text.StringBuilder(line.Length);
        var inStr = false; var ch = '\0'; var triple = false;
        for (var i = 0; i < line.Length; i++)
        {
            var c = line[i];
            if (inStr)
            {
                if (triple)
                {
                    if (c == ch && i + 2 < line.Length
                        && line[i + 1] == ch && line[i + 2] == ch)
                    { inStr = false; i += 2; }
                }
                else if (c == ch && line[i - 1] != '\\') inStr = false;
                continue;
            }
            if (c is '"' or '\'')
            {
                inStr = true; ch = c;
                triple = i + 2 < line.Length
                    && line[i + 1] == c && line[i + 2] == c;
                if (triple) i += 2;
                continue;
            }
            if (c == '#') break;
            sb.Append(c);
        }
        return sb.ToString();
    }

    /// <summary>Args text of the call whose ``(`` is at index.</summary>
    private static string CallArgs(string text, int openParen)
    {
        var depth = 0;
        for (var i = openParen; i < text.Length; i++)
        {
            var c = text[i];
            if (c == '(') depth++;
            else if (c == ')')
            {
                depth--;
                if (depth == 0)
                    return text[(openParen + 1)..i];
            }
        }
        return text[(openParen + 1)..];
    }
}
