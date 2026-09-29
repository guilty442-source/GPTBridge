using System.Text;
using System.Text.Json.Nodes;

namespace GPTBridge.CodexPipeline;

internal static partial class CodexEntryState
{
    private static Dictionary<string, object?> EmptyState() => new(
        StringComparer.Ordinal)
    {
        ["consumed_nonces"] = new SortedDictionary<string, object?>(
            StringComparer.Ordinal),
        ["grants"] = new SortedDictionary<string, object?>(
            StringComparer.Ordinal),
        ["revocation_generation"] = 0L,
        ["sessions"] = new SortedDictionary<string, object?>(
            StringComparer.Ordinal),
    };

    private static Dictionary<string, object?> DeepCopy(
        string serialized) =>
        (Dictionary<string, object?>)Repo.ToPlain(
            JsonNode.Parse(serialized))!;

    /// <summary>Read the persisted entry state; fail closed when
    /// corrupt.  Retries the brief window in which an atomic replace
    /// makes the path resolve as missing.</summary>
    public static Dictionary<string, object?> LoadState()
    {
        var path = StatePath();
        (long Mtime, long Size)? key = null;
        try
        {
            var info = new FileInfo(path);
            if (info.Exists && info.Length > 0)
                key = (info.LastWriteTimeUtc.Ticks, info.Length);
        }
        catch (IOException)
        {
            key = null;
        }
        string? text = null;
        if (key is not null)
            lock (StateLock)
                if (StateCache.TryGetValue(key.Value, out var cached))
                    text = cached;
        Exception? lastError = null;
        if (text is null)
        {
            for (var attempt = 0; attempt < 5; attempt++)
            {
                try
                {
                    if (!File.Exists(path))
                        return EmptyState();
                    text = File.ReadAllText(path, Encoding.UTF8);
                    break;
                }
                catch (FileNotFoundException)
                {
                    Thread.Sleep(10);
                }
                catch (DirectoryNotFoundException)
                {
                    return EmptyState();
                }
                catch (IOException error)
                {
                    lastError = error;
                    Thread.Sleep(10);
                }
                catch (System.Text.Json.JsonException error)
                {
                    throw new CodexReadDenied(
                        $"CODEX_STATE_CORRUPT:{error.GetType().Name}");
                }
            }
            if (text is null)
                throw new CodexReadDenied(
                    "CODEX_STATE_UNAVAILABLE:"
                    + (lastError?.GetType().Name ?? ""));
        }
        Dictionary<string, object?> data;
        try
        {
            data = DeepCopy(text);
        }
        catch (System.Text.Json.JsonException error)
        {
            throw new CodexReadDenied(
                $"CODEX_STATE_CORRUPT:{error.GetType().Name}");
        }
        if (!data.TryGetValue("revocation_generation", out var gen)
            || gen is not long && gen is not int && gen is not decimal)
            throw new CodexReadDenied("CODEX_STATE_CORRUPT:schema");
        foreach (var section in new[] { "sessions", "grants",
            "consumed_nonces" })
            if (data[section] is not Dictionary<string, object?>)
                data[section] = new Dictionary<string, object?>(
                    StringComparer.Ordinal);
        if (key is not null)
            lock (StateLock)
            {
                StateCache.Clear();
                StateCache[key.Value] = text;
            }
        return data;
    }

    private static object? SortDeep(object? value) => value switch
    {
        Dictionary<string, object?> map => new SortedDictionary<
            string, object?>(map.ToDictionary(p => p.Key,
            p => SortDeep(p.Value), StringComparer.Ordinal),
            StringComparer.Ordinal),
        List<object?> list => list.Select(SortDeep).ToList(),
        _ => value,
    };

