using Npgsql;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Staged-generation and mirror quality validation — direct port of
/// codex_update_validation.py.  Never writes the Codex; measures a staged
/// or live generation so the publisher cannot ship replacement-character
/// corruption (the 2026-09-17 U+003F defect) and the audit can
/// independently recompute identical metrics.
/// </summary>
internal static class UpdateValidation
{
    public const int ReplacementRunThreshold = 5;

    /// <summary>Content-bearing tables and their human-language text
    /// fields.</summary>
    public static readonly (string Table, string Field)[] TextFields =
    {
        ("articles", "subject"), ("articles", "rule"),
        ("articles", "prohibition"), ("articles", "exception"),
        ("sections", "title"), ("sections", "summary"),
        ("preamble", "title"), ("preamble", "authority_rank"),
        ("preamble", "issuance"), ("preamble", "binding_scope"),
        ("principles", "statement"), ("edicts", "edict"),
        ("sovereigns", "name"), ("sovereigns", "area"),
    };

    public static int LongestReplacementRun(object? text)
    {
        var value = text?.ToString() ?? "";
        var longest = 0;
        var current = 0;
        foreach (var character in value)
        {
            if (character == '?')
            {
                current += 1;
                longest = Math.Max(longest, current);
            }
            else
            {
                current = 0;
            }
        }
        return longest;
    }

    private static bool HasCjk(string text) =>
        text.Any(c => c >= '一' && c <= '鿿');

    public static int ReplacementCharacterCount(object? text)
    {
        var value = text?.ToString() ?? "";
        var count = 0;
        for (var index = 0; index < value.Length; index++)
        {
            if (value[index] != '?')
                continue;
            var start = Math.Max(0, index - 2);
            var window = value[start..Math.Min(value.Length, index + 3)];
            if (HasCjk(window)
                || LongestReplacementRun(window)
                    >= ReplacementRunThreshold)
                count += 1;
        }
        return count;
    }

    public static bool IsReplacementDamaged(object? text,
        int threshold = ReplacementRunThreshold) =>
        LongestReplacementRun(text) >= threshold;

    private static List<string> TableColumns(StageConnection connection,
        string table)
    {
        try
        {
            var cursor = connection.Execute(
                $"PRAGMA table_info({table})");
            return cursor.Rows
                .Select(row => row[1]?.ToString() ?? "").ToList();
        }
        catch (PostgresException)
        {
            return new List<string>();
        }
    }

    /// <summary>Report ``table.field:row-key`` for text fields carrying
    /// replacement damage.</summary>
    public static string[] FindReplacementDamage(
        StageConnection connection,
        IEnumerable<(string Table, string Field)>? fields = null,
        int threshold = ReplacementRunThreshold)
    {
        var findings = new List<string>();
        var columnsByTable = new Dictionary<string, List<string>>(
            StringComparer.Ordinal);
        foreach (var (table, field) in fields ?? TextFields)
        {
            if (!columnsByTable.TryGetValue(table, out var columns))
                columnsByTable[table] = columns =
                    TableColumns(connection, table);
            if (columns.Count == 0 || !columns.Contains(field))
                continue;
            var key = new[] { "provision_id", "id", "name", "position" }
                .FirstOrDefault(columns.Contains);
            var select = key is not null ? $"{key}, {field}" : field;
            var cursor = connection.Execute(
                $"SELECT {select} FROM {table}"); // sql-ok: bounded schema columns
            foreach (var row in cursor.Rows)
            {
                var rowKey = key is not null
                    ? row[0]?.ToString() ?? "" : "?";
                var value = row[^1];
                if (IsReplacementDamaged(value, threshold))
                    findings.Add($"{table}.{field}:{rowKey}");
            }
        }
        return findings.ToArray();
    }

