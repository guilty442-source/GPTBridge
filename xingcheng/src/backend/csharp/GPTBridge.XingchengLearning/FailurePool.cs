// FailurePool.cs — ``star-capability-failure-pool/v1`` (§29) +
// AutonomousCapabilityRecoveryLoop §6-§11 collection substrate.
//
// Unified capability-failure store: every evaluation surface records
// failures here so the recovery loop consumes the pool directly — no
// re-mining logs. Per-capability bounded JSONL pools under
// xingcheng/runtime/state/failure-pool (pool-<class>.jsonl). Exact +
// normalized near-duplicate dedup keeps one repeated error from
// dominating a dataset (§11); a repeat bumps seen_count instead of
// appending. Recording never throws — a pool write failure must not
// mask the evaluation verdict it was describing.

using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class FailurePool
{
    public const string Format = "star-capability-failure-pool/v1";
    public const string PoolDirRel =
        "xingcheng/runtime/state/failure-pool";
    public const string PoolFile = "pool.jsonl";
    public const int MaxEntries = 4096;   // bounded per class

    // §7 capability vocabulary (pool-class spelling) + the historical
    // suite classes. §7 names are the primary contract; the older
    // entries stay so existing suite mappings remain total.
    public static readonly string[] Classes =
    {
        "instruction", "context", "multi-turn", "structured-output",
        "tool-call", "reading", "rag", "math", "coding", "vision",
        "system1", "thinking",
        "reasoning", "routing", "grounding",
    };

    // §55 failure memory states.
    public static readonly string[] States =
        { "OPEN", "TRAINED", "RESOLVED", "REGRESSED" };

    /// <summary>Map a §7 capability name (INSTRUCTION, MULTI_TURN, ...)
    /// to its pool class — the recovery loop's classifier vocabulary.</summary>
    public static string ClassForCapability(string capability) =>
        capability.Trim().ToUpperInvariant() switch
        {
            "INSTRUCTION" => "instruction",
            "CONTEXT" => "context",
            "MULTI_TURN" => "multi-turn",
            "STRUCTURED" => "structured-output",
            "TOOL" => "tool-call",
            "READING" => "reading",
            "RAG" => "rag",
            "MATH" => "math",
            "CODING" => "coding",
            "VISION" => "vision",
            "SYSTEM1" => "system1",
            "THINKING" => "thinking",
            _ => "reasoning",
        };

    /// <summary>Map an evaluation suite name to its failure class —
    /// the suite vocabulary is closed, so this mapping is total.</summary>
    public static string ClassForSuite(string suite) => suite switch
    {
        "tool-decision" or "tool_call_format" or "tool_calling"
            => "tool-call",
        "structured-output" or "structured_output" or "fim"
            => "structured-output",
        "vision" => "vision",
        "system1" => "system1",
        "thinking" or "thinking_eval" => "thinking",
        "moe-routing" or "expert_routing" => "routing",
        "citation" => "rag",
        "rag" => "rag",
        "code" or "coding" => "coding",
        "math" => "math",
        "reading" => "reading",
        "multi_turn" => "multi-turn",
        "long-context" or "context_tracking" => "context",
        "runtime-parity" or "precision-parity" or "kv-cache"
            or "recurrent-state" or "generation-migration"
            or "bundle-provenance" => "reasoning",
        _ => "instruction",
    };

    private static string PoolDir(string toolRoot) => Path.Combine(
        toolRoot, PoolDirRel.Replace('/', Path.DirectorySeparatorChar));

    private static string PoolPath(string toolRoot, string cls) =>
        Path.Combine(PoolDir(toolRoot), $"pool-{cls}.jsonl");

    private static string Fingerprint(string input)
    {
        byte[] h = SHA256.HashData(Encoding.UTF8.GetBytes(input ?? ""));
        return Convert.ToHexString(h)[..16].ToLowerInvariant();
    }

    /// <summary>Normalized near-duplicate key (§11): case-folded,
    /// whitespace/punctuation-collapsed — catches failures that differ
    /// only in formatting, which must not re-enter as "new" data.</summary>
    private static string NormFingerprint(string input)
    {
        var sb = new StringBuilder((input ?? "").Length);
        bool ws = false;
        foreach (char ch in (input ?? "").ToLowerInvariant())
        {
            if (char.IsWhiteSpace(ch)) { ws = true; continue; }
            if (char.IsPunctuation(ch) || char.IsSymbol(ch)) continue;
            if (ws && sb.Length > 0) sb.Append(' ');
            ws = false;
            sb.Append(ch);
        }
        byte[] h = SHA256.HashData(Encoding.UTF8.GetBytes(sb.ToString()));
        return Convert.ToHexString(h)[..16].ToLowerInvariant();
    }

    /// <summary>Append one failure record (§10 fields) into the
    /// capability's own pool, with §11 dedup: an exact or normalized
    /// fingerprint repeat bumps seen_count/last_seen instead of
    /// appending. Never throws; returns the record on success.</summary>
    public static Dictionary<string, object?>? Record(
        string toolRoot, string input, string generation,
        string failureClass, string expected, string actual,
        string evidence, string severity = "medium",
        bool reproducible = true, string? reason = null,
        string? modelVersion = null, string? runtimeVersion = null,
        string? provenance = null, string? capabilityId = null)
    {
        try
        {
            if (!Classes.Contains(failureClass))
                failureClass = "reasoning";
            string fp = Fingerprint(input);
            string nfp = NormFingerprint(input);
            string now = XcPaths.IsoNow();
            var rec = new Dictionary<string, object?>
            {
                ["format"] = Format,
                ["input_fingerprint"] = fp,
                ["norm_fingerprint"] = nfp,
                ["input"] = input,
                ["generation"] = generation,
                ["model_version"] = modelVersion ?? generation,
                ["runtime_version"] =
                    runtimeVersion ?? "xc-native-cpp23",
                ["failure_class"] = failureClass,
                // AC §15: the pool bucket is a storage detail; the
                // canonical capability_id is the governing identity.
                ["capability_id"] = capabilityId ?? "",
                ["failure_reason"] = reason ?? "",
                ["expected"] = expected,
                ["actual"] = actual,
                ["evidence"] = evidence,
                ["provenance"] = provenance ?? "",
                ["severity"] = severity,
                ["reproducible"] = reproducible,
                ["state"] = "OPEN",
                ["seen_count"] = 1,
                ["recorded_at"] = now,
                ["last_seen"] = now,
            };
            string dir = PoolDir(toolRoot);
            Directory.CreateDirectory(dir);
            string path = PoolPath(toolRoot, failureClass);
            // §11 dedup: a repeat of an existing fingerprint updates
            // the record in place — identical failures never dominate.
            var lines = File.Exists(path)
                ? File.ReadAllLines(path).ToList()
                : new List<string>();
            for (int i = 0; i < lines.Count; ++i)
            {
                if (!lines[i].TrimStart().StartsWith("{")) continue;
                try
                {
                    var old = JsonSerializer
                        .Deserialize<Dictionary<string, object?>>(
                            lines[i]);
                    if (old == null ||
                        !old.TryGetValue("input_fingerprint",
                            out object? ofp) ||
                        !old.TryGetValue("norm_fingerprint",
                            out object? onfp))
                        continue;
                    if (ofp?.ToString() != fp && onfp?.ToString() != nfp)
                        continue;
                    int seen = old.TryGetValue("seen_count",
                        out object? sc) && sc is JsonElement je &&
                        je.TryGetInt32(out int n) ? n : 1;
                    old["seen_count"] = seen + 1;
                    old["last_seen"] = now;
                    if (severity == "high") old["severity"] = "high";
                    lines[i] = CanonicalJson.CanonicalDict(old);
                    File.WriteAllLines(path, lines);
                    rec["seen_count"] = seen + 1;
                    rec["dedup"] = "repeat";
                    return rec;
                }
                catch (JsonException) { }
            }
            File.AppendAllText(
                path, CanonicalJson.CanonicalDict(rec) + "\n");
            RotateIfNeeded(path);
            return rec;
        }
        catch { return null; }
    }

    /// <summary>Bounded store: when a class pool exceeds MaxEntries,
    /// keep the newest records (lineage lives in eval reports; the
    /// pool is a working set for the recovery loop).</summary>
    private static void RotateIfNeeded(string path)
    {
        var lines = File.ReadAllLines(path);
        if (lines.Length <= MaxEntries) return;
        File.WriteAllLines(
            path, lines.Skip(lines.Length - MaxEntries).ToArray());
    }

    /// <summary>All records for one capability class — the recovery
    /// loop's FailurePool read path (§10 per-capability pools).</summary>
    public static List<Dictionary<string, object?>> ReadPool(
        string toolRoot, string cls)
    {
        var rows = new List<Dictionary<string, object?>>();
        string path = PoolPath(toolRoot, cls);
        if (!File.Exists(path)) return rows;
        foreach (string line in File.ReadLines(path))
        {
            if (!line.TrimStart().StartsWith("{")) continue;
            try
            {
                var rec = JsonSerializer
                    .Deserialize<Dictionary<string, object?>>(line);
                if (rec != null) rows.Add(rec);
            }
            catch (JsonException) { }
        }
        return rows;
    }

    /// <summary>Pool status for converge-check / governance reads:
    /// per-capability entry counts across every pool-<class>.jsonl.</summary>
    public static Dictionary<string, object?> Status(string toolRoot)
    {
        var byClass = Classes.ToDictionary(c => c, _ => 0);
        var byState = States.ToDictionary(s => s, _ => 0);
        int total = 0, repeats = 0;
        string dir = PoolDir(toolRoot);
        if (Directory.Exists(dir))
            foreach (string f in Directory.EnumerateFiles(
                         dir, "pool-*.jsonl"))
                foreach (string line in File.ReadLines(f))
                {
                    if (!line.TrimStart().StartsWith("{")) continue;
                    try
                    {
                        using var doc = JsonDocument.Parse(line);
                        var root = doc.RootElement;
                        if (root.TryGetProperty("failure_class",
                                out var fc) &&
                            byClass.ContainsKey(fc.GetString() ?? ""))
                            ++byClass[fc.GetString()!];
                        if (root.TryGetProperty("state", out var st) &&
                            byState.ContainsKey(st.GetString() ?? ""))
                            ++byState[st.GetString()!];
                        if (root.TryGetProperty("seen_count",
                                out var sc) &&
                            sc.TryGetInt32(out int n) && n > 1)
                            repeats += n - 1;
                        ++total;
                    }
                    catch (JsonException) { }
                }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = Format,
            ["entries"] = total,
            ["repeat_observations"] = repeats,
            ["by_class"] = byClass,
            ["by_state"] = byState,
            ["per_capability_pools"] = true,
            ["bounded"] = MaxEntries,
        };
    }
}
