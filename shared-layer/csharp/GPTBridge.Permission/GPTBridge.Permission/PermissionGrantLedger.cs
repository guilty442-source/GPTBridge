using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.Permission;

/// <summary>
/// Permission grant ledger — append-only JSONL persistence (A46/A435).
///
/// Direct port of core_system/permission_grant_ledger.py: grants and
/// lifecycle transitions are recorded in an append-only JSON-Lines
/// ledger so they survive restarts, carry version and revocation
/// evidence, and can be audited without in-memory state.  Entries are
/// never mutated; a lifecycle transition appends a new entry.
///
/// Canonical paths (unchanged from the retired Python implementation):
///   main-system/runtime/state/permission-grant-ledger.jsonl
///   main-system/runtime/state/permission-violation-ledger.jsonl
/// </summary>
public sealed class PermissionGrantLedger
{
    private static readonly HashSet<string> LifecycleOps = new(
        StringComparer.Ordinal)
    {
        "terminate", "renew", "restrict", "suspend", "revoke",
    };

    private readonly object _lock = new();
    private readonly string _ledgerPath;
    private readonly string _violationPath;
    private readonly Func<DateTimeOffset> _clock;

    public PermissionGrantLedger(
        string ledgerPath, string? violationPath = null,
        Func<DateTimeOffset>? clock = null)
    {
        _ledgerPath = ledgerPath;
        _violationPath = violationPath ??
            Path.Combine(
                Path.GetDirectoryName(Path.GetFullPath(ledgerPath))!,
                "permission-violation-ledger.jsonl");
        _clock = clock ?? (() => DateTimeOffset.UtcNow);
    }

    /// <summary>Default ledger path under a project root.</summary>
    public static string DefaultLedgerPath(string projectRoot) =>
        Path.Combine(projectRoot, "main-system", "runtime", "state",
                     "permission-grant-ledger.jsonl");

    private static string IsoNow(DateTimeOffset t) =>
        t.UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss.fffffffzzz")
            .Replace("+00:00", "Z");

    private int NextSequence()
    {
        if (!File.Exists(_ledgerPath)) return 1;
        var count = 0;
        try
        {
            using var reader = new StreamReader(_ledgerPath);
            while (reader.ReadLine() is not null) count++;
        }
        catch (IOException) { }
        return count + 1;
    }

    private int AppendEntry(JsonObject entry, string path)
    {
        var dir = Path.GetDirectoryName(Path.GetFullPath(path));
        if (dir is { Length: > 0 }) Directory.CreateDirectory(dir);
        lock (_lock)
        {
            // Sorted keys match json.dumps(sort_keys=True) parity.
            var ordered = new JsonObject();
            foreach (var kv in entry.OrderBy(
                         p => p.Key, StringComparer.Ordinal))
                ordered[kv.Key] = kv.Value?.DeepClone();
            File.AppendAllText(
                path, ordered.ToJsonString() + "\n");
        }
        return entry["sequence"]!.GetValue<int>();
    }

    /// <summary>Record a permission issuance (operation=issue).</summary>
    public int RecordGrant(
        string permissionId, string actor, string capability,
        string target, string action, string dataScope,
        string requester, string reviewFinding = "",
        string reviewId = "", IReadOnlyList<string>? basis = null)
    {
        var entry = new JsonObject
        {
            ["sequence"] = NextSequence(),
            ["timestamp"] = IsoNow(_clock()),
            ["operation"] = "issue",
            ["permission_id"] = permissionId,
            ["actor"] = actor,
            ["capability"] = capability,
            ["target"] = target,
            ["action"] = action,
            ["data_scope"] = dataScope,
            ["status"] = "issued",
            ["requester"] = requester,
            ["review_finding"] = reviewFinding,
            ["review_id"] = reviewId,
            ["basis"] = new JsonArray(
                (basis ?? Array.Empty<string>())
                    .Select(b => (JsonNode?)b).ToArray()),
        };
        return AppendEntry(entry, _ledgerPath);
    }

    /// <summary>
    /// Record a lifecycle transition (terminate/renew/restrict/suspend/
    /// revoke).  The prior entry is never mutated.
    /// </summary>
    public int RecordLifecycle(
        string operation, string permissionId, string requester,
        IReadOnlyList<string>? basis = null,
        JsonObject? detail = null)
    {
        if (!LifecycleOps.Contains(operation))
            throw new ArgumentException(
                $"invalid lifecycle operation: {operation}");
        var entry = new JsonObject
        {
            ["sequence"] = NextSequence(),
            ["timestamp"] = IsoNow(_clock()),
            ["operation"] = operation,
            ["permission_id"] = permissionId,
            // Parity: Python used operation + "d" ("renewd",
            // "restrictd").  Keep the quirk — ledger consumers only
            // compare "terminated"/"suspended"/"revoked" which are
            // spelled identically either way.
            ["status"] = operation + "d",
            ["requester"] = requester,
            ["basis"] = new JsonArray(
                (basis ?? Array.Empty<string>())
                    .Select(b => (JsonNode?)b).ToArray()),
            ["detail"] = detail?.DeepClone() ?? new JsonObject(),
        };
        return AppendEntry(entry, _ledgerPath);
    }

    /// <summary>Record one compliance violation (A436/A121/A46).</summary>
    public int RecordViolation(
        string sovereignId, JsonObject violation, string requester,
        IReadOnlyList<string>? basis = null)
    {
        var entry = new JsonObject
        {
            ["sequence"] = NextViolationSequence(),
            ["timestamp"] = IsoNow(_clock()),
            ["operation"] = "violation",
            ["sovereign_id"] = sovereignId,
            ["requester"] = requester,
            ["violation"] = violation.DeepClone(),
            ["basis"] = new JsonArray(
                (basis ?? Array.Empty<string>())
                    .Select(b => (JsonNode?)b).ToArray()),
        };
        return AppendEntry(entry, _violationPath);
    }

    private int NextViolationSequence()
    {
        if (!File.Exists(_violationPath)) return 1;
        var count = 0;
        try
        {
            using var reader = new StreamReader(_violationPath);
            while (reader.ReadLine() is not null) count++;
        }
        catch (IOException) { }
        return count + 1;
    }

    private static List<JsonObject> LoadEntries(string path)
    {
        var entries = new List<JsonObject>();
        if (!File.Exists(path)) return entries;
        try
        {
            foreach (var line in File.ReadLines(path))
            {
                var trimmed = line.Trim();
                if (trimmed.Length == 0) continue;
                try
                {
                    if (JsonNode.Parse(trimmed) is JsonObject obj)
                        entries.Add(obj);
                }
                catch (JsonException) { }
            }
        }
        catch (IOException) { }
        return entries;
    }

    /// <summary>Full append-only history for one permission_id.</summary>
    public List<JsonObject> LoadGrantHistory(string permissionId) =>
        LoadEntries(_ledgerPath)
            .Where(e => e["permission_id"]?.GetValue<string>()
                        == permissionId)
            .ToList();

    /// <summary>Most recent ledger entry for a permission_id.</summary>
    public JsonObject? CurrentStatus(string permissionId) =>
        LoadGrantHistory(permissionId).LastOrDefault();

    /// <summary>True if the permission_id has at least one issue entry.</summary>
    public bool WasIssued(string permissionId) =>
        LoadGrantHistory(permissionId)
            .Any(e => e["operation"]?.GetValue<string>() == "issue");

    /// <summary>Every recorded compliance violation (append-only).</summary>
    public List<JsonObject> LoadViolations() => LoadEntries(_violationPath);
}
