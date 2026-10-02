using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

/// <summary>
/// SQL↔Git parity and projection refresh — the SQL half of workspace
/// sync (parity with the governed CodexPipeline read lanes; this file
/// owns orchestration only, never SQL semantics).
///
/// The SQL codex advances independently of git. Its git projections
/// (mirror part-*.txt, derived search/index projections, audit
/// manifest) go stale silently, and a sync that merges + audits
/// against stale projections certifies the wrong generation. This lane
/// closes the loop: measure version drift first (cheap), refresh only
/// on measured divergence (the 37s manifest export and the DB-heavy
/// projection rebuilds never run speculatively), then let the normal
/// commit→merge→audit→fast-forward flow certify the refreshed tree.
///
/// Fail-open on tooling, fail-closed on evidence: a missing pipeline,
/// an unreadable projection, or a failed refresh step is reported and
/// the git sync proceeds exactly as before (retried next cycle) — a
/// refresh step never blocks valuable git work, and a corrupt
/// projection is never mistaken for absence.
/// </summary>
internal static class SqlSync
{
    public const string AuthorityFormat = "star-codex-authority/v1";
    private const int PipelineTimeoutMs = 600_000;

    /// <summary>Locate the governed CodexPipeline exe: walk ancestors
    /// for a sibling GPTBridge.CodexPipeline/publish entry. Covers the
    /// publish layout (…/csharp/X/publish, up 2) and the bin layout
    /// (…/X/bin/Debug/net10.0, up 4). Null when absent.</summary>
    internal static string? PipelineExe()
    {
        var cursor = new DirectoryInfo(AppContext.BaseDirectory);
        for (var i = 0; i < 6 && cursor is not null; i++)
        {
            var candidate = Path.Combine(cursor.FullName,
                "GPTBridge.CodexPipeline", "publish",
                "GPTBridge.CodexPipeline.exe");
            if (File.Exists(candidate))
                return candidate;
            cursor = cursor.Parent;
        }
        return null;
    }

    private static JsonObject? RunPipeline(string exe, string[] args)
    {
        var run = Git.Exec(exe, null, args, PipelineTimeoutMs);
        if (run.TimedOut || run.Code != 0)
            return null;
        var text = run.Stdout.Trim();
        var start = text.IndexOf('{');
        if (start < 0)
            return null;
        try
        {
            return JsonNode.Parse(text[start..]) as JsonObject;
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private static string? MirrorVersion(string projectRoot)
    {
        var path = Path.Combine(projectRoot, "governance_rule",
            "codex", "governance_codex.zh-TW.part-1.txt");
        string head;
        try
        {
            using var stream = new FileStream(path, FileMode.Open,
                FileAccess.Read, FileShare.ReadWrite);
            var buffer = new byte[4096];
            var read = stream.Read(buffer, 0, buffer.Length);
            head = System.Text.Encoding.UTF8.GetString(buffer, 0, read);
        }
        catch (IOException) { return null; }
        catch (UnauthorizedAccessException) { return null; }
        const string marker = "\"codex_version\":\"";
        var at = head.IndexOf(marker, StringComparison.Ordinal);
        if (at < 0)
            return null;
        var valueStart = at + marker.Length;
        var end = head.IndexOf('"', valueStart);
        if (end < 0)
            return null;
        return head[valueStart..end];
    }

    /// <summary>Read-only SQL↔Git parity: authority version vs the
    /// checked-in mirror version. Never throws; unknown sides are
    /// reported, never guessed.</summary>
    public static JsonObject Parity(string projectRoot)
    {
        var report = new JsonObject
        {
            ["format"] = AuthorityFormat,
            ["sql_version"] = null,
            ["sql_tables"] = null,
            ["sql_rows"] = null,
            ["mirror_version"] = null,
            ["in_sync"] = null,
            ["pipeline"] = null,
        };
        var exe = PipelineExe();
        if (exe is null)
        {
            report["pipeline"] = "codex-pipeline-unavailable";
            report["note"] = "governed SQL read lane absent; " +
                "parity unknown, sync proceeds git-only";
            return report;
        }
        report["pipeline"] = exe;
        var authority = RunPipeline(exe, new[] { "--authority-state" });
        if (authority is null)
        {
            report["note"] = "authority-state unreadable; " +
                "parity unknown, sync proceeds git-only";
            return report;
        }
        var sqlVersion =
            authority["codex_version"]?.GetValue<string>();
        report["sql_version"] = sqlVersion is null
            ? null : JsonValue.Create(sqlVersion);
        report["sql_tables"] = authority["table_count"] is JsonNode tables
            ? JsonValue.Create(tables.GetValue<int>()) : null;
        report["sql_rows"] = authority["row_count"] is JsonNode rows
            ? JsonValue.Create(rows.GetValue<int>()) : null;
        var mirror = MirrorVersion(projectRoot);
        report["mirror_version"] = mirror is null
            ? null : JsonValue.Create(mirror);
        if (sqlVersion is not null && mirror is not null)
            report["in_sync"] = JsonValue.Create(sqlVersion == mirror);
        else
            report["note"] = "one side unreadable; parity unknown, " +
                "sync proceeds git-only";
        return report;
    }

    /// <summary>Refresh SQL-derived git projections when (and only
    /// when) the mirror lags the authority: repair projections →
    /// re-render mirror → refresh audit manifest. Returns the outcome;
    /// a failed step reports and leaves git sync to proceed (retried
    /// next cycle).</summary>
    public static JsonObject RefreshIfStale(string projectRoot)
    {
        var parity = Parity(projectRoot);
        var outcome = new JsonObject
        {
            ["format"] = AuthorityFormat,
            ["refreshed"] = JsonValue.Create(false),
            ["parity_before"] = parity,
        };
        if (parity["in_sync"]?.GetValue<bool>() == true)
        {
            outcome["reason"] = "in-sync";
            return outcome;
        }
        if (parity["sql_version"] is null)
        {
            outcome["reason"] = "authority-unknown";
            outcome["note"] = parity["note"]?.GetValue<string>()
                ?? "parity unknown; refresh skipped";
            return outcome;
        }
        var exe = PipelineExe();
        if (exe is null)
        {
            outcome["reason"] = "codex-pipeline-unavailable";
            return outcome;
        }
        var steps = new JsonObject();
        foreach (var step in new[] { "--repair-projections", "--mirror-zh" })
        {
            var run = Git.Exec(exe, projectRoot,
                new[] { step }, PipelineTimeoutMs);
            var ok = !run.TimedOut && run.Code == 0;
            steps[step[2..]] = JsonValue.Create(ok);
            if (!ok)
            {
                outcome["steps"] = steps;
                outcome["error"] =
                    $"pipeline {step} failed: " +
                    $"{(run.TimedOut ? "timeout" : $"exit {run.Code}")}";
                return outcome;
            }
        }
        try
        {
            ManifestExport.Refresh(projectRoot);
            steps["manifest-export"] = JsonValue.Create(true);
        }
        catch (Exception error)
        {
            steps["manifest-export"] = JsonValue.Create(false);
            outcome["steps"] = steps;
            outcome["error"] =
                $"manifest export failed: {error.Message}";
            return outcome;
        }
        outcome["steps"] = steps;
        var after = Parity(projectRoot);
        outcome["parity_after"] = after;
        outcome["refreshed"] =
            after["in_sync"]?.GetValue<bool>() == true;
        if (!(bool)outcome["refreshed"]!)
            outcome["note"] = "projections rebuilt but versions " +
                "still diverge; sync proceeds git-only, retried " +
                "next cycle";
        return outcome;
    }
}
