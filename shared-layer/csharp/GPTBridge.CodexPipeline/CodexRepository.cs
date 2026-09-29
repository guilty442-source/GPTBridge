namespace GPTBridge.CodexPipeline;

/// <summary>
/// Read-only adapter for the authoritative PostgreSQL Governance Codex —
/// direct port of ``codex_repository`` (A279 governed repository
/// interface).  Certified tooling reads through this loader; viewers and
/// runtime consumers enter through ``governance-codex://official``
/// (<see cref="CodexSession"/>).
/// </summary>
internal sealed class CodexPreamble
{
    public required string Title;
    public required string AuthorityRank;
    public required string Issuance;
    public required string BindingScope;
}

internal sealed class CodexSection
{
    public required string Index;
    public required string Title;
    public required string Summary;
}

internal sealed class CodexPrinciple
{
    public required string Id;
    public required string Statement;
    public required bool Binding;
}

internal sealed class CodexArticle
{
    public required string Id;
    public required string Section;
    public required string Subject;
    public required string Rule;
    public string Prohibition = "";
    public string Exception = "";
}

internal sealed class CodexEdict
{
    public required string Id;
    public required string Area;
    public required string Text;
    public required string Immutability;
}

internal sealed class CodexSavings
{
    public required string Mutability;
    public required string Function;
    public required string Amendment;
    public required string OverridingAuthority;
    public required string Interpretation;
    public required string ConflictResolution;
}

internal sealed class CodexSovereign
{
    public required string Id;
    public required string Name;
    public required string Area;
    public required string Rank;
    public required string[] Duties;
    public required string[] Powers;
    public required string[] Prohibitions;
    public required string Basis;
}

/// <summary>One immutable row of an authoritative codex directory or
/// registry table (A223-A228/A334 non-content identity/binding data).</summary>
internal sealed class CodexDirectoryRow
{
    public required (string Key, string Value)[] Fields;

    public string? Get(string name)
    {
        foreach (var (key, value) in Fields)
            if (key == name)
                return value;
        return null;
    }

    public Dictionary<string, string> AsDict() =>
        Fields.ToDictionary(f => f.Key, f => f.Value,
            StringComparer.Ordinal);
}

internal sealed class GovernanceCodex
{
    public required string Schema;
    public required long CodexVersion;
    public required CodexPreamble Preamble;
    public CodexSection[] Sections = Array.Empty<CodexSection>();
    public CodexPrinciple[] Principles = Array.Empty<CodexPrinciple>();
    public CodexArticle[] Articles = Array.Empty<CodexArticle>();
    public CodexEdict[] Edicts = Array.Empty<CodexEdict>();
    public CodexSavings? Savings;
    public CodexSovereign[] Sovereigns = Array.Empty<CodexSovereign>();
    public Dictionary<string, CodexDirectoryRow[]> Directories = new(
        StringComparer.Ordinal);
    public Dictionary<string, CodexDirectoryRow[]> Registries = new(
        StringComparer.Ordinal);
}

internal static class CodexRepository
{
    /// <summary>``format_codex_version``: integer units → legacy
    /// ``int.5digits`` or UTC timestamp.</summary>
    public const long TimestampVersionThreshold = 10_000_000;

    public static string FormatCodexVersion(long version)
    {
        if (version >= TimestampVersionThreshold)
            return DateTimeOffset.FromUnixTimeSeconds(version)
                .UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ssZ");
        return $"{version / CodexVersion.CodexVersionUnit}."
            + $"{version % CodexVersion.CodexVersionUnit:D5}";
    }

    // -- cached load --------------------------------------------------------

    private static readonly object CacheLock = new();
    private static readonly Dictionary<string, GovernanceCodex> CodexCache
        = new(StringComparer.Ordinal);
    private static readonly Dictionary<string, List<string>> ColumnsCache
        = new(StringComparer.Ordinal);

    private static string Str(object? value) => value?.ToString() ?? "";

