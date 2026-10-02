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

internal static partial class UpdatePipeline
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

    internal static void SetReadOnly(string path, bool readOnly = true)
    {
        var info = new FileInfo(path);
        info.IsReadOnly = readOnly;
    }

    /// <summary>temp → fsync → replace → read-only, never leaving a
    /// half file.</summary>
    internal static void AtomicReplace(string source, string target)
    {
        // Unique temp name: concurrent publishers (update pipeline and
        // codex-maintenance mirror refresh) may target the same file;
        // a shared temp name lets one caller truncate or move the
        // other's bytes.
        var temporary = target + ".staging-tmp-"
            + Guid.NewGuid().ToString("N")[..8];
        using (var handle = new FileStream(temporary, FileMode.Create,
            FileAccess.Write, FileShare.None))
        {
            var bytes = File.ReadAllBytes(source);
            handle.Write(bytes);
            handle.Flush(flushToDisk: true);
        }
        // Windows readers may hold the target without delete sharing for
        // a while (indexers, watchers, repo-wide scans), and a racing
        // publisher may re-seal the read-only attribute between our
        // clear and our move.  Re-clear inside the retry loop and allow
        // a generous window before failing.
        Exception? lastError = null;
        for (var attempt = 0; attempt < 60; attempt++)
        {
            try
            {
                if (File.Exists(target)
                    && new FileInfo(target).IsReadOnly)
                    SetReadOnly(target, false);
                File.Move(temporary, target, overwrite: true);
                lastError = null;
                break;
            }
            catch (Exception error) when (error is IOException
                or UnauthorizedAccessException)
            {
                lastError = error;
                Thread.Sleep(1000);
            }
        }
        if (lastError is not null)
        {
            try { File.Delete(temporary); }
            catch (IOException) { }
            if (File.Exists(target))
                SetReadOnly(target, true);
            throw new IOException(
                $"atomic-replace failed: {temporary} -> {target}: "
                + lastError.Message, lastError);
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

    /// <summary>Version and baseline (acknowledged legacy) foreign-key
    /// violations of the live generation — read in one store session so
    /// the artifact materializes only once.</summary>
    private static (string Version, List<string[]> Violations)
        SourceIdentity(string database)
    {
        using var store = AmendmentContract.OpenCodexStore(database);
        var row = store.Connection.Execute(
            "SELECT value FROM metadata WHERE key='codex_version'")
            .FetchOne();
        var version = row?[0]?.ToString()?.Trim() ?? "";
        return (version,
            UpdateValidation.ForeignKeyViolations(store.Connection));
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
        var (sourceVersion, sourceViolations) =
            SourceIdentity(isolatedDatabase);
        var stage = new IsolatedStage
        {
            FenceId = fenceId,
            CodexRoot = root,
            StagingRoot = staging,
            Database = isolatedDatabase,
            Parts = parts.ToArray(),
            SourceVersion = sourceVersion,
            SourceDigests = digests,
            SourceFkViolations = sourceViolations,
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

}
