using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.GitAutomation;

internal static partial class ManifestExport
{
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
        if (!Directory.Exists(Rel(root, aiRoot)))
            aiRoot = "Standalone tools/ai-assistant";
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

}
