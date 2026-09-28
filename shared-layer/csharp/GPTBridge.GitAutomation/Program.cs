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
/// </summary>
internal static class Program
{
    private const string StateRelative =
        "main-system/runtime/state/git-automation.json";

    private sealed class Options
    {
        public string? DiffLeft;
        public string? DiffRight;
        public string Mode = "watch";
        public string Root = Environment.CurrentDirectory;
        public string Hook = "";
        public bool Push;
        public bool CommitDirty = true;
        public double SweepInterval = 60;
        public double SyncInterval = 300;
        public double Debounce = 60;
    }

    private static Options Parse(string[] args)
    {
        var options = new Options();
        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--watch": options.Mode = "watch"; break;
                case "--once": options.Mode = "once"; break;
                case "--sweep": options.Mode = "sweep"; break;
                case "--sync": options.Mode = "sync"; break;
                case "--status": options.Mode = "status"; break;
                case "--install-hooks": options.Mode = "install-hooks"; break;
                case "--update-templates":
                    options.Mode = "update-templates"; break;
                case "--manifest-export":
                    options.Mode = "manifest-export"; break;
                case "--manifest-diff":
                    options.Mode = "manifest-diff";
                    options.DiffLeft = args[++i];
                    options.DiffRight = args[++i];
                    break;
                case "--hook":
                    options.Mode = "hook";
                    options.Hook = args[++i];
                    break;
                case "--root": options.Root = args[++i]; break;
                case "--push": options.Push = true; break;
                case "--no-commit": options.CommitDirty = false; break;
                case "--interval":
                    options.SweepInterval = double.Parse(
                        args[++i], System.Globalization.CultureInfo.InvariantCulture);
                    break;
                case "--debounce":
                    options.Debounce = double.Parse(
                        args[++i], System.Globalization.CultureInfo.InvariantCulture);
                    break;
                case "--sync-interval":
                    options.SyncInterval = double.Parse(
                        args[++i], System.Globalization.CultureInfo.InvariantCulture);
                    break;
            }
        }
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
                    return Print(Sweep(projectRoot, options,
                        new Dictionary<string, (string, double)>()));
                case "sync":
                    return Print(SyncCycle(projectRoot, options));
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

    // -- service loop ------------------------------------------------------

    private sealed class Service
    {
        private readonly string _root;
        private readonly Options _options;
        private readonly Dictionary<string, (string Fingerprint, double Since)>
            _dirtySince = new(StringComparer.OrdinalIgnoreCase);
        private int _sweeps;
        private int _syncs;
        private JsonObject _lastSweep = new();
        private JsonObject _lastSync = new();
        private double _nextSyncAt;
        private bool _running = true;
        private bool _wake;
        private readonly List<FileSystemWatcher> _watchers = new();
        private double _lastEventWake;

        public Service(string root, Options options)
        {
            _root = root;
            _options = options;
        }

        public async Task<int> Run()
        {
            if (!FlowsConfig.FlowEnabled(_root))
            {
                Console.WriteLine(
                    "[git-automation] disabled by automation-flows manifest");
                return 0;
            }
            if (!File.Exists(Path.Combine(_root, ".git"))
                && !Directory.Exists(Path.Combine(_root, ".git")))
            {
                Console.WriteLine(
                    "[git-automation] skipped: not-a-git-worktree");
                return 0;
            }
            StartDirwatch();
            Console.CancelKeyPress += (_, e) =>
            {
                e.Cancel = true;
                _running = false;
            };
            // Push flag: manifest flow entry wins; --push is the fallback.
            var push = FlowsConfig.PushEnabled(_root) || _options.Push;
            Console.WriteLine(
                $"[git-automation] started " +
                $"(sweep={_options.SweepInterval:0}s " +
                $"sync={_options.SyncInterval:0}s " +
                $"debounce={_options.Debounce:0}s push={push})");
            var tickDeadline = TimeSpan.FromSeconds(
                Math.Max(60.0,
                    Math.Min(900.0, _options.SweepInterval * 5)));
            while (_running)
            {
                try
                {
                    var tick = CycleTick(push);
                    if (await Task.WhenAny(
                            tick, Task.Delay(tickDeadline)) != tick)
                        Console.Error.WriteLine(
                            $"[git-automation] cycle exceeded " +
                            $"{tickDeadline.TotalSeconds:0}s deadline");
                }
                catch (Exception error)
                {
                    Console.Error.WriteLine(
                        $"[git-automation] cycle error: {error.Message}");
                }
                var delay = Task.Delay(
                    TimeSpan.FromSeconds(_options.SweepInterval));
                while (_running && !_wake)
                {
                    if (await Task.WhenAny(
                            delay, Task.Delay(500)) == delay)
                        break;
                }
                _wake = false;
            }
            StopDirwatch();
            return 0;
        }

        private async Task CycleTick(bool push)
        {
            await Task.Run(() =>
            {
                Sweep(_root, _options, _dirtySince);
                _sweeps++;
                WriteState();
            });
            var queueEvent = await Task.Run(QueueHasPending);
            var now = Environment.TickCount64 / 1000.0;
            if (queueEvent || now >= _nextSyncAt)
            {
                var sync = await Task.Run(() => SyncCycle(_root, _options, push));
                _nextSyncAt = now + _options.SyncInterval;
                _syncs++;
                _lastSync = new JsonObject
                {
                    ["at"] = Canon.EpochSeconds(),
                    ["result"] = sync,
                };
                WriteState();
            }
        }

        private bool QueueHasPending()
        {
            try
            {
                var common = Git.CommonDir(_root);
                var queueFile = Path.Combine(common,
                    "gptbridge-automation", "merge-queue", "queue.json");
                var node = JsonNode.Parse(File.ReadAllText(queueFile));
                if (node?["entries"] is not JsonArray entries)
                    return false;
                return entries.OfType<JsonObject>().Any(
                    e => e["status"]?.GetValue<string>() == "pending");
            }
            catch (IOException) { return false; }
            catch (JsonException) { return false; }
        }

        // -- dirwatch (event-driven early wake, bounded like the Python
        //    native dirwatch: ≤16 handles, ≥15 s between wakes) ----------

        private void StartDirwatch()
        {
            try
            {
                var worktrees = Sync.ListWorktrees(_root)
                    .Select(w => w.Path).Take(16).ToList();
                foreach (var worktree in worktrees)
                {
                    if (!Directory.Exists(worktree))
                        continue;
                    var watcher = new FileSystemWatcher(worktree)
                    {
                        IncludeSubdirectories = true,
                        EnableRaisingEvents = true,
                        NotifyFilter = NotifyFilters.FileName
                            | NotifyFilters.DirectoryName
                            | NotifyFilters.LastWrite,
                    };
                    watcher.Changed += (_, _) => OnChanged();
                    watcher.Created += (_, _) => OnChanged();
                    watcher.Deleted += (_, _) => OnChanged();
                    watcher.Renamed += (_, _) => OnChanged();
                    _watchers.Add(watcher);
                }
            }
            catch (Exception)
            {
                // Fail-soft: sweep TTL still covers change detection.
            }
        }

        private void OnChanged()
        {
            var now = Environment.TickCount64 / 1000.0;
            if (now - _lastEventWake >= 15.0)
            {
                _lastEventWake = now;
                _wake = true;
            }
        }

        private void StopDirwatch()
        {
            foreach (var watcher in _watchers)
            {
                watcher.EnableRaisingEvents = false;
                watcher.Dispose();
            }
            _watchers.Clear();
        }

        private void WriteState()
        {
            var path = Path.Combine(_root,
                StateRelative.Replace('/', Path.DirectorySeparatorChar));
            try
            {
                var payload = new JsonObject
                {
                    ["updated_at"] = Canon.UtcNow(),
                    ["running"] = _running,
                    ["project_root"] = _root,
                    ["sweep_interval"] = _options.SweepInterval,
                    ["sync_interval"] = _options.SyncInterval,
                    ["debounce_seconds"] = _options.Debounce,
                    ["push"] = _options.Push,
                    ["sweeps"] = _sweeps,
                    ["syncs"] = _syncs,
                    ["pending_debounce"] = new JsonArray(
                        _dirtySince.Keys.Order(StringComparer.Ordinal)
                            .Select(k => (JsonNode?)JsonValue.Create(k))
                            .ToArray()),
                    ["dirwatch_worktrees"] = _watchers.Count,
                    ["last_sweep"] = _lastSweep,
                    ["last_sync"] = _lastSync,
                };
                Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                using var document =
                    JsonDocument.Parse(payload.ToJsonString());
                Canon.WriteJsonAtomic(
                    path, Canon.Indented(document.RootElement) + "\n");
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }

    private static async Task<int> Watch(string root, Options options)
    {
        var service = new Service(root, options);
        return await service.Run();
    }

    // -- sweep / sync primitives (shared with --once / --sweep / --sync) --

    private static JsonObject Sweep(
        string root, Options options,
        Dictionary<string, (string, double)> dirtySince)
    {
        var results = new JsonObject();
        var scopes = new JsonObject();
        var now = Environment.TickCount64 / 1000.0;
        var worktrees = Sync.ListWorktrees(root).Select(w => w.Path)
            .Distinct(StringComparer.OrdinalIgnoreCase).ToList();
        if (!worktrees.Contains(root, StringComparer.OrdinalIgnoreCase))
            worktrees.Insert(0, Path.GetFullPath(root));
        foreach (var worktree in worktrees)
        {
            Status.Snapshot snapshot;
            try
            {
                snapshot = Status.CaptureSnapshot(worktree);
            }
            catch (Exception)
            {
                dirtySince.Remove(worktree);
                continue;
            }
            if (!snapshot.Dirty)
            {
                dirtySince.Remove(worktree);
                continue;
            }
            scopes[worktree] = new JsonArray(
                snapshot.AffectedScopes
                    .Select(s => (JsonNode?)JsonValue.Create(s)).ToArray());
            if (!dirtySince.TryGetValue(worktree, out var marker)
                || marker.Item1 != snapshot.Fingerprint)
            {
                dirtySince[worktree] = (snapshot.Fingerprint, now);
                results[worktree] = "debounce";
                continue;
            }
            if (now - marker.Item2 < options.Debounce)
            {
                results[worktree] = "debounce";
                continue;
            }
            var status = SelfCommit.RunOnce(
                root, worktree, snapshot: snapshot);
            results[worktree] = status;
            if (status is "committed" or "clean")
                dirtySince.Remove(worktree);
        }
        return new JsonObject
        {
            ["at"] = Canon.EpochSeconds(),
            ["results"] = results,
            ["affected_scope"] = scopes,
        };
    }

    private static string SyncCycle(
        string root, Options options, bool push = false)
    {
        var effectivePush = push || options.Push;
        try
        {
            return Sync.Synchronize(root,
                commitDirty: options.CommitDirty, push: effectivePush);
        }
        catch (LockBusyException)
        {
            return "skipped:lock-busy";
        }
    }
}
