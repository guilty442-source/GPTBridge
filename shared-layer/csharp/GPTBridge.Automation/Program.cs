using System.Text.Json;
using System.Text.Json.Nodes;
using CodexAutomation = GPTBridge.CodexPipeline.CodexAutomation;
using CodexDriver = GPTBridge.CodexPipeline.Driver;
using CodexRepo = GPTBridge.CodexPipeline.Repo;
using PermissionAutomation = GPTBridge.Permission.PermissionAutomation;
using GitProgram = GPTBridge.GitAutomation.Program;

namespace GPTBridge.Automation;

/// Unified resident automation host (module layer; protocol targets
/// NO_GLOBAL_STATE / LINEAR_OPERATION — autonomy lives in the
/// per-plane watch loops, this module is a thin entry / sequencer).
///
/// ``GPTBridge.Automation.exe --watch`` hosts every governed periodic
/// flow from ``main-system/config/automation-flows.json`` in a single
/// deadline-driven process — the ``periodic_scheduler`` named by
/// ``main-system/config/resident-core.json`` (R3 consolidation):
///
///   git:        sweep+sync via ``GPTBridge.GitAutomation`` in-process
///   codex:      amendment-intake / pin-sync / maintenance
///   permission: six ``permission-automation-*`` flows
///
/// Single-instance arbitration mirrors the retired standalone hosts:
/// the host holds ``automation-host.lock`` for its whole lifetime;
/// each plane additionally acquires its own lock
/// (``codex-automation.lock`` / ``permission-automation.lock`` /
/// ``git-automation.lock``) so an externally-launched standalone
/// watcher keeps ownership of just that plane — the unified host
/// defers the contested plane (exit code 1 contract) and adopts it
/// when the external holder releases.  Bounded retries are per plane
/// (``Planes.Run``); a plane that exhausts its fault budget parks for
/// the rest of the host lifetime instead of burning supervisor
/// restarts.  Graceful plane exits (flow disabled, stop-file)
/// schedule re-evaluation rather than propagating a restart.
///
/// Bounded one-shot (``--once``) and ``--status`` keep standalone
/// parity: they take no instance lock, so inspection and manual runs
/// still work beside a supervised host.
internal static class Program
{
    private const string StateDir = "main-system/runtime/state";
    private const string FlowsPath =
        "main-system/config/automation-flows.json";
    private const string HostLockName = "automation-host.lock";

    public static async Task<int> Main(string[] args)
    {
        string? mode = null;
        string? rootArg = null;
        string? planesArg = null;
        for (var index = 0; index < args.Length; index++)
        {
            var argument = args[index];
            if (argument is "--watch" or "--once" or "--status")
                mode = argument[2..];
            else if (argument == "--root" && index + 1 < args.Length)
                rootArg = args[index + 1];
            else if (argument == "--planes" && index + 1 < args.Length)
                planesArg = args[index + 1];
        }
        if (mode is null)
        {
            Console.Error.WriteLine(
                "usage: GPTBridge.Automation " +
                "--watch|--once|--status [--root <path>] " +
                "[--planes <csv>]");
            return 2;
        }
        var root = ResolveRoot(rootArg);
        if (root is null)
        {
            Console.Error.WriteLine(
                "usage: GPTBridge.Automation --root <path>" +
                " or set GPTBRIDGE_PROJECT_ROOT/GPTBRIDGE_ROOT");
            return 2;
        }
        Environment.SetEnvironmentVariable("GPTBRIDGE_ROOT", root);
        Environment.SetEnvironmentVariable(
            "GPTBRIDGE_PROJECT_ROOT", root);
        return mode switch
        {
            "watch" => await Watch(root, planesArg),
            "once" => await Once(root),
            _ => Status(root),
        };
    }

    private static string? ResolveRoot(string? rootArg)
    {
        if (!string.IsNullOrWhiteSpace(rootArg))
            return Path.GetFullPath(rootArg);
        var root = Environment.GetEnvironmentVariable(
            "GPTBRIDGE_PROJECT_ROOT")
            ?? Environment.GetEnvironmentVariable("GPTBRIDGE_ROOT");
        return string.IsNullOrWhiteSpace(root)
            ? null : Path.GetFullPath(root);
    }

