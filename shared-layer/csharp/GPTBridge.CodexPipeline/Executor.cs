using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Governed codex amendment executor (governor-invoked; A382/A488,
/// A104) — port of codex_amendment_executor.py.
///
/// Fail-closed contract:
/// * ``apply=true`` requires a unanimous five-sovereign audit result
///   (``ok`` with a certificate).  Never fabricates receipts, never
///   runs the gate on behalf of a sovereign.
/// * Seal roots and revision/lineage/certification rows remain governed
///   amendment-pipeline authority; the unanimous certificate is the
///   seal-closing condition (``sealed-governed-certification``).
/// * ``apply=false`` (default) rehearses the change in isolation only.
/// </summary>
internal class ExecutorDenied : Exception
{
    public const string FailureCode = "CODEX_AMENDMENT_DENIED";

    public ExecutorDenied(string message) : base(message) { }
}

internal sealed record AmendmentExecutionResult(
    bool Ok, bool Applied, string AmendmentId, string Version,
    string Reason, IReadOnlyList<Dictionary<string, object?>> Phases,
    string SealState = "sealed-governed-certification")
{
    public Dictionary<string, object?> ToDict() =>
        new(StringComparer.Ordinal)
        {
            ["ok"] = Ok,
            ["applied"] = Applied,
            ["amendment_id"] = AmendmentId,
            ["version"] = Version,
            ["reason"] = Reason,
            ["phases"] = Phases.Cast<object?>().ToList(),
            ["seal_state"] = SealState,
        };
}

internal static class Executor
{
    public static string DefaultStaging() =>
        Path.Combine(Repo.Root(), "main-system", "runtime", "temp",
            "codex-amendment-stage");

    private static JsonObject LoadRequest(string requestPath)
    {
        JsonNode? payload;
        try
        {
            payload = JsonNode.Parse(File.ReadAllText(requestPath));
        }
        catch (Exception error) when (error is IOException
            or JsonException or UnauthorizedAccessException)
        {
            throw new ExecutorDenied(
                $"AMENDMENT_REQUEST_UNREADABLE:{error.Message}");
        }
        if (payload is not JsonObject obj)
            throw new ExecutorDenied("AMENDMENT_REQUEST_NOT_AN_OBJECT");
        if (Repo.Str(obj, "request_id").Trim().Length == 0)
            throw new ExecutorDenied("AMENDMENT_REQUEST_ID_REQUIRED");
        return obj;
    }

    private static void LoadAuditResult(
        Dictionary<string, object?>? auditResult, string amendmentId)
    {
        if (auditResult is null)
            throw new ExecutorDenied(
                "FIVE_SOVEREIGN_AUDIT_RESULT_REQUIRED");
        var resultId = auditResult.TryGetValue("amendment_id",
            out var aid) ? aid?.ToString() ?? "" : "";
        if (resultId != amendmentId)
            throw new ExecutorDenied(
                "FIVE_SOVEREIGN_AUDIT_AMENDMENT_MISMATCH");
        if (!(auditResult.TryGetValue("ok", out var okv)
            && okv is true))
            throw new ExecutorDenied(
                "FIVE_SOVEREIGN_AUDIT_NOT_PASSED:"
                + (auditResult.TryGetValue("reason", out var reason)
                    ? reason?.ToString() ?? "unknown" : "unknown"));
        if (!(auditResult.TryGetValue("audit_recorded", out var ar)
            && ar is true))
            throw new ExecutorDenied("FIVE_SOVEREIGN_AUDIT_UNRECORDED");
        if (!(auditResult.TryGetValue("certificate", out var cert)
            && cert is IDictionary<string, object?> certificate))
            throw new ExecutorDenied("AMENDMENT_CERTIFICATE_REQUIRED");
        var schema = certificate.TryGetValue("schema", out var sv)
            ? sv?.ToString() ?? "" : "";
        if (schema != SovereignAudit.CertificateSchema)
            throw new ExecutorDenied(
                "AMENDMENT_CERTIFICATE_SCHEMA_INVALID");
        var certAmendment = certificate.TryGetValue("amendment_id",
            out var av) ? av?.ToString() ?? "" : "";
        if (certAmendment != amendmentId)
            throw new ExecutorDenied(
                "AMENDMENT_CERTIFICATE_AMENDMENT_MISMATCH");
    }

    /// <summary>Run the governed amendment pipeline; fail closed
    /// without the audit gate.</summary>
    public static AmendmentExecutionResult ExecuteAmendment(
        string requestPath,
        string? preparedDatabase = null,
        Dictionary<string, object?>? auditResult = null,
        bool apply = false,
        string? stagingRoot = null,
        string? codexRoot = null)
    {
        var request = LoadRequest(requestPath);
        var amendmentId = Repo.Str(request, "request_id");
        var prepared = preparedDatabase is not null
            ? Path.GetFullPath(preparedDatabase) : null;
        if (prepared is not null && !File.Exists(prepared))
            throw new ExecutorDenied(
                $"PREPARED_DATABASE_MISSING:{prepared}");
        if (apply)
            LoadAuditResult(auditResult, amendmentId);
        var root = codexRoot is not null
            ? Path.GetFullPath(codexRoot)
            : UpdatePipeline.CanonicalCodexRoot();
        if (apply && !string.Equals(root,
                Path.GetFullPath(UpdatePipeline.CanonicalCodexRoot()),
                StringComparison.OrdinalIgnoreCase))
            // An applied amendment that skips the canonical root never
            // reaches the PostgreSQL authority — reporting applied=True
            // would fabricate a publication that did not happen.
            throw new ExecutorDenied(
                "AMENDMENT_APPLY_REQUIRES_CANONICAL_ROOT");
        var staging = stagingRoot is not null
            ? Path.GetFullPath(stagingRoot) : DefaultStaging();
        var result = UpdatePipeline.RunAutoUpdate(root, staging,
            preparedDatabase: prepared, apply: apply,
            bookkeeping: new Dictionary<string, string>(
                StringComparer.Ordinal)
            {
                ["change_id"] = amendmentId,
                ["change_scope"] =
                    Repo.Str(request, "change_class") is { Length: > 0 }
                        cc ? cc : "amendment-execution",
                ["summary"] =
                    Repo.Str(request, "title") is { Length: > 0 } t ? t
                    : Repo.Str(request, "summary") is { Length: > 0 } s
                        ? s : amendmentId,
            });
        var phases = result.Phases.Select(phase =>
            new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["phase"] = phase.Phase,
                ["ok"] = phase.Ok,
                ["detail"] = phase.Detail,
            }).ToList();
        return new AmendmentExecutionResult(result.Ok, result.Applied,
            amendmentId, result.Version,
            result.Ok ? "" : "update-pipeline-rejected", phases);
    }
}
