using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace GPTBridge.CodexPipeline;

internal class MirrorRenderError : Exception
{
    public MirrorRenderError(string message) : base(message) { }
}

/// <summary>
/// Canonical Chinese Codex mirror rendering and quality evidence —
/// direct port of chinese_codex_mirror.py + codex_mirror_writer.py
/// (A537/A538 five-part mirror, SEAL_CANONICAL_V1 chain).
/// </summary>
internal static class ChineseMirror
{
    public static readonly string[] PartNames = Enumerable.Range(1, 5)
        .Select(i => $"governance_codex.zh-TW.part-{i}.txt").ToArray();

    private static byte[] Canonical(object? value) =>
        Encoding.UTF8.GetBytes(CanonJson.Serialize(value));

    private static string Sha256Hex(byte[] data) =>
        Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();

    /// <summary>Validate and assemble the one logical mirror from its
    /// ordered parts — parity with ``load_chinese_codex_parts``.</summary>
    public static Dictionary<string, object?> LoadParts(string codexRoot)
    {
        var parts = PartNames
            .Select(name => JsonNode.Parse(File.ReadAllText(
                Path.Combine(codexRoot, name))) as JsonObject
                ?? throw new InvalidDataException(
                    $"mirror part not an object: {name}"))
            .ToList();
        var version = Repo.Str(parts[0], "codex_version");
        var mirrorId = Repo.Str(parts[0], "mirror_id");
        var assembledHash = Repo.Str(parts[0], "assembled_payload_hash");
        var previousHash = new string('0', 64);
        var tables = new Dictionary<string, List<object?>>(
            StringComparer.Ordinal);
        for (var index = 0; index < parts.Count; index++)
        {
            var part = parts[index];
            var expectedIndex = index + 1;
            var partIndex = Repo.Get(part, "part_index") is JsonValue pi
                && pi.TryGetValue<long>(out var piv) ? piv : -1;
            var partCount = Repo.Get(part, "part_count") is JsonValue pc
                && pc.TryGetValue<long>(out var pcv) ? pcv : -1;
            if (partIndex != expectedIndex
                || partCount != PartNames.Length
                || Repo.Str(part, "codex_version") != version
                || Repo.Str(part, "mirror_id") != mirrorId
                || Repo.Str(part, "assembled_payload_hash") != assembledHash
                || Repo.Str(part, "previous_part_hash") != previousHash)
                throw new InvalidDataException(
                    "Chinese Codex mirror part identity or chain mismatch");
            var unsigned = new Dictionary<string, object?>(
                StringComparer.Ordinal);
            foreach (var pair in part)
                if (pair.Key != "part_hash")
                    unsigned[pair.Key] = Repo.ToPlain(pair.Value);
            var actualHash = Sha256Hex(Canonical(unsigned));
            if (actualHash != Repo.Str(part, "part_hash"))
                throw new InvalidDataException(
                    "Chinese Codex mirror part hash mismatch");
            previousHash = actualHash;
            if (Repo.Get(part, "tables") is JsonObject partTables)
                foreach (var table in partTables)
                {
                    if (!tables.TryGetValue(table.Key, out var rows))
                        tables[table.Key] = rows = new List<object?>();
                    if (table.Value is JsonArray array)
                        foreach (var row in array)
                            rows.Add(Repo.ToPlain(row));
                }
        }
        var assembled = new Dictionary<string, object?>(
            StringComparer.Ordinal)
        {
            ["codex_version"] = version,
            ["tables"] = tables.ToDictionary(
                pair => pair.Key,
                pair => (object?)pair.Value,
                StringComparer.Ordinal),
        };
        if (Sha256Hex(Canonical(assembled)) != assembledHash)
            throw new InvalidDataException(
                "Chinese Codex assembled payload hash mismatch");
        return assembled;
    }
}

/// <summary>Mirror render/evidence writer — port of
/// codex_mirror_writer.py.</summary>
internal static class MirrorWriter
{
    public const string EvidenceTable = "chinese_mirror_quality_evidence";
    public const string EvidenceSchema =
        "CREATE TABLE IF NOT EXISTS chinese_mirror_quality_evidence ("
        + "evidence_id TEXT, part_count INTEGER, "
        + "replacement_character_count INTEGER, "
        + "question_loss_field_count INTEGER, chain_valid INTEGER, "
        + "assembled_hash_valid INTEGER, result TEXT, "
        + "version_identity TEXT, status TEXT)";

