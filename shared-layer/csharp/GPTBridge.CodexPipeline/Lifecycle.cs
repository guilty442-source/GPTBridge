using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Codex amendment request lifecycle and single-lineage lock (G71) —
/// direct port of codex_amendment_lifecycle.py.
///
/// The ledger manages only request-side metadata; its filesystem lineage
/// lock serializes candidate construction for one predecessor generation
/// so two same-generation requests cannot fork the same authority
/// lineage.  Every transition is persisted atomically before the lock is
/// released; terminal states release it.
/// </summary>
internal class AmendmentLifecycleError : Exception
{
    public string Code { get; }
    public string Detail { get; }

    public AmendmentLifecycleError(string code, string detail = "")
        : base(detail.Length > 0 ? $"{code}:{detail}" : code)
    {
        Code = code;
        Detail = detail;
    }
}

internal sealed record AmendmentRequest(
    string RequestId,
    string RequestHash,
    Dictionary<string, object?> Predecessor,
    string LineageKey,
    JsonObject Payload,
    string[] Scope);

internal sealed class LifecycleRecord
{
    public string RequestId = "";
    public string State = "";
    public string RequestHash = "";
    public string LineageKey = "";
    public string RecordPath = "";
    public string LockPath = "";
    public bool NotExecuted = true;
    public List<object?> History = new();

    public Dictionary<string, object?> AsDict() => new(StringComparer.Ordinal)
    {
        ["schema"] = Lifecycle.LifecycleSchema,
        ["request_id"] = RequestId,
        ["state"] = State,
        ["request_hash"] = RequestHash,
        ["lineage_key"] = LineageKey,
        ["record_path"] = RecordPath,
        ["lock_path"] = LockPath,
        ["not_executed"] = NotExecuted,
        ["history"] = History,
    };
}

internal static class Lifecycle
{
    public const string RequestArtifact = "codex-amendment-request";
    public const string RequestAuthority = "request-only";
    public const string LifecycleSchema =
        "gptbridge-codex-amendment-lifecycle/v1";

    public const string StateSubmitted = "submitted";
    public const string StateUnderReview = "under-review";
    public const string StateSuccessorBuilt = "successor-built";
    public const string StateAuditing = "auditing";
    public const string StateAuditPassed = "audit-passed";
    public const string StateReadyForGovernor = "ready-for-governor";
    public const string StateExecuted = "executed";
    public const string StateRejected = "rejected";
    public const string StateWithdrawn = "withdrawn";

    public const int OrphanedLineageLockGraceSeconds = 60;

    public static readonly HashSet<string> TerminalStates = new(
        StringComparer.Ordinal)
    { StateExecuted, StateRejected, StateWithdrawn };

    public static readonly Dictionary<string, HashSet<string>>
        StateTransitions = new(StringComparer.Ordinal)
        {
            [StateSubmitted] = new(StringComparer.Ordinal)
            { StateUnderReview, StateRejected, StateWithdrawn },
            [StateUnderReview] = new(StringComparer.Ordinal)
            { StateSuccessorBuilt, StateRejected, StateWithdrawn },
            [StateSuccessorBuilt] = new(StringComparer.Ordinal)
            { StateAuditing, StateRejected, StateWithdrawn },
            [StateAuditing] = new(StringComparer.Ordinal)
            { StateAuditPassed, StateSuccessorBuilt, StateRejected },
            [StateAuditPassed] = new(StringComparer.Ordinal)
            { StateReadyForGovernor, StateRejected, StateWithdrawn },
            [StateReadyForGovernor] = new(StringComparer.Ordinal)
            { StateExecuted, StateRejected, StateWithdrawn },
        };

    public static string DefaultLedgerRoot() =>
        Path.Combine(Repo.StateDir(), "codex-amendments");

    internal static string SafeName(string value)
    {
        var safe = Regex.Replace((value ?? "").Trim(),
            "[^A-Za-z0-9_.-]+", "-").Trim('-', '.');
        if (safe.Length == 0)
            throw new AmendmentLifecycleError("REQUEST_ID_REQUIRED");
        return safe.Length > 160 ? safe[..160] : safe;
    }

    internal static JsonObject LoadJsonObject(string path)
    {
        JsonNode? payload;
        try
        {
            payload = JsonNode.Parse(File.ReadAllText(path));
        }
        catch (Exception error) when (error is IOException
            or JsonException or UnauthorizedAccessException)
        {
            throw new AmendmentLifecycleError(
                "REQUEST_UNREADABLE", error.Message);
        }
        if (payload is not JsonObject obj)
            throw new AmendmentLifecycleError("REQUEST_NOT_AN_OBJECT");
        return obj;
    }

