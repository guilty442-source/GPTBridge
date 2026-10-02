using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.Automation;

/// Per-plane supervision inside the unified host (module layer —
/// NO_GLOBAL_STATE / LINEAR_OPERATION sequencing; each plane is an
/// isolated failure domain over its own state file and lock).
///
/// ``Run`` wraps one plane entrypoint (``CodexAutomation.RunWatch``,
/// ``PermissionAutomation.RunWatch``, ``GitAutomation WatchService``)
/// with the bounded contract the retired per-exe Rust supervisors
/// enforced, expressed per plane instead of per process:
///
///   exit 0  graceful end (flow disabled / stop-file) → re-check the
///           manifest every ``IdleRecheck`` so a re-enabled or
///           temporarily gated plane resumes without a host restart;
///   exit 1  single-instance lock held by an external standalone
///           watcher → defer for ``LockDefer`` and adopt the plane
///           when the holder releases (never double-runs);
///   throw   plane fault → ``FaultBackoff``, ``MaxFaults`` consecutive
///           faults parks the plane for the host lifetime (equivalent
///           to the old supervisor exhausting ``max_restart_attempts``,
///           but no other plane is disturbed).
///
/// Outcome transitions append to
/// ``main-system/runtime/state/automation-host-planes.jsonl`` — the
/// host-side audit ledger, complementing the Rust supervisor's
/// ``automation-host`` lifecycle ledger.
internal static class Planes
{
    private const int MaxFaults = 5;
    private static readonly TimeSpan LockDefer =
        TimeSpan.FromSeconds(15);
    private static readonly TimeSpan FaultBackoff =
        TimeSpan.FromSeconds(5);
    private static readonly TimeSpan IdleRecheck =
        TimeSpan.FromSeconds(30);
    private const string LedgerRelative =
        "main-system/runtime/state/automation-host-planes.jsonl";
    private const string FlowsRelative =
        "main-system/config/automation-flows.json";

    private enum Outcome { Graceful, LockHeld, Faulted }

    public static async Task Run(
        string root, string plane, Func<Task<int>> entry)
    {
        var faults = 0;
        Outcome? last = null;
        for (;;)
        {
            int code;
            var note = "";
            try
            {
                code = await entry();
            }
            catch (Exception error)
            {
                code = -1;
                note = error.GetType().Name;
            }
            var outcome = code switch
            {
                0 => Outcome.Graceful,
                1 => Outcome.LockHeld,
                _ => Outcome.Faulted,
            };
            if (outcome != last)
            {
                Journal(root, plane,
                    outcome switch
                    {
                        Outcome.Graceful => "graceful-exit",
                        Outcome.LockHeld => "defer-external-holder",
                        _ => $"faulted:{note}",
                    });
                last = outcome;
            }
            switch (outcome)
            {
                case Outcome.Graceful:
                    faults = 0;
                    await Task.Delay(IdleRecheck);
                    continue;
                case Outcome.LockHeld:
                    faults = 0;
                    await Task.Delay(LockDefer);
                    continue;
            }
            faults++;
            if (faults >= MaxFaults)
            {
                Journal(root, plane, "parked-fault-budget");
                return;
            }
            await Task.Delay(FaultBackoff);
        }
    }

    /// Manifest kill switch — same lookup the per-plane loops use.
    public static bool FlowEnabled(string root, string name)
    {
        var flow = ReadFlow(root, name);
        return flow?["enabled"]?.GetValue<bool?>() ?? true;
    }

    /// ``auto_execute`` tunable from the flow manifest (codex
    /// amendment intake's governed execution gate).
    public static bool AutoExecute(string root, string name)
    {
        var flow = ReadFlow(root, name);
        return flow?["auto_execute"]?.GetValue<bool?>() ?? false;
    }

    private static JsonNode? ReadFlow(string root, string name)
    {
        try
        {
            var manifest = JsonNode.Parse(File.ReadAllText(
                Path.Combine(root, FlowsRelative)));
            // flows is an object keyed by flow name (same lookup the
            // codex/permission planes' Flow() use) — an array-shaped
            // read silently returned null and defeated kill switches.
            return manifest?["flows"]?[name];
        }
        catch (Exception error) when (error is IOException
            or JsonException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    /// Append one audit line for a plane lifecycle transition.
    /// File append is fail-soft: audit must never kill a plane.
    public static void Journal(string root, string plane, string @event)
    {
        try
        {
            var path = Path.Combine(root, LedgerRelative);
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.AppendAllText(path, JsonSerializer.Serialize(new
            {
                at = DateTimeOffset.UtcNow.ToString("o"),
                plane,
                @event,
            }) + "\n");
        }
        catch (Exception error) when (error is IOException
            or UnauthorizedAccessException)
        {
            Console.Error.WriteLine(
                $"[automation-host] journal write failed: {error.Message}");
        }
    }
}