    private static List<Dictionary<string, object?>> MirrorTableRows(
        StageConnection connection, string table)
    {
        var cursor = connection.Execute($"PRAGMA table_info({table})");
        var columns = cursor.Rows
            .Select(row => (Name: row[1]?.ToString() ?? "",
                Pk: Convert.ToInt32(row[5] ?? 0)))
            .ToList();
        if (columns.Count == 0)
            return new List<Dictionary<string, object?>>();
        var names = columns.Select(c => c.Name).ToList();
        var primary = columns.Where(c => c.Pk > 0)
            .Select(c => c.Name).ToList();
        var order = primary.Count > 0
            ? $"ORDER BY {string.Join(", ", primary)}"
            : "ORDER BY rowid";
        var rows = new List<Dictionary<string, object?>>();
        foreach (var row in connection.Execute( // sql-ok: introspected
            $"SELECT {string.Join(", ", names)} FROM {table} {order}")
            .Rows)
        {
            var entry = new Dictionary<string, object?>(
                StringComparer.Ordinal);
            for (var i = 0; i < names.Count; i++)
            {
                if (row[i] is byte[])
                    throw new MirrorRenderError(
                        "binary column cannot be mirrored: "
                        + $"{table}.{names[i]}");
                entry[names[i]] = row[i];
            }
            rows.Add(entry);
        }
        rows.Sort((a, b) =>
        {
            var ka = string.Join("\x1f",
                primary.Select(n => a.TryGetValue(n, out var v)
                    ? v?.ToString() ?? "" : ""));
            var kb = string.Join("\x1f",
                primary.Select(n => b.TryGetValue(n, out var v)
                    ? v?.ToString() ?? "" : ""));
            var cmp = string.CompareOrdinal(ka, kb);
            return cmp != 0 ? cmp : string.CompareOrdinal(
                CanonJson.Serialize(a), CanonJson.Serialize(b));
        });
        return rows;
    }

    private static Dictionary<string, int>? TemplatePartMapping(
        string templateRoot)
    {
        var mapping = new Dictionary<string, int>(StringComparer.Ordinal);
        try
        {
            for (var index = 0; index < ChineseMirror.PartNames.Length;
                index++)
            {
                var part = JsonNode.Parse(File.ReadAllText(Path.Combine(
                    templateRoot, ChineseMirror.PartNames[index])))
                    as JsonObject;
                if (Repo.Get(part, "tables") is JsonObject tables)
                    foreach (var table in tables)
                        if (!mapping.ContainsKey(table.Key))
                            mapping[table.Key] = index + 1;
            }
        }
        catch (Exception)
        {
            return null;
        }
        return mapping.Count > 0 ? mapping : null;
    }

