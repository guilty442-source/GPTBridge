// Startup manifest loader — C# port of startup_core/startup_config.py.
//
// The manifest (main-system/config/startup_manifest.json) is the single
// source for the bootstrap gate list, the certified dependency DAG,
// probe constants, ports, supervisor policy and the governed-startup
// phase budgets (A191/A192: adjustable without source-code changes).
// The loader is fail-closed: a missing or malformed manifest yields no
// StartupManifest — the host refuses to boot on guessed defaults.

using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.MainSystem;

/// <summary>A191 DEPENDENCY-DECLARATION row.</summary>
public sealed record DependencyDeclaration
{
    public required string Identity { get; init; }
    public required string Owner { get; init; }
    public required string RequiredBy { get; init; }
    public required string Criticality { get; init; }
    public required string ReadinessContract { get; init; }
    public required string Deadline { get; init; }
    public required int RetryBudget { get; init; }
    public required int ShutdownOrder { get; init; }
    /// §10.7: "eager" (started at boot) | "on-demand" (probe only).
    public string Activation { get; init; } = "eager";

    public bool IsCoreCritical => Criticality == "core-critical";
    public bool IsCapabilityCritical => Criticality == "capability-critical";
    public bool IsOptional => Criticality == "optional";
}

/// <summary>Parsed startup_manifest.json.</summary>
public sealed record StartupManifest
{
    public required IReadOnlyList<string> BootstrapPhases { get; init; }
    public required IReadOnlyList<DependencyDeclaration> Dependencies { get; init; }
    public required IReadOnlyDictionary<string, double> ProbeConstants { get; init; }
    public required IReadOnlyDictionary<string, int> Ports { get; init; }
    public required IReadOnlyDictionary<string, JsonNode?> Supervisor { get; init; }
    public required IReadOnlyList<string> StartupPhases { get; init; }
    public required IReadOnlyList<string> CoreReadyConditions { get; init; }
    public required IReadOnlyList<string> CriticalityClasses { get; init; }
    public required IReadOnlyList<string> NoFixedCriticalityServices { get; init; }
    public double StartupCompleteDeadlineMs { get; init; }
    public IReadOnlyDictionary<string, double> PhaseBudgetMs { get; init; } =
        new Dictionary<string, double>();

    public double ProbeConstant(string name, double fallback) =>
        ProbeConstants.TryGetValue(name, out var v) ? v : fallback;

    public int Port(string name, int fallback) =>
        Ports.TryGetValue(name, out var v) ? v : fallback;
}

public static class StartupManifestLoader
{
    /// <summary>Parse the manifest document; throws on malformed input.</summary>
    public static StartupManifest Load(string json)
    {
        var root = JsonNode.Parse(json)?.AsObject()
            ?? throw new InvalidDataException("startup-manifest:not-an-object");

        return new StartupManifest
        {
            BootstrapPhases = StringList(root["bootstrap_phases"]),
            Dependencies = DependencyList(root["dependency_manifest"]),
            ProbeConstants = NumberMap(root["probe_constants"]),
            Ports = IntMap(root["ports"]),
            Supervisor = NodeMap(root["supervisor"]),
            StartupPhases = StringList(
                root["governed_startup"]?["startup_phases"]),
            CoreReadyConditions = StringList(
                root["governed_startup"]?["core_ready_conditions"]),
            CriticalityClasses = StringList(
                root["governed_startup"]?["dependency_criticality_classes"]),
            NoFixedCriticalityServices = StringList(
                root["governed_startup"]?["no_fixed_criticality_services"]),
            StartupCompleteDeadlineMs =
                root["governed_startup"]?["startup_complete_deadline_ms"]
                    ?.GetValue<double>() ?? 40000.0,
            PhaseBudgetMs = NumberMap(
                root["governed_startup"]?["phase_budget_ms"]),
        };
    }

    /// <summary>Load from disk; returns null when absent (fail-closed).</summary>
    public static StartupManifest? LoadFile(string path) =>
        File.Exists(path) ? Load(File.ReadAllText(path)) : null;

    private static IReadOnlyList<string> StringList(JsonNode? node) =>
        node is JsonArray arr
            ? arr.Select(n => n?.GetValue<string>() ?? "")
                 .Where(s => s.Length > 0).ToList()
            : [];

    private static IReadOnlyDictionary<string, double> NumberMap(JsonNode? node)
    {
        var map = new Dictionary<string, double>();
        if (node is JsonObject obj)
            foreach (var (key, value) in obj)
                if (value is JsonValue v && v.TryGetValue<double>(out var d))
                    map[key] = d;
        return map;
    }

    private static IReadOnlyDictionary<string, int> IntMap(JsonNode? node)
    {
        var map = new Dictionary<string, int>();
        if (node is JsonObject obj)
            foreach (var (key, value) in obj)
                if (value is JsonValue v && v.TryGetValue<int>(out var d))
                    map[key] = d;
        return map;
    }

    private static IReadOnlyDictionary<string, JsonNode?> NodeMap(JsonNode? node)
    {
        var map = new Dictionary<string, JsonNode?>();
        if (node is JsonObject obj)
            foreach (var (key, value) in obj)
                map[key] = value;
        return map;
    }

    private static IReadOnlyList<DependencyDeclaration> DependencyList(
        JsonNode? node)
    {
        if (node is not JsonArray arr) return [];
        var list = new List<DependencyDeclaration>();
        foreach (var item in arr)
        {
            if (item is not JsonObject o) continue;
            list.Add(new DependencyDeclaration
            {
                Identity = Str(o, "identity"),
                Owner = Str(o, "owner"),
                RequiredBy = Str(o, "required_by"),
                Criticality = Str(o, "criticality"),
                ReadinessContract = Str(o, "readiness_contract"),
                Deadline = Str(o, "deadline"),
                RetryBudget = Int(o, "retry_budget"),
                ShutdownOrder = Int(o, "shutdown_order"),
                Activation = StrOr(o, "activation", "eager"),
            });
        }
        return list;
    }

    private static string Str(JsonObject o, string key) =>
        o[key]?.GetValue<string>()?.Trim() ?? "";

    private static string StrOr(JsonObject o, string key, string fallback) =>
        o[key]?.GetValue<string>()?.Trim() is { Length: > 0 } s
            ? s : fallback;

    private static int Int(JsonObject o, string key) =>
        o[key] is JsonValue v && v.TryGetValue<int>(out var d) ? d : 0;
}
