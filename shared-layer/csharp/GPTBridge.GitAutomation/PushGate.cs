using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

/// <summary>
/// Mandatory native-test push gate + push/convergence evidence +
/// release checkpoints — parity with git_tiers.push_gate,
/// release_checkpoint and repo_sync.
/// </summary>
internal static partial class PushGate
{
    private const string OrchestratorReleaseRelative =
        "native/test_suites/csharp/bin/Release/net10.0/TestSuiteOrchestrator.exe";
    private const string OrchestratorDebugRelative =
        "native/test_suites/csharp/bin/Debug/net10.0/TestSuiteOrchestrator.exe";
    private const string OrchestratorProjectRelative =
        "native/test_suites/csharp/TestSuiteOrchestrator.csproj";
    private const string OrchestratorSourceRelative =
        "native/test_suites/csharp/Program.cs";
    private const string BinRelative = "native/test_suites/bin";
    private const string BuildScriptRelative =
        "native/test_suites/build.ps1";
    private const string StateFilename = "gptbridge-push-gate.json";

    private static readonly string[] DepRoots =
    {
        "native/test_suites", "native/core", "native/include",
        "native/tool_runtime", "native/audit",
        "Standalone tools/local-model/src/backend/cpp",
    };

    private static readonly HashSet<string> CodeSuffixes =
        new(StringComparer.OrdinalIgnoreCase)
        { ".c", ".cpp", ".h", ".hpp" };

    private static string Rel(string root, string relative) =>
        Path.Combine(root, relative.Replace('/', Path.DirectorySeparatorChar));

    private sealed class Config
    {
        public bool Enabled = true;
        public bool AutoBuild = true;
        public double BuildTimeoutS = 600;
        public double SuiteTimeoutS = 30;
        public double RunBudgetS = 120;
        public double LockWaitS = 30;
        public int MaxParallel = 4;
        public string? ConfigError;
    }

    private static Config LoadConfig(string root)
    {
        var config = new Config();
        JsonObject? entry;
        try
        {
            var path = Rel(root, "main-system/config/automation-flows.json");
            var node = JsonNode.Parse(File.ReadAllText(path));
            entry = node?["flows"]?["git-automation"]?["push_gate"]
                as JsonObject;
        }
        catch (Exception error)
        {
            config.ConfigError = error.GetType().Name;
            return config;
        }
        if (entry is null)
            return config;
        if (entry["enabled"] is { } e) config.Enabled = e.GetValue<bool>();
        if (entry["auto_build"] is { } a)
            config.AutoBuild = a.GetValue<bool>();
        if (entry["build_timeout_s"]?.GetValue<double>() is double b && b > 0)
            config.BuildTimeoutS = b;
        if (entry["suite_timeout_s"]?.GetValue<double>() is double s && s > 0)
            config.SuiteTimeoutS = s;
        if (entry["run_budget_s"]?.GetValue<double>() is double r && r > 0)
            config.RunBudgetS = r;
        if (entry["lock_timeout_s"]?.GetValue<double>() is double l0 && l0 > 0)
            config.LockWaitS = l0;
        if (entry["lock_wait_s"]?.GetValue<double>() is double l && l > 0)
            config.LockWaitS = l;
        if (entry["max_parallel_suites"]?.GetValue<int>() is int p && p >= 1)
            config.MaxParallel = Math.Min(8, p);
        return config;
    }

}
