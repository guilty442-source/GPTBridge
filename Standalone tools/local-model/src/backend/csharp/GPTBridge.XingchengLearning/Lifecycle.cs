// Lifecycle.cs — ``star-model-lifecycle/v1`` port (ModelLifecycle).
//
// State machine, artifact versioning, governed rollback gate, retention
// retire_weights and atomic lifecycle.json persistence — identical
// semantics to the retired Python implementation. Rollback compat
// fingerprinting: Python resolved torch config_json sha256; the native lane
// resolves a bundle manifest's canonical ``config`` object sha256 (same
// deny-by-default contract: no fingerprint -> no targets).

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal sealed class ModelLifecycle
{
    public const string LifecycleFormat = "star-model-lifecycle/v1";

    public static readonly HashSet<string> States = new(StringComparer.Ordinal)
    {
        "UNINITIALIZED", "INITIALIZED", "PRETRAINING", "PRETRAINED",
        "SFT_TRAINING", "INSTRUCT_READY", "EVALUATING", "READY",
        "LOADED", "UNLOADED", "FAILED",
    };

    private static readonly Dictionary<string, HashSet<string>> Transitions =
        new(StringComparer.Ordinal)
        {
            ["UNINITIALIZED"] = new() { "INITIALIZED" },
            ["INITIALIZED"] = new() { "PRETRAINING", "SFT_TRAINING", "FAILED" },
            ["PRETRAINING"] = new() { "PRETRAINED", "FAILED" },
            ["PRETRAINED"] = new()
                { "SFT_TRAINING", "EVALUATING", "READY", "PRETRAINING", "FAILED" },
            ["SFT_TRAINING"] = new() { "INSTRUCT_READY", "FAILED" },
            ["INSTRUCT_READY"] = new()
                { "EVALUATING", "READY", "SFT_TRAINING", "FAILED" },
            ["EVALUATING"] = new() { "READY", "INSTRUCT_READY", "FAILED" },
            ["READY"] = new() { "LOADED", "SFT_TRAINING", "EVALUATING", "FAILED" },
            ["LOADED"] = new() { "UNLOADED", "FAILED" },
            ["UNLOADED"] = new() { "LOADED", "READY", "FAILED" },
            ["FAILED"] = new() { "INITIALIZED" },
        };

    public static readonly string[] ArtifactKinds =
        { "weights", "tokenizer", "config", "training_state", "evaluation_report" };

    public string ModelId { get; }
    public string State { get; private set; } = "UNINITIALIZED";
    public Dictionary<string, Dictionary<string, object?>> Artifacts { get; private set; } = new();
    public List<Dictionary<string, object?>> History { get; private set; } = new();
    public int ActiveWeightsVersion { get; private set; }

    public ModelLifecycle(string modelId) => ModelId = modelId;

    private static string UtcNow()
        => DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'");

    public void Transition(string target, string reason = "")
    {
        target = (target ?? "").ToUpperInvariant();
        if (!States.Contains(target))
            throw new ArgumentException($"LIFECYCLE_STATE_UNKNOWN:{target}");
        if (!Transitions[State].Contains(target))
            throw new ArgumentException(
                $"LIFECYCLE_TRANSITION_DENIED:{State}->{target}");
        string previous = State;
        State = target;
        History.Add(new Dictionary<string, object?>
        {
            ["at"] = UtcNow(),
            ["from"] = previous,
            ["to"] = target,
            ["reason"] = reason ?? "",
        });
    }

    public void Fail(string reason) => Transition("FAILED", reason);

    public Dictionary<string, object?> RegisterArtifact(
        string kind, string path,
        Dictionary<string, object?>? metadata = null,
        bool activate = false)
    {
        if (!ArtifactKinds.Contains(kind))
            throw new ArgumentException($"ARTIFACT_KIND_UNKNOWN:{kind}");
        if (!File.Exists(path))
            throw new FileNotFoundException($"ARTIFACT_MISSING:{path}");
        if (!Artifacts.TryGetValue(kind, out var versions))
        {
            versions = new Dictionary<string, object?>
            {
                ["versions"] = new List<object?>(),
            };
            Artifacts[kind] = versions;
        }
        if (versions["versions"] is not List<object?> versionList)
        {
            // Normalize decoded/typed lists into List<object?> so
            // load-then-register paths share one representation.
            versionList = WeightVersionsFor(kind).Cast<object?>().ToList();
            versions["versions"] = versionList;
        }
        var known = versionList
            .OfType<Dictionary<string, object?>>()
            .Select(e => Convert.ToInt32(e["version"]))
            .Concat(
                versions.TryGetValue("retired", out object? r) &&
                r is IEnumerable<object?> rl
                    ? rl.OfType<Dictionary<string, object?>>()
                        .Select(e => Convert.ToInt32(e["version"]))
                    : Enumerable.Empty<int>())
            .ToList();
        var entry = new Dictionary<string, object?>
        {
            ["version"] = known.Count > 0 ? known.Max() + 1 : 1,
            ["path"] = path,
            ["sha256"] = TransformerTrainingRepository.Sha256File(path),
            ["registered_at"] = UtcNow(),
            ["metadata"] = metadata ?? new Dictionary<string, object?>(),
        };
        versionList.Add(entry);
        if (kind == "weights" && activate)
            ActiveWeightsVersion = (int)entry["version"]!;
        History.Add(new Dictionary<string, object?>
        {
            ["at"] = UtcNow(),
            ["event"] = "artifact_registered",
            ["kind"] = kind,
            ["version"] = entry["version"],
            ["sha256"] = entry["sha256"],
        });
        return entry;
    }

    public Dictionary<string, object?> RollbackWeights(int version)
    {
        foreach (var entry in WeightVersions())
        {
            if (Convert.ToInt32(entry["version"]) == version)
            {
                ActiveWeightsVersion = version;
                History.Add(new Dictionary<string, object?>
                {
                    ["at"] = UtcNow(),
                    ["event"] = "weights_rollback",
                    ["version"] = version,
                });
                return entry;
            }
        }
        throw new ArgumentException($"WEIGHTS_VERSION_UNKNOWN:{version}");
    }

    public List<int> RollbackTargetVersions(
        string? compatFingerprint,
        Func<string, string?>? compatResolver = null,
        IEnumerable<int>? excludeVersions = null,
        int minMaturityLevel = 1)
    {
        if (string.IsNullOrEmpty(compatFingerprint))
            return new List<int>();
        var excluded = new HashSet<int>(excludeVersions ?? Enumerable.Empty<int>());
        var targets = new List<int>();
        foreach (var entry in WeightVersions())
        {
            int version = Convert.ToInt32(entry["version"] ?? 0);
            if (excluded.Contains(version))
                continue;
            string path = (string?)entry["path"] ?? "";
            if (path.Length == 0 || !File.Exists(path))
                continue;
            var metadata = entry.TryGetValue("metadata", out object? m)
                ? m as Dictionary<string, object?> : null;
            bool certified = false;
            if (metadata != null && metadata.TryGetValue("maturity_level", out object? lvl) && lvl != null)
            {
                try { certified = Convert.ToInt32(lvl) >= minMaturityLevel; }
                catch { certified = false; }
            }
            if (!certified)
            {
                string report = "";
                if (metadata != null &&
                    metadata.TryGetValue("maturity_report", out object? mr))
                    report = mr?.ToString() ?? "";
                certified = report.Trim().Length > 0;
            }
            if (!certified)
                continue;
            string fingerprint = "";
            if (metadata != null &&
                metadata.TryGetValue("config_sha256", out object? fp))
                fingerprint = fp?.ToString() ?? "";
            if (fingerprint.Length == 0 && compatResolver != null)
                fingerprint = compatResolver(path) ?? "";
            if (fingerprint.Length == 0 || fingerprint != compatFingerprint)
                continue;
            targets.Add(version);
        }
        targets.Sort();
        return targets;
    }

    public Dictionary<string, object?> GovernedRollbackWeights(
        int version,
        string? compatFingerprint,
        Func<string, string?>? compatResolver = null,
        IEnumerable<int>? excludeVersions = null,
        int minMaturityLevel = 1)
    {
        var targets = RollbackTargetVersions(
            compatFingerprint, compatResolver, excludeVersions, minMaturityLevel);
        if (!targets.Contains(version))
            throw new ArgumentException($"WEIGHTS_ROLLBACK_DENIED:{version}");
        var entry = RollbackWeights(version);
        History[^1]["gate"] = "star-rollback-gate/v1";
        return entry;
    }

    public void RecordEvent(string evt, params (string Key, object? Value)[] fields)
    {
        var entry = new Dictionary<string, object?>
        {
            ["at"] = UtcNow(),
            ["event"] = evt,
        };
        foreach (var (key, value) in fields)
            entry[key] = value;
        History.Add(entry);
    }

    public Dictionary<string, object?>? ActiveWeights()
    {
        foreach (var entry in WeightVersions())
            if (Convert.ToInt32(entry["version"]) == ActiveWeightsVersion)
                return entry;
        return null;
    }

    /// <summary>Absolute paths of every registered weights version
    /// (used by generation purge to enumerate prior artifacts).</summary>
    public List<string> WeightVersionPaths()
    {
        var paths = new List<string>();
        foreach (var entry in WeightVersions())
            if (entry.TryGetValue("path", out object? p) &&
                p is string s && s.Length > 0)
                paths.Add(s);
        return paths;
    }

    public List<Dictionary<string, object?>> RetireWeights(
        int keepLatest = 1, HashSet<string>? extraKeepPaths = null)
    {
        if (!Artifacts.TryGetValue("weights", out var weights))
            return new List<Dictionary<string, object?>>();
        var versions = WeightVersions().ToList();
        int keep = Math.Max(1, keepLatest);
        var keepVersions = versions.TakeLast(keep)
            .Select(e => Convert.ToInt32(e["version"])).ToHashSet();
        if (ActiveWeightsVersion != 0)
            keepVersions.Add(ActiveWeightsVersion);
        var extra = extraKeepPaths ?? new HashSet<string>();
        var retiredNow = new List<Dictionary<string, object?>>();
        var remaining = new List<object?>();
        foreach (var entry in versions)
        {
            bool entryProtected =
                keepVersions.Contains(Convert.ToInt32(entry["version"])) ||
                extra.Contains((string?)entry["path"] ?? "");
            if (entryProtected)
            {
                remaining.Add(entry);
                continue;
            }
            var moved = new Dictionary<string, object?>(entry);
            moved["retired_at"] = UtcNow();
            retiredNow.Add(moved);
        }
        if (retiredNow.Count == 0)
            return retiredNow;
        weights["versions"] = remaining;
        if (!weights.TryGetValue("retired", out object? rl) ||
            rl is not List<object?> retiredList)
        {
            retiredList = new List<object?>();
            weights["retired"] = retiredList;
        }
        retiredList.AddRange(retiredNow);
        History.Add(new Dictionary<string, object?>
        {
            ["at"] = UtcNow(),
            ["event"] = "weights_retired",
            ["versions"] = retiredNow.Select(e => (object?)Convert.ToInt32(e["version"])).ToList(),
            ["kept"] = keepVersions.Order().Select(v => (object?)v).ToList(),
        });
        return retiredNow;
    }

    private IEnumerable<Dictionary<string, object?>> WeightVersions()
        => WeightVersionsFor("weights");

    private IEnumerable<Dictionary<string, object?>> WeightVersionsFor(string kind)
    {
        if (!Artifacts.TryGetValue(kind, out var w) ||
            !w.TryGetValue("versions", out object? v))
            yield break;
        IEnumerable<object?>? items = v switch
        {
            List<object?> l => l,
            List<Dictionary<string, object?>> l => l,
            _ => null,
        };
        if (items == null) yield break;
        foreach (object? item in items)
            if (item is Dictionary<string, object?> entry)
                yield return entry;
    }

    // ------------------------------------------------------------ persist --

    public Dictionary<string, object?> ToDict() => new()
    {
        ["format"] = LifecycleFormat,
        ["model_id"] = ModelId,
        ["state"] = State,
        ["active_weights_version"] = ActiveWeightsVersion,
        ["artifacts"] = Artifacts,
        ["history"] = History,
    };

    public string Save(string directory)
    {
        string path = Path.Combine(directory, "lifecycle.json");
        AtomicWrite(path, CanonicalJson.PrettyDict(ToDict()) + "\n");
        return path;
    }

    public static ModelLifecycle Load(string directory)
    {
        string path = Path.Combine(directory, "lifecycle.json");
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        return FromElement(doc.RootElement);
    }

    public static ModelLifecycle LoadOrCreate(string directory, string modelId)
    {
        string path = Path.Combine(directory, "lifecycle.json");
        return File.Exists(path) ? Load(directory) : new ModelLifecycle(modelId);
    }

    private static ModelLifecycle FromElement(JsonElement root)
    {
        if (root.TryGetProperty("format", out var f) == false ||
            f.GetString() != LifecycleFormat)
            throw new ArgumentException("LIFECYCLE_FORMAT_UNSUPPORTED");
        var lifecycle = new ModelLifecycle(
            root.GetProperty("model_id").GetString() ?? "");
        string state = root.TryGetProperty("state", out var s)
            ? s.GetString() ?? "UNINITIALIZED" : "UNINITIALIZED";
        if (!States.Contains(state))
            throw new ArgumentException($"LIFECYCLE_STATE_UNKNOWN:{state}");
        lifecycle.State = state;
        if (root.TryGetProperty("active_weights_version", out var awv) &&
            awv.ValueKind == JsonValueKind.Number)
            lifecycle.ActiveWeightsVersion = awv.GetInt32();
        if (root.TryGetProperty("artifacts", out var artifacts) &&
            artifacts.ValueKind == JsonValueKind.Object)
        {
            foreach (var kind in artifacts.EnumerateObject())
            {
                if (kind.Value.ValueKind != JsonValueKind.Object) continue;
                var group = new Dictionary<string, object?>();
                foreach (var prop in kind.Value.EnumerateObject())
                    group[prop.Name] = Decode(prop.Value);
                lifecycle.Artifacts[kind.Name] = group;
            }
        }
        if (root.TryGetProperty("history", out var history) &&
            history.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in history.EnumerateArray())
                if (Decode(item) is Dictionary<string, object?> entry)
                    lifecycle.History.Add(entry);
        }
        return lifecycle;
    }

    /// <summary>JsonElement → CLR tree (dict/list/primitive).</summary>
    internal static object? Decode(JsonElement el)
    {
        switch (el.ValueKind)
        {
            case JsonValueKind.Object:
                var map = new Dictionary<string, object?>(StringComparer.Ordinal);
                foreach (var p in el.EnumerateObject())
                    map[p.Name] = Decode(p.Value);
                return map;
            case JsonValueKind.Array:
                return el.EnumerateArray().Select(Decode).ToList();
            case JsonValueKind.String:
                return el.GetString();
            case JsonValueKind.Number:
                if (el.TryGetInt64(out long l)) return l;
                return el.GetDouble();
            case JsonValueKind.True: return true;
            case JsonValueKind.False: return false;
            default: return null;
        }
    }

    /// <summary>CLR tree → JsonElement (for canonicalizing inside events).</summary>
    internal static JsonElement Encode(object? value)
        => JsonSerializer.SerializeToElement(value);

    internal static void AtomicWrite(string path, string payload)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        string temp = Path.Combine(
            Path.GetDirectoryName(path)!,
            Path.GetFileName(path) + "." + Guid.NewGuid().ToString("N") + ".tmp");
        try
        {
            File.WriteAllText(temp, payload, new System.Text.UTF8Encoding(false));
            File.Move(temp, path, overwrite: true);
        }
        catch
        {
            try { File.Delete(temp); } catch { /* best effort */ }
            throw;
        }
    }
}
