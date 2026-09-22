// C# orchestration for the native test suites (no Python).
// Reads the C++ harness report, aggregates by suite, enforces PASS/FAIL/BLOCKED
// policy and writes a consolidated report.
using System.Text.Json;
using System.Text.Json.Nodes;

var reportPath = args.Length > 0
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
    ["verdict"] = failed > 0 ? "FAIL" : (blocked > 0 ? "INCOMPLETE_EVIDENCE" : "PASS"),
};

var outPath = Path.Combine(Path.GetDirectoryName(reportPath)!, "native-orchestration-report.json");
File.WriteAllText(outPath, output.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));

Console.WriteLine($"suites={bySuite.Count} PASS={passed} FAIL={failed} BLOCKED={blocked} verdict={output["verdict"]}");
foreach (var suite in bySuite)
{
    Console.WriteLine($"  {suite.suite}: PASS={suite.pass} FAIL={suite.fail} BLOCKED={suite.blocked} ({suite.total_ms} ms)");
}
Console.WriteLine($"report: {outPath}");
return failed > 0 ? 1 : 0;
