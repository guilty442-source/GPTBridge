using System.Text.Json.Nodes;
using Npgsql;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Codex/SQL plane automation — the resident loop that replaces the
/// retired Python drivers (B167/B38).  Owns three governed flows from
/// ``main-system/config/automation-flows.json`` (re-read every tick so a
/// ``enabled=false`` kill switch propagates within one interval):
///
///   codex-amendment-intake  — Driver.AdvanceAll over the governed
///                           intake; auto_execute taken from the flow
///                           entry (A10 allowlist; fail-closed).
///   codex-pin-sync          — release-dependencies.json
///                           governance_references codex_version /
///                           codex_sha256 convergence (surgical
///                           two-field rewrite; never touches authority).
///   codex-maintenance       — artifact presence + live-parity +
///                           projection-pollution probe; repairs
///                           derived projections first, re-verifies,
///                           only then re-exports the .sql artifact and
///                           re-renders the zh-TW mirrors (authority is
///                           never written by this pass).  Also refreshes
///                           the autogen blocks inside the
///                           architecture-*.md view documents so the
///                           projection never drifts from the scanned
///                           implementation trees (autogen-scanner/v1).
///
/// State: ``main-system/runtime/state/codex-automation.json``; single
/// instance enforced by ``codex-automation.lock`` (exclusive handle).
/// </summary>
internal static class CodexAutomation
{
    private const string AmendmentFlow = "codex-amendment-intake";
    private const string PinSyncFlow = "codex-pin-sync";
    private const string MaintenanceFlow = "codex-maintenance";

    private const double DefaultTickSeconds = 30;
    private const double DefaultFlowInterval = 300;

    private static readonly string[] FlowNames =
        { AmendmentFlow, PinSyncFlow, MaintenanceFlow };

    private static string ArtifactPath() =>
        Path.Combine(UpdatePipeline.CanonicalCodexRoot(), "data",
            UpdatePipeline.DatabaseName);

    private static string FlowsPath() =>
        Path.Combine(Repo.Root(), "main-system", "config",
            "automation-flows.json");

    private static string StatePath() =>
        Path.Combine(Repo.StateDir(), "codex-automation.json");

    // --------------------------------------------------------------
    //  flow config — fail-closed: unreadable/missing entry = disabled.
    // --------------------------------------------------------------