    private static List<Dictionary<string, object?>> DictRows(
        StageConnection connection, string statement,
        IReadOnlyList<object?>? parameters = null)
    {
        var cursor = connection.Execute(statement, parameters);
        var rows = new List<Dictionary<string, object?>>();
        foreach (var row in cursor.Rows)
        {
            var entry = new Dictionary<string, object?>(
                StringComparer.Ordinal);
            for (var i = 0; i < row.Length; i++)
                entry[cursor.Columns[i]] = row[i];
            rows.Add(entry);
        }
        return rows;
    }

    private static List<string> TableColumns(StageConnection connection,
        string table)
    {
        if (ColumnsCache.TryGetValue(table, out var cached))
            return cached;
        var names = connection.Execute(
            "SELECT column_name FROM information_schema.columns "
            + "WHERE table_schema=? AND table_name=? "
            + "ORDER BY ordinal_position",
            new object?[] { PgDsn.CodexSchema, table }).Rows
            .Select(r => Str(r[0])).ToList();
        ColumnsCache[table] = names;
        return names;
    }

    /// <summary>``_load_codex_tables`` — every authoritative
    /// ``*{suffix}`` table read-only into directory-row form.</summary>
    private static Dictionary<string, CodexDirectoryRow[]>
        LoadCodexTables(StageConnection connection, string suffix)
    {
        var tables = connection.Execute(
            "SELECT table_name FROM information_schema.tables "
            + "WHERE table_schema=? AND table_type='BASE TABLE' "
            + "AND table_name LIKE ? ORDER BY table_name",
            new object?[] { PgDsn.CodexSchema, $"%{suffix}" }).Rows
            .Select(r => Str(r[0])).ToList();
        var result = new Dictionary<string, CodexDirectoryRow[]>(
            StringComparer.Ordinal);
        foreach (var table in tables)
        {
            var columnNames = TableColumns(connection, table);
            var rows = new List<CodexDirectoryRow>();
            foreach (var row in connection.Execute( // sql-ok: catalog names
                $"SELECT {string.Join(", ",
                    columnNames.Select(c => $"\"{c}\""))} "
                + $"FROM \"{table}\" ORDER BY 1").Rows)
                rows.Add(new CodexDirectoryRow
                {
                    Fields = columnNames
                        .Select((name, i) => (name, CanonJson.PyStr(row[i])))
                        .ToArray(),
                });
            result[table] = rows.ToArray();
        }
        return result;
    }

    private static Dictionary<string, string[]> BulkSovereignList(
        StageConnection connection, string table,
        IReadOnlyList<string> sovereignIds)
    {
        var grouped = new Dictionary<string, List<string>>(
            StringComparer.Ordinal);
        foreach (var row in connection.Execute( // sql-ok: fixed allowlist
            $"SELECT sovereign_id, value FROM \"{table}\" "
            + "ORDER BY sovereign_id, position").Rows)
        {
            var key = Str(row[0]);
            if (!grouped.TryGetValue(key, out var list))
                grouped[key] = list = new List<string>();
            list.Add(Str(row[1]));
        }
        return sovereignIds.ToDictionary(id => id,
            id => grouped.TryGetValue(id, out var list)
                ? list.ToArray() : Array.Empty<string>(),
            StringComparer.Ordinal);
    }

    private static GovernanceCodex Load(StageConnection connection)
    {
        var metadata = new Dictionary<string, string>(
            StringComparer.Ordinal);
        foreach (var row in connection.Execute(
            "SELECT key, value FROM metadata").Rows)
            metadata[Str(row[0])] = Str(row[1]);
        var preamble = DictRows(connection,
            "SELECT * FROM preamble WHERE id=1").First();
        var savings = DictRows(connection,
            "SELECT * FROM savings WHERE id=1").First();
        var sovereignIds = connection.Execute(
            "SELECT sovereign_id FROM sovereigns").Rows
            .Select(r => Str(r[0])).ToList();
        var lists = new[] { "sovereign_duties", "sovereign_powers",
            "sovereign_prohibitions" }.ToDictionary(t => t,
            t => BulkSovereignList(connection, t, sovereignIds),
            StringComparer.Ordinal);
        var version = CodexVersion.Units(metadata["codex_version"]);
        var declared = Str(metadata["schema"]).Trim();
        var baseName = declared.Contains("-v", StringComparison.Ordinal)
            ? declared[..declared.LastIndexOf("-v",
                StringComparison.Ordinal)].Trim() : "";
        if (baseName.Length == 0)
            baseName = "gptbridge-governance-codex";
        return new GovernanceCodex
        {
            Schema = $"{baseName}-v{FormatCodexVersion(version)}",
            CodexVersion = version,
            Preamble = new CodexPreamble
            {
                Title = Str(preamble["title"]),
                AuthorityRank = Str(preamble["authority_rank"]),
                Issuance = Str(preamble["issuance"]),
                BindingScope = Str(preamble["binding_scope"]),
            },
            Sections = DictRows(connection,
                "SELECT * FROM sections ORDER BY position")
                .Select(r => new CodexSection
                {
                    Index = Str(r["section_index"]),
                    Title = Str(r["title"]),
                    Summary = Str(r["summary"]),
                }).ToArray(),
            Principles = DictRows(connection,
                "SELECT * FROM principles ORDER BY position")
                .Select(r => new CodexPrinciple
                {
                    Id = Str(r["provision_id"]),
                    Statement = Str(r["statement"]),
                    Binding = Convert.ToInt64(r["binding"]) != 0,
                }).ToArray(),
            Articles = DictRows(connection,
                "SELECT * FROM articles ORDER BY position")
                .Select(r => new CodexArticle
                {
                    Id = Str(r["provision_id"]),
                    Section = Str(r["section_index"]),
                    Subject = Str(r["subject"]),
                    Rule = Str(r["rule"]),
                    Prohibition = Str(r["prohibition"]),
                    Exception = Str(r["exception"]),
                }).ToArray(),
            Edicts = DictRows(connection,
                "SELECT * FROM edicts ORDER BY position")
                .Select(r => new CodexEdict
                {
                    Id = Str(r["provision_id"]),
                    Area = Str(r["area"]),
                    Text = Str(r["edict"]),
                    Immutability = Str(r["immutability"]),
                }).ToArray(),
            Savings = new CodexSavings
            {
                Mutability = Str(savings["mutability"]),
                Function = Str(savings["function"]),
                Amendment = Str(savings["amendment"]),
                OverridingAuthority = Str(savings["overriding_authority"]),
                Interpretation = Str(savings["interpretation"]),
                ConflictResolution = Str(savings["conflict_resolution"]),
            },
            Sovereigns = DictRows(connection,
                "SELECT * FROM sovereigns ORDER BY position")
                .Select(r => new CodexSovereign
                {
                    Id = Str(r["sovereign_id"]),
                    Name = Str(r["name"]),
                    Area = Str(r["area"]),
                    Rank = Str(r["rank"]),
                    Duties = lists["sovereign_duties"][Str(r["sovereign_id"])],
                    Powers = lists["sovereign_powers"][Str(r["sovereign_id"])],
                    Prohibitions =
                        lists["sovereign_prohibitions"][Str(r["sovereign_id"])],
                    Basis = Str(r["basis"]),
                }).ToArray(),
            Directories = LoadCodexTables(connection, "_directory"),
            Registries = LoadCodexTables(connection, "_registry"),
        };
    }

    /// <summary>``load_governance_codex`` — the authoritative codex,
    /// cached by PostgreSQL authority generation (version + source hash)
    /// so governed amendments stay visible while repeated loads within
    /// one report collapse.</summary>
    public static GovernanceCodex LoadGovernanceCodex()
    {
        var state = PgExport.AuthorityState();
        var key = $"{PgDsn.AuthorityUri}|"
            + $"{state.GetValueOrDefault("codex_version")}|"
            + $"{state.GetValueOrDefault("source_sha256")}";
        lock (CacheLock)
        {
            if (CodexCache.TryGetValue(key, out var cached))
                return cached;
            using var store = CodexStore.ForAuthority();
            var codex = Load(store.Connection);
            CodexCache.Clear();
            ColumnsCache.Clear();
            CodexCache[key] = codex;
            return codex;
        }
    }
}
