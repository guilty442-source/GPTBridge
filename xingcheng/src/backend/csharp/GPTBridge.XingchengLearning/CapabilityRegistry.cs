// CapabilityRegistry.cs — ``star-capability-registry/v1`` (capability
// unification directive §3, §11, §28, §83).
//
// The canonical answer to "which capabilities does the system have?"
// — the only capability catalog. Every other surface (maturation
// sequence, failure pool, eval categories, feature catalog, kernel
// registry, lifecycle) references ``capability_id`` and never builds
// its own name list (§28-§30, §96: no mega-registry — the registry
// describes capabilities, architecture describes structure, kernels
// describe execution; IDs cross-reference).
//
// The registry never executes (§11): it describes, resolves and
// validates. Code is the source of truth; the persisted file is the
// governed projection stamped with progression state.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CapabilityRegistry
{
    public const string Format = "star-capability-registry/v1";
    public const string Rel =
        "xingcheng/runtime/state/capability-registry.json";
    /// <summary>AC §26: the registry snapshot bound to the last
    /// successful promotion — the promotion gate's delta baseline,
    /// refreshed only when the gate allows.</summary>
    public const string BaselineRel =
        "xingcheng/runtime/state/capability-registry.promoted.json";

    public static readonly CapabilityDescriptor[] Canonical =
        CapabilitySeed.All;

    private static string Path_(string toolRoot) =>
        Path.Combine(toolRoot, Rel.Replace('/', Path.DirectorySeparatorChar));

    // ----------------------------------------------------- resolution --

    private static string Normalize(string name) =>
        (name ?? "").Trim().ToLowerInvariant()
            .Replace('-', '_').Replace(' ', '_');

    /// <summary>Alias → canonical id map, built once from the seed
    /// rows. An alias that collides with a different canonical id or
    /// another capability's alias is a seed defect — the collision is
    /// surfaced by CheckConsistency, and Resolve keeps the canonical
    /// id match first.</summary>
    public static IReadOnlyDictionary<string, string> AliasMap()
    {
        var map = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var d in Canonical)
        {
            map[Normalize(d.CapabilityId)] = d.CapabilityId;
            foreach (var a in d.Aliases)
                map.TryAdd(Normalize(a), d.CapabilityId);
        }
        return map;
    }

    /// <summary>Resolve any historical spelling to the canonical
    /// capability_id; null when the name is unknown (§83).</summary>
    public static string? Resolve(string name)
    {
        string key = Normalize(name);
        var map = AliasMap();
        return map.TryGetValue(key, out string? id) ? id : null;
    }

    public static CapabilityDescriptor? Get(string name)
    {
        string? id = Resolve(name);
        return id == null ? null
            : Canonical.FirstOrDefault(d => d.CapabilityId == id);
    }

    // -------------------------------------------------------- emit --

    /// <summary>Stamp descriptor maturity_stage/status/protected from
    /// the progression state (Maturation300M): frozen → MATURE +
    /// protected (§68); unsupported → UNAVAILABLE; pending stays
    /// IMPLEMENTED; unsequenced capabilities keep their seed
    /// status.</summary>
    private static void StampProgression(
        CapabilityDescriptor d,
        IReadOnlyDictionary<string, object?> matState)
    {
        if (matState.TryGetValue("capabilities", out object? raw) &&
            raw is Dictionary<string, object?> caps &&
            caps.TryGetValue(d.CapabilityId, out object? crow) &&
            crow is Dictionary<string, object?> cap)
        {
            string st = cap.TryGetValue("status", out object? s)
                ? s?.ToString() ?? "pending" : "pending";
            d.MaturityStage = st;
            if (st == "frozen")
            {
                d.Status = "MATURE";
                d.Protected = true;
            }
            else if (st == "unsupported")
            {
                d.Status = "UNAVAILABLE";
            }
        }
    }

    /// <summary>Persist the canonical registry projection (atomic
    /// write) and return the emitted document.</summary>
    public static Dictionary<string, object?> Emit(string toolRoot)
    {
        var matState = Maturation300M.LoadState(toolRoot);
        var rows = new List<object?>();
        var byClass = new Dictionary<string, int>();
        var byPlane = new Dictionary<string, int>();
        var byStatus = new Dictionary<string, int>();
        foreach (var d in Canonical)
        {
            StampProgression(d, matState);
            rows.Add(d.ToDict());
            byClass[d.CapabilityClass] =
                byClass.GetValueOrDefault(d.CapabilityClass) + 1;
            byPlane[d.OwnerPlane] =
                byPlane.GetValueOrDefault(d.OwnerPlane) + 1;
            byStatus[d.Status] =
                byStatus.GetValueOrDefault(d.Status) + 1;
        }
        var doc = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["generated_at"] = XcPaths.IsoNow(),
            ["model_core"] = ArchitectureTaxonomy.ModelCore,
            ["architecture_generation"] =
                ArchitectureTaxonomy.CanonicalArchitecture,
            ["capability_count"] = Canonical.Length,
            ["capabilities"] = rows,
            ["aliases"] = AliasMap().ToDictionary(
                kv => kv.Key, kv => (object?)kv.Value),
            ["summary"] = new Dictionary<string, object?>
            {
                ["by_class"] = byClass.ToDictionary(
                    kv => kv.Key, kv => (object?)kv.Value),
                ["by_owner_plane"] = byPlane.ToDictionary(
                    kv => kv.Key, kv => (object?)kv.Value),
                ["by_status"] = byStatus.ToDictionary(
                    kv => kv.Key, kv => (object?)kv.Value),
            },
        };
        ModelLifecycle.AtomicWrite(
            Path_(toolRoot), CanonicalJson.PrettyDict(doc) + "\n");
        doc["ok"] = true;
        doc["registry_file"] = Rel;
        return doc;
    }

    // ---------------------------------------------------- validation --

    /// <summary>Schema validation for a persisted registry file (any
    /// producer): format, unique canonical ids, closed vocabularies,
    /// alias sanity.</summary>
    public static Dictionary<string, object?> Validate(string file)
    {
        if (string.IsNullOrEmpty(file) || !NativeStateProjection.Exists(file))
            throw new ExecutorError(
                "CAPABILITY_REGISTRY_INVALID", "registry file missing");
        JsonElement root;
        try
        {
            root = JsonDocument.Parse(NativeStateProjection.ReadAllText(file))
                               .RootElement;
        }
        catch (JsonException)
        {
            throw new ExecutorError("CAPABILITY_REGISTRY_INVALID",
                                    "registry not valid json");
        }
        if (root.ValueKind != JsonValueKind.Object ||
            !root.TryGetProperty("format", out var f) ||
            f.GetString() != Format)
            throw new ExecutorError("CAPABILITY_REGISTRY_INVALID",
                                    "bad registry format");
        if (!root.TryGetProperty("capabilities", out var caps) ||
            caps.ValueKind != JsonValueKind.Array)
            throw new ExecutorError("CAPABILITY_REGISTRY_INVALID",
                                    "capabilities missing");
        var ids = new HashSet<string>(StringComparer.Ordinal);
        var failures = new List<object?>();
        var required = new[]
        {
            "capability_id", "category", "capability_class",
            "owner_plane", "model_dependency", "runtime_dependency",
            "data_dependency", "evaluation_suite", "maturity_stage",
            "resource_class", "training_policy",
            "regression_dependencies", "status",
        };
        foreach (var el in caps.EnumerateArray())
        {
            string id = el.TryGetProperty("capability_id", out var i)
                ? i.GetString() ?? "" : "";
            foreach (var k in required)
                if (!el.TryGetProperty(k, out _))
                    failures.Add(new Dictionary<string, object?>
                        { ["capability_id"] = id, ["missing"] = k });
            if (id.Length == 0 || !ids.Add(id))
                failures.Add(new Dictionary<string, object?>
                    { ["capability_id"] = id, ["error"] = "duplicate" });
            foreach (var (prop, vocab) in new (string, string[])[]
            {
                ("capability_class", CapabilityDescriptor.Classes),
                ("owner_plane", CapabilityDescriptor.OwnerPlanes),
                ("status", CapabilityDescriptor.Statuses),
            })
            {
                if (el.TryGetProperty(prop, out var v) &&
                    v.ValueKind == JsonValueKind.String &&
                    !vocab.Contains(v.GetString()))
                    failures.Add(new Dictionary<string, object?>
                    {
                        ["capability_id"] = id,
                        ["error"] = $"bad {prop} {v.GetString()}",
                    });
            }
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = failures.Count == 0,
            ["format"] = Format,
            ["file"] = file,
            ["capability_count"] = caps.GetArrayLength(),
            ["failures"] = failures,
        };
    }
}
