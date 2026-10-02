using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.Automation;

/// Self-learning plane of the unified automation host — the native
/// successor of the retired ``self_learning_driver`` duty. Cadence-only:
/// each tick spawns ``xc-learning.exe --run-once`` as a bounded governed
/// subprocess; every policy gate (kill switch, quiet hours, min-new-
/// examples, inference exclusion via the service-descriptor probe,
/// governor quota, serial lane cap) is enforced authoritatively inside
/// ``xc-learning`` — the plane never duplicates or relaxes them. No
/// ``--force``: the threshold stays a policy decision.
///
/// Exit contract (consumed by ``Planes.Run``):
///   0  flow disabled / graceful stop → host re-checks the manifest
///      every IdleRecheck and resumes without a restart;
///   1  ``self-learning-automation.lock`` held by an external watcher →
///      defer (never double-runs a cadence);
///   other / throw → faulted, supervised per-plane.
internal static class SelfLearningPlane
{
    private const string LockName = "self-learning-automation.lock";
    private const string StateRelative =
        "main-system/runtime/state/self-learning-automation.json";
    private const string StateOverrideRelative =
        "main-system/runtime/state/automation-flows-state.json";
    private const string FlowsRelative =
        "main-system/config/automation-flows.json";
    private const double DefaultIntervalS = 900;
    // Absolute bound on one spawned cycle (dataset → queued job →
    // native training → eval gates → lifecycle). Training itself is
    // capped by policy max_train_seconds (21600); the plane adds slack
    // for collection + evaluation and still refuses to wait forever.
    private static readonly TimeSpan CycleTimeout =
        TimeSpan.FromHours(8);

    private static readonly string[] ExeCandidates =
    {
        @"Standalone tools\local-model\src\backend\csharp" +
            @"\GPTBridge.XingchengLearning\publish\xc-learning.exe",
        @"Standalone tools\local-model\src\backend\csharp" +
            @"\GPTBridge.XingchengLearning\bin\Release\net10.0\xc-learning.exe",
    };

    public static async Task<int> RunWatch(string root)
    {
        var lockPath = Path.Combine(
            root, "main-system", "runtime", "state", LockName);
        Directory.CreateDirectory(Path.GetDirectoryName(lockPath)!);
        FileStream planeLock;
        try
        {
            planeLock = new FileStream(lockPath, FileMode.Create,
                FileAccess.ReadWrite, FileShare.None);
        }
        catch (Exception error) when (error is IOException
            or UnauthorizedAccessException)
        {
            return 1; // an external holder owns this cadence
        }
        await using (planeLock)
        {
            while (true)
            {
                if (!Enabled(root))
                    return 0; // kill switch — graceful, host re-checks
                var interval = Interval(root);
                // Scheduling is internalized in the tool body:
                // xc-learning --schedule heartbeats at
                // self-learning-schedule.json. A fresh heartbeat means the
                // native resident loop owns cadence — this plane must not
                // act as a second scheduler, so it defers the whole tick.
                if (NativeSchedulerAlive(root))
                {
                    Persist(root, new JsonObject
                    {
                        ["at"] = DateTimeOffset.UtcNow.ToString("o"),
                        ["action"] = "deferred",
                        ["reason"] = "native-scheduler-alive",
                    });
                    await Task.Delay(interval);
                    continue;
                }
                var started = Stopwatch.StartNew();
                var outcome = await RunCycle(root);
                Persist(root, outcome);
                var remaining = interval - started.Elapsed;
                if (remaining > TimeSpan.Zero)
                    await Task.Delay(remaining);
            }
        }
    }

    /// True while the in-body scheduler is alive: its heartbeat file
    /// carries the owning pid — liveness is the pid, not the timestamp,
    /// because a training drain can legitimately stall a tick for hours.
    private static bool NativeSchedulerAlive(string root)
    {
        var heartbeat = Path.Combine(root, "Standalone tools",
            "local-model", "xingcheng", "runtime", "state",
            "self-learning-schedule.json");
        try
        {
            if (!File.Exists(heartbeat)) return false;
            using var doc = JsonDocument.Parse(
                File.ReadAllText(heartbeat));
            if (!doc.RootElement.TryGetProperty("pid",
                    out var pidEl) ||
                pidEl.ValueKind != JsonValueKind.Number)
                return false;
            using var process = Process.GetProcessById(
                pidEl.GetInt32());
            return !process.HasExited;
        }
        catch { return false; }
    }

