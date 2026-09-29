// GPTBridge.MainSystem — resident C# host for the main-system migration.
//
// architecture_registry: main-system/boot-core python_residency=migrate-csharp
// (A341 orchestration ownership, RULE_MAIN_SYSTEM_CSHARP14).
//
// This host owns the governed startup gate: it loads
// main-system/config/startup_manifest.json, verifies the dependency DAG,
// executes the certified probes inside a bounded pool, evaluates the
// gate verdict, and journals every transition. The Python runtime stays
// the authoritative resident path during migration (fallback preserved
// per docs/python-reduction-queue.md §4); this host first proves parity
// on the startup gate before absorbing resident duties.
//
// Fail-closed argument contract (same family as GPTBridge.GitAutomation):
// a command flag is REQUIRED; unknown switches and missing values exit
// non-zero — nothing silently falls through to a resident loop.

using System.Text.Json.Nodes;
using GPTBridge.MainSystem;

internal static class Program
{
    private const string Usage =
        "usage: GPTBridge.MainSystem <--startup-gate|--status> " +
        "[--project-root <path>] [--generation <id>]";

    public static async Task<int> Main(string[] args)
    {
        string? mode = null;
        var projectRoot = Environment.CurrentDirectory;
        var generationId =
            $"gen-{DateTimeOffset.UtcNow:yyyyMMddHHmmss}-{Guid.NewGuid():N}";
        var errors = new List<string>();

        for (var i = 0; i < args.Length; i++)
        {
            var arg = args[i];
            string? TakeValue()
            {
                if (i + 1 >= args.Length || args[i + 1].StartsWith("--"))
                {
                    errors.Add($"{arg} requires a value");
                    return null;
                }
                return args[++i];
            }
            void SetMode(string m)
            {
                if (mode is not null && mode != m)
                    errors.Add($"conflicting commands: {mode} vs {m}");
                mode ??= m;
            }

            switch (arg)
            {
                case "--startup-gate": SetMode("startup-gate"); break;
                case "--status": SetMode("status"); break;
                case "--project-root":
                    if (TakeValue() is { } r) projectRoot = r;
                    break;
                case "--generation":
                    if (TakeValue() is { } g) generationId = g;
                    break;
                default:
                    errors.Add(arg.StartsWith("--")
                        ? $"unknown option '{arg}'"
                        : $"unrecognized argument '{arg}' — commands " +
                          "require a --flag");
                    break;
            }
        }
        if (mode is null && errors.Count == 0)
            errors.Add("no command given");
        if (errors.Count > 0 || mode is null)
        {
            foreach (var error in errors)
                Console.Error.WriteLine($"[main-system] {error}");
            Console.Error.WriteLine(Usage);
            return 2;
        }

        var root = Path.GetFullPath(projectRoot);
        var manifestPath = Path.Combine(
            root, "main-system", "config", "startup_manifest.json");
        var manifest = StartupManifestLoader.LoadFile(manifestPath);
        if (manifest is null)
        {
            Console.Error.WriteLine(
                $"[main-system] startup-manifest-missing:{manifestPath}");
            return 3; // fail-closed: never boot on guessed defaults
        }

        return mode switch
        {
            "startup-gate" => await RunStartupGate(
                manifest, generationId).ConfigureAwait(false),
            "status" => RunStatus(manifest, generationId),
            _ => 2,
        };
    }

    private static async Task<int> RunStartupGate(
        StartupManifest manifest, string generationId)
    {
        using var cts = new CancellationTokenSource(
            TimeSpan.FromMilliseconds(
                manifest.StartupCompleteDeadlineMs > 0
                    ? manifest.StartupCompleteDeadlineMs
                    : 40000));
        var gate = new StartupGate(manifest);
        var result = await gate.RunAsync(generationId, cts.Token)
            .ConfigureAwait(false);
        Console.WriteLine(result.AsJson().ToJsonString());
        if (!result.GateOk)
        {
            var signal = GovernedStartup.StartupFailureSignal(
                "core-critical", generationId: generationId);
            Console.Error.WriteLine(signal.ToJsonString());
            return 1;
        }
        return 0;
    }

    private static int RunStatus(
        StartupManifest manifest, string generationId)
    {
        var dag = new DependencyDag(manifest.Dependencies);
        var generation = new StartupGeneration
        {
            GenerationId = generationId,
            ReleaseId = "",
            StartedAt = DateTimeOffset.UtcNow.ToString("O"),
            CurrentPhase = "manifest-loaded",
            CoreReady = false,
            DeferredActive = false,
        };
        Console.WriteLine(GovernedStartup
            .StartupStatus(generation, dag).ToJsonString());
        return 0;
    }
}
