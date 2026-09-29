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
            var stopFile = Path.Combine(_root, "main-system", "runtime",
                "state", "git-automation.stop");
            try { File.Delete(stopFile); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
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
                    var tick = CycleTick(push);
                    if (await Task.WhenAny(
                            tick, Task.Delay(tickDeadline)) != tick)
                        Console.Error.WriteLine(
                            $"[git-automation] cycle exceeded " +
                            $"{tickDeadline.TotalSeconds:0}s deadline");
                    else
                        await tick; // observe faults — WhenAny alone
                                    // swallows a failed cycle silently
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

        private async Task CycleTick(bool push)
        {
            _lastSweep = await Task.Run(() =>
            {
                var result = Sweep(_root, _options, _dirtySince);
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