    /// <summary>Deterministic mutation/rebind scope for lineage
    /// evidence — parity with ``request_scope``.</summary>
    public static string[] RequestScope(JsonObject payload)
    {
        var scope = new SortedSet<string>(StringComparer.Ordinal);
        if (Repo.Get(payload, "changes") is JsonArray changes)
            foreach (var item in changes)
            {
                if (item is not JsonObject map)
                    continue;
                var table = Repo.Str(map, "table").Trim();
                var field = Repo.Str(map, "field").Trim();
                if (table.Length > 0)
                    scope.Add($"table:{table}");
                if (table.Length > 0 && field.Length > 0)
                    scope.Add($"field:{table}.{field}");
            }
        var successors = Repo.Get(payload, "proposed_successors");
        IEnumerable<JsonNode?> items = successors switch
        {
            JsonObject single => new[] { single },
            JsonArray array => array,
            _ => Enumerable.Empty<JsonNode?>(),
        };
        foreach (var item in items)
        {
            if (item is not JsonObject map)
                continue;
            var registry = Repo.Str(map, "registry").Trim();
            if (registry.Length > 0)
                scope.Add($"registry:{registry}");
            if (Repo.Get(map, "provision") is JsonObject provision)
            {
                var artifact = Repo.Str(provision,
                    "architecture_artifact").Trim();
                if (artifact.Length > 0)
                    scope.Add($"artifact:{artifact}");
            }
            var direct = Repo.Str(map, "architecture_artifact").Trim();
            if (direct.Length > 0)
                scope.Add($"artifact:{direct}");
        }
        if (Repo.Get(payload, "proposed_successor") is JsonObject singular)
            scope.Add("provision:"
                + (Repo.Str(singular, "provision_id") is { Length: > 0 } id
                    ? id : "pending"));
        if (Repo.Get(payload, "proposed_change") is JsonObject proposed)
        {
            var table = Repo.Str(proposed, "table").Trim();
            if (table.Length > 0)
                scope.Add($"table:{table}");
        }
        foreach (var key in new[] { "proposed_repair",
            "proposed_resolution" })
            if (Repo.Get(payload, key) is JsonObject)
                scope.Add($"proposal:{key}");
        return scope.ToArray();
    }

    /// <summary>Load and validate one request artifact without executing
    /// it — parity with ``load_amendment_request``.</summary>
    public static AmendmentRequest LoadAmendmentRequest(string path)
    {
        var payload = LoadJsonObject(path);
        if (Repo.Str(payload, "artifact").Trim() != RequestArtifact)
            throw new AmendmentLifecycleError("REQUEST_ARTIFACT_INVALID");
        if (Repo.Str(payload, "authority").Trim() != RequestAuthority)
            throw new AmendmentLifecycleError("REQUEST_AUTHORITY_INVALID");
        var requestId = Repo.Str(payload, "request_id").Trim();
        if (requestId.Length == 0)
            throw new AmendmentLifecycleError("REQUEST_ID_REQUIRED");
        if (Repo.Str(payload, "requested_by").Trim().Length == 0)
            throw new AmendmentLifecycleError("REQUEST_REQUESTER_REQUIRED");
        try
        {
            Amendment.NormalizeChangeClass(
                Repo.Str(payload, "change_class"));
        }
        catch (CodexAmendmentDenied error)
        {
            throw new AmendmentLifecycleError(
                "REQUEST_CHANGE_CLASS_INVALID", error.Message);
        }
        if (Repo.Str(payload, "required_review").Trim().Length == 0)
            throw new AmendmentLifecycleError("REQUEST_REVIEW_REQUIRED");
        if (Repo.Get(payload, "predecessor") is not JsonObject predecessor)
            throw new AmendmentLifecycleError("REQUEST_PREDECESSOR_REQUIRED");
        var predecessorVersion =
            Repo.Str(predecessor, "codex_version").Trim();
        var historyHead = Repo.Str(predecessor, "history_head").Trim();
        if (predecessorVersion.Length == 0 || historyHead.Length == 0)
            throw new AmendmentLifecycleError("REQUEST_LINEAGE_REQUIRED");
        if (Repo.Get(payload, "not_executed") is not JsonValue notExecuted
            || !notExecuted.TryGetValue<bool>(out var ne) || !ne)
            throw new AmendmentLifecycleError("REQUEST_ALREADY_CLOSED");
        var keys = new[]
        {
            "changes", "proposed_successors", "proposed_successor",
            "proposed_change", "proposed_repair", "proposed_resolution",
        };
        if (!keys.Any(key => Repo.Truthy(Repo.Get(payload, key))))
            throw new AmendmentLifecycleError("REQUEST_SUCCESSOR_REQUIRED");
        var requestHash = AmendmentContract.ContentHash(
            Repo.ToPlain(payload));
        var predecessorPlain = (Dictionary<string, object?>)
            Repo.ToPlain(predecessor)!;
        var lineageKey = AmendmentContract.ContentHash(
            new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["codex_version"] = predecessorVersion,
                ["history_head"] = historyHead,
                ["revision_sequence"] =
                    Repo.ToPlain(Repo.Get(predecessor,
                        "revision_sequence")),
            });
        return new AmendmentRequest(
            requestId, requestHash, predecessorPlain, lineageKey,
            payload, RequestScope(payload));
    }
}
