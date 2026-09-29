using System.Diagnostics;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;

// Aggregate mode: project <bin>/native-report.json into the
// orchestration report with G99 blocked-case classification.
internal static partial class Orchestrator
{
internal static int AggregateReport(string[] args)
{
    var reportPath = args.Length > 0 && !args[0].StartsWith("--")
        ? args[0]
        : Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "bin", "native-report.json");
    reportPath = Path.GetFullPath(reportPath);

    if (!File.Exists(reportPath))
    {
        Console.Error.WriteLine($"native report not found: {reportPath}");
        return 2;
    }

    var cases = JsonNode.Parse(File.ReadAllText(reportPath))?["cases"]?.AsArray() ?? new JsonArray();
    var bySuite = cases
        .GroupBy(item => item?["suite"]?.GetValue<string>() ?? "UNKNOWN")
        .OrderBy(group => group.Key)
        .Select(group => new
        {
            suite = group.Key,
            pass = group.Count(item => item?["status"]?.GetValue<string>() == "PASS"),
            fail = group.Count(item => item?["status"]?.GetValue<string>() == "FAIL"),
            blocked = group.Count(item => item?["status"]?.GetValue<string>() == "BLOCKED"),
            total_ms = Math.Round(group.Sum(item => item?["ms"]?.GetValue<double>() ?? 0.0), 3),
        })
        .ToList();

    var failed = bySuite.Sum(item => item.fail);
    var blocked = bySuite.Sum(item => item.blocked);
    var passed = bySuite.Sum(item => item.pass);

    // G99: classify blocked cases; release-critical blocked → FAIL.
    var registry = LoadCriticality(Path.GetDirectoryName(reportPath)!);
    var blockedClassifications = new JsonArray();
    foreach (var c in cases)
    {
        if (c?["status"]?.GetValue<string>() != "BLOCKED") continue;
        var stem = c?["suite_exe"]?.GetValue<string>()
            ?? c?["suite"]?.GetValue<string>() ?? "UNKNOWN";
        blockedClassifications.Add(ClassifyBlocked(stem, c, registry));
    }
    var blockedUnclassified = blocked > 0
        && (registry?["suites"] as JsonObject) == null;
    var criticalBlocked = blockedClassifications.Count(item =>
        item?["criticality"]?.GetValue<string>() != "experimental");
    var deny = failed > 0 || blockedUnclassified || criticalBlocked > 0;

    var output = new JsonObject
    {
        ["orchestrator"] = "native-test-orchestrator/v1",
        ["language"] = "csharp",
        ["source_report"] = reportPath,
        ["generated_at"] = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ"),
        ["suites"] = JsonSerializer.SerializeToNode(bySuite),
        ["passed"] = passed,
        ["failed"] = failed,
        ["blocked"] = blocked,
        ["blocked_classifications"] = blockedClassifications,
        ["blocked_release_critical"] = criticalBlocked,
        ["blocked_unclassified"] = blockedUnclassified,
        ["verdict"] = deny ? "FAIL"
            : (blocked > 0 ? "INCOMPLETE_EVIDENCE" : "PASS"),
    };

    var outPath = Path.Combine(Path.GetDirectoryName(reportPath)!, "native-orchestration-report.json");
    File.WriteAllText(outPath, output.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));

    Console.WriteLine($"suites={bySuite.Count} PASS={passed} FAIL={failed} BLOCKED={blocked} verdict={output["verdict"]}");
    foreach (var suite in bySuite)
    {
        Console.WriteLine($"  {suite.suite}: PASS={suite.pass} FAIL={suite.fail} BLOCKED={suite.blocked} ({suite.total_ms} ms)");
    }
    Console.WriteLine($"report: {outPath}");
    return deny ? 1 : 0;
}
}
