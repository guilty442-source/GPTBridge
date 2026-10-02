using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.GitAutomation;

internal static partial class ManifestExport
{

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
        // The xingcheng enclave's bound manifest lives at
        // xingcheng/xingcheng/manifest.json — outside every generic
        // scan above — so it is registered explicitly.
        var enclaveManifest = Path.Combine(
            root, "xingcheng", "xingcheng", "manifest.json");
        if (File.Exists(enclaveManifest))
            tmPaths.Add(enclaveManifest);
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

}
