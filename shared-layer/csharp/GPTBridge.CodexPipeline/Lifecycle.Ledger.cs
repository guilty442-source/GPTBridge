using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.CodexPipeline;


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
        try
        {
            return Transition(requestId, Lifecycle.StateRejected,
                payload);
        }
        catch (AmendmentLifecycleError error)
            when (error.Code == "REQUEST_STATE_TERMINAL")
        {
            // A concurrent lane can reach the terminal verdict first
            // (watcher + CLI on the same request).  The recorded
            // terminal state stands; a second rejection adds nothing.
            return RecordFromPayload(LoadRecordRequired(requestId));
        }
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
