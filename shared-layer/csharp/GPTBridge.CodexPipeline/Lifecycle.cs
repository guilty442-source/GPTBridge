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

/// <summary>Append/request-state ledger plus one active lock per
/// lineage — port of ``CodexAmendmentRequestLedger``.</summary>
internal sealed class CodexAmendmentRequestLedger
{
    public string Root { get; }
    public string RecordsDir { get; }
    public string LocksDir { get; }

    public CodexAmendmentRequestLedger(string? root = null)
    {
        Root = Path.GetFullPath(root ?? Lifecycle.DefaultLedgerRoot());
        RecordsDir = Path.Combine(Root, "requests");
        LocksDir = Path.Combine(Root, "lineage-locks");
    }

    private string RecordPathFor(string requestId) =>
        Path.Combine(RecordsDir, $"{Lifecycle.SafeName(requestId)}.json");

    private string LockPathFor(string lineageKey) =>
        Path.Combine(LocksDir,
            $"lineage-{lineageKey[..Math.Min(32, lineageKey.Length)]}.lock");

    public Dictionary<string, object?>? LoadRecord(string requestId)
    {
        var path = RecordPathFor(requestId);
        if (!File.Exists(path))
            return null;
        var obj = Lifecycle.LoadJsonObject(path);
        return (Dictionary<string, object?>)Repo.ToPlain(obj)!;
    }

    private Dictionary<string, object?> LoadRecordRequired(
        string requestId)
        => LoadRecord(requestId)
            ?? throw new AmendmentLifecycleError(
                "REQUEST_RECORD_REQUIRED", requestId);

