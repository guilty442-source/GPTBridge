using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.GitAutomation;

internal static partial class ManifestExport
{
    private static void EmitMirrorAndPolicy(Ctx ctx)
    {
        LoadSnapshots(ctx);
        LoadMirrorTables(ctx);
        var e = ctx.E;
        var root = ctx.Root;

        // --- check_contract_axes (G48) ----------------------------------
        foreach (var axis in new[]
        {
            "ai-connection", "backend-lifecycle", "data-architecture",
            "ipc", "sql-schema", "tool-runtime",
        })
        {
            var relAxis = $"main-system/config/{axis}-contract.json";
            e.Emit($"contract-axis:{axis}:exists", "file-exists", relAxis);
            e.Emit($"contract-axis:{axis}:parse", "json-parses", relAxis);
            var axisDoc = ReadJsonObject(Rel(root, relAxis))
                          ?? new JsonObject();
            var ver = axisDoc["contract_version"];
            var min = axisDoc["minimum_supported_contract_version"];
            if (ver is JsonValue v && v.TryGetValue<int>(out var vi))
            {
                var bound = min is JsonValue mv
                    && mv.TryGetValue<int>(out var mi) && mi >= 1
                    ? mi : 1;
                e.Checks.Add(new JsonObject
                {
                    ["id"] = $"contract-axis:{axis}:version",
                    ["kind"] = "json-key-value",
                    ["path"] = relAxis,
                    ["markers"] = Emitter.Arr(
                        new[] { $"contract_version>={bound}" }),
                });
            }
            else
            {
                e.Checks.Add(new JsonObject
                {
                    ["id"] = $"contract-axis:{axis}:schema",
                    ["kind"] = "json-has-keys",
                    ["path"] = relAxis,
                    ["markers"] = Emitter.Arr(new[] { "schema" }),
                });
            }
            if (min is JsonValue m2 && m2.TryGetValue<int>(out _))
                e.Checks.Add(new JsonObject
                {
                    ["id"] = $"contract-axis:{axis}:minimum",
                    ["kind"] = "json-key-value",
                    ["path"] = relAxis,
                    ["markers"] = Emitter.Arr(new[]
                    {
                        "minimum_supported_contract_version>=0",
                    }),
                });
        }

        // --- check_activation_states ------------------------------------
        var archRoots = TableRows(ctx, "project_architecture_directory")
            .Where(r => r["architecture_code"] is not null)
            .ToDictionary(
                r => r["architecture_code"]!.GetValue<string>(),
                r => r["physical_root"]?.GetValue<string>() ?? "",
                StringComparer.Ordinal);
        var retiredSovereigns = TableRows(ctx, "sovereigns")
            .Where(r => (r["rank"]?.GetValue<string>() ?? "")
                .Contains("retired"))
            .Select(r => (r["sovereign_id"] ?? r["id"])
                ?.GetValue<string>() ?? "")
            .Where(s => s.Length > 0)
            .OrderBy(s => s, StringComparer.Ordinal).ToList();
        if (!ctx.MirrorTables.ContainsKey(
                "architecture_activation_states"))
        {
            TableAbsent(ctx, "architecture_activation_states");
        }
        else
        {
            var actPart = ctx.MirrorTables[
                "architecture_activation_states"].File;
            foreach (var sid in retiredSovereigns)
                e.Checks.Add(new JsonObject
                {
                    ["id"] = $"activation:retired-owner:{sid}",
                    ["kind"] = "file-not-contains",
                    ["path"] = actPart,
                    ["markers"] = Emitter.Arr(
                        new[] { $"\"verification_owner\":\"{sid}\"" }),
                });
            foreach (var row in TableRows(ctx,
                     "architecture_activation_states"))
            {
                var code = row["architecture_code"]
                    ?.GetValue<string>() ?? "?";
                var owner = row["verification_owner"]
                    ?.GetValue<string>() ?? "";
                if (owner.Length > 0)
                    TableAssert(ctx, $"activation:owner:{code}",
                        "architecture_activation_states",
                        new[] { $"architecture_code={code}" }
                            .Concat(retiredSovereigns.Select(
                                s => $"verification_owner!={s}")));
                var target = row["target_root"]?.GetValue<string>() ?? "";
                var state = row["current_state"]?.GetValue<string>();
                if (target.Length > 0
                    && state is "active" or "mandated")
                {
                    var resolved = target.StartsWith("ARCH_CODE:",
                        StringComparison.Ordinal)
                        ? archRoots.GetValueOrDefault(
                            target["ARCH_CODE:".Length..], "")
                        : target;
                    if (resolved.Length == 0)
                        e.Fail($"activation:unresolved-target:{code}",
                            "activation target_root code unregistered: " +
                            $"{code}: {target}");
                    else
                        e.Emit($"activation:target:{code}", "dir-exists",
                            Relativize(root, resolved));
                }
                var legacy = row["legacy_root"]?.GetValue<string>() ?? "";
                if (legacy.Length > 0
                    && row["old_root_deletion_state"]
                        ?.GetValue<string>() == "not-applicable"
                    && !legacy.StartsWith("ARCH_LEGACY_CODE:",
                        StringComparison.Ordinal))
                    e.Emit($"activation:legacy-gone:{code}",
                        "file-not-exists",
                        Relativize(root, legacy));
            }
        }

        // --- check_formal_rules ------------------------------------------
        TablePresent(ctx, "formal_rule_registry");
        const string evaluatorsSrc =
            "governance_rule/execution/formal_rules/evaluators.py";
        foreach (var frule in TableRows(ctx, "formal_rule_registry"))
        {
            var code = frule["rule_code"]?.GetValue<string>() ?? "";
            if (code.Length == 0) continue;
            var status = frule["status"]?.GetValue<string>() ?? "";
            if (status is "withdrawn" or "retired") continue;
            e.Contains($"formal-rule:evaluator:{code}", evaluatorsSrc,
                new[] { $"\"{code}\"" });
            var pid = frule["controlling_provision_id"]
                ?.GetValue<string>() ?? "";
            TableAssert(ctx, $"formal-rule:row:{code}",
                "formal_rule_registry",
                new[]
                {
                    $"rule_code={code}",
                    $"controlling_provision_id={pid}",
                    $"status={status}",
                });
            if (status == "active")
                TableAssert(ctx, $"formal-rule:parity:{code}",
                    "formal_rule_registry",
                    new[]
                    {
                        $"rule_code={code}",
                        "parity_status=VERIFIED",
                    });
        }

        // --- check_implementation_obligations (A292) ---------------------
        TablePresent(ctx, "implementation_obligations");
        foreach (var oblig in TableRows(ctx, "implementation_obligations"))
        {
            var ocode = oblig["obligation_code"]
                ?.GetValue<string>() ?? "";
            if (ocode.Length == 0) continue;
            var markers = new List<string>
                { $"obligation_code={ocode}" };
            markers.AddRange(Bind("implementation_owner",
                oblig["implementation_owner"]));
            markers.AddRange(Bind("target_state",
                oblig["target_state"]));
            markers.AddRange(Bind("acceptance_evidence",
                oblig["acceptance_evidence"]));
            markers.AddRange(Bind("current_state",
                oblig["current_state"]));
            TableAssert(ctx, $"obligation:row:{ocode}",
                "implementation_obligations", markers);
        }
        foreach (var rcode in new[]
        {
            "OBL_LAYERED_ARCHITECTURE", "OBL_ARCHITECTURE_CATALOG",
            "OBL_TEST_SUITE_DIRECTORY", "OBL_TOP_LEVEL_PATHS",
            "OBL_ANTI_JAILBREAK", "OBL_SYSTEM_RELIABILITY",
            "OBL_星澄_AUDIT", "OBL_CHANNEL_ANOMALY_ISOLATION",
            "OBL_NATIVE_PROMOTION_RECORD_CHECKER",
            "OBL_LANGUAGE_DEPENDENCY_DAG_GATE",
            "OBL_DIRECTORY_GOVERNANCE_DATA_CLOSURE",
            "OBL_FORMAL_EVALUATOR_V2_PARITY",
        })
            TableAssert(ctx, $"obligation:required:{rcode}",
                "implementation_obligations",
                new[] { $"obligation_code={rcode}" });

        // --- authority policy family -------------------------------------
        var policySrc = "governance_rule/governance_policy.py";
        var dirSrc =
            "governance_rule/permission_directory/directory_authority.py";
        var codeSrc = "governance_rule/code_rule_directory.py";

        e.Contains("authority-policy:policy", policySrc, new[]
        {
            $"authority=\"{ctx.PolicyStr("authority")}\"",
            $"top_level_rule=\"{ctx.PolicyStr("top_level_rule")}\"",
            $"governance_rule_count=" +
            $"{(ctx.PolicyCall?.Kw.FirstOrDefault(k => k.Name ==
                "governance_rule_count").Val is PyLit.Num n
                ? (int)n.N : 1)}",
            "governance_rule_partitioning=False",
            "subordinate_governance_rule_definition=False",
            $"permission_hierarchy_role=" +
            $"\"{ctx.PolicyStr("permission_hierarchy_role")}\"",
        });
        e.Contains("authority-policy:directory", dirSrc, new[]
        {
            "permission_hierarchy_role=" +
            "\"subordinate-read-only-permission-directory\"",
            $"current_version=" +
            $"{(ctx.PolicyCall?.Kw.FirstOrDefault(k => k.Name ==
                "authority_version").Val is PyLit.Num av
                ? (int)av.N : 1)}",
        });
        e.Contains("authority-policy:code-rules", codeSrc, new[]
        {
            "\"codex-v1.32010-is-sole-rule-source\"",
            $"governing_source=" +
            $"\"{ctx.PolicyStrings("governance_rule_sources")
                .FirstOrDefault() ?? ""}\"",
            "independent_authority=False",
            "runtime_write_allowed=False",
            $"canonical_project_root=" +
            $"\"{KwStr(ctx.PolicyKw("code_architecture"),
                "all_source_code_root") ?? "E:/GPTBridge"}\"",
        });
        var resp = ctx.PolicyKw("system_responsibilities");
        e.Contains("authority-policy:responsibilities", policySrc, new[]
        {
            $"git=\"{KwStr(resp, "git")}\"",
            $"sql=\"{KwStr(resp, "sql")}\"",
            $"vector_rag=\"{KwStr(resp, "vector_rag")}\"",
            $"local_vector_fallback=" +
            $"\"{KwStr(resp, "local_vector_fallback")}\"",
            $"llm=\"{KwStr(resp, "llm")}\"",
            $"separation=\"{KwStr(resp, "separation")}\"",
            $"governed_flow=\"{KwStr(resp, "governed_flow")}\"",
            $"management_owner=\"{KwStr(resp, "management_owner")}\"",
            "llm_inference_as_source_of_truth=False",
        });

        var directoryShared = ctx.Directory is not null
            && ctx.Directory.TryGetValue(
                "SHARED_LAYER_ACCESS_POLICY", out var sharedVal)
                ? PyLit.AsCall(sharedVal) : null;
        var activation = ctx.PolicyKw("activation");
        e.Contains("shared-layer-policy:directory", dirSrc, new[]
        {
            $"module_root=\"{KwStr(directoryShared, "module_root")}\"",
            "jurisdiction=\"governance-policy-only\"",
            "main_system_module_member=False",
            "token_required=True", "database_only=True",
            "source_write=False", "direct_data_write=False",
            "executable_content=False",
            "direct_process_instruction=False",
            "unchanneled_instruction=\"PERMISSION_DENIED\"",
            $"database_path=\"{KwStr(directoryShared, "database_path")}\"",
            $"ai_database_path=" +
            $"\"{KwStr(directoryShared, "ai_database_path")}\"",
        });
        e.Contains("shared-layer-policy:activation", policySrc, new[]
        {
            "default_active=True",
            $"activation_order=\"{KwStr(activation, "activation_order")}\"",
            "direct_load_required=True",
            "independent_tool_packaging_exception=" +
            $"\"{KwStr(activation,
                "independent_tool_packaging_exception")}\"",
            "packaged_executable_allowed=False",
            $"execution_access=" +
            $"\"{KwStr(activation, "execution_access")}\"",
            "encapsulation_allowed=False",
            "optional=False", "stop_permission=False",
            "disable_permission=False", "unload_permission=False",
            $"lifetime=\"{KwStr(activation, "lifetime")}\"",
            $"main_system_start_failure=" +
            $"\"{KwStr(activation, "main_system_start_failure")}\"",
            "main_system_repair_authority=\"governance-policy-only\"",
            $"main_system_repair_scope=" +
            $"\"{KwStr(activation, "main_system_repair_scope")}\"",
            "repair_completion_gate=" +
            "\"governance-reverify-before-normal-mode\"",
        });
        e.Contains("shared-layer-policy:directory-lifecycle", dirSrc, new[]
        {
            "governance_default_active=True",
            $"governance_activation_order=" +
            $"\"{KwStr(activation, "activation_order")}\"",
            "governance_direct_load=True",
            "governance_packaged_executable_allowed=False",
            $"governance_execution_access=" +
            $"\"{KwStr(activation, "execution_access")}\"",
            "governance_encapsulation_allowed=False",
            "governance_stop_permission=False",
            "governance_disable_permission=False",
            "governance_unload_permission=False",
        });
        e.Contains("shared-layer-policy:labels", policySrc, new[]
        {
            "aliases_allowed=False", "category_labels_allowed=False",
        });
        e.NotContains("shared-layer-policy:no-categories", codeSrc,
            new[] { "category_labels=True" });

        var repair = ctx.PolicyKw("automatic_repair");
        e.Contains("repair-policy:policy", policySrc, new[]
        {
            "backup_assistance_allowed=True",
            $"backup_owner=\"{KwStr(repair, "backup_owner")}\"",
            "direct_backup_access=False",
            $"backup_request_channel=" +
            $"\"{KwStr(repair, "backup_request_channel")}\"",
            "authorization_per_step=True",
            "authority_restore_from_backup=False",
        });
        e.Contains("repair-policy:boundaries",
            "governance_rule/permission_directory/registries/" +
            "permissions/capability_boundaries.py",
            new[] { "automatic-repair" });

        // --- identity / permission family --------------------------------
        var ipSrc = "governance_rule/permission_directory/registries/" +
                    "permissions/identity_permissions.py";
        var cbSrc = "governance_rule/permission_directory/registries/" +
                    "permissions/capability_boundaries.py";
        var registryDir = Rel(root,
            "governance_rule/permission_directory/registries");
        var registryFiles = Directory.Exists(registryDir)
            ? Directory.EnumerateFiles(registryDir, "*.py",
                  SearchOption.AllDirectories)
                .Where(p => !p.Split(Path.DirectorySeparatorChar)
                    .Contains("__pycache__"))
                .OrderBy(p => p, StringComparer.Ordinal).ToList()
            : new List<string>();

        void LiteralAssert(string id, string literal)
        {
            string? found = null;
            foreach (var reg in registryFiles)
            {
                if (ReadText(reg).Contains(literal,
                        StringComparison.Ordinal))
                {
                    found = Path.GetRelativePath(root, reg)
                        .Replace('\\', '/');
                    break;
                }
            }
            if (found is null)
                e.Fail(id, $"registry literal absent: {literal}");
            else
                e.Contains(id, found, new[] { literal });
        }

        foreach (var ident in ctx.Identities)
        {
            LiteralAssert($"identity:actor:{ident.Actor}",
                $"actor=\"{ident.Actor}\"");
            LiteralAssert($"identity:code:{ident.Actor}",
                $"identity_code=\"{ident.Code}\"");
            if (ident.BoundToolId.Length > 0)
                LiteralAssert($"identity:tool:{ident.Actor}",
                    $"\"{ident.BoundToolId}\"");
            if (ident.Lifecycle != "active")
                LiteralAssert($"identity:lifecycle:{ident.Actor}",
                    $"lifecycle=\"{ident.Lifecycle}\"");
        }
        foreach (var (actor, caps) in ctx.Bindings)
        {
            LiteralAssert($"binding:actor:{actor}",
                $"actor=\"{actor}\"");
            foreach (var cap in caps)
                LiteralAssert($"binding:cap:{actor}:{cap}", $"\"{cap}\"");
        }
        foreach (var capability in ctx.CapabilityNames)
            e.Contains($"capability:{capability}", cbSrc,
                new[] { $"\"{capability}\"" });
        e.NotContains("identity:no-arbitrary-storage", ipSrc,
            new[] { "\"shared-layer-read-write\"" });
        e.NotContains("capability:no-arbitrary-storage", cbSrc,
            new[] { "\"shared-layer-read-write\"" });
        foreach (var approved in
                 KwStrings(ctx.CodeRulesCall, "approved_actor_names"))
            e.Contains($"approved-actor:{approved}", codeSrc,
                new[] { $"\"{approved}\"" });
        foreach (var approvedTool in
                 KwStrings(ctx.CodeRulesCall, "approved_tool_ids"))
            e.Contains($"approved-tool:{approvedTool}", codeSrc,
                new[] { $"\"{approvedTool}\"" });

        var nonIndependent = new HashSet<string>(StringComparer.Ordinal)
        {
            "governance_rule", "shared-layer", "star-chat",
            "xingcheng-assistant",
        };
        var retiredIds = ctx.Identities
            .Where(i => i.Lifecycle == "retired")
            .Select(i => i.BoundToolId)
            .ToHashSet(StringComparer.Ordinal);
        foreach (var ident in ctx.Identities)
        {
            var tid = ident.BoundToolId;
            if (tid == "main-system" || nonIndependent.Contains(tid)
                || retiredIds.Contains(tid))
                continue;
            LiteralAssert($"tool-identity:{tid}",
                $"actor=\"governance/tool/{tid}\"");
            e.Contains($"tool-approved:{tid}", codeSrc,
                new[] { $"\"{tid}\"" });
        }
        ctx.RetiredIds = retiredIds;
        ctx.NonIndependent = nonIndependent;
    }
}
