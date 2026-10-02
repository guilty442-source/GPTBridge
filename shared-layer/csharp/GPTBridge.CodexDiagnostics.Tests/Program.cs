using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using GPTBridge.ToolHost;
using GPTBridge.ToolHost.App;

if (args.Length > 0 && args[0] != "--live")
{
    if (args[0] == "--fixture")
    {
        switch (args[1])
        {
            case "stderr": Console.Error.Write(new string('x', 512 * 1024)); break;
            case "wait": File.WriteAllText(args[2], Environment.ProcessId.ToString()); await Task.Delay(Timeout.Infinite); break;
            case "bad": Console.Write("invalid-json"); return 0;
            case "array": Console.Write("[]"); return 0;
            case "failure": Console.Write("{\"ok\":true}"); return 9;
        }
        Console.Write("{\"ok\":true}");
        return 0;
    }
    if (Path.GetFileName(Environment.ProcessPath) == "GPTBridge.CodexPipeline.exe")
    {
        Console.Write(JsonSerializer.Serialize(new { ok = true, verb = args[0], root = args[2], cwd = Environment.CurrentDirectory }));
        return 0;
    }
}
var root = Path.Combine(Path.GetTempPath(), "GPTBridge diagnostics 空白 " + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(root);
var passed = 0;
void Require(bool ok, string name)
{
    if (!ok) throw new Exception("CODEX_DIAGNOSTIC_REGRESSION:" + name);
    passed++;
}
ProcessStartInfo Fixture(string mode, string? marker = null)
{
    var start = new ProcessStartInfo(Environment.ProcessPath!)
    {
        UseShellExecute = false, CreateNoWindow = true,
        RedirectStandardOutput = true, RedirectStandardError = true,
    };
    start.ArgumentList.Add("--fixture"); start.ArgumentList.Add(mode);
    if (marker is not null) start.ArgumentList.Add(marker);
    return start;
}
bool Alive(int pid)
{
    try { using var process = Process.GetProcessById(pid); return !process.HasExited; }
    catch (ArgumentException) { return false; }
}
try
{
    foreach (var payload in new[] { "{\"ok\":false,\"errors\":[]}", "{\"errors\":[]}", "{\"ok\":\"true\",\"errors\":[]}", "{\"ok\":true,\"errors\":\"invalid\"}", "{\"ok\":true}" })
    {
        var checks = new JsonArray();
        CodexDiagnostics.AppendChecks(checks, new StringBuilder(), "fixture", JsonNode.Parse(payload)!.AsObject());
        Require(checks.Any(check => check?["ok"]?.GetValue<bool>() == false), "incomplete-report-never-passes");
    }
    var validChecks = new JsonArray();
    var validReport = new StringBuilder();
    CodexDiagnostics.AppendChecks(validChecks, validReport, "fixture", JsonNode.Parse("{\"ok\":true,\"errors\":[]}")!.AsObject());
    Require(validChecks[0]!["ok"]!.GetValue<bool>(), "valid-report-passes");
    Require(validReport.ToString().Contains("fixture:errors：0"), "report-renders-evidence-detail");
    var env = new GovernedEnvironment
    {
        ToolId = "model-dialogue", ProjectRoot = root, ToolRoot = root,
        SessionToken = "test", ShutdownToken = "test", SidecarExecutable = "test", Port = 1,
    };
    var result = await CodexDiagnostics.RunAsync(env, "--mirror-check", default);
    Require(result["error_code"]!.GetValue<string>() == "CODEX_PIPELINE_UNAVAILABLE", "missing-native-entry");
    result = await CodexDiagnostics.RunAsync(env, "--repair-projections", default);
    Require(result["error_code"]!.GetValue<string>() == "CODEX_DIAGNOSTIC_VERB_DENIED", "mutation-verb-denied");
    foreach (var folder in new[] { "bin/Release/net10.0", "publish" })
    {
        var target = Path.Combine(root, "shared-layer/csharp/GPTBridge.CodexPipeline", folder);
        Directory.CreateDirectory(target);
        foreach (var file in Directory.GetFiles(AppContext.BaseDirectory)) File.Copy(file, Path.Combine(target, Path.GetFileName(file)));
        File.Copy(Environment.ProcessPath!, Path.Combine(target, "GPTBridge.CodexPipeline.exe"));
        foreach (var verb in new[] { "--mirror-check", "--arch-docs" })
        {
            result = await CodexDiagnostics.RunAsync(env, verb, default);
            Require(result["ok"]?.GetValue<bool>() == true, "native-entry:" + folder + verb);
            Require(result["root"]!.GetValue<string>() == root && result["cwd"]!.GetValue<string>() == root, "workspace-root-bound");
        }
    }
    result = await CodexDiagnostics.RunProcessAsync(Fixture("stderr"), TimeSpan.FromSeconds(5), default);
    Require(result["ok"]?.GetValue<bool>() == true, "stderr-saturation-does-not-deadlock");
    foreach (var mode in new[] { "bad", "array", "failure" })
    {
        result = await CodexDiagnostics.RunProcessAsync(Fixture(mode), TimeSpan.FromSeconds(5), default);
        Require(result["ok"]?.GetValue<bool>() == false, "bad-result-denied:" + mode);
        Require(result["error_code"]!.GetValue<string>() == (mode == "failure" ? "CODEX_PIPELINE_FAILED" : "CODEX_PIPELINE_BAD_OUTPUT"), "diagnostic-reason:" + mode);
    }
    foreach (var cancel in new[] { false, true })
    {
        var marker = Path.Combine(root, "pid-" + cancel);
        using var cancellation = new CancellationTokenSource();
        var task = CodexDiagnostics.RunProcessAsync(Fixture("wait", marker), TimeSpan.FromSeconds(cancel ? 5 : 1), cancellation.Token);
        var deadline = DateTime.UtcNow.AddSeconds(3);
        while (!File.Exists(marker) && DateTime.UtcNow < deadline) await Task.Delay(25);
        Require(File.Exists(marker), "fixture-started");
        var pid = int.Parse(File.ReadAllText(marker));
        if (cancel) cancellation.Cancel();
        result = await task;
        Require(result["error_code"]!.GetValue<string>() == (cancel ? "CODEX_PIPELINE_CANCELLED" : "CODEX_PIPELINE_TIMEOUT"), "cancellation-classified");
        Require(!Alive(pid), "child-process-reaped");
    }
    if (args.Contains("--live"))
    {
        var repo = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "../../../../../.."));
        var live = new GovernedEnvironment
        {
            ToolId = "model-dialogue", ProjectRoot = repo, ToolRoot = root,
            SessionToken = "test", ShutdownToken = "test", SidecarExecutable = "test", Port = 1,
        };
        result = await CodexDiagnostics.RunAsync(live, "--mirror-check", default);
        Require(result["ok"]?.GetValue<bool>() == true && result["error_code"] is null, "live-mirror-entry");
        result = await CodexDiagnostics.RunAsync(live, "--arch-docs", default);
        Require(result["error_code"] is null && result["errors"] is JsonArray, "live-architecture-entry");
    }
    Console.WriteLine(JsonSerializer.Serialize(new { artifact = "codex-native-diagnostic-regression", passed, failed = 0 }));
    return 0;
}
finally { Directory.Delete(root, recursive: true); }
