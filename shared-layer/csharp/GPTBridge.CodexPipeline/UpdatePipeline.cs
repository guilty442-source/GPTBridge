using System.Security.Cryptography;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Automatic Codex update pipeline — isolate, change, wire, release,
/// refresh — direct port of codex_update_pipeline.py.
///
/// Governor-directed order (2026-09-17): 先隔離 → 後執行變更 → 再執行接線
/// → 再解除隔離 → 前端連線刷新.
///
/// A382/A488 non-disruptive amendment flow; A537/A538 automatic
/// synchronization of updates, the five Chinese mirror parts and
/// architecture artifacts; A383 isolation; A446 bounded stages.
/// The module never runs implicitly: ``apply=false`` stops after the
/// change was validated in isolation; only the governed executor runs
/// ``apply=true`` and it writes only inside the explicit codex root.
/// </summary>
internal class CodexUpdateError : Exception
{
    public string Phase { get; }
    public string Reason { get; }

    public CodexUpdateError(string phase, string reason)
        : base($"{phase}: {reason}")
    {
        Phase = phase;
        Reason = reason;
    }
}

internal sealed record PhaseRecord(
    string Phase, bool Ok, string Detail,
    IReadOnlyDictionary<string, object?>? Evidence = null);

internal sealed class IsolatedStage
{
    public required string FenceId;
    public required string CodexRoot;
    public required string StagingRoot;
    public required string Database;
    public required string[] Parts;
    public required string SourceVersion;
    public required Dictionary<string, string> SourceDigests;
    public List<string[]> SourceFkViolations = new();
}

internal sealed record AutoUpdateResult(
    bool Ok, bool Applied, string Version, PhaseRecord[] Phases,
    bool RefreshPending = false);

internal static class UpdatePipeline
{
    public const string UpdateFlowIdentity =
        "A382/A488/A537/A538-isolate-change-wire-release-refresh";
    public static readonly string[] UpdatePhases =
    {
        "isolate", "execute-change", "wire", "release-isolation",
        "frontend-refresh",
    };
    public const string DatabaseName = "governance_codex.sql";
    public const string IsolationMarker = "STAGE_ISOLATION.json";
    public const string ReleasedMarker = "STAGE_RELEASED.json";
    public const string RefreshRequest = "FRONTEND_REFRESH.json";
    public static readonly string[] FrontendRefreshChannels =
        { "frontend", "backend", "ui-projection" };

    public static string CanonicalCodexRoot() =>
        Path.Combine(Repo.Root(), "governance_rule", "codex");