    private static JsonObject? Flow(string name)
    {
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(FlowsPath()));
            return node?["flows"]?[name] as JsonObject;
        }
        catch (IOException) { return null; }
        catch (UnauthorizedAccessException) { return null; }
        catch (System.Text.Json.JsonException) { return null; }
    }

    private static bool FlowEnabled(string name) =>
        Flow(name)?["enabled"]?.GetValue<bool>() ?? false;

    private static double FlowInterval(string name)
    {
        var raw = Flow(name)?["interval_s"];
        return raw is not null
            && double.TryParse(raw.ToString(), out var seconds)
            && seconds > 0
                ? seconds
                : DefaultFlowInterval;
    }

    // --------------------------------------------------------------
    //  maintenance pass — artifact parity + derived-projection health
    // --------------------------------------------------------------

    /// <summary>``codex_search_document`` lossy-encoding probe: a
    /// ``??`` payload means a writer mangled zh text (the column holds
    /// subject+content, never a literal double question mark).</summary>
    private static long PollutionCount()
    {
        using var connection = PgDsn.Readonly();
        using var command = new NpgsqlCommand(
            $"SELECT count(*) FROM \"{PgDsn.CodexSchema}\""
            + ".codex_search_document WHERE subject LIKE '%??%'"
            + " OR content LIKE '%??%'", connection);
        return (long)(command.ExecuteScalar() ?? 0L);
    }

    /// <summary>zh-TW mirror render — temp dir → atomic replace →
    /// read-only; returns the residual error list (empty = clean).</summary>
    private static string[] MirrorRefresh()
    {
        var codexRoot = UpdatePipeline.CanonicalCodexRoot();
        var tempRoot = Path.Combine(Path.GetTempPath(),
            $"codex-mirror-{Guid.NewGuid():N}");
        try
        {
            MirrorWriter.RenderMirrorParts(PgDsn.CodexSchema,
                tempRoot, codexRoot);
            foreach (var name in ChineseMirror.PartNames)
                UpdatePipeline.AtomicReplace(
                    Path.Combine(tempRoot, name),
                    Path.Combine(codexRoot, name));
            return MirrorWriter.MirrorErrors(
                PgDsn.CodexSchema, codexRoot, "live");
        }
        finally
        {
            if (Directory.Exists(tempRoot))
                Directory.Delete(tempRoot, true);
        }
    }

    /// <summary>An amendment execution holds an in-flight stage when a
    /// staging directory still carries its isolation marker.  Writing
    /// projections (artifact export, zh-TW mirror refresh) while a wire
    /// is publishing races the same target files and re-seals read-only
    /// attributes under the publisher — defer instead.  Stale markers
    /// (abandoned by a failed run) age out after fifteen minutes so a
    /// dead executor can never block maintenance forever.</summary>
    private static bool AmendmentInFlight()
    {
        var stagingRoot = Executor.DefaultStaging();
        if (!Directory.Exists(stagingRoot))
            return false;
        var cutoff = DateTime.UtcNow - TimeSpan.FromMinutes(15);
        try
        {
            foreach (var dir in Directory.EnumerateDirectories(
                stagingRoot))
            {
                var marker = Path.Combine(dir,
                    UpdatePipeline.IsolationMarker);
                if (File.Exists(marker)
                    && !File.Exists(Path.Combine(dir,
                        UpdatePipeline.ReleasedMarker))
                    && File.GetLastWriteTimeUtc(marker) > cutoff)
                    return true;
            }
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
        return false;
    }

    /// <summary>One maintenance pass.  Order matters: the artifact is a
    /// deterministic projection of the authority — repair the live
    /// derived tables first, and only re-export the artifact when the
    /// authoritative tables themselves have moved (amendment sealed
    /// without a fresh export).</summary>
    public static Dictionary<string, object?> Maintain()
    {
        var artifact = ArtifactPath();
        if (AmendmentInFlight())
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = true,
                ["actions"] = new List<object?>
                    { "deferred:amendment-in-flight" },
                ["detail"] = new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["deferred"] = "amendment-in-flight",
                },
            };
        var actions = new List<object?>();
        var detail = new Dictionary<string, object?>(
            StringComparer.Ordinal);

        if (!File.Exists(artifact))
        {
            PgExport.ExportPostgresqlCodex(artifact);
            UpdatePipeline.SetReadOnly(artifact);
            actions.Add("export:artifact-missing");
        }

        var parity = PgExport.VerifySqlParity(artifact);
        detail["parity"] = parity["result"];
        var polluted = PollutionCount();
        detail["polluted_rows"] = polluted;

        if ((string?)parity["result"] == "FAIL" || polluted > 0)
        {
            var repair = GenerationProjections
                .RepairLiveProjections();
            detail["repaired"] = repair;
            actions.Add("repair-projections");
            if ((string?)parity["result"] == "FAIL")
            {
                parity = PgExport.VerifySqlParity(artifact);
                detail["parity_after_repair"] = parity["result"];
            }
        }

        if ((string?)parity["result"] == "FAIL")
        {
            // Authority moved legitimately (sealed amendment) — refresh
            // the interchange artifact to match.  The artifact is sealed
            // read-only between exports; unseal, rewrite, re-seal.
            UpdatePipeline.SetReadOnly(artifact, false);
            PgExport.ExportPostgresqlCodex(artifact);
            UpdatePipeline.SetReadOnly(artifact);
            detail["export_mismatches"] = parity["mismatches"];
            actions.Add("export:authority-drift");
        }

        var codexRoot = UpdatePipeline.CanonicalCodexRoot();
        var mirrorErrors = MirrorWriter.MirrorErrors(
            PgDsn.CodexSchema, codexRoot, "live");
        if (mirrorErrors.Length > 0)
        {
            mirrorErrors = MirrorRefresh();
            actions.Add("mirror-zh");
        }
        detail["mirror_errors"] = mirrorErrors
            .Cast<object?>().ToList();

        // Architecture-*.md autogen blocks are derived projections of
        // the implementation trees — refresh them in the same pass so
        // the normative view never drifts.  A refresh failure surfaces
        // as not-ok exactly like mirror_errors.
        var arch = ArchitectureProjection.Refresh(Repo.Root());
        detail["arch_projection"] = arch;
        if (arch["changed"] is List<object?> archChanged
            && archChanged.Count > 0)
            actions.Add("arch-projection");

        if (actions.Count == 0)
            actions.Add("clean");

        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["ok"] = detail["parity"] is "PASS"
                && polluted == 0
                && mirrorErrors.Length == 0
                && arch["ok"] is true,
            ["actions"] = actions,
            ["detail"] = detail,
        };
    }

    // --------------------------------------------------------------
    //  pin sync — release-dependencies codex reference convergence
    // --------------------------------------------------------------

    /// <summary>Surgical two-field rewrite inside the
    /// ``governance_references`` block only; the file layout is
    /// hand-maintained so a full JSON round-trip would churn it.</summary>
    public static Dictionary<string, object?> PinSync()
    {
        var state = PgExport.AuthorityState();
        var version = state["codex_version"]?.ToString() ?? "";
        var sha = state["source_sha256"]?.ToString() ?? "";
        var file = Path.Combine(Repo.Root(), "shared-layer",
            "release-dependencies.json");
        var text = File.ReadAllText(file);
        var anchor = text.IndexOf("\"governance_references\"",
            StringComparison.Ordinal);
        if (anchor < 0)
            return new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["error"] = "governance_references:anchor-missing",
            };
        var slice = text[anchor..];
        string Rewrite(string source, string key, string value)
        {
            var marker = $"\"{key}\": \"";
            var start = source.IndexOf(marker,
                StringComparison.Ordinal);
            if (start < 0)
                return source;
            start += marker.Length;
            var end = source.IndexOf('"', start);
            return end < 0 ? source
                : source[..start] + value + source[end..];
        }
        var patched = Rewrite(
            Rewrite(slice, "codex_version", version),
            "codex_sha256", sha);
        var drift = patched != slice;
        if (drift)
        {
            // Plain tmp+move: the contract file is a normal tracked
            // source — UpdatePipeline.AtomicReplace would leave the
            // read-only attribute meant for codex artifacts.
            var temporary = file + ".pinsync-tmp";
            File.WriteAllText(temporary, text[..anchor] + patched);
            File.Move(temporary, file, overwrite: true);
        }
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["ok"] = true,
            ["drift"] = drift,
            ["codex_version"] = version,
            ["codex_sha256"] = sha,
        };
    }

    // --------------------------------------------------------------
    //  resident loop
    // --------------------------------------------------------------

    private static void PersistState(JsonObject state)
    {
        Directory.CreateDirectory(Repo.StateDir());
        var temporary = StatePath() + ".tmp";
        File.WriteAllText(temporary, state.ToJsonString());
        File.Move(temporary, StatePath(), overwrite: true);
    }

    /// <summary>Resident automation loop; one tick evaluates each flow
    /// against its own ``interval_s`` and per-flow last-run stamp.
    /// ``--interval`` overrides the tick granularity only — flow
    /// cadence still comes from the manifest.</summary>
    public static async Task<int> RunWatch(double? tickOverride)
    {
        Directory.CreateDirectory(Repo.StateDir());
        var lockPath = Path.Combine(Repo.StateDir(),
            "codex-automation.lock");
        FileStream lockHandle;
        try
        {
            lockHandle = new FileStream(lockPath, FileMode.Create,
                FileAccess.ReadWrite, FileShare.None);
        }
        catch (IOException)
        {
            Console.Error.WriteLine(
                "codex-automation already running (lock held)");
            return 1;
        }
        await using (lockHandle)
        {
            var tick = tickOverride ?? DefaultTickSeconds;
            var nextDue = FlowNames.ToDictionary(
                name => name, _ => DateTimeOffset.MinValue);
            var lastResult = new Dictionary<string, object?>();
            var counters = new Dictionary<string, long>
            {
                ["intake_passes"] = 0,
                ["pin_syncs"] = 0,
                ["maintenance_passes"] = 0,
            };
            using var watch = new IntakeWatch();
            for (;;)
            {
                var now = DateTimeOffset.UtcNow;
                // Event wake: a request drop forces the intake flow due
                // ahead of ``interval_s``; staging churn (an amendment
                // releasing) forces pin-sync so the codex pin converges
                // right away.  Both still keep their own interval as
                // the fallback for silently missed notifications.
                if (watch.TakeIntake())
                    nextDue[AmendmentFlow] = DateTimeOffset.MinValue;
                if (watch.TakePin())
                    nextDue[PinSyncFlow] = DateTimeOffset.MinValue;
                var results = new Dictionary<string, object?>();
                foreach (var name in FlowNames)
                {
                    if (now < nextDue[name])
                        continue;
                    nextDue[name] = now + TimeSpan.FromSeconds(
                        FlowInterval(name));
                    if (!FlowEnabled(name))
                    {
                        results[name] = "disabled";
                        lastResult[name] = "disabled";
                        continue;
                    }
                    try
                    {
                        results[name] = name switch
                        {
                            AmendmentFlow => await Driver.AdvanceAll(
                                autoExecute:
                                    Flow(AmendmentFlow)?["auto_execute"]
                                        ?.GetValue<bool>() ?? false),
                            PinSyncFlow => PinSync(),
                            MaintenanceFlow => Maintain(),
                            _ => null,
                        };
                        counters[name switch
                        {
                            AmendmentFlow => "intake_passes",
                            PinSyncFlow => "pin_syncs",
                            _ => "maintenance_passes",
                        }]++;
                        lastResult[name] = results[name];
                    }
                    catch (Exception error)
                    {
                        results[name] =
                            $"error:{error.GetType().Name}:" +
                            error.Message;
                        lastResult[name] = results[name];
                    }
                }
                PersistState(new JsonObject
                {
                    ["running"] = true,
                    ["pid"] = Environment.ProcessId,
                    ["tick_seconds"] = tick,
                    ["updated_at"] = Repo.UtcNow(),
                    ["counters"] = JsonNode.Parse(
                        System.Text.Json.JsonSerializer
                            .Serialize(counters)),
                    ["last_tick"] = JsonNode.Parse(
                        System.Text.Json.JsonSerializer
                            .Serialize(results)),
                    ["last_results"] = JsonNode.Parse(
                        System.Text.Json.JsonSerializer
                            .Serialize(lastResult)),
                });
                await watch.WaitAsync(TimeSpan.FromSeconds(tick));
            }
        }
    }

    // --------------------------------------------------------------
    //  intake dirwatch — event-driven early wake
    // --------------------------------------------------------------

    /// <summary>Watches the governed intake dirs for new
    /// ``codex-amendment-request-*.json`` drops and the amendment
    /// staging root for publish churn.  A request event forces the
    /// intake flow due immediately (pickup in ~one tick-grain instead
    /// of waiting out ``interval_s``); staging events force the
    /// pin-sync flow so the codex pin converges as soon as an
    /// amendment releases.  The per-flow ``interval_s`` cadence is
    /// unchanged and remains the fallback when notifications are
    /// unavailable or silently dropped.</summary>
    private sealed class IntakeWatch : IDisposable
    {
        private readonly List<FileSystemWatcher> _watchers = new();
        private TaskCompletionSource<bool> _signal =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        private volatile bool _intakeDirty;
        private volatile bool _pinDirty;

        public IntakeWatch()
        {
            try
            {
                foreach (var dir in Driver.IntakeDirs())
                {
                    if (!Directory.Exists(dir))
                        continue;
                    var watcher = new FileSystemWatcher(dir)
                    {
                        // Filtered to the request glob — the state dir
                        // also receives this loop's own state file and
                        // unrelated automation state writes, which
                        // must never self-trigger a wake.
                        Filter = Driver.IntakeGlob,
                        EnableRaisingEvents = true,
                    };
                    Hook(watcher, () => _intakeDirty = true);
                    _watchers.Add(watcher);
                }
                // The staging root may not exist before the first
                // amendment — creating the empty scratch dir is
                // idempotent (the executor does the same on use).
                var staging = Executor.DefaultStaging();
                Directory.CreateDirectory(staging);
                var stage = new FileSystemWatcher(staging)
                {
                    IncludeSubdirectories = true,
                    EnableRaisingEvents = true,
                };
                Hook(stage, () => _pinDirty = true);
                _watchers.Add(stage);
            }
            catch (Exception)
            {
                // Fail-soft: the interval cadence still covers pickup.
            }
        }

        private void Hook(FileSystemWatcher watcher, Action mark)
        {
            void Handler(object? _, FileSystemEventArgs __)
            {
                mark();
                _signal.TrySetResult(true);
            }
            watcher.Changed += Handler;
            watcher.Created += Handler;
            watcher.Deleted += Handler;
            watcher.Renamed += (_, e) =>
            {
                mark();
                _signal.TrySetResult(true);
            };
            // Buffer overflow = events were lost; reconcile both flows
            // conservatively on the next loop pass.
            watcher.Error += (_, _) =>
            {
                _intakeDirty = true;
                _pinDirty = true;
                _signal.TrySetResult(true);
            };
        }

        public bool TakeIntake()
        {
            var value = _intakeDirty;
            _intakeDirty = false;
            return value;
        }

        public bool TakePin()
        {
            var value = _pinDirty;
            _pinDirty = false;
            return value;
        }

        /// <summary>Sleep for the tick, returning early on the first
        /// watched event.</summary>
        public async Task WaitAsync(TimeSpan timeout)
        {
            var signaled = _signal.Task;
            await Task.WhenAny(Task.Delay(timeout), signaled);
            if (signaled.IsCompleted)
                _signal = new TaskCompletionSource<bool>(
                    TaskCreationOptions
                        .RunContinuationsAsynchronously);
        }

        public void Dispose()
        {
            foreach (var watcher in _watchers)
            {
                watcher.EnableRaisingEvents = false;
                watcher.Dispose();
            }
            _watchers.Clear();
        }
    }
}
