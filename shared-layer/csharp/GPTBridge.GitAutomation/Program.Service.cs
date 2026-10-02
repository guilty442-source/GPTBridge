using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

internal static partial class Program
{
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
        private bool _wakeAll;
        private readonly List<FileSystemWatcher> _watchers = new();
        private readonly List<string> _watchRoots = new();
        private readonly HashSet<string> _wakeWorktrees =
            new(StringComparer.OrdinalIgnoreCase);
        private double _lastEventWake;
        private readonly object _gate = new();
        private bool _push;

        public Service(string root, Options options)
        {
            _root = root;
            _options = options;
        }

        /// Single-instance arbitration for the resident watch loop —
        /// same contract as codex-automation.lock /
        /// permission-automation.lock: an external holder means another
        /// host (standalone --watch or the unified automation host)
        /// owns the git plane.  Bounded one-shot modes skip the lock so
        /// manual --once/--sweep/--sync still work beside a supervised
        /// instance.
        private FileStream? AcquireInstanceLock()
        {
            if (_options.Mode != "watch")
                return null;
            var lockPath = Path.Combine(_root, "main-system", "runtime",
                "state", "git-automation.lock");
            try
            {
                Directory.CreateDirectory(
                    Path.GetDirectoryName(lockPath)!);
                return new FileStream(lockPath, FileMode.Create,
                    FileAccess.ReadWrite, FileShare.None);
            }
            catch (Exception error) when (error is IOException
                or UnauthorizedAccessException)
            {
                Console.Error.WriteLine(
                    "[git-automation] already running (lock held)");
                return null;
            }
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
            using var instanceLock = AcquireInstanceLock();
            if (instanceLock is null && _options.Mode == "watch")
                return 1;
            StartDirwatch();
            Console.CancelKeyPress += (_, e) =>
            {
                e.Cancel = true;
                _running = false;
            };
            // Push flag: manifest flow entry wins; --push is the fallback.
            var push = FlowsConfig.PushEnabled(_root) || _options.Push;
            _push = push;
            Console.WriteLine(
                $"[git-automation] started " +
                $"(sweep={_options.SweepInterval:0}s " +
                $"sync={_options.SyncInterval:0}s " +
                $"debounce={_options.Debounce:0}s push={push})");
            var tickDeadline = TimeSpan.FromSeconds(
                Math.Max(60.0,
                    Math.Min(900.0, _options.SweepInterval * 5)));
            var stopFile = Path.Combine(_root, "main-system", "runtime",
                "state", "git-automation.stop");
            try { File.Delete(stopFile); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
            Task? cycle = null;
            while (_running)
            {
                // Kill-switch re-check every cycle (manifest enabled=false
                // or runtime override) — fail closed, then exit so the
                // supervisor never respawns a denied flow.
                if (!FlowsConfig.FlowEnabled(_root))
                {
                    Console.WriteLine(
                        "[git-automation] disabled by automation-flows " +
                        "manifest/override — stopping");
                    break;
                }
                try
                {
                    // Surface a fault left behind by a timed-out tick
                    // before deciding whether the lane is free.
                    if (cycle is { IsFaulted: true })
                        Console.Error.WriteLine(
                            "[git-automation] cycle error: " +
                            cycle.Exception?.GetBaseException().Message);
                    // A tick that outlives its deadline keeps running to
                    // completion in the background — never stack a second
                    // CycleTick on top of it.  The dirty-state map and the
                    // sweep/sync counters are single-writer state; two
                    // live ticks corrupt the shared Dictionary.
                    if (cycle is null || cycle.IsCompleted)
                    {
                        // Drain the event scope before the cycle: an
                        // unscoped wake (overflow, unresolvable path)
                        // falls back to a full sweep.
                        HashSet<string>? scope = null;
                        lock (_gate)
                        {
                            if (!_wakeAll && _wakeWorktrees.Count > 0)
                                scope = new HashSet<string>(
                                    _wakeWorktrees,
                                    StringComparer.OrdinalIgnoreCase);
                            _wakeWorktrees.Clear();
                        }
                        var wakeAll = _wakeAll;
                        _wakeAll = false;
                        if (wakeAll)
                        {
                            // A stale watcher set must not skip the
                            // reconciliation sweep.
                            try { RefreshWatchers(); }
                            catch (Exception) { }
                        }
                        cycle = CycleTick(push, scope);
                        if (await Task.WhenAny(
                                cycle, Task.Delay(tickDeadline)) != cycle)
                            Console.Error.WriteLine(
                                $"[git-automation] cycle exceeded " +
                                $"{tickDeadline.TotalSeconds:0}s " +
                                "deadline — draining in background");
                        else
                            await cycle; // observe faults — WhenAny
                                         // alone swallows them silently
                    }
                }
                catch (Exception error)
                {
                    Console.Error.WriteLine(
                        $"[git-automation] cycle error: {error.Message}");
                }
                // --once: exactly one sweep+sync cycle, then exit.
                if (_options.Mode == "once")
                    break;
                var delay = Task.Delay(
                    TimeSpan.FromSeconds(_options.SweepInterval));
                while (_running && !_wake)
                {
                    // Supervisor stop sentinel: a governed shutdown request
                    // written as a file — exits within ~500 ms instead of
                    // waiting out the sweep interval.
                    if (File.Exists(stopFile))
                        _running = false;
                    if (await Task.WhenAny(
                            delay, Task.Delay(500)) == delay)
                        break;
                }
                _wake = false;
            }
            StopDirwatch();
            // Final state write: leave running=false behind so --status
            // never reports a dead service as alive.
            WriteState();
            return 0;
        }

        private async Task CycleTick(
            bool push, IReadOnlySet<string>? scope = null)
        {
            _lastSweep = await Task.Run(() =>
            {
                var result = Sweep(_root, _options, _dirtySince, scope);
                _sweeps++;
                WriteState();
                return result;
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
                    ["result"] = sync.Status,
                };
                if (sync.Sql is not null)
                    _lastSync["sql"] = sync.Sql;
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
        //    Events carry their path so the wake sweeps only the
        //    signalled worktree; unresolvable paths and buffer
        //    overflows escalate to a full sweep — the periodic tick
        //    remains the fallback for silently missed events.

        private void StartDirwatch()
        {
            try
            {
                RefreshWatchers();
            }
            catch (Exception)
            {
                // Fail-soft: sweep TTL still covers change detection.
            }
        }

        private void RefreshWatchers()
        {
            var worktrees = Sync.ListWorktrees(_root)
                .Select(w => Path.GetFullPath(w.Path))
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .Take(16).ToHashSet(StringComparer.OrdinalIgnoreCase);
            if (!worktrees.Contains(_root))
                worktrees.Add(Path.GetFullPath(_root));
            for (var i = _watchers.Count - 1; i >= 0; i--)
            {
                if (worktrees.Contains(_watchRoots[i]))
                    continue;
                _watchers[i].EnableRaisingEvents = false;
                _watchers[i].Dispose();
                _watchers.RemoveAt(i);
                lock (_gate) _watchRoots.RemoveAt(i);
            }
            foreach (var worktree in worktrees)
            {
                if (_watchRoots.Contains(worktree,
                        StringComparer.OrdinalIgnoreCase)
                    || !Directory.Exists(worktree))
                    continue;
                var watcher = new FileSystemWatcher(worktree)
                {
                    IncludeSubdirectories = true,
                    EnableRaisingEvents = true,
                    NotifyFilter = NotifyFilters.FileName
                        | NotifyFilters.DirectoryName
                        | NotifyFilters.LastWrite,
                };
                watcher.Changed += (_, e) => OnChanged(e.FullPath);
                watcher.Created += (_, e) => OnChanged(e.FullPath);
                watcher.Deleted += (_, e) => OnChanged(e.FullPath);
                watcher.Renamed += (_, e) => OnChanged(e.FullPath);
                watcher.Error += (_, _) => OnError();
                _watchers.Add(watcher);
                lock (_gate) _watchRoots.Add(worktree);
            }
        }

        /// <summary>Longest watched root that prefixes the changed path —
        /// a nested worktree outranks the parent watcher it also fired
        /// through.</summary>
        private string? ScopeFor(string path)
        {
            string? best = null;
            string[] roots;
            lock (_gate) roots = _watchRoots.ToArray();
            foreach (var watched in roots)
            {
                if (path.StartsWith(
                        watched + Path.DirectorySeparatorChar,
                        StringComparison.OrdinalIgnoreCase)
                    && (best is null || watched.Length > best.Length))
                    best = watched;
            }
            return best;
        }

        private void OnChanged(string? fullPath)
        {
            // Resolve and record the scope even when the wake itself is
            // throttled — the next wake (or the periodic sweep) then
            // visits every worktree that signalled, not just the one
            // whose event happened to fall outside the throttle window.
            var scope = fullPath is null ? null : ScopeFor(fullPath);
            // A path under a ``.worktrees/<name>`` subtree that resolved
            // to the parent root means the nested worktree has no
            // watcher yet (created after startup) — escalate so the
            // sweep and the watcher set pick it up.
            var nestedPrefix = Path.Combine(_root, ".worktrees")
                + Path.DirectorySeparatorChar;
            if (scope is null
                || (fullPath!.StartsWith(nestedPrefix,
                        StringComparison.OrdinalIgnoreCase)
                    && !scope.StartsWith(nestedPrefix,
                        StringComparison.OrdinalIgnoreCase)))
            {
                _wakeAll = true;
            }
            else
            {
                lock (_gate) _wakeWorktrees.Add(scope);
            }
            var now = Environment.TickCount64 / 1000.0;
            if (now - _lastEventWake < 15.0)
                return;
            _lastEventWake = now;
            _wake = true;
        }

        private void OnError()
        {
            // Internal buffer overflowed — events were lost; the only
            // safe recovery is a full reconciliation sweep.
            _wakeAll = true;
            _wake = true;
        }

        private void StopDirwatch()
        {
            foreach (var watcher in _watchers)
            {
                watcher.EnableRaisingEvents = false;
                watcher.Dispose();
            }
            _watchers.Clear();
            lock (_gate) _watchRoots.Clear();
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
                    ["push"] = _push,
                    ["sweeps"] = _sweeps,
                    ["syncs"] = _syncs,
                    ["pending_debounce"] = new JsonArray(
                        _dirtySince.Keys.ToArray()
                            .Order(StringComparer.Ordinal)
                            .Select(k => (JsonNode?)JsonValue.Create(k))
                            .ToArray()),
                    ["dirwatch_worktrees"] = _watchers.Count,
                    ["last_sweep"] = (JsonObject)_lastSweep.DeepClone(),
                    ["last_sync"] = (JsonObject)_lastSync.DeepClone(),
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
}
