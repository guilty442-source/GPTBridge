using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.GitAutomation;

internal static partial class ManifestExport
{
    private static readonly HashSet<string> ManifestArtifactRoots =
        new(StringComparer.Ordinal) { "worktrees", "backups", "xingcheng" };

    private static bool ManifestScanned(string root, string manifestPath)
    {
        var parts = Path.GetRelativePath(root, manifestPath)
            .Split(Path.DirectorySeparatorChar);
        if (parts.Length >= 3
            && parts[0] == "main-system" && parts[1] == "runtime"
            && parts[2] is "releases" or "temp")
            return false;
        return !parts[0].StartsWith('.')
               && !ManifestArtifactRoots.Contains(parts[0]);
    }

    /// <summary>check_tool_manifests — the four-layer scan and
    /// per-manifest emit logic (build_manifest lines ~910-1065).</summary>
    private static void EmitToolManifests(Ctx ctx)
    {
        var root = ctx.Root;
        var e = ctx.E;
        var requiredLocaleKeys = new List<string>(
            KwStrings(ctx.CodeRulesCall, "required_locale_keys"));
        requiredLocaleKeys.Sort(StringComparer.Ordinal);
        var standaloneDir = Rel(root, "Standalone tools");

        void EmitManifest(string manifestPath, bool topLevel,
            string? expectedOwner, bool selfHealth)
        {
            var rel = Path.GetRelativePath(root, manifestPath)
                .Replace('\\', '/');
            e.Emit($"tool-manifest:parse:{rel}", "json-parses", rel);
            var manifest = ReadJsonObject(manifestPath);
            if (manifest is null) return;
            var lifecycle = manifest["lifecycle"] as JsonObject;
            var retired = lifecycle is not null
                && (lifecycle["status"]?.GetValue<string>() ?? "")
                    .Trim().Equals("retired",
                        StringComparison.OrdinalIgnoreCase);
            var toolId = manifest["id"]?.GetValue<string>() ?? "";
            if (retired)
            {
                e.Emit($"tool-manifest:retired:{rel}",
                    "json-key-value", rel, r => r["markers"] =
                        Emitter.Arr(new[]
                        {
                            "enabled=false",
                            "lifecycle.stoppable=false",
                            "status!=running",
                            "main_system_independent_tool!=true",
                        }));
                return;
            }
            var markers = new List<string> { "name_key=tool.name" };
            var absent = new List<string> { "name" };
            if (manifest["window"] is JsonObject)
            {
                markers.Add("window.title_key=tool.window_title");
                absent.Add("window.title");
            }
            if (topLevel)
            {
                var codeScope = toolId == "xingcheng"
                    ? "project-source-excluding-governance-rule"
                    : "tool-root-only";
                var dbScope = toolId == "xingcheng"
                    ? "opaque-central-index-read-and-xingcheng-internal" +
                      "-read-write"
                    : toolId == "governance_rule"
                        ? "none"
                        : "tool-database-only";
                markers.Add($"permissions.code_scope={codeScope}");
                markers.Add($"permissions.database_scope={dbScope}");
                if (toolId == "governance_rule")
                {
                    markers.AddRange(new[]
                    {
                        "status=running",
                        "lifecycle.startup=default-before-main-system",
                        "lifecycle.directLoad=true",
                        "lifecycle.encapsulated=false",
                        "lifecycle.optional=false",
                        "lifecycle.stoppable=false",
                        "lifecycle.disableable=false",
                        "lifecycle.unloadable=false",
                    });
                    absent.Add("executable");
                }
            }
            else
            {
                markers.Add($"physical_owner_root={expectedOwner}");
            }
            e.Emit($"tool-manifest:values:{rel}", "json-key-value", rel,
                r => r["markers"] = Emitter.Arr(markers));
            e.Emit($"tool-manifest:absent:{rel}", "json-key-absent", rel,
                r => r["markers"] = Emitter.Arr(absent));
            e.Emit($"tool-manifest:dicts:{rel}", "json-has-keys", rel,
                r => r["markers"] =
                    Emitter.Arr(new[] { "permissions", "capabilities" }));
            var localePath = Path.Combine(
                Path.GetDirectoryName(manifestPath)!, "locales",
                "zh-TW.json");
            var localeRel = Path.GetRelativePath(root, localePath)
                .Replace('\\', '/');
            e.Emit($"tool-locale:exists:{rel}", "file-exists", localeRel);
            e.Emit($"tool-locale:parse:{rel}", "json-parses", localeRel);
            e.Emit($"tool-locale:keys:{rel}", "json-has-keys", localeRel,
                r => r["markers"] = Emitter.Arr(requiredLocaleKeys));
            var enabledNode = manifest["enabled"];
            if (!selfHealth
                || (enabledNode is JsonValue ev
                    && ev.TryGetValue<bool>(out var en) && !en))
                return;
            var toolRoot = Path.GetFullPath(
                Path.GetDirectoryName(manifestPath)!);
            if (toolId.Length == 0
                || manifest["test_targets"] is not JsonArray targets
                || targets.Count == 0)
            {
                e.Fail($"self-health:test-targets:{rel}",
                    "governed tool must declare test_targets");
                return;
            }
            for (var index = 0; index < targets.Count; index++)
            {
                var target = targets[index]?.GetValue<string>()
                    ?.Trim() ?? "";
                var baseId =
                    $"self-health:test-target:{toolId}:{index}";
                if (target.StartsWith("pending-native:",
                        StringComparison.Ordinal))
                {
                    var prefix = target["pending-native:".Length..]
                        .TrimEnd('/') + "/";
                    e.Checks.Add(new JsonObject
                    {
                        ["id"] = $"{baseId}:pending-native",
                        ["kind"] = "json-array-min-count",
                        ["path"] = "governance_rule/execution/audit/" +
                                   "pytest_retirement_inventory.json",
                        ["items"] = "rows",
                        ["min_count"] = 1,
                        ["markers"] = Emitter.Arr(new[]
                        {
                            "status=PENDING", $"source_test^={prefix}",
                        }),
                    });
                    continue;
                }
                if (target.StartsWith("native-suite:",
                        StringComparison.Ordinal))
                {
                    e.Emit($"{baseId}:native-suite", "file-exists",
                        "native/test_suites/suite_" +
                        $"{target["native-suite:".Length..]}.cpp");
                    continue;
                }
                var candidate = Path.GetFullPath(
                    Path.Combine(toolRoot, target));
                string relTarget;
                try
                {
                    relTarget = Path.GetRelativePath(root, candidate)
                        .Replace('\\', '/');
                    _ = Path.GetRelativePath(toolRoot, candidate);
                    if (Path.GetRelativePath(toolRoot, candidate)
                        .StartsWith(".."))
                        throw new ArgumentException("escaped");
                }
                catch (Exception)
                {
                    e.Fail($"{baseId}:escaped",
                        "test target escaped tool root: " +
                        $"{toolId}: {target}");
                    continue;
                }
                if (candidate.EndsWith(".py",
                        StringComparison.OrdinalIgnoreCase))
                {
                    e.Fail($"{baseId}:py",
                        "non-conforming Python test target " +
                        $"(FORBID:pytest): {toolId}: {target}");
                    continue;
                }
                e.Emit($"{baseId}:exists", "file-exists", relTarget);
            }
        }

        // root.glob("*/manifest.json") — direct children of root
        foreach (var dir in Directory
                     .EnumerateDirectories(root)
                     .OrderBy(d => d, StringComparer.Ordinal))
        {
            var path = Path.Combine(dir, "manifest.json");
            if (File.Exists(path) && ManifestScanned(root, path))
                EmitManifest(path, topLevel: true,
                    expectedOwner: null, selfHealth: true);
        }
        if (Directory.Exists(standaloneDir))
        {
            // "Standalone tools/*/manifest.json"
            foreach (var dir in Directory
                         .EnumerateDirectories(standaloneDir)
                         .OrderBy(d => d, StringComparer.Ordinal))
            {
                var path = Path.Combine(dir, "manifest.json");
                if (File.Exists(path))
                    EmitManifest(path, topLevel: true,
                        expectedOwner: null, selfHealth: true);
            }
            // "Standalone tools/*/*/manifest.json"
            foreach (var dir in Directory
                         .EnumerateDirectories(standaloneDir)
                         .OrderBy(d => d, StringComparer.Ordinal))
            {
                var owner = Path.GetFileName(dir);
                foreach (var sub in Directory
                             .EnumerateDirectories(dir)
                             .OrderBy(d => d, StringComparer.Ordinal))
                {
                    var path = Path.Combine(sub, "manifest.json");
                    if (File.Exists(path))
                        EmitManifest(path, topLevel: false,
                            expectedOwner: owner, selfHealth: true);
                }
            }
        }
        // The xingcheng enclave keeps its bound manifest at
        // xingcheng/xingcheng/manifest.json; the generic scans skip the
        // enclave (internal corpus/vector manifests are not tools).
        var enclaveManifest = Rel(root, "xingcheng/xingcheng/manifest.json");
        if (File.Exists(enclaveManifest))
            EmitManifest(enclaveManifest, topLevel: false,
                expectedOwner: "xingcheng", selfHealth: true);
        // root.glob("*/*/*/*/manifest.json") — depth-4 anywhere
        // (4 directory segments + filename = 5 path parts)
        foreach (var path in DepthFourManifests(root)
                     .OrderBy(p => p, StringComparer.Ordinal))
            if (ManifestScanned(root, path))
                EmitManifest(path, topLevel: false,
                    expectedOwner: Path.GetFileName(
                        Path.GetDirectoryName(
                            Path.GetDirectoryName(
                                Path.GetDirectoryName(path)!))),
                    selfHealth: true);
    }
}
