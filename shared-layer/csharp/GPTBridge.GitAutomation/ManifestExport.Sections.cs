using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.GitAutomation;

internal static partial class ManifestExport
{
    private static readonly string[] Contracts =
    {
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
    };

    // -- snapshot loaders -------------------------------------------------

    private static void LoadSnapshots(Ctx ctx)
    {
        var root = ctx.Root;
        ctx.Identities.Clear();
        ctx.Bindings.Clear();
        ctx.CapabilityNames.Clear();
        ctx.Policy = Module(root, "governance_rule/governance_policy.py");
        ctx.PolicyCall =
            ctx.Policy.TryGetValue("GOVERNANCE_POLICY", out var gp)
                ? PyLit.AsCall(gp) : null;
        ctx.Directory = Module(root,
            "governance_rule/permission_directory/" +
            "directory_authority.py");
        ctx.CodeRules = Module(root,
            "governance_rule/code_rule_directory.py");
        ctx.CodeRulesCall =
            ctx.CodeRules.TryGetValue("CODE_RULE_DIRECTORY", out var cr)
                ? PyLit.AsCall(cr) : null;

        var identityModule = Module(root,
            "governance_rule/permission_directory/registries/" +
            "permissions/identity_groups.py");
        foreach (var (name, value) in identityModule)
        {
            if (value is not PyLit.Call call) continue;
            if (call.Func == "CapabilityIdentity")
            {
                ctx.Identities.Add(new Identity(
                    PyLit.KwStr(call, "actor") ?? "",
                    PyLit.KwStr(call, "identity_code") ?? "",
                    PyLit.KwStr(call, "bound_tool_id") ?? "",
                    PyLit.KwStr(call, "lifecycle") ?? "active"));
            }
            else if (call.Func == "_business_tool_identity"
                     && call.Args.Count > 0
                     && call.Args[0] is PyLit.Str toolId)
            {
                ctx.Identities.Add(new Identity(
                    $"governance/tool/{toolId.Text}",
                    PyLit.KwStr(call, "identity_code") ?? "",
                    toolId.Text, "active"));
            }
        }

        var bindingsModule = Module(root,
            "governance_rule/permission_directory/registries/" +
            "permissions/identity_permissions.py");
        if (bindingsModule.TryGetValue("IDENTITY_PERMISSION_BINDINGS",
                out var bindings)
            && bindings is PyLit.Seq bindingSeq)
        {
            foreach (var item in bindingSeq.Items)
            {
                if (item is not PyLit.Call binding) continue;
                ctx.Bindings.Add((
                    PyLit.KwStr(binding, "actor") ?? "",
                    PyLit.KwStrings(binding, "capabilities")));
            }
        }

        var capModule = Module(root,
            "governance_rule/permission_directory/registries/" +
            "permissions/capability_boundaries.py");
        if (capModule.TryGetValue("CAPABILITY_AUTHORITIES", out var caps)
            && caps is PyLit.Seq capSeq)
        {
            foreach (var item in capSeq.Items)
                if (item is PyLit.Call authority
                    && authority.Args.Count > 0
                    && authority.Args[0] is PyLit.Str capability)
                    ctx.CapabilityNames.Add(capability.Text);
        }

        if (Environment.GetEnvironmentVariable(
                "GPTBRIDGE_MANIFEST_DEBUG") == "1")
            Console.Error.WriteLine(
                $"[manifest-debug] policy={(ctx.PolicyCall is null
                    ? "null" : ctx.PolicyCall.Func)} " +
                $"coderules={(ctx.CodeRulesCall is null
                    ? "null" : ctx.CodeRulesCall.Func)} " +
                $"identities={ctx.Identities.Count} " +
                $"bindings={ctx.Bindings.Count} " +
                $"caps={ctx.CapabilityNames.Count} " +
                $"bindingKeys={(bindingsModule.ContainsKey(
                    "IDENTITY_PERMISSION_BINDINGS"))} " +
                $"bindingType={bindingsModule.GetValueOrDefault(
                    "IDENTITY_PERMISSION_BINDINGS")?.GetType().Name} " +
                $"capKeys={(capModule.ContainsKey(
                    "CAPABILITY_AUTHORITIES"))} " +
                $"capType={capModule.GetValueOrDefault(
                    "CAPABILITY_AUTHORITIES")?.GetType().Name} " +
                $"capModuleKeys={capModule.Count}");
    }

    private static List<string> ProtectedSources(Ctx ctx)
    {
        var seen = new List<string>();
        void Add(IEnumerable<string> items)
        {
            foreach (var item in items)
                if (!seen.Contains(item)) seen.Add(item);
        }
        Add(ctx.PolicyStrings("authority_files"));
        var directory = ctx.Directory;
        if (directory is not null
            && directory.TryGetValue(
                "MANAGED_READ_ONLY_REGISTRY_PATHS", out var managed))
            Add(PyLit.Strings(managed));
        return seen;
    }

    private static void EmitForbiddenAndProtected(Ctx ctx)
    {
        foreach (var relative in ModuleStrings(ctx.Root,
            "governance_rule/execution/audit/audit_protected.py",
            "FORBIDDEN_LEGACY_SOURCES"))
            ctx.E.Emit($"forbidden-legacy:{relative}",
                "file-not-exists", relative);
        foreach (var relative in ProtectedSources(ctx))
        {
            ctx.E.Emit($"protected-source:{relative}",
                "file-exists", relative);
            if (relative.StartsWith(
                    "governance_rule/codex/governance_codex.zh-TW.part-",
                    StringComparison.Ordinal))
                ctx.E.Emit($"protected-source-readonly:{relative}",
                    "file-readonly", relative);
        }
    }

    private static void EmitPollutionAndContracts(Ctx ctx)
    {
        var codexDir = Rel(ctx.Root, "governance_rule/codex");
        if (Directory.Exists(codexDir))
        {
            foreach (var path in Directory.EnumerateFiles(
                         codexDir, "architecture-*.md")
                         .OrderBy(p => p, StringComparer.Ordinal))
                ctx.E.Emit(
                    $"architecture-pollution:{Path.GetFileName(path)}",
                    "text-no-pollution",
                    Path.GetRelativePath(ctx.Root, path)
                        .Replace('\\', '/'));
            foreach (var path in Directory.EnumerateFiles(
                         codexDir, "*.zh-TW.part-*.txt")
                         .OrderBy(p => p, StringComparer.Ordinal))
                ctx.E.Emit(
                    $"mirror-part-pollution:{Path.GetFileName(path)}",
                    "text-no-pollution",
                    Path.GetRelativePath(ctx.Root, path)
                        .Replace('\\', '/'));
        }
        foreach (var relative in Contracts)
            ctx.E.Emit($"contract-parse:{relative}", "json-parses",
                relative);
    }

    // -- static section (build_manifest lines ~549-909) --------------------

}