    /// One bounded cycle spawn. The result record is evidence, never
    /// a gate — xc-learning's own policy pipeline decides allowed/denied.
    private static async Task<JsonObject> RunCycle(string root)
    {
        var exe = ExeCandidates
            .Select(relative => Path.Combine(root, relative))
            .FirstOrDefault(File.Exists);
        var result = new JsonObject
        {
            ["at"] = DateTimeOffset.UtcNow.ToString("o"),
        };
        if (exe is null)
        {
            result["action"] = "skipped";
            result["reason"] = "xc-learning.exe not found";
            return result;
        }
        var toolRoot = Path.Combine(
            root, "Standalone tools", "local-model");
        var info = new ProcessStartInfo
        {
            FileName = exe,
            WorkingDirectory = root,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
        };
        info.ArgumentList.Add("--tool-root");
        info.ArgumentList.Add(toolRoot);
        info.ArgumentList.Add("--run-once");
        using var process = Process.Start(info)!;
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        result["action"] = "run-once";
        try
        {
            using var timeout = new CancellationTokenSource(CycleTimeout);
            await process.WaitForExitAsync(timeout.Token);
            result["exit_code"] = process.ExitCode;
        }
        catch (OperationCanceledException)
        {
            try { process.Kill(entireProcessTree: true); }
            catch { /* already gone */ }
            result["exit_code"] = -1;
            result["timeout_s"] = CycleTimeout.TotalSeconds;
        }
        // Bounded evidence excerpt — the authoritative report lands in
        // xingcheng/runtime/logs written by xc-learning itself.
        var output = await stdout;
        await stderr;
        result["report_excerpt"] = output.Length > 4000
            ? output[..4000]
            : output;
        return result;
    }

    /// Manifest ``flows.self-learning.enabled`` with the documented
    /// runtime override (``automation-flows-state.json`` wins when it
    /// names this flow) — same kill-switch semantics as the other
    /// planes' Flow() lookups.
    private static bool Enabled(string root)
    {
        try
        {
            var overrides = JsonNode.Parse(File.ReadAllText(
                Path.Combine(root, StateOverrideRelative)))
                ?["overrides"]?["self-learning"]?["enabled"];
            if (overrides is not null)
                return overrides.GetValue<bool>();
        }
        catch (Exception error) when (error is IOException
            or JsonException or UnauthorizedAccessException)
        {
            // no override file — fall through to the manifest
        }
        try
        {
            return JsonNode.Parse(File.ReadAllText(
                    Path.Combine(root, FlowsRelative)))
                ?["flows"]?["self-learning"]?["enabled"]
                ?.GetValue<bool>() ?? false;
        }
        catch (Exception error) when (error is IOException
            or JsonException or UnauthorizedAccessException)
        {
            return false; // unreadable manifest — fail closed
        }
    }

    private static TimeSpan Interval(string root)
    {
        try
        {
            var seconds = JsonNode.Parse(File.ReadAllText(
                    Path.Combine(root, FlowsRelative)))
                ?["flows"]?["self-learning"]?["interval_s"]
                ?.GetValue<double?>() ?? DefaultIntervalS;
            return seconds > 0 ? TimeSpan.FromSeconds(seconds)
                               : TimeSpan.FromSeconds(DefaultIntervalS);
        }
        catch (Exception error) when (error is IOException
            or JsonException or UnauthorizedAccessException)
        {
            return TimeSpan.FromSeconds(DefaultIntervalS);
        }
    }

    private static void Persist(string root, JsonObject outcome)
    {
        try
        {
            outcome["next_tick_in_s"] = Interval(root).TotalSeconds;
            var state = new JsonObject
            {
                ["format"] = "star-self-learning-automation/v1",
                ["updated_at"] =
                    DateTimeOffset.UtcNow.ToString("o"),
                ["last_cycle"] = outcome,
            };
            var path = Path.Combine(root, StateRelative);
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var tmp = path + ".tmp";
            File.WriteAllText(tmp, state.ToJsonString() + "\n");
            File.Move(tmp, path, overwrite: true);
        }
        catch (Exception error) when (error is IOException
            or UnauthorizedAccessException)
        {
            Console.Error.WriteLine(
                $"[self-learning-plane] state write failed: " +
                error.Message);
        }
    }
}