    /// Resident mode: hold the host lock, then run the selected plane
    /// entrypoints concurrently under ``Planes.Run`` supervision.
    /// ``--planes <csv>`` restricts the host to a subset (e.g.
    /// ``--planes self-learning`` hosts only the xingcheng cadence,
    /// leaving codex/permission/git planes untouched — their own
    /// semantics, including codex amendment auto-execution, stay off).
    /// The host process exits only when every selected plane has parked.
    private static async Task<int> Watch(string root, string? planesArg)
    {
        var selected = string.IsNullOrWhiteSpace(planesArg)
            ? null
            : planesArg.Split(',', StringSplitOptions.RemoveEmptyEntries |
                                    StringSplitOptions.TrimEntries)
                       .ToHashSet(StringComparer.Ordinal);
        var lockPath = Path.Combine(root, StateDir, HostLockName);
        Directory.CreateDirectory(Path.GetDirectoryName(lockPath)!);
        FileStream hostLock;
        try
        {
            hostLock = new FileStream(lockPath, FileMode.Create,
                FileAccess.ReadWrite, FileShare.None);
        }
        catch (Exception error) when (error is IOException
            or UnauthorizedAccessException)
        {
            Console.Error.WriteLine(
                "[automation-host] already running (lock held)");
            return 1;
        }
        await using (hostLock)
        {
            Planes.Journal(root, "host", "start");
            Planes.Journal(root, "governance-engine",
                "single-engine:domain-planes=git,sql(codex)");
            var planes = new List<Task>();
            // Every plane is queued via Task.Run: a plane entry whose
            // synchronous prefix blocks (e.g. a contested plane lock)
            // would otherwise starve every plane listed after it
            // during sequential array evaluation.
            if (selected is null || selected.Contains("codex"))
                planes.Add(Task.Run(() => Planes.Run(root, "codex",
                    () => CodexAutomation.RunWatch(null))));
            if (selected is null || selected.Contains("permission"))
                planes.Add(Task.Run(() => Planes.Run(root, "permission",
                    () => PermissionAutomation.RunWatch(null))));
            if (selected is null || selected.Contains("self-learning"))
                planes.Add(Task.Run(() => Planes.Run(root,
                    "self-learning",
                    () => SelfLearningPlane.RunWatch(root))));
            if (selected is null || selected.Contains("git"))
                planes.Add(Task.Run(() => Planes.Run(root, "git",
                    () => GitProgram.WatchService(root))));
            if (planes.Count == 0)
            {
                Console.Error.WriteLine(
                    "[automation-host] --planes selected nothing");
                return 2;
            }
            await Task.WhenAll(planes);
            Planes.Journal(root, "host", "all-planes-parked");
            return 0;
        }
    }

    /// Bounded one-shot: every enabled flow exactly once (standalone
    /// ``--all`` / ``--once`` parity across the three planes), then
    /// exit.  No instance lock — inspection parity with the retired
    /// standalone hosts.
    private static async Task<int> Once(string root)
    {
        var result = new JsonObject();
        async Task Plane(string name, Func<Task<object?>> body)
        {
            try
            {
                result[name] = JsonNode.Parse(
                    JsonSerializer.Serialize(await body()));
            }
            catch (Exception error)
            {
                result[name] = $"error:{error.GetType().Name}";
            }
        }
        if (Planes.FlowEnabled(root, "codex-pin-sync"))
            await Plane("codex-pin-sync",
                () => Task.FromResult<object?>(
                    CodexAutomation.PinSync()));
        if (Planes.FlowEnabled(root, "codex-maintenance"))
            await Plane("codex-maintenance",
                () => Task.FromResult<object?>(
                    CodexAutomation.Maintain()));
        if (Planes.FlowEnabled(root, "codex-amendment-intake"))
        {
            CodexRepo.SetRoot(root);
            await Plane("codex-amendment-intake",
                async () => await CodexDriver.AdvanceAll(
                    autoExecute: Planes.AutoExecute(
                        root, "codex-amendment-intake")));
        }
        // RunOnceAll already honours each permission-automation-*
        // flow's manifest kill switch internally; the returned code
        // aggregates per-flow health.
        PermissionAutomation.SetRoot(root);
        await Plane("permission-automation",
            async () => await PermissionAutomation.RunOnceAll());
        if (Planes.FlowEnabled(root, "git-automation"))
            await Plane("git-automation",
                async () => await GitProgram.RunOnce(root));
        Console.WriteLine(result.ToJsonString());
        return 0;
    }

    /// Aggregate the per-plane state files each watch loop already
    /// persists — same information as the three standalone
    /// ``--status`` invocations, unified.
    private static int Status(string root)
    {
        var result = new JsonObject
        {
            ["host_lock_held"] = File.Exists(
                Path.Combine(root, StateDir, HostLockName))
                && HostLockHeld(root),
        };
        foreach (var plane in new[]
                 { "git", "codex", "permission", "self-learning" })
        {
            var state = Path.Combine(root, StateDir,
                $"{plane}-automation.json");
            if (File.Exists(state))
                result[plane] = JsonNode.Parse(
                    File.ReadAllText(state));
        }
        var convergence = Path.Combine(root, StateDir,
            "governance-convergence.json");
        if (File.Exists(convergence))
        {
            try
            {
                result["governance_engine"] = JsonNode.Parse(
                    File.ReadAllText(convergence));
            }
            catch (Exception error) when (error is IOException
                or JsonException)
            {
                result["governance_engine"] =
                    "unreadable:convergence-receipt";
            }
        }
        Console.WriteLine(result.ToJsonString());
        return 0;
    }

    private static bool HostLockHeld(string root)
    {
        try
        {
            using var probe = new FileStream(
                Path.Combine(root, StateDir, HostLockName),
                FileMode.Open, FileAccess.ReadWrite, FileShare.None);
            return false;
        }
        catch (Exception error) when (error is IOException
            or UnauthorizedAccessException)
        {
            return true;
        }
    }
}
