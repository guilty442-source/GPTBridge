using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

internal static partial class ManifestExport
{
    // ------------------------------------------------------------------
    //  Frozen contract-text sources.  The retired Python registry
    //  modules (governance_policy, directory_authority, permission
    //  registries, source_ownership, tool_routes, audit data modules,
    //  formal-rule evaluators) are preserved verbatim as .txt artifacts
    //  under permission_directory/database/frozen/ — the same
    //  "frozen contract text" convention EvaluatorRegistry already used
    //  (static parse, never executed).  SourceText/SourceRel resolve the
    //  live worktree file first for compatibility, then the frozen
    //  artifact; Module() and every PyLit-derived snapshot therefore
    //  keep producing identical data without a Python lane.
    // ------------------------------------------------------------------

    private const string FrozenDir =
        "governance_rule/permission_directory/database/frozen";

    private static string FrozenRel(string legacyRel) =>
        $"{FrozenDir}/{Path.GetFileNameWithoutExtension(legacyRel)}.txt";

    /// <summary>Emit-path for a registry module — the frozen artifact
    /// when the legacy file no longer exists so file-contains pins keep
    /// evaluating against the frozen contract text.</summary>
    private static string SourceRel(string root, string legacyRel) =>
        File.Exists(Rel(root, legacyRel))
            ? legacyRel
            : FrozenRel(legacyRel);

    /// <summary>Text of a registry module — live file when present,
    /// otherwise the frozen contract-text artifact.</summary>
    private static string SourceText(string root, string legacyRel)
    {
        var live = Rel(root, legacyRel);
        if (File.Exists(live)) return ReadText(live);
        return ReadText(Rel(root, FrozenRel(legacyRel)));
    }

    /// <summary>Curated protected-source list — the JSON successor of
    /// ``GOVERNANCE_POLICY.authority_files`` +
    /// ``MANAGED_READ_ONLY_REGISTRY_PATHS`` after the registry
    /// retirement.</summary>
    private static List<string> ProtectedSources(Ctx ctx)
    {
        var seen = new List<string>();
        var doc = ReadJsonObject(Rel(ctx.Root,
            "governance_rule/execution/audit/protected_sources.json"));
        if (doc?["paths"] is JsonArray paths)
            foreach (var p in paths)
                if (p?.GetValue<string>() is { } s && !seen.Contains(s))
                    seen.Add(s);
        return seen;
    }
}