    private static string Digest(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream))
            .ToLowerInvariant();
    }

    private static void SetReadOnly(string path, bool readOnly = true)
    {
        var info = new FileInfo(path);
        info.IsReadOnly = readOnly;
    }

    /// <summary>temp → fsync → replace → read-only, never leaving a
    /// half file.</summary>
    private static void AtomicReplace(string source, string target)
    {
        var temporary = target + ".staging-tmp";
        using (var handle = new FileStream(temporary, FileMode.Create,
            FileAccess.Write, FileShare.None))
        {
            var bytes = File.ReadAllBytes(source);
            handle.Write(bytes);
            handle.Flush(flushToDisk: true);
        }
        if (File.Exists(target))
            SetReadOnly(target, false);
        // Windows readers may hold the target without delete sharing for
        // a short window (indexers, watchers); retry before failing.
        Exception? lastError = null;
        for (var attempt = 0; attempt < 10; attempt++)
        {
            try
            {
                File.Move(temporary, target, overwrite: true);
                lastError = null;
                break;
            }
            catch (Exception error) when (error is IOException
                or UnauthorizedAccessException)
            {
                lastError = error;
                Thread.Sleep(500);
            }
        }
        if (lastError is not null)
        {
            try { File.Delete(temporary); }
            catch (IOException) { }
            if (File.Exists(target))
                SetReadOnly(target, true);
            throw lastError;
        }
        SetReadOnly(target, true);
    }

    private static void WriteJson(string path,
        IReadOnlyDictionary<string, object?> payload) =>
        Repo.AtomicJson(path, payload);

    private static string ReadVersion(string database)
    {
        using var store = AmendmentContract.OpenCodexStore(database);
        var row = store.Connection.Execute(
            "SELECT value FROM metadata WHERE key='codex_version'")
            .FetchOne();
        return row?[0]?.ToString()?.Trim() ?? "";
    }

    /// <summary>Baseline: the live generation's acknowledged legacy
    /// violations.</summary>
    private static List<string[]> SourceForeignKeyViolations(
        string database)
    {
        using var store = AmendmentContract.OpenCodexStore(database);
        return UpdateValidation.ForeignKeyViolations(store.Connection);
    }

    private static bool SamePath(string left, string right) =>
        string.Equals(Path.GetFullPath(left), Path.GetFullPath(right),
            StringComparison.OrdinalIgnoreCase);

    /// <summary>Phase 1: copy the live generation into a
    /// non-authoritative isolation.</summary>
    public static IsolatedStage IsolateGeneration(string codexRoot,
        string stagingRoot)
    {
        var root = Path.GetFullPath(codexRoot);
        var staging = Path.GetFullPath(stagingRoot);
        if (!Directory.Exists(root))
            throw new CodexUpdateError("isolate",
                $"codex root not found: {root}");
        if (staging == root
            || staging.StartsWith(
                root.TrimEnd(Path.DirectorySeparatorChar)
                + Path.DirectorySeparatorChar,
                StringComparison.OrdinalIgnoreCase))
            throw new CodexUpdateError("isolate",
                "staging root must be outside the codex root");
        var database = Path.Combine(root, "data", DatabaseName);
        if (Directory.Exists(staging) && new[]
            { DatabaseName, IsolationMarker, ReleasedMarker }
            .Any(marker => File.Exists(Path.Combine(staging, marker))))
        {
            // Parity: <name>-stale-<UTC>-<8hex> beside the staging root.
            var residue = Path.Combine(
                Path.GetDirectoryName(staging) ?? ".",
                $"{Path.GetFileName(staging)}-stale-"
                + DateTime.UtcNow.ToString("yyyyMMddTHHmmss")
                + $"-{Guid.NewGuid().ToString("N")[..8]}");
            try { Directory.Move(staging, residue); }
            catch (IOException error)
            {
                throw new CodexUpdateError("isolate",
                    $"stale staging residue not quarantined: {error}");
            }
        }
        Directory.CreateDirectory(staging);
        var isolatedDatabase = Path.Combine(staging, DatabaseName);
        if (File.Exists(database))
            File.Copy(database, isolatedDatabase, overwrite: true);
        else if (SamePath(root, CanonicalCodexRoot()))
            // Canonical root post-cutover (A173): the live authority is
            // the PostgreSQL schema, so the staged working copy is a
            // non-authoritative export of it.
            PgExport.ExportPostgresqlCodex(isolatedDatabase);
        else
            throw new CodexUpdateError("isolate",
                $"codex database not found: {database}");
        SetReadOnly(isolatedDatabase, false);
        var parts = new List<string>();
        foreach (var name in ChineseMirror.PartNames)
        {
            var source = Path.Combine(root, name);
            if (!File.Exists(source))
                throw new CodexUpdateError("isolate",
                    $"mirror part not found: {name}");
            var target = Path.Combine(staging, name);
            File.Copy(source, target, overwrite: true);
            SetReadOnly(target, false);
            parts.Add(target);
        }
        var digests = new Dictionary<string, string>(
            StringComparer.Ordinal)
        { [DatabaseName] = Digest(isolatedDatabase) };
        foreach (var name in ChineseMirror.PartNames)
            digests[name] = Digest(Path.Combine(root, name));
        var fenceId = Guid.NewGuid().ToString();
        var stage = new IsolatedStage
        {
            FenceId = fenceId,
            CodexRoot = root,
            StagingRoot = staging,
            Database = isolatedDatabase,
            Parts = parts.ToArray(),
            SourceVersion = ReadVersion(isolatedDatabase),
            SourceDigests = digests,
            SourceFkViolations =
                SourceForeignKeyViolations(isolatedDatabase),
        };
        WriteJson(Path.Combine(staging, IsolationMarker),
            new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["fence_id"] = fenceId,
                ["flow"] = UpdateFlowIdentity,
                ["state"] = "staged-non-authoritative",
                ["source_version"] = stage.SourceVersion,
                ["source_digests"] = digests.ToDictionary(
                    p => p.Key, p => (object?)p.Value,
                    StringComparer.Ordinal),
            });
        return stage;
    }

    private static void NormalizeVersion(string database,
        string? version)
    {
        if (string.IsNullOrEmpty(version))
            return;
        using var store = AmendmentContract.OpenCodexStore(database,
            writeBack: true);
        store.Connection.Execute(
            "UPDATE metadata SET value=? WHERE key='codex_version'",
            new object?[] { version });
        store.Connection.Commit();
    }

    /// <summary>A537/A538 architecture-artifact atomicity gate
    /// (fail-closed).</summary>
    public static string[] ArchitectureSyncErrors()
    {
        Dictionary<string, object?> report;
        try
        {
            report = ArchitectureDocs.Report(Repo.Root());
        }
        catch (Exception error)
        {
            return new[] { "architecture document report unavailable: "
                + error.GetType().Name };
        }
        var errors = new List<string>();
        if (report.TryGetValue("errors", out var errs)
            && errs is System.Collections.IEnumerable errorItems)
            foreach (var item in errorItems)
                errors.Add(item?.ToString() ?? "");
        if (report.TryGetValue("gaps", out var gapList)
            && gapList is System.Collections.IEnumerable gaps)
            foreach (var gap in gaps)
            {
                if (gap is IDictionary<string, object?> map
                    && map.TryGetValue("reason", out var reason))
                    errors.Add(reason?.ToString() ?? "");
                else
                    errors.Add(gap?.ToString() ?? "");
            }
        return errors.ToArray();
    }

    private static string[] StagedErrors(IsolatedStage stage,
        string? version) =>
        UpdateValidation.StagedGenerationErrors(stage.Database,
            version: version?.Trim().Length > 0 ? version.Trim() : null,
            baselineViolations: stage.SourceFkViolations);

    private static List<string> ValidateAndRender(IsolatedStage stage,
        string? version)
    {
        var errors = new List<string>(StagedErrors(stage, version));
        if (errors.Count > 0)
            return errors;
        errors.AddRange(ArchitectureSyncErrors());
        if (errors.Count > 0)
            return errors;
        try
        {
            MirrorWriter.RenderMirrorParts(stage.Database,
                stage.StagingRoot, templateRoot: stage.CodexRoot);
            MirrorWriter.RecordMirrorQualityEvidence(stage.Database,
                stage.StagingRoot);
            MirrorWriter.RenderMirrorParts(stage.Database,
                stage.StagingRoot, templateRoot: stage.CodexRoot);
            errors.AddRange(MirrorWriter.MirrorErrors(stage.Database,
                stage.StagingRoot));
        }
        catch (Exception error)
        {
            errors.Add($"staged mirror rendering failed: {error}");
        }
        return errors;
    }

    /// <summary>Rebind a prepared successor's derived projections to
    /// the staged codex_version.  Rebuild failures return as rejection
    /// evidence — never an uncaught exception.</summary>
    private static (Dictionary<string, object?> Evidence,
        string[] Errors) RebindProjections(IsolatedStage stage,
            IReadOnlyDictionary<string, string>? bookkeeping)
    {
        try
        {
            return (GenerationProjections.RebuildGenerationBookkeeping(
                stage.Database,
                changeId: bookkeeping?.TryGetValue("change_id",
                    out var ci) == true ? ci : "",
                changeScope: bookkeeping?.TryGetValue("change_scope",
                    out var cs) == true ? cs : "amendment-execution",
                summary: bookkeeping?.TryGetValue("summary",
                    out var su) == true ? su : ""), Array.Empty<string>());
        }
        catch (Exception error)
        {
            return (new Dictionary<string, object?>(),
                new[] { $"generation bookkeeping rebuild failed: {error}" });
        }
    }

    /// <summary>Phase 2: apply the prepared successor and validate the
    /// staged change.</summary>
    public static PhaseRecord ExecuteStagedChange(IsolatedStage stage,
        string? preparedDatabase = null, string? version = null,
        IReadOnlyDictionary<string, string>? bookkeeping = null)
    {
        if (preparedDatabase is not null)
        {
            var prepared = Path.GetFullPath(preparedDatabase);
            if (!File.Exists(prepared))
                throw new CodexUpdateError("execute-change",
                    $"prepared database not found: {prepared}");
            SetReadOnly(stage.Database, false);
            File.Copy(prepared, stage.Database, overwrite: true);
        }
        NormalizeVersion(stage.Database, version);
        var errors = new List<string>();
        var bookkeepingEvidence = new Dictionary<string, object?>();
        if (preparedDatabase is not null)
        {
            // A prepared successor must pass basic generation integrity
            // before any derived projection is rebound onto it.
            errors.AddRange(StagedErrors(stage, version));
            if (errors.Count == 0)
            {
                var (evidence, rebindErrors) =
                    RebindProjections(stage, bookkeeping);
                bookkeepingEvidence = evidence;
                errors.AddRange(rebindErrors);
            }
        }
        if (errors.Count == 0)
            errors = ValidateAndRender(stage, version);
        return new PhaseRecord("execute-change", errors.Count == 0,
            errors.Count == 0
                ? "staged generation validated"
                : "staged generation rejected",
            new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["fence_id"] = stage.FenceId,
                ["version"] = version ?? stage.SourceVersion,
                ["errors"] = errors.Cast<object?>().ToList(),
                ["bookkeeping"] = bookkeepingEvidence,
            });
    }

    /// <summary>Phase 3: atomically publish the staged database and
    /// mirror parts.</summary>
    public static PhaseRecord WireGeneration(IsolatedStage stage)
    {
        var published = new Dictionary<string, string>(
            StringComparer.Ordinal);
        foreach (var name in ChineseMirror.PartNames)
            if (!File.Exists(Path.Combine(stage.StagingRoot, name)))
                throw new CodexUpdateError("wire",
                    $"staged mirror part missing: {name}");
        // The PostgreSQL authority is the single live codex: it may only
        // be wired from the canonical codex root.  Synthetic roots
        // (tests, rehearsals) must never republish the shared authority.
        var canonical = SamePath(stage.CodexRoot, CanonicalCodexRoot());
        object? postgresState;
        object? postgresParity;
        if (canonical)
        {
            postgresState = PgImport.ImportCodexArtifact(stage.Database);
            var parity = PgExport.VerifySqlParity(stage.Database);
            postgresParity = parity;
            if ((parity.TryGetValue("result", out var result)
                    ? result?.ToString() : "") != "PASS")
                throw new CodexUpdateError("wire",
                    "PostgreSQL codex parity verification failed");
        }
        else
        {
            postgresState = new Dictionary<string, object?>
            { ["skipped"] = "non-canonical-codex-root" };
            postgresParity = new Dictionary<string, object?>
            { ["result"] = "SKIPPED" };
        }
        var publishedDatabase = Path.Combine(stage.CodexRoot, "data",
            DatabaseName);
        if (canonical && !File.Exists(publishedDatabase))
        {
            // A173 residue deletion: the canonical codex root no longer
            // carries a sqlite file — the PostgreSQL import above IS the
            // publication.
        }
        else
        {
            AtomicReplace(stage.Database, publishedDatabase);
        }
        published[DatabaseName] = Digest(stage.Database);
        foreach (var name in ChineseMirror.PartNames)
        {
            var stagedPart = Path.Combine(stage.StagingRoot, name);
            AtomicReplace(stagedPart,
                Path.Combine(stage.CodexRoot, name));
            published[name] =
                Digest(Path.Combine(stage.CodexRoot, name));
        }
        return new PhaseRecord("wire", true,
            "published generation wired atomically",
            new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["fence_id"] = stage.FenceId,
                ["published"] = published.ToDictionary(
                    p => p.Key, p => (object?)p.Value,
                    StringComparer.Ordinal),
                ["postgresql"] = postgresState,
                ["postgresql_parity"] = postgresParity,
            });
    }

    /// <summary>The published generation to verify: the codex-root
    /// artifact when it exists, otherwise the staged copy that was
    /// imported into PostgreSQL.</summary>
    private static string PublishedDatabase(IsolatedStage stage)
    {
        var published = Path.Combine(stage.CodexRoot, "data",
            DatabaseName);
        return File.Exists(published) ? published : stage.Database;
    }

    private static string PublishedVersion(IsolatedStage stage)
    {
        if (SamePath(stage.CodexRoot, CanonicalCodexRoot()))
        {
            try
            {
                var state = PgExport.AuthorityState();
                var version = state.TryGetValue("codex_version",
                    out var v) ? v?.ToString() ?? "" : "";
                if (version.Length > 0)
                    return version;
            }
            catch (Exception) { }
        }
        return ReadVersion(Path.Combine(stage.CodexRoot, "data",
            DatabaseName));
    }

    private static string[] PublishedGenerationErrors(
        IsolatedStage stage)
    {
        var codexRoot = stage.CodexRoot;
        var database = PublishedDatabase(stage);
        var errors = new List<string>(
            UpdateValidation.StagedGenerationErrors(database,
                baselineViolations: stage.SourceFkViolations));
        var publishedPaths = ChineseMirror.PartNames
            .Select(name => Path.Combine(codexRoot, name)).ToList();
        if (database != stage.Database)
            // The codex-root artifact exists and is a published file;
            // the staged scratch copy is exempt from the read-only
            // check.
            publishedPaths.Insert(0, database);
        foreach (var path in publishedPaths)
            if (!new FileInfo(path).IsReadOnly)
                errors.Add(
                    $"published artifact is writable: {Path.GetFileName(path)}");
        errors.AddRange(MirrorWriter.MirrorErrors(database, codexRoot,
            label: "published"));
        return errors.ToArray();
    }

    /// <summary>Phase 4: verify the published generation and release
    /// the fence.</summary>
    public static PhaseRecord ReleaseIsolation(IsolatedStage stage)
    {
        var errors = PublishedGenerationErrors(stage);
        var marker = Path.Combine(stage.StagingRoot, IsolationMarker);
        if (File.Exists(marker))
            File.Delete(marker);
        WriteJson(Path.Combine(stage.StagingRoot, ReleasedMarker),
            new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["fence_id"] = stage.FenceId,
                ["flow"] = UpdateFlowIdentity,
                ["state"] = "released",
                ["version"] = PublishedVersion(stage),
                ["errors"] = errors.Cast<object?>().ToList(),
            });
        return new PhaseRecord("release-isolation", errors.Length == 0,
            errors.Length == 0
                ? "isolation fence released"
                : "release verification failed",
            new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["fence_id"] = stage.FenceId,
                ["errors"] = errors.Cast<object?>().ToList(),
            });
    }

    /// <summary>Phase 5: emit the frontend connection refresh
    /// request.</summary>
    public static PhaseRecord FrontendRefresh(IsolatedStage stage,
        Action<IReadOnlyDictionary<string, object?>>? notify = null)
    {
        var version = PublishedVersion(stage);
        var payload = new Dictionary<string, object?>(
            StringComparer.Ordinal)
        {
            ["contract"] = "codex-generation-published",
            ["flow"] = UpdateFlowIdentity,
            ["fence_id"] = stage.FenceId,
            ["version"] = version,
            ["channels"] = FrontendRefreshChannels
                .Cast<object?>().ToList(),
            ["action"] = "reconnect-and-reload-authority",
        };
        var delivered = false;
        if (notify is not null)
        {
            notify(payload);
            delivered = true;
        }
        WriteJson(Path.Combine(stage.StagingRoot, RefreshRequest),
            payload);
        return new PhaseRecord("frontend-refresh", true,
            delivered
                ? "refresh request delivered through the governed hook"
                : "refresh request recorded; no governed notification "
                    + "hook wired",
            new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["version"] = version,
                ["delivered"] = delivered,
            });
    }

    private static void DiscardStaging(IsolatedStage stage)
    {
        try
        {
            Directory.Delete(stage.StagingRoot, recursive: true);
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    /// <summary>Run the directed update order; stop fail-closed at the
    /// first failure.</summary>
    public static AutoUpdateResult RunAutoUpdate(string codexRoot,
        string stagingRoot, string? preparedDatabase = null,
        string? version = null, bool apply = false,
        Action<IReadOnlyDictionary<string, object?>>? notify = null,
        IReadOnlyDictionary<string, string>? bookkeeping = null)
    {
        var phases = new List<PhaseRecord>();
        IsolatedStage stage;
        try
        {
            stage = IsolateGeneration(codexRoot, stagingRoot);
        }
        catch (CodexUpdateError error)
        {
            phases.Add(new PhaseRecord(error.Phase, false,
                error.Reason));
            return new AutoUpdateResult(false, false, "",
                phases.ToArray());
        }
        phases.Add(new PhaseRecord("isolate", true,
            "live generation isolated; staged copy is "
            + "non-authoritative",
            new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["fence_id"] = stage.FenceId,
                ["source_version"] = stage.SourceVersion,
            }));
        var change = ExecuteStagedChange(stage,
            preparedDatabase: preparedDatabase, version: version,
            bookkeeping: bookkeeping);
        phases.Add(change);
        if (!change.Ok)
        {
            DiscardStaging(stage);
            return new AutoUpdateResult(false, false,
                stage.SourceVersion, phases.ToArray());
        }
        return FinishUpdate(stage, phases, version, apply, notify);
    }

    /// <summary>Dry-run stop, or wire + release + frontend refresh
    /// when applying.</summary>
    private static AutoUpdateResult FinishUpdate(IsolatedStage stage,
        List<PhaseRecord> phases, string? version, bool apply,
        Action<IReadOnlyDictionary<string, object?>>? notify)
    {
        var targetVersion = version ?? stage.SourceVersion;
        if (!apply)
        {
            DiscardStaging(stage);
            return new AutoUpdateResult(true, false, targetVersion,
                phases.ToArray());
        }
        var wire = WireGeneration(stage);
        phases.Add(wire);
        if (!wire.Ok)
            return new AutoUpdateResult(false, true, targetVersion,
                phases.ToArray());
        var release = ReleaseIsolation(stage);
        phases.Add(release);
        var refresh = FrontendRefresh(stage, notify);
        phases.Add(refresh);
        var delivered = refresh.Evidence is not null
            && refresh.Evidence.TryGetValue("delivered", out var d)
            && d is true;
        return new AutoUpdateResult(wire.Ok && release.Ok, true,
            targetVersion, phases.ToArray(),
            RefreshPending: !delivered);
    }
}
