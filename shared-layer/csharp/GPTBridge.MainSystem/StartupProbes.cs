// Dependency readiness probes — C# port of startup_core/phases.py.
//
// Readiness contracts come from the manifest; a probe must never raise —
// every fault becomes a typed phase result (fail-closed contract).
//
// Contracts implemented:
//   "tcp-or-dsn-select-1"  — PostgreSQL: prefer governed DSN + SELECT 1,
//                            degrade to TCP connect when no DSN exists.
//   "loopback-tcp-<port>"  — loopback TCP connect within deadline.
// §10.7 on-demand activation: absent-but-installed reports "deferred",
// never "degraded"; only a missing capability degrades.

using System.Net.Sockets;
using System.Text.Json.Nodes;

namespace GPTBridge.MainSystem;

/// <summary>Probe outcome — the phase-result dictionary contract.</summary>
public sealed class ProbeResult
{
    public required string Phase { get; init; }
    public required string Label { get; init; }
    public bool Critical { get; init; }
    public bool OnDemand { get; init; }
    public bool Installed { get; init; } = true;
    public bool Ready { get; init; }
    public string State { get; init; } = "fault";
    public string FaultCode { get; init; } = "";
    public string Message { get; init; } = "";
    public int DurationMs { get; init; }

    public JsonObject AsJson() => new()
    {
        ["phase"] = Phase,
        ["label"] = Label,
        ["critical"] = Critical,
        ["on_demand"] = OnDemand,
        ["installed"] = Installed,
        ["ready"] = Ready,
        ["state"] = State,
        ["fault_code"] = FaultCode,
        ["message"] = Message,
        ["duration_ms"] = DurationMs,
    };
}

public static class StartupProbes
{
    /// <summary>
    /// Loopback TCP connect with a hard deadline; returns true when the
    /// connection completes inside ``timeout``.
    /// </summary>
    public static async Task<bool> ProbeTcpAsync(
        string host, int port, TimeSpan timeout, CancellationToken ct)
    {
        using var client = new TcpClient();
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(ct);
        linked.CancelAfter(timeout);
        try
        {
            await client.ConnectAsync(host, port, linked.Token)
                .ConfigureAwait(false);
            return true;
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            return false; // probe deadline elapsed — not an abort
        }
        catch (SocketException)
        {
            return false;
        }
    }

    /// <summary>
    /// "3s" / "400ms" deadline parsing per the manifest contract.
    /// </summary>
    public static TimeSpan ParseDeadline(string deadline)
    {
        var d = deadline.Trim();
        if (d.EndsWith("ms", StringComparison.Ordinal)
            && double.TryParse(d[..^2], out var ms))
            return TimeSpan.FromMilliseconds(ms);
        if (d.EndsWith('s')
            && double.TryParse(d[..^1], out var seconds))
            return TimeSpan.FromSeconds(seconds);
        return TimeSpan.FromSeconds(3); // manifest default
    }

    /// <summary>
    /// Bounded retry driver: ``retry_budget`` extra attempts after the
    /// first failure (manifest semantic: retry_budget is the retry count).
    /// </summary>
    public static async Task<ProbeResult> WithRetryAsync(
        DependencyDeclaration dep,
        Func<CancellationToken, Task<ProbeResult>> probe,
        CancellationToken ct)
    {
        var attempts = Math.Max(1, dep.RetryBudget + 1);
        ProbeResult? last = null;
        for (var attempt = 0; attempt < attempts; attempt++)
        {
            last = await probe(ct).ConfigureAwait(false);
            if (last.Ready || ct.IsCancellationRequested) break;
            if (attempt + 1 < attempts)
                await Task.Delay(250, ct).ConfigureAwait(false);
        }
        return last!;
    }
}
