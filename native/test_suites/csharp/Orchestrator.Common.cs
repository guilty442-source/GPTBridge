using System.Diagnostics;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;

// G99 shared helpers: per-suite criticality registry + BLOCKED case
// classification (single authority: TestSuiteOrchestrator).
internal static partial class Orchestrator
{
// G99: load the per-suite criticality registry that lives next to bin/.
static JsonObject? LoadCriticality(string binDir)
{
    var path = Path.GetFullPath(
        Path.Combine(binDir, "..", "suite_criticality.json"));
    try { return JsonNode.Parse(File.ReadAllText(path)) as JsonObject; }
    catch (Exception) { return null; }
}

// G99: project one BLOCKED case through the registry.  Unknown suites
// default to release-critical so unclassified evidence never stays
// non-blocking.
static string Trunc(string? s, int max = 200) =>
    s == null ? "" : (s.Length > max ? s[..max] : s);

static JsonObject ClassifyBlocked(string suiteStem, JsonNode? caseNode,
                                  JsonObject? registry)
{
    var suites = registry?["suites"] as JsonObject;
    var entry = suites?[suiteStem] as JsonObject;
    var defaults = registry?["defaults"] as JsonObject;
    string Field(JsonObject? e, string key, string fallback) =>
        e?[key]?.GetValue<string>() ?? fallback;
    return new JsonObject
    {
        ["blocked_suite"] = suiteStem,
        ["blocked_case"] = caseNode?["name"]?.GetValue<string>() ?? "?",
        ["blocked_reason"] = Trunc(
            caseNode?["detail"]?.GetValue<string>()),
        ["affected_capability"] = Field(entry, "affected_capability",
                                        "unregistered-suite"),
        ["release_impact"] = Field(entry, "release_impact", "unclassified"),
        ["required_evidence"] = Field(
            entry, "required_evidence",
            Field(defaults, "required_evidence", "suite PASS report")),
        ["criticality"] = Field(
            entry, "criticality",
            Field(defaults, "criticality", "release-critical")),
    };
}
}