    /// <summary>Recomputable mirror-quality metrics for one database
    /// generation.</summary>
    public static Dictionary<string, object?> GenerationTextMetrics(
        StageConnection connection)
    {
        var replacementCount = 0;
        var lossFields = 0;
        foreach (var (table, field) in TextFields)
        {
            var columns = TableColumns(connection, table);
            if (columns.Count == 0 || !columns.Contains(field))
                continue;
            var cursor = connection.Execute(
                $"SELECT {field} FROM {table}"); // sql-ok: bounded column
            foreach (var row in cursor.Rows)
            {
                replacementCount += ReplacementCharacterCount(row[0]);
                if (IsReplacementDamaged(row[0]))
                    lossFields += 1;
            }
        }
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["replacement_character_count"] = replacementCount,
            ["question_loss_field_count"] = lossFields,
        };
    }

    /// <summary>Normalized ``PRAGMA foreign_key_check`` rows for
    /// baseline comparison.</summary>
    public static List<string[]> ForeignKeyViolations(
        StageConnection connection) =>
        connection.Execute("PRAGMA foreign_key_check").Rows
            .Select(row => row.Select(v => v?.ToString() ?? "").ToArray())
            .ToList();

    /// <summary>Validate integrity; only NEW foreign-key violations
    /// fail — acknowledged legacy violations on the live generation are
    /// tolerated through the baseline.</summary>
    public static string[] ValidateDatabaseIntegrity(
        StageConnection connection,
        IEnumerable<IEnumerable<object?>>? baselineViolations = null)
    {
        var errors = new List<string>();
        var check = connection
            .Execute("PRAGMA integrity_check").FetchOne();
        var status = check?[0]?.ToString() ?? "missing";
        if (!string.Equals(status, "ok",
                StringComparison.OrdinalIgnoreCase))
            errors.Add(
                $"staged codex database failed integrity_check: {status}");
        var baseline = new HashSet<string>(
            (baselineViolations ?? Enumerable.Empty<IEnumerable<object?>>())
            .Select(row => string.Join("\x1f",
                row.Select(v => v?.ToString() ?? ""))),
            StringComparer.Ordinal);
        foreach (var row in ForeignKeyViolations(connection))
        {
            if (!baseline.Contains(string.Join("\x1f", row)))
                errors.Add(
                    "staged codex database foreign key violation: "
                    + $"({string.Join(", ", row)})");
        }
        foreach (var required in new[] { "metadata", "articles" })
            if (TableColumns(connection, required).Count == 0)
                errors.Add(
                    $"staged codex database lacks required table: {required}");
        return errors.ToArray();
    }

    /// <summary>Validate one staged ``.sql`` generation: integrity,
    /// version identity, replacement damage.</summary>
    public static string[] StagedGenerationErrors(string databasePath,
        string? version = null,
        IEnumerable<IEnumerable<object?>>? baselineViolations = null)
    {
        var errors = new List<string>();
        CodexStore? store;
        try
        {
            store = AmendmentContract.OpenCodexStore(databasePath);
        }
        catch (Exception error)
        {
            return new[] { $"staged codex store cannot be opened: {error}" };
        }
        using (store)
        {
            try
            {
                var connection = store.Connection;
                errors.AddRange(ValidateDatabaseIntegrity(connection,
                    baselineViolations));
                var metadata = new Dictionary<string, string>(
                    StringComparer.Ordinal);
                foreach (var row in connection
                    .Execute("SELECT key, value FROM metadata").Rows)
                    metadata[row[0]?.ToString() ?? ""] =
                        row[1]?.ToString() ?? "";
                var stored = metadata.TryGetValue("codex_version",
                    out var v) ? v.Trim() : "";
                if (stored.Length == 0)
                    errors.Add(
                        "staged codex database lacks metadata.codex_version");
                else if (version is not null
                    && stored != version.Trim())
                    errors.Add(
                        "staged codex version identity mismatch: "
                        + $"{stored} != {version}");
                foreach (var finding in FindReplacementDamage(connection))
                    errors.Add(
                        $"staged codex text replacement damage: {finding}");
            }
            catch (Exception error)
            {
                errors.Add(
                    $"staged codex store validation failed: {error}");
            }
        }
        return errors.ToArray();
    }
}
