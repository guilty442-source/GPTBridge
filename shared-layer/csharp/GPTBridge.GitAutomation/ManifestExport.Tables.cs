using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.GitAutomation;

internal static partial class ManifestExport
{
    // ------------------------------------------------------------------
    //  Codex-mirror table family + authority policy / identity bindings.
    // ------------------------------------------------------------------

    private static void LoadMirrorTables(Ctx ctx)
    {
        var codexDir = Rel(ctx.Root, "governance_rule/codex");
        if (!Directory.Exists(codexDir)) return;
        foreach (var part in Directory
                     .EnumerateFiles(codexDir,
                         "governance_codex.zh-TW.part-*.txt")
                     .OrderBy(p => p, StringComparer.Ordinal))
        {
            var doc = ReadJsonObject(part);
            if (doc?["tables"] is not JsonObject tables) continue;
            var rel = Path.GetRelativePath(ctx.Root, part)
                .Replace('\\', '/');
            foreach (var (name, rows) in tables)
                if (rows is JsonArray array
                    && !ctx.MirrorTables.ContainsKey(name))
                    ctx.MirrorTables[name] =
                        (rel, (JsonArray)array.DeepClone());
        }
    }

    private static void TableAbsent(Ctx ctx, string table) =>
        ctx.E.Fail($"codex-table:absent:{table}",
            $"codex mirror table missing: {table}");

    private static List<JsonObject> TableRows(Ctx ctx, string table) =>
        ctx.MirrorTables.TryGetValue(table, out var entry)
            ? entry.Rows.OfType<JsonObject>().ToList()
            : new List<JsonObject>();

    private static void TableAssert(Ctx ctx, string id, string table,
        IEnumerable<string> markers, int count = 1)
    {
        if (!ctx.MirrorTables.TryGetValue(table, out var entry))
        { TableAbsent(ctx, table); return; }
        ctx.E.Checks.Add(new JsonObject
        {
            ["id"] = id,
            ["kind"] = "json-array-min-count",
            ["path"] = entry.File,
            ["items"] = $"tables.{table}",
            ["markers"] = Emitter.Arr(markers),
            ["min_count"] = count,
        });
    }

    private static void TablePresent(Ctx ctx, string table)
    {
        if (!ctx.PresentSeen.Add(table)) return;
        if (!ctx.MirrorTables.TryGetValue(table, out var entry))
        { TableAbsent(ctx, table); return; }
        ctx.E.Checks.Add(new JsonObject
        {
            ["id"] = $"codex-table:present:{table}",
            ["kind"] = "json-array-min-count",
            ["path"] = entry.File,
            ["items"] = $"tables.{table}",
            ["markers"] = new JsonArray(),
            ["min_count"] = 0,
        });
    }

    private static readonly string[] MarkerTokens = { "!=", "^=", ">=" };

    private static List<string> Bind(string field, JsonNode? value)
    {
        if (value is null) return new List<string>();
        if (value is JsonValue jv
            && jv.TryGetValue<bool>(out var b))
            return new List<string>
                { $"{field}={(b ? "true" : "false")}" };
        var text = value is JsonValue ? value.ToString()
            : value.ToJsonString();
        var first = -1;
        foreach (var tok in MarkerTokens)
        {
            var i = text.IndexOf(tok, StringComparison.Ordinal);
            if (i >= 0 && (first < 0 || i < first)) first = i;
        }
        if (first < 0)
            return new List<string> { $"{field}={text}" };
        return first > 0
            ? new List<string> { $"{field}^={text[..first]}" }
            : new List<string>();
    }

    private const int RowBindCap = 150;

    private static void RowsOrCount(Ctx ctx, string prefix, string table,
        string idcol, Func<JsonObject, List<string>>? extra = null)
    {
        var rows = TableRows(ctx, table);
        if (rows.Count == 0) return;
        TableAssert(ctx, $"{prefix}:count:{table}", table,
            Array.Empty<string>(), count: rows.Count);
        if (rows.Count > RowBindCap) return;
        foreach (var row in rows)
        {
            var rid = row[idcol];
            if (rid is null) continue;
            var markers = Bind(idcol, rid);
            if (extra is not null) markers.AddRange(extra(row));
            if (markers.Count > 0)
                TableAssert(ctx, $"{prefix}:{rid.GetValue<string>()}",
                    table, markers);
        }
    }

    private static string Relativize(string root, string pathText)
    {
        var norm = pathText.Replace('\\', '/').TrimEnd('/');
        var prefix = root.Replace('\\', '/') + "/";
        if (norm.StartsWith(prefix, StringComparison.Ordinal))
            return norm[prefix.Length..];
        return norm == root.Replace('\\', '/') ? "." : norm;
    }

}
