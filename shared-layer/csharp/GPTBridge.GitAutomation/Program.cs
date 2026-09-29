using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

/// <summary>
/// GPTBridge Git automation host — the governed C# replacement for the
/// retired Python GitAutomationService + git_tiers entry points:
///
///   --watch          sweep + sync loop (sweep 60 s / sync 300 s,
///                    60 s debounce, queue-event early sync)
///   --once           one sweep + one sync, then exit
///   --sweep          one debounced sweep only
///   --sync           one workspace sync only
///   --hook <name>    governed git hook (pre-commit, pre-merge-commit,
///                    pre-push, pre-receive)
///   --install-hooks  write governed sh shims into .git/hooks
///   --update-templates  rewrite governance_rule/git-hooks templates
///   --status         print the service state file
///
/// Options: --root <path>  --push  --interval <s>  --debounce <s>
///          --sync-interval <s>  --no-commit
///
/// Fail-closed argument contract: a command flag is REQUIRED.  Bare
/// words, unknown switches and a missing command all exit non-zero —
/// nothing silently falls through to the resident service loop.
/// </summary>
internal static partial class Program
{
    private const string StateRelative =
        "main-system/runtime/state/git-automation.json";

    private sealed class Options
    {
        public string? DiffLeft;
        public string? DiffRight;
        public string? Mode;
        public string Root = Environment.CurrentDirectory;
        public string Hook = "";
        public bool Push;
        public bool CommitDirty = true;
        public double SweepInterval = 60;
        public double SyncInterval = 300;
        public double Debounce = 60;
        public readonly List<string> Errors = new();
    }

    private const string Usage =
        "usage: GPTBridge.GitAutomation <--watch|--once|--sweep|--sync|" +
        "--status|--install-hooks|--update-templates|--manifest-export|" +
        "--manifest-diff <left> <right>|--hook <name>> " +
        "[--root <path>] [--push] [--no-commit] [--interval <s>] " +
        "[--debounce <s>] [--sync-interval <s>]";

    private static string? TakeValue(
        string[] args, ref int i, string flag, Options options)
    {
        if (i + 1 >= args.Length || args[i + 1].StartsWith("--"))
        {
            options.Errors.Add($"{flag} requires a value");
            return null;
        }
        return args[++i];
    }

    private static void SetMode(Options options, string mode)
    {
        if (options.Mode is not null && options.Mode != mode)
            options.Errors.Add(
                $"conflicting commands: {options.Mode} vs {mode}");
        options.Mode ??= mode;
    }

    private static Options Parse(string[] args)
    {
        var options = new Options();
        for (var i = 0; i < args.Length; i++)
        {
            var arg = args[i];
            switch (arg)
            {
                case "--watch": SetMode(options, "watch"); break;
                case "--once": SetMode(options, "once"); break;
                case "--sweep": SetMode(options, "sweep"); break;
                case "--sync": SetMode(options, "sync"); break;
                case "--status": SetMode(options, "status"); break;
                case "--install-hooks":
                    SetMode(options, "install-hooks"); break;
                case "--update-templates":
                    SetMode(options, "update-templates"); break;
                case "--manifest-export":
                    SetMode(options, "manifest-export"); break;
                case "--manifest-diff":
                    SetMode(options, "manifest-diff");
                    options.DiffLeft = TakeValue(args, ref i, arg, options);
                    options.DiffRight = TakeValue(args, ref i, arg, options);
                    break;
                case "--hook":
                    SetMode(options, "hook");
                    var hook = TakeValue(args, ref i, arg, options);
                    if (hook is not null)
                        options.Hook = hook;
                    break;
                case "--root":
                    var root = TakeValue(args, ref i, arg, options);
                    if (root is not null)
                        options.Root = root;
                    break;
                case "--push": options.Push = true; break;
                case "--no-commit": options.CommitDirty = false; break;
                case "--interval":
                    if (TakeValue(args, ref i, arg, options) is { } iv)
                        options.SweepInterval = double.Parse(
                            iv,
                            System.Globalization.CultureInfo.InvariantCulture);
                    break;
                case "--debounce":
                    if (TakeValue(args, ref i, arg, options) is { } db)
                        options.Debounce = double.Parse(
                            db,
                            System.Globalization.CultureInfo.InvariantCulture);
                    break;
                case "--sync-interval":
                    if (TakeValue(args, ref i, arg, options) is { } si)
                        options.SyncInterval = double.Parse(
                            si,
                            System.Globalization.CultureInfo.InvariantCulture);
                    break;
                default:
                    options.Errors.Add(arg.StartsWith("--")
                        ? $"unknown option '{arg}'"
                        : $"unrecognized argument '{arg}' — commands " +
                          "require a --flag (e.g. --manifest-export)");
                    break;
            }
        }
        if (options.Mode is null && options.Errors.Count == 0)
            options.Errors.Add("no command given");
        options.Root = Path.GetFullPath(options.Root);
        options.SweepInterval = Math.Max(10.0, options.SweepInterval);
        options.SyncInterval = Math.Max(
            options.SweepInterval, options.SyncInterval);
        options.Debounce = Math.Max(0.0, options.Debounce);
        return options;
    }

