using System.Security.Cryptography;

namespace GPTBridge.CodexPipeline;

internal static partial class UpdatePipeline
{
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
