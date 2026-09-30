// FailurePool.cs — ``star-capability-failure-pool/v1``.
//
// Unified capability-failure store: every evaluation surface records
// failures here with a fixed class vocabulary, so the future
// unfreeze phase can consume the pool directly — no re-mining logs.
// Bounded append-only JSONL under xingcheng/runtime/state/failure-pool;
// recording never throws — a pool write failure must not mask the
// evaluation verdict it was describing.

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
    public const int MaxEntries = 4096;   // bounded: oldest rotate out

    public static readonly string[] Classes =
    {
        "instruction", "context", "math", "reading", "coding",
        "tool-call", "structured-output", "vision", "reasoning",
        "routing", "grounding",
    };

    /// <summary>Map an evaluation suite name to its failure class —
    /// the suite vocabulary is closed, so this mapping is total.</summary>
    public static string ClassForSuite(string suite) => suite switch
    {
        "tool-decision" or "tool_call_format" => "tool-call",
        "structured-output" or "fim" => "structured-output",
        "vision" => "vision",
        "moe-routing" or "expert_routing" => "routing",
        "citation" or "rag" => "grounding",
        "code" or "coding" => "coding",
        "math" => "math",
        "reading" => "reading",
        "long-context" or "context_tracking" or "multi_turn"
            => "context",
        "runtime-parity" or "precision-parity" or "kv-cache"
            or "recurrent-state" or "generation-migration"
            or "bundle-provenance" => "reasoning",
        _ => "instruction",
    };

    private static string PoolPath(string toolRoot) => Path.Combine(
        toolRoot, PoolDirRel.Replace('/', Path.DirectorySeparatorChar),
        PoolFile);

    private static string Fingerprint(string input)
    {
        byte[] h = SHA256.HashData(Encoding.UTF8.GetBytes(input ?? ""));
        return Convert.ToHexString(h)[..16].ToLowerInvariant();
    }

    /// <summary>Append one failure record. Never throws; returns the
    /// record on success, null when the store is unwritable.</summary>
    public static Dictionary<string, object?>? Record(
        string toolRoot, string input, string generation,
        string failureClass, string expected, string actual,
        string evidence, string severity = "medium",
        bool reproducible = true)
    {
        try
        {
            if (!Classes.Contains(failureClass))
                failureClass = "reasoning";
            var rec = new Dictionary<string, object?>
            {
                ["format"] = Format,
                ["input_fingerprint"] = Fingerprint(input),
                ["generation"] = generation,
                ["failure_class"] = failureClass,
                ["expected"] = expected,
                ["actual"] = actual,
                ["evidence"] = evidence,
                ["severity"] = severity,
                ["reproducible"] = reproducible,
                ["recorded_at"] = XcPaths.IsoNow(),
            };
            string path = PoolPath(toolRoot);
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.AppendAllText(
                path, CanonicalJson.CanonicalDict(rec) + "\n");
            RotateIfNeeded(path);
            return rec;
        }
        catch { return null; }
    }

    /// <summary>Bounded store: when the pool exceeds MaxEntries, keep
    /// the newest records (lineage lives in eval reports; the pool is a
    /// working set for the next unfreeze phase).</summary>
    private static void RotateIfNeeded(string path)
    {
        var lines = File.ReadAllLines(path);
        if (lines.Length <= MaxEntries) return;
        File.WriteAllLines(
            path, lines.Skip(lines.Length - MaxEntries).ToArray());
    }

    /// <summary>Pool status for converge-check / governance reads.</summary>
    public static Dictionary<string, object?> Status(string toolRoot)
    {
        string path = PoolPath(toolRoot);
        var byClass = Classes.ToDictionary(c => c, _ => 0);
        int total = 0;
        if (File.Exists(path))
            foreach (string line in File.ReadLines(path))
            {
                if (!line.TrimStart().StartsWith("{")) continue;
                try
                {
                    using var doc = JsonDocument.Parse(line);
                    if (doc.RootElement.TryGetProperty(
                            "failure_class", out var fc) &&
                        byClass.ContainsKey(fc.GetString() ?? ""))
                        ++byClass[fc.GetString()!];
                    ++total;
                }
                catch (JsonException) { }
            }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = Format,
            ["entries"] = total,
            ["by_class"] = byClass,
            ["bounded"] = MaxEntries,
        };
    }
}
