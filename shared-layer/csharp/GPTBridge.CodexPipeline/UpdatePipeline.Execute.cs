using System.Security.Cryptography;

namespace GPTBridge.CodexPipeline;

internal static partial class UpdatePipeline
{
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
}
