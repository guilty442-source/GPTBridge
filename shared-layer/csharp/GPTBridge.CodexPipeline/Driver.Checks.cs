using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.CodexPipeline;

internal static partial class Driver
{
    private static string? ManifestPathFor(
        CodexAmendmentRequestLedger ledger, string requestId)
    {
        var record = ledger.LoadRecord(requestId)
            ?? new Dictionary<string, object?>();
        if (record.TryGetValue("history", out var hist)
            && hist is List<object?> entries)
            for (var i = entries.Count - 1; i >= 0; i--)
            {
                if (entries[i] is not IDictionary<string, object?> entry
                    || !entry.TryGetValue("evidence", out var ev)
                    || ev is not IDictionary<string, object?> evidence)
                    continue;
                if (evidence.TryGetValue("manifest_path", out var mp)
                    && mp is not null)
                {
                    var path = mp.ToString() ?? "";
                    if (File.Exists(path))
                        return path;
                }
            }
        var fallback = Path.Combine(ledger.Root, CandidatesDirname,
            $"{requestId}.candidate-manifest.json");
        return File.Exists(fallback) ? fallback : null;
    }

    /// <summary>The standard five-sovereign check callables for one
    /// request — deterministic standard-charter verifications of the
    /// staged request/candidate.</summary>
    public static Dictionary<string, Func<Dictionary<string, object?>>>
        DefaultSovereignChecks(
            string requestPath,
            CodexAmendmentRequestLedger ledger,
            string? manifestPath = null,
            string? candidatePath = null,
            string? sourceDatabase = null)
    {
        string? manifest;
        if (manifestPath is null)
        {
            var requestId = "";
            try
            {
                requestId = Lifecycle.LoadAmendmentRequest(requestPath)
                    .RequestId;
            }
            catch (AmendmentLifecycleError) { }
            manifest = requestId.Length > 0
                ? ManifestPathFor(ledger, requestId) : null;
        }
        else
        {
            manifest = manifestPath;
        }
        string? candidate = candidatePath
            ?? (manifest is not null
                ? Path.Combine(Path.GetDirectoryName(manifest)!,
                    Path.GetFileName(manifest)!
                        .Replace(".candidate-manifest.json", ".sql",
                            StringComparison.Ordinal))
                : null);
        if (candidate is not null
            && !candidate.EndsWith(".sql", StringComparison.Ordinal))
            candidate = null;
        string requestIdFinal;
        string[] scope;
        try
        {
            var request = Lifecycle.LoadAmendmentRequest(requestPath);
            requestIdFinal = request.RequestId;
            scope = request.Scope;
        }
        catch (AmendmentLifecycleError)
        {
            requestIdFinal = "";
            scope = Array.Empty<string>();
        }
        return new Dictionary<string,
            Func<Dictionary<string, object?>>>(StringComparer.Ordinal)
        {
            ["decision-sovereign"] =
                DecisionCheck(requestPath, ledger),
            ["permission-sovereign"] =
                PermissionCheck(requestPath, sourceDatabase),
            ["system-runtime-sovereign"] =
                RuntimeCheck(requestPath, sourceDatabase),
            ["automation-sovereign"] =
                AutomationCheck(manifest, candidate, ledger),
            ["xingcheng"] =
                SovereignAudit.BuildXingchengAssistantCoreCheck(
                    requestIdFinal, scope),
        };
    }

    /// <summary>Discover staged request artifacts and their ledger
    /// states.</summary>
    public static List<Dictionary<string, object?>> ScanRequests(
        IEnumerable<string>? intakeDirs = null,
        CodexAmendmentRequestLedger? ledger = null)
    {
        ledger ??= new CodexAmendmentRequestLedger();
        var directories = intakeDirs?.ToArray() ?? IntakeDirs();
        var found = new Dictionary<string, Dictionary<string, object?>>(
            StringComparer.Ordinal);
        foreach (var directory in directories)
        {
            if (!Directory.Exists(directory))
                continue;
            foreach (var path in Directory.GetFiles(directory,
                IntakeGlob).OrderBy(p => p, StringComparer.Ordinal))
            {
                AmendmentRequest request;
                try
                {
                    request = Lifecycle.LoadAmendmentRequest(path);
                }
                catch (AmendmentLifecycleError error)
                {
                    found[path] = new Dictionary<string, object?>(
                        StringComparer.Ordinal)
                    {
                        ["path"] = path,
                        ["request_id"] =
                            Path.GetFileNameWithoutExtension(path),
                        ["valid"] = false,
                        ["error"] = error.Message,
                        ["state"] = "",
                    };
                    continue;
                }
                var record = ledger.LoadRecord(request.RequestId);
                found[request.RequestId] =
                    new Dictionary<string, object?>(
                        StringComparer.Ordinal)
                    {
                        ["path"] = path,
                        ["request_id"] = request.RequestId,
                        ["valid"] = true,
                        ["state"] = record?.TryGetValue("state",
                            out var s) == true
                            ? s?.ToString() ?? "" : "",
                        ["scope"] = request.Scope
                            .Cast<object?>().ToList(),
                    };
            }
        }
        return found.Values
            .OrderBy(item => item["request_id"]?.ToString(),
                StringComparer.Ordinal)
            .ToList();
    }

    /// <summary>Newest recorded audit verdict for ``requestId`` from
    /// the append-only audit ledger — its presence there is itself the
    /// recording evidence the executor revalidates.</summary>
    private static Dictionary<string, object?>? LatestAuditResult(
        string requestId)
    {
        string[] lines;
        try
        {
            lines = File.ReadAllLines(SovereignAudit.AuditLedgerPath());
        }
        catch (IOException) { return null; }
        Dictionary<string, object?>? latest = null;
        foreach (var line in lines)
        {
            var trimmed = line.Trim();
            if (trimmed.Length == 0)
                continue;
            JsonNode? entry;
            try { entry = JsonNode.Parse(trimmed); }
            catch (JsonException) { continue; }
            if (entry is JsonObject obj
                && Repo.Str(obj, "amendment_id") == requestId)
                latest = (Dictionary<string, object?>)
                    Repo.ToPlain(obj)!;
        }
        if (latest is null
            || !(latest.TryGetValue("ok", out var ok) && ok is true))
            return null;
        latest["audit_recorded"] = true;
        return latest;
    }

    /// <summary>Apply a certified amendment end to end (A488 publish +
    /// seal).  Every failure is terminal ``rejected`` with evidence.
    /// </summary>
}