    private string AcquireLineage(AmendmentRequest request)
    {
        var lockPath = LockPathFor(request.LineageKey);
        Directory.CreateDirectory(Path.GetDirectoryName(lockPath)!);
        var payload = new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["schema"] = Lifecycle.LifecycleSchema,
            ["request_id"] = request.RequestId,
            ["request_hash"] = request.RequestHash,
            ["lineage_key"] = request.LineageKey,
            ["predecessor"] = request.Predecessor,
            ["scope"] = request.Scope.ToList(),
            ["created_at"] = Repo.UtcNow(),
        };
        var reclaimed = false;
        while (true)
        {
            FileStream? descriptor;
            try
            {
                descriptor = new FileStream(lockPath, FileMode.CreateNew,
                    FileAccess.Write, FileShare.None);
            }
            catch (IOException)
            {
                var existing = Lifecycle.LoadJsonObject(lockPath);
                var existingRequest = Repo.Str(existing, "request_id");
                if (existingRequest == request.RequestId)
                    return lockPath;
                if (reclaimed || !LineageLockIsDead(existing))
                    throw new AmendmentLifecycleError(
                        "REQUEST_LINEAGE_LOCKED",
                        $"{request.LineageKey} held by "
                        + $"{(existingRequest.Length > 0 ? existingRequest : "unknown")}");
                try { File.Delete(lockPath); }
                catch (FileNotFoundException) { }
                reclaimed = true;
                continue;
            }
            using (descriptor)
            {
                var text = CanonJson.Serialize(payload);
                var bytes = System.Text.Encoding.UTF8.GetBytes(text);
                descriptor.Write(bytes);
                descriptor.Flush(flushToDisk: true);
            }
            return lockPath;
        }
    }

    private bool LineageLockIsDead(JsonObject lck)
    {
        var holder = Repo.Str(lck, "request_id");
        if (holder.Length == 0)
            return false;
        var record = LoadRecord(holder);
        if (record is not null)
            return Lifecycle.TerminalStates.Contains(
                record.TryGetValue("state", out var s)
                    ? s?.ToString() ?? "" : "");
        var createdAt = Repo.Str(lck, "created_at");
        if (!DateTimeOffset.TryParse(createdAt, out var created))
            return false;
        return (DateTimeOffset.UtcNow - created).TotalSeconds
            > Lifecycle.OrphanedLineageLockGraceSeconds;
    }

    private void ReleaseLineage(Dictionary<string, object?> record)
    {
        var rawLockPath = record.TryGetValue("lock_path", out var lp)
            ? lp?.ToString() ?? "" : "";
        if (rawLockPath.Length == 0)
            return;
        JsonObject lck;
        try
        {
            lck = Lifecycle.LoadJsonObject(rawLockPath);
        }
        catch (AmendmentLifecycleError)
        {
            return;
        }
        if (Repo.Str(lck, "request_id")
            == (record.TryGetValue("request_id", out var rid)
                ? rid?.ToString() ?? "" : ""))
        {
            try { File.Delete(rawLockPath); }
            catch (FileNotFoundException) { }
        }
    }

    /// <summary>``_record_invalid_request`` — also invoked by the
    /// driver for malformed intake artifacts (zombie reprocessing
    /// guard).</summary>
    internal void RecordInvalidRequest(string requestPath,
        AmendmentLifecycleError error)
    {
        var requestId = Path.GetFileNameWithoutExtension(requestPath);
        var requestHash = "";
        try
        {
            var payload = Lifecycle.LoadJsonObject(requestPath);
            var declared = Repo.Str(payload, "request_id").Trim();
            if (declared.Length > 0)
                requestId = declared;
            requestHash = AmendmentContract.ContentHash(
                Repo.ToPlain(payload));
        }
        catch (AmendmentLifecycleError)
        {
            try
            {
                requestHash = Convert.ToHexString(
                    System.Security.Cryptography.SHA256.HashData(
                        File.ReadAllBytes(requestPath))).ToLowerInvariant();
            }
            catch (IOException) { requestHash = ""; }
        }
        try { requestId = Lifecycle.SafeName(requestId); }
        catch (AmendmentLifecycleError)
        {
            requestId = Lifecycle.SafeName(
                Path.GetFileNameWithoutExtension(requestPath));
        }
        var existing = LoadRecord(requestId);
        if (existing is not null)
        {
            var existingHash = existing.TryGetValue("request_hash",
                out var eh) ? eh?.ToString() ?? "" : "";
            if (existingHash.Length > 0 && requestHash.Length > 0
                && existingHash != requestHash)
                return;
            var state = existing.TryGetValue("state", out var s)
                ? s?.ToString() ?? "" : "";
            if (Lifecycle.TerminalStates.Contains(state))
                return;
        }
        var recordPath = RecordPathFor(requestId);
        var now = Repo.UtcNow();
        var record = new Dictionary<string, object?>(
            StringComparer.Ordinal)
        {
            ["schema"] = Lifecycle.LifecycleSchema,
            ["request_id"] = requestId,
            ["state"] = Lifecycle.StateRejected,
            ["request_hash"] = requestHash,
            ["lineage_key"] = "",
            ["request_path"] = Path.GetFullPath(requestPath),
            ["predecessor"] = new Dictionary<string, object?>(),
            ["scope"] = new List<object?>(),
            ["record_path"] = recordPath,
            ["lock_path"] = "",
            ["not_executed"] = true,
            ["closed_at"] = now,
            ["history"] = new List<object?>
            {
                new Dictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["at"] = now,
                    ["from"] = "",
                    ["to"] = Lifecycle.StateRejected,
                    ["evidence"] = new Dictionary<string, object?>
                    { ["error"] = error.Message },
                },
            },
        };
        Repo.AtomicJson(recordPath, record);
    }

    private void RecordStaleRequest(AmendmentRequest request,
        string requestPath)
    {
        var recordPath = RecordPathFor(request.RequestId);
        var now = Repo.UtcNow();
        var record = new Dictionary<string, object?>(
            StringComparer.Ordinal)
        {
            ["schema"] = Lifecycle.LifecycleSchema,
            ["request_id"] = request.RequestId,
            ["state"] = Lifecycle.StateRejected,
            ["request_hash"] = request.RequestHash,
            ["lineage_key"] = request.LineageKey,
            ["request_path"] = Path.GetFullPath(requestPath),
            ["predecessor"] = request.Predecessor,
            ["scope"] = request.Scope.ToList(),
            ["record_path"] = recordPath,
            ["lock_path"] = "",
            ["not_executed"] = true,
            ["closed_at"] = now,
            ["history"] = new List<object?>
            {
                new Dictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["at"] = now,
                    ["from"] = "",
                    ["to"] = Lifecycle.StateRejected,
                    ["evidence"] = new Dictionary<string, object?>
                    { ["stale_at"] = now },
                },
            },
        };
        Repo.AtomicJson(recordPath, record);
    }

    private void ValidateLineage(AmendmentRequest request,
        string? currentVersion, long? expectedRevisionSequence)
    {
        if (currentVersion is not null
            && (request.Predecessor.TryGetValue("codex_version",
                    out var v) ? v?.ToString() ?? "" : "")
                != currentVersion)
            throw new AmendmentLifecycleError(
                "STALE_PREDECESSOR_VERSION",
                $"{request.Predecessor.GetValueOrDefault("codex_version")}"
                + $" != {currentVersion}");
        if (expectedRevisionSequence is not null)
        {
            var sequence = request.Predecessor
                .TryGetValue("revision_sequence", out var seq)
                ? seq : null;
            var seqLong = sequence switch
            {
                long l => l,
                int i => (long)i,
                _ => (long?)null,
            };
            if (seqLong != expectedRevisionSequence)
                throw new AmendmentLifecycleError(
                    "STALE_REVISION_SEQUENCE",
                    $"{sequence} != {expectedRevisionSequence}");
        }
    }

    /// <summary>Register a request and acquire its predecessor lineage
    /// lock.</summary>
    public LifecycleRecord Begin(string requestPath,
        string? currentVersion = null,
        long? expectedRevisionSequence = null)
    {
        AmendmentRequest request;
        try
        {
            request = Lifecycle.LoadAmendmentRequest(requestPath);
        }
        catch (AmendmentLifecycleError error)
        {
            RecordInvalidRequest(requestPath, error);
            throw;
        }
        var existing = LoadRecord(request.RequestId);
        if (existing is not null)
        {
            var existingHash = existing.TryGetValue("request_hash",
                out var eh) ? eh?.ToString() ?? "" : "";
            if (existingHash != request.RequestHash)
                throw new AmendmentLifecycleError(
                    "REQUEST_ID_REUSE", request.RequestId);
            var state = existing.TryGetValue("state", out var s)
                ? s?.ToString() ?? "" : "";
            if (Lifecycle.TerminalStates.Contains(state))
                throw new AmendmentLifecycleError(
                    "REQUEST_ALREADY_TERMINAL",
                    $"{request.RequestId}:{state}");
            try
            {
                ValidateLineage(request, currentVersion,
                    expectedRevisionSequence);
            }
            catch (AmendmentLifecycleError error)
            {
                try
                {
                    Reject(request.RequestId, reason: error.Message,
                        evidence: new Dictionary<string, object?>
                        { ["stale_at"] = Repo.UtcNow() });
                }
                catch (AmendmentLifecycleError) { }
                throw;
            }
            var lockPath = existing.TryGetValue("lock_path", out var lp)
                ? lp?.ToString() ?? "" : "";
            if (lockPath.Length == 0 || !File.Exists(lockPath))
            {
                lockPath = AcquireLineage(request);
                existing["lock_path"] = lockPath;
                var recordPath = existing["record_path"]?.ToString()
                    ?? RecordPathFor(request.RequestId);
                Repo.AtomicJson(recordPath, existing);
            }
            return RecordFromPayload(existing);
        }
        try
        {
            ValidateLineage(request, currentVersion,
                expectedRevisionSequence);
        }
        catch (AmendmentLifecycleError)
        {
            RecordStaleRequest(request, requestPath);
            throw;
        }
        var newLockPath = AcquireLineage(request);
        var newRecordPath = RecordPathFor(request.RequestId);
        var record = new Dictionary<string, object?>(
            StringComparer.Ordinal)
        {
            ["schema"] = Lifecycle.LifecycleSchema,
            ["request_id"] = request.RequestId,
            ["state"] = Lifecycle.StateSubmitted,
            ["request_hash"] = request.RequestHash,
            ["lineage_key"] = request.LineageKey,
            ["request_path"] = Path.GetFullPath(requestPath),
            ["predecessor"] = request.Predecessor,
            ["scope"] = request.Scope.ToList(),
            ["record_path"] = newRecordPath,
            ["lock_path"] = newLockPath,
            ["not_executed"] = true,
            ["history"] = new List<object?>
            {
                new Dictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["at"] = Repo.UtcNow(),
                    ["from"] = "",
                    ["to"] = Lifecycle.StateSubmitted,
                    ["evidence"] = new Dictionary<string, object?>(),
                },
            },
        };
        Repo.AtomicJson(newRecordPath, record);
        return RecordFromPayload(record);
    }

    /// <summary>Move one request through the explicit lifecycle state
    /// machine.</summary>
    public LifecycleRecord Transition(string requestId, string target,
        Dictionary<string, object?>? evidence = null)
    {
        var record = LoadRecordRequired(requestId);
        var current = record.TryGetValue("state", out var s)
            ? s?.ToString() ?? "" : "";
        var targetState = (target ?? "").Trim();
        if (Lifecycle.TerminalStates.Contains(current))
            throw new AmendmentLifecycleError(
                "REQUEST_STATE_TERMINAL", $"{requestId}:{current}");
        if (!Lifecycle.StateTransitions.TryGetValue(current,
                out var allowed)
            || !allowed.Contains(targetState))
            throw new AmendmentLifecycleError(
                "REQUEST_STATE_TRANSITION_DENIED",
                $"{requestId}:{current}->{targetState}");
        var history = record.TryGetValue("history", out var h)
            && h is List<object?> list
                ? list : new List<object?>();
        history.Add(new Dictionary<string, object?>(
            StringComparer.Ordinal)
        {
            ["at"] = Repo.UtcNow(),
            ["from"] = current,
            ["to"] = targetState,
            ["evidence"] = evidence ?? new Dictionary<string, object?>(),
        });
        record["state"] = targetState;
        record["history"] = history;
        if (targetState == Lifecycle.StateExecuted)
        {
            record["not_executed"] = false;
            record["execution_record"] =
                evidence ?? new Dictionary<string, object?>();
        }
        if (Lifecycle.TerminalStates.Contains(targetState))
            record["closed_at"] = Repo.UtcNow();
        Repo.AtomicJson(record["record_path"]?.ToString()
            ?? RecordPathFor(requestId), record);
        if (Lifecycle.TerminalStates.Contains(targetState))
            ReleaseLineage(record);
        return RecordFromPayload(record);
    }

    public LifecycleRecord Reject(string requestId, string reason,
        Dictionary<string, object?>? evidence = null)
    {
        var payload = evidence is null
            ? new Dictionary<string, object?>()
            : new Dictionary<string, object?>(evidence);
        payload["reason"] = reason;
        return Transition(requestId, Lifecycle.StateRejected, payload);
    }

    private LifecycleRecord RecordFromPayload(
        Dictionary<string, object?> payload)
    {
        var record = new LifecycleRecord
        {
            RequestId = payload.TryGetValue("request_id", out var rid)
                ? rid?.ToString() ?? "" : "",
            State = payload.TryGetValue("state", out var s)
                ? s?.ToString() ?? "" : "",
            RequestHash = payload.TryGetValue("request_hash", out var rh)
                ? rh?.ToString() ?? "" : "",
            LineageKey = payload.TryGetValue("lineage_key", out var lk)
                ? lk?.ToString() ?? "" : "",
            RecordPath = payload.TryGetValue("record_path", out var rp)
                ? rp?.ToString() ?? "" : "",
            LockPath = payload.TryGetValue("lock_path", out var lp)
                ? lp?.ToString() ?? "" : "",
            NotExecuted = payload.TryGetValue("not_executed", out var ne)
                && ne is bool b && b,
        };
        if (payload.TryGetValue("history", out var h)
            && h is List<object?> hist)
            record.History = hist;
        return record;
    }
}