    /// <summary>Atomically persist entry state; fail closed when
    /// unavailable.  Temp file is unique per process/thread.</summary>
    private static void StoreState(Dictionary<string, object?> state)
    {
        var path = StatePath();
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        }
        catch (IOException error)
        {
            throw new CodexReadDenied(
                $"CODEX_STATE_UNAVAILABLE:{error.GetType().Name}");
        }
        catch (UnauthorizedAccessException error)
        {
            throw new CodexReadDenied(
                $"CODEX_STATE_UNAVAILABLE:{error.GetType().Name}");
        }
        var payload = CanonJson.SerializeSpaced(SortDeep(state)) + "\n";
        var temporary = Path.Combine(Path.GetDirectoryName(path)!,
            $"{Path.GetFileName(path)}.{Environment.ProcessId}."
            + $"{Environment.CurrentManagedThreadId}.tmp");
        Exception? lastError = null;
        for (var attempt = 0; attempt < 5; attempt++)
        {
            try
            {
                File.WriteAllText(temporary, payload,
                    new UTF8Encoding(false));
                File.Move(temporary, path, true);
                try
                {
                    var info = new FileInfo(path);
                    lock (StateLock)
                    {
                        StateCache.Clear();
                        StateCache[(info.LastWriteTimeUtc.Ticks,
                            info.Length)] = payload;
                    }
                }
                catch (IOException)
                {
                    lock (StateLock)
                        StateCache.Clear();
                }
                return;
            }
            catch (IOException error)
            {
                lastError = error;
                Thread.Sleep(20 * (attempt + 1));
            }
            catch (UnauthorizedAccessException error)
            {
                lastError = error;
                Thread.Sleep(20 * (attempt + 1));
            }
            finally
            {
                try
                {
                    if (File.Exists(temporary))
                        File.Delete(temporary);
                }
                catch (IOException)
                {
                }
            }
        }
        throw new CodexReadDenied(
            "CODEX_STATE_UNAVAILABLE:"
            + (lastError?.GetType().Name ?? ""));
    }

    /// <summary>Drop dead session/grant/consumed records; live entries
    /// untouched.</summary>
    private static void PruneExpired(
        Dictionary<string, object?> state, double now)
    {
        if (state["sessions"] is Dictionary<string, object?> sessions)
            foreach (var nonce in sessions
                .Where(p => p.Value is Dictionary<string, object?> r
                    && Convert.ToDouble(
                        r.GetValueOrDefault("expires_at"))
                    <= now - SessionRetentionSeconds)
                .Select(p => p.Key).ToList())
                sessions.Remove(nonce);
        if (state["grants"] is Dictionary<string, object?> grants)
            foreach (var nonce in grants
                .Where(p => p.Value is Dictionary<string, object?> r
                    && Convert.ToDouble(
                        r.GetValueOrDefault("expires_at"))
                    <= now - GrantRetentionSeconds)
                .Select(p => p.Key).ToList())
                grants.Remove(nonce);
        if (state["consumed_nonces"] is Dictionary<string, object?>
            consumed)
        {
            var cutoff = DateTime.UtcNow
                .AddSeconds(-ConsumedRetentionSeconds)
                .ToString("yyyy-MM-ddTHH:mm:ssZ");
            foreach (var nonce in consumed
                .Where(p => string.CompareOrdinal(
                    p.Value?.ToString() ?? "", cutoff) < 0)
                .Select(p => p.Key).ToList())
                consumed.Remove(nonce);
        }
    }

    /// <summary>Load state, apply ``mutator`` and persist atomically
    /// under lock.</summary>
    public static T MutateEntryState<T>(
        Func<Dictionary<string, object?>, T> mutator)
    {
        lock (StateLock)
        {
            var state = LoadState();
            var result = mutator(state);
            PruneExpired(state,
                DateTimeOffset.UtcNow.ToUnixTimeSeconds());
            StoreState(state);
            return result;
        }
    }

    public static void MutateEntryState(
        Action<Dictionary<string, object?>> mutator) =>
        MutateEntryState<object?>(state =>
        {
            mutator(state);
            return null;
        });

    public static Dictionary<string, object?> ReadEntryState()
    {
        lock (StateLock)
            return LoadState();
    }

    // -- sessions / revocation / grants -------------------------------------

    public static void RevokeCodexReadContexts() =>
        MutateEntryState(state =>
        {
            state["revocation_generation"] =
                Convert.ToInt64(state["revocation_generation"]) + 1;
        });

    public static long CurrentRevocation() =>
        Convert.ToInt64(ReadEntryState()["revocation_generation"]);

    private static Dictionary<string, object?>? Section(
        Dictionary<string, object?> state, string name) =>
        state.TryGetValue(name, out var value)
            && value is Dictionary<string, object?> map ? map : null;

    /// <summary>Persist a minted session record (nonce uniqueness +
    /// lifecycle evidence).</summary>
    public static void RegisterSessionNonce(string nonce, string actor,
        string purpose, string accessClass,
        IReadOnlyCollection<string> scope, long codexVersion,
        long generation, double expiresAt)
    {
        MutateEntryState(state =>
        {
            var sessions = Section(state, "sessions")!;
            var consumed = Section(state, "consumed_nonces")!;
            if (sessions.ContainsKey(nonce)
                || consumed.ContainsKey(nonce))
                throw new CodexReadDenied("CODEX_NONCE_REPLAY");
            sessions[nonce] = new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["actor"] = actor,
                ["purpose"] = purpose,
                ["access_class"] = accessClass,
                ["scope_hash"] = ScopeHash(scope),
                ["codex_version"] = codexVersion,
                ["generation"] = generation,
                ["expires_at"] = expiresAt,
                ["closed"] = false,
            };
        });
    }

    /// <summary>Mark a persisted session record closed (consumed,
    /// single-use).</summary>
    public static void CloseSessionNonce(string nonce) =>
        MutateEntryState(state =>
        {
            var sessions = Section(state, "sessions")!;
            if (sessions.TryGetValue(nonce, out var record)
                && record is Dictionary<string, object?> map)
                map["closed"] = true;
            Section(state, "consumed_nonces")![nonce] = UtcNow();
        });

    /// <summary>``parse_scope`` — governed ``kind:name`` scope grammar.</summary>
    public static HashSet<string> ParseScope(IEnumerable<string> scope)
    {
        var items = scope
            .Select(item => (item ?? "").Trim())
            .Where(item => item.Length > 0)
            .ToHashSet(StringComparer.Ordinal);
        foreach (var item in items)
        {
            var separator = item.IndexOf(':');
            if (separator < 0
                || !ValidScopeKinds.Contains(item[..separator])
                || separator == item.Length - 1)
                throw new CodexReadDenied(
                    $"CODEX_SCOPE_MALFORMED:{item}");
        }
        if (items.Count == 0)
            throw new CodexReadDenied("CODEX_SCOPE_REQUIRED");
        return items;
    }
}