    private static List<List<string>> MirrorTableChunks(
        StageConnection connection, string? templateRoot)
    {
        var cursor = connection.Execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            + "AND name NOT LIKE 'sqlite_%' "
            + "AND name NOT LIKE '%_fts_%' ORDER BY name");
        var names = cursor.Rows
            .Select(row => row[0]?.ToString() ?? "").ToList();
        var mapping = templateRoot is not null
            ? TemplatePartMapping(templateRoot) : null;
        if (mapping is not null)
        {
            // Template preserves part assignment, but only for tables
            // that still exist; tables absent from the template fall
            // back to the round-robin distribution.
            var live = names.ToHashSet(StringComparer.Ordinal);
            var chunks = Enumerable.Range(1, 5)
                .Select(index => mapping
                    .Where(pair => pair.Value == index
                        && live.Contains(pair.Key))
                    .Select(pair => pair.Key).ToList())
                .ToList();
            var mapped = chunks.SelectMany(c => c)
                .ToHashSet(StringComparer.Ordinal);
            var extra = names.Where(n => !mapped.Contains(n)).ToList();
            for (var i = 0; i < extra.Count; i++)
                chunks[i % 5].Add(extra[i]);
            return chunks;
        }
        return Enumerable.Range(0, 5)
            .Select(index => names.Where((_, i) => i % 5 == index)
                .ToList())
            .ToList();
    }

    /// <summary>Render the five ordered mirror parts from one database
    /// generation — never touches the live mirror.</summary>
    public static string[] RenderMirrorParts(string database,
        string targetRoot, string? templateRoot = null)
    {
        Directory.CreateDirectory(targetRoot);
        string version;
        List<List<string>> chunks;
        Dictionary<string, List<Dictionary<string, object?>>> tables;
        using (var store =
            AmendmentContract.OpenCodexStore(database))
        {
            var connection = store.Connection;
            var metadata = new Dictionary<string, string>(
                StringComparer.Ordinal);
            foreach (var row in connection.Execute(
                "SELECT key, value FROM metadata").Rows)
                metadata[row[0]?.ToString() ?? ""] =
                    row[1]?.ToString() ?? "";
            version = metadata.TryGetValue("codex_version", out var v)
                ? v.Trim() : "";
            if (version.Length == 0)
                throw new MirrorRenderError(
                    "database lacks codex_version");
            chunks = MirrorTableChunks(connection, templateRoot);
            tables = new Dictionary<string,
                List<Dictionary<string, object?>>>(StringComparer.Ordinal);
            foreach (var chunk in chunks)
                foreach (var table in chunk)
                    tables[table] = MirrorTableRows(connection, table);
        }
        var assembledHash = Convert.ToHexString(SHA256.HashData(
            Encoding.UTF8.GetBytes(CanonJson.Serialize(
                new Dictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["codex_version"] = version,
                    ["tables"] = tables.ToDictionary(p => p.Key,
                        p => (object?)p.Value, StringComparer.Ordinal),
                })))).ToLowerInvariant();
        var paths = new List<string>();
        var previousHash = new string('0', 64);
        for (var i = 0; i < chunks.Count; i++)
        {
            var index = i + 1;
            var names = chunks[i];
            var part = new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["mirror_id"] = "GOVERNANCE_CODEX_ZH_TW",
                ["codex_version"] = version,
                ["part_index"] = (long)index,
                ["part_count"] = 5L,
                ["previous_part_hash"] = previousHash,
                ["assembled_payload_hash"] = assembledHash,
                ["tables"] = names.ToDictionary(n => n,
                    n => (object?)(tables.TryGetValue(n, out var t)
                        ? t : new List<Dictionary<string, object?>>()),
                    StringComparer.Ordinal),
            };
            var partHash = Convert.ToHexString(SHA256.HashData(
                Encoding.UTF8.GetBytes(CanonJson.Serialize(part))))
                .ToLowerInvariant();
            part["part_hash"] = partHash;
            var path = Path.Combine(targetRoot,
                ChineseMirror.PartNames[index - 1]);
            File.WriteAllText(path, CanonJson.Serialize(part) + "\n",
                new UTF8Encoding(false));
            paths.Add(path);
            previousHash = partHash;
        }
        return paths.ToArray();
    }

    /// <summary>Validate the rendered mirror against its database
    /// generation.</summary>
    public static string[] MirrorErrors(string database, string partsRoot,
        string label = "staged")
    {
        Dictionary<string, object?> mirror;
        try
        {
            mirror = ChineseMirror.LoadParts(partsRoot);
        }
        catch (Exception error) when (error is IOException
            or InvalidDataException or System.Text.Json.JsonException
            or KeyNotFoundException)
        {
            return new[] { $"{label} mirror is invalid: {error}" };
        }
        var mirrorTables = mirror.TryGetValue("tables", out var t)
            && t is Dictionary<string, object?> map
                ? map : new Dictionary<string, object?>();
        var errors = new List<string>(MirrorParityErrors(database,
            mirrorTables));
        var metrics = MirrorQualityMetrics(mirrorTables);
        var loss = Convert.ToInt32(metrics["question_loss_field_count"]);
        var rep = Convert.ToInt32(metrics["replacement_character_count"]);
        if (loss != 0)
            errors.Add($"{label} mirror carries replacement damage: "
                + $"{loss} fields lost");
        if (rep != 0)
            errors.Add($"{label} mirror carries replacement "
                + $"characters: {rep}");
        return errors.ToArray();
    }

    /// <summary>``mirror_quality_metrics`` over assembled mirror
    /// tables.</summary>
    public static Dictionary<string, object?> MirrorQualityMetrics(
        Dictionary<string, object?> mirrorTables)
    {
        var replacementCount = 0;
        var lossFields = 0;
        foreach (var (table, field) in UpdateValidation.TextFields)
        {
            if (!mirrorTables.TryGetValue(table, out var rowsObj)
                || rowsObj is not System.Collections.IEnumerable rows)
                continue;
            foreach (var rowObj in rows)
            {
                if (rowObj is not IDictionary<string, object?> row
                    || !row.TryGetValue(field, out var value))
                    continue;
                replacementCount +=
                    UpdateValidation.ReplacementCharacterCount(value);
                if (UpdateValidation.IsReplacementDamaged(value))
                    lossFields += 1;
            }
        }
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["replacement_character_count"] = replacementCount,
            ["question_loss_field_count"] = lossFields,
        };
    }

    /// <summary>``mirror_text_parity_errors`` — per-provision parity
    /// between mirror text and the database generation.</summary>
    public static string[] MirrorParityErrors(string database,
        Dictionary<string, object?> mirrorTables)
    {
        using var store = AmendmentContract.OpenCodexStore(database);
        var connection = store.Connection;
        var columns = new List<string>();
        try
        {
            columns = connection.Execute("PRAGMA table_info(articles)")
                .Rows.Select(r => r[1]?.ToString() ?? "").ToList();
        }
        catch (Exception) { }
        if (columns.Count == 0)
            return new[]
            { "mirror parity check requires the articles table" };
        var mirrorRows = new Dictionary<string,
            IDictionary<string, object?>>(StringComparer.Ordinal);
        if (mirrorTables.TryGetValue("articles", out var articlesObj)
            && articlesObj is System.Collections.IEnumerable articles)
            foreach (var rowObj in articles)
                if (rowObj is IDictionary<string, object?> row
                    && row.TryGetValue("provision_id", out var pid)
                    && pid is not null)
                    mirrorRows[pid.ToString() ?? ""] = row;
        var errors = new List<string>();
        foreach (var row in connection.Execute(
            "SELECT provision_id, rule FROM articles").Rows)
        {
            var provisionId = row[0]?.ToString() ?? "";
            if (!mirrorRows.TryGetValue(provisionId, out var mirrorRow))
            {
                errors.Add($"mirror is missing article: {provisionId}");
                continue;
            }
            var databaseRule = row[1]?.ToString() ?? "";
            var mirrorRule = mirrorRow.TryGetValue("rule", out var mr)
                ? mr?.ToString() ?? "" : "";
            var databaseCjk = CjkCount(databaseRule);
            var mirrorCjk = CjkCount(mirrorRule);
            if (databaseCjk > 0 && mirrorCjk == 0)
                errors.Add(
                    $"mirror lost Chinese text for article: {provisionId}");
            if (UpdateValidation.IsReplacementDamaged(mirrorRule)
                && !UpdateValidation.IsReplacementDamaged(databaseRule))
                errors.Add("mirror carries replacement damage absent "
                    + $"from the database: {provisionId}");
        }
        return errors.ToArray();
    }

    private static int CjkCount(string text) =>
        text.Count(c => c >= '一' && c <= '鿿');

    /// <summary>Record the recomputed mirror-quality evidence row in the
    /// staged database (write-back).</summary>
    public static Dictionary<string, object?>
        RecordMirrorQualityEvidence(string database, string partsRoot)
    {
        var mirror = ChineseMirror.LoadParts(partsRoot);
        var mirrorTables = mirror.TryGetValue("tables", out var t)
            && t is Dictionary<string, object?> map
                ? map : new Dictionary<string, object?>();
        var metrics = MirrorQualityMetrics(mirrorTables);
        var store = AmendmentContract.OpenCodexStore(database,
            writeBack: true);
        try
        {
        var connection = store.Connection;
        connection.Execute(EvidenceSchema);
        var version = connection.Execute(
            "SELECT value FROM metadata WHERE key='codex_version'")
            .FetchOne()?[0]?.ToString()?.Trim() ?? "";
        connection.Execute(
            $"UPDATE {EvidenceTable} SET status='superseded' "
            + "WHERE status='current'");
        var replacement = Convert.ToInt32(
            metrics["replacement_character_count"]);
        var loss = Convert.ToInt32(
            metrics["question_loss_field_count"]);
        var result = replacement == 0 && loss == 0 ? "PASS" : "FAIL";
        connection.Execute( // sql-ok: fixed evidence-table insert
            $"INSERT INTO {EvidenceTable} "
            + "VALUES (?, 5, ?, ?, 1, 1, ?, ?, 'current')",
            new object?[]
            {
                $"MIRROR@{version}", (long)replacement, (long)loss,
                result, version,
            });
        connection.Commit();
        }
        catch
        {
            store.SuppressWriteBack();
            throw;
        }
        finally
        {
            store.Dispose();
        }
        return metrics;
    }
}