    /// <summary>
    /// The governed project root: the exe lives at
    /// shared-layer/csharp/GPTBridge.GitAutomation/(bin|publish)/... so the
    /// repo root is derived from its location unless --root or the
    /// governed env override is supplied.  Hooks execute per-worktree and
    /// must still audit into the main checkout — same contract as the
    /// retired scripts (which resolved root from the script path).
    /// </summary>
    private static string ProjectRoot(string? explicitRoot = null)
    {
        var env = Environment.GetEnvironmentVariable("GPTBRIDGE_PROJECT_ROOT");
        if (!string.IsNullOrWhiteSpace(env))
            return Path.GetFullPath(env.Trim());
        if (explicitRoot is not null)
            return Path.GetFullPath(explicitRoot);
        var baseDir = AppContext.BaseDirectory;
        var cursor = new DirectoryInfo(baseDir);
        while (cursor is not null)
        {
            if (File.Exists(Path.Combine(
                    cursor.FullName, "governance_rule", "execution",
                    "git_tiers", "git_governance_manifest.json"))
                || Directory.Exists(Path.Combine(
                    cursor.FullName, "governance_rule")))
                return cursor.FullName;
            cursor = cursor.Parent;
        }
        return Environment.CurrentDirectory;
    }

    public static async Task<int> Main(string[] args)
    {
        var options = Parse(args);
        if (options.Errors.Count > 0 || options.Mode is null)
        {
            foreach (var error in options.Errors)
                Console.Error.WriteLine($"[git-automation] {error}");
            Console.Error.WriteLine(Usage);
            return Fail("invalid-arguments");
        }
        var projectRoot = options.Mode is "hook"
            ? ProjectRoot(null)
            : ProjectRoot(options.Root);
        var worktree = options.Root;

        try
        {
            switch (options.Mode)
            {
                case "hook":
                    return options.Hook switch
                    {
                        "pre-commit" or "pre-merge-commit" =>
                            Hooks.Commit(projectRoot, worktree),
                        "pre-push" => Hooks.Push(projectRoot, worktree),
                        "pre-receive" => Hooks.Receive(projectRoot, worktree),
                        _ => Fail($"unknown hook '{options.Hook}'"),
                    };
                case "install-hooks":
                    return Hooks.Install(projectRoot,
                        Environment.ProcessPath
                        ?? Path.Combine(AppContext.BaseDirectory,
                            "GPTBridge.GitAutomation"));
                case "update-templates":
                    return Hooks.UpdateTemplates(projectRoot,
                        Environment.ProcessPath
                        ?? Path.Combine(AppContext.BaseDirectory,
                            "GPTBridge.GitAutomation"));
                case "manifest-export":
                    ManifestExport.Refresh(projectRoot);
                    return 0;
                case "manifest-diff":
                    return ManifestExport.Diff(
                        options.DiffLeft!, options.DiffRight!);
                case "status":
                    return ShowStatus(projectRoot);
                case "sweep":
                case "sync":
                    if (!FlowsConfig.FlowEnabled(projectRoot))
                    {
                        Console.WriteLine(
                            "[git-automation] disabled by " +
                            "automation-flows manifest/override");
                        return 0;
                    }
                    return options.Mode == "sweep"
                        ? Print(Sweep(projectRoot, options,
                            new Dictionary<string, (string, double)>()))
                        : Print(SyncCycle(projectRoot, options));
                case "once":
                case "watch":
                    return await Watch(projectRoot, options);
                default:
                    return Fail($"unknown mode '{options.Mode}'");
            }
        }
        catch (LockBusyException error)
        {
            Console.Error.WriteLine($"[git-automation] {error.Message}");
            return 2;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(
                $"[git-automation] fatal: {error.GetType().Name}: " +
                error.Message);
            return 13;
        }
    }

    private static int Fail(string detail)
    {
        Console.Error.WriteLine($"GIT_AUTOMATION_FAILED:{detail}");
        return 13;
    }

    private static int Print(object payload)
    {
        Console.WriteLine(JsonSerializer.Serialize(payload,
            new JsonSerializerOptions { WriteIndented = true }));
        return 0;
    }

    private static int ShowStatus(string projectRoot)
    {
        var path = Path.Combine(projectRoot,
            StateRelative.Replace('/', Path.DirectorySeparatorChar));
        if (!File.Exists(path))
        {
            Console.WriteLine("{}");
            return 0;
        }
        Console.WriteLine(File.ReadAllText(path));
        return 0;
    }

}
