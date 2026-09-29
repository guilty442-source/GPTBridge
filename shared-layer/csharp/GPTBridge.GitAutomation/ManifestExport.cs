using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.GitAutomation;

/// <summary>
/// C# port of governance_rule.execution.audit.export_audit_manifest
/// (``star-audit-manifest/v1``).  Rebuilds the manifest the native audit
/// engine consumes — same check ids, kinds and ordering semantics as the
/// retired Python exporter.  Unevaluable inputs emit ``fail`` rows
/// rather than silently dropping coverage.
/// </summary>
internal static partial class ManifestExport
{
    private const string ManifestRelative =
        "governance_rule/execution/audit/audit_checks_manifest.json";

    private static readonly string[] CheckModules =
    {
        "audit_artifacts", "audit_codex_integrity", "audit_authority",
        "audit_activation", "audit_architecture", "audit_directories",
        "audit_formal_rules", "audit_manifests", "audit_protected",
        "audit_contract_axes", "audit_runtime_contracts",
        "audit_sql_patterns", "audit_optimization",
    };

    private static readonly HashSet<string> NativeCovered =
        new(StringComparer.Ordinal)
    {
        "check_protected_sources", "check_forbidden_legacy",
        "check_codex_consistency", "check_codex_mirror_quality",
        "check_runtime_contracts", "check_metadata_contract",
        "check_shared_layer_structure", "check_embedded_browser",
        "check_orphan_scanner", "check_git_tiers",
        "check_release_manifest_file", "check_release_manifest_module",
        "check_architecture_sources", "check_main_system_source",
        "check_reconcile_modules", "check_bootstrap_native_entry",
        "check_channel_gateway_csharp",
        "check_tool_host_native_boundary", "check_tool_manifests",
        "check_typescript_retirement", "check_tool_isolation_hardening",
        "check_third_party_inventory", "check_bounded_worker_pools",
        "check_codex_text_integrity", "check_authority_policy",
        "check_identity_permissions", "check_repair_policy",
        "check_shared_layer_policy", "check_activation_states",
        "check_architecture_registry", "check_directory_audit",
        "check_directory_schemas", "check_directory_catalog_coverage",
        "check_directory_identity_and_format",
        "check_directory_mirror_parity", "check_directory_relationships",
        "check_directory_seal", "check_provision_classification",
        "check_formal_rules", "check_implementation_obligations",
        "check_tool_identity_registration", "check_contract_axes",
        "check_sql_anti_patterns", "check_gpu_coordinator_torch_free",
        "check_renderer_idle_gating",
    };

    private static string Rel(string root, string relative) =>
        Path.Combine(root, relative.Replace('/', Path.DirectorySeparatorChar));

    private static string ReadText(string path)
    {
        try { return File.ReadAllText(path); }
        catch (IOException) { return ""; }
        catch (UnauthorizedAccessException) { return ""; }
    }

    private static JsonObject? ReadJsonObject(string path)
    {
        try
        {
            return JsonNode.Parse(File.ReadAllText(path)) as JsonObject;
        }
        catch (IOException) { return null; }
        catch (JsonException) { return null; }
        catch (UnauthorizedAccessException) { return null; }
    }

    /// <summary>Regenerate the manifest on disk — the governed refresh
    /// lane replacing export_audit_manifest.build_manifest.</summary>
    public static void Refresh(string root)
    {
        var manifest = Build(root);
        var path = Rel(root, ManifestRelative);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var wasReadonly = File.Exists(path)
            && (File.GetAttributes(path) & FileAttributes.ReadOnly) != 0;
        if (wasReadonly)
            File.SetAttributes(path,
                File.GetAttributes(path) & ~FileAttributes.ReadOnly);
        try
        {
            using var doc = JsonDocument.Parse(manifest.ToJsonString());
            Canon.WriteJsonAtomic(
                path, Canon.Indented(doc.RootElement) + "\n");
        }
        finally
        {
            if (wasReadonly)
                File.SetAttributes(path,
                    File.GetAttributes(path) | FileAttributes.ReadOnly);
        }
    }

    /// <summary>Check-id multiset diff between two manifest files —
    /// parity evidence for the C# port vs the retired Python lane.
    /// Exit 0 when the id multisets are identical.</summary>
    public static int Diff(string leftPath, string rightPath)
    {
        static List<string> Ids(string path)
        {
            var doc = ReadJsonObject(path);
            return doc?["checks"] is JsonArray checks
                ? checks.Select(c => c?["id"]?.GetValue<string>() ?? "")
                    .ToList()
                : new List<string>();
        }
        var left = Ids(leftPath);
        var right = Ids(rightPath);
        var removed = left.GroupBy(i => i)
            .Select(g => (Id: g.Key,
                N: g.Count() - right.Count(r => r == g.Key)))
            .Where(x => x.N > 0).OrderBy(x => x.Id).ToList();
        var added = right.GroupBy(i => i)
            .Select(g => (Id: g.Key,
                N: g.Count() - left.Count(r => r == g.Key)))
            .Where(x => x.N > 0).OrderBy(x => x.Id).ToList();
        Console.WriteLine($"left={leftPath} checks={left.Count}");
        Console.WriteLine($"right={rightPath} checks={right.Count}");
        Console.WriteLine(
            $"removed={removed.Sum(x => x.N)} " +
            $"added={added.Sum(x => x.N)}");
        foreach (var x in removed.Take(60))
            Console.WriteLine($"  - {x.Id} (x{x.N})");
        foreach (var x in added.Take(60))
            Console.WriteLine($"  + {x.Id} (x{x.N})");
        return removed.Count == 0 && added.Count == 0 ? 0 : 1;
    }


    private sealed record Identity(
        string Actor, string Code, string BoundToolId, string Lifecycle);
}
