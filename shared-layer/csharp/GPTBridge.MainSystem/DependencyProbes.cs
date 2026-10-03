// Dependency phase probes — the per-identity handlers the Python
// PhaseMixin exposes (_phase_vectord; the retired
// _phase_ollama is gone with the B154 retirement — no probe may target
// an external model service).
//
// Each probe maps a manifest declaration to a ProbeResult. Unknown
// identities resolve through the generic loopback-tcp contract so new
// manifest entries need no source changes (A191/A192).

using System.Diagnostics;
using System.Text.Json.Nodes;


namespace GPTBridge.MainSystem;

public sealed class DependencyProbes
{
    private readonly StartupManifest _manifest;
    private readonly Func<DependencyDeclaration, CancellationToken, Task<bool>>? _nativeIntegrity;

    // The DSN parameter remains for source compatibility; retired PostgreSQL
    // contracts never resolve it or open a connection.
    public DependencyProbes(StartupManifest manifest, Func<string?>? dsn = null,
        Func<DependencyDeclaration, CancellationToken, Task<bool>>? nativeIntegrity = null)
    {
        _manifest = manifest;
        _nativeIntegrity = nativeIntegrity;
    }

    /// <summary>Dispatch by readiness_contract; cancellation propagates.</summary>
    public Task<ProbeResult> ProbeAsync(
        DependencyDeclaration dep, CancellationToken ct)
    {
        var contract = dep.ReadinessContract;
        if (contract == "tcp-or-dsn-select-1"
            || dep.Identity.Equals("postgresql", StringComparison.OrdinalIgnoreCase)
            || dep.Identity.Equals("postgres", StringComparison.OrdinalIgnoreCase))
            return Task.FromResult(Fault(dep, "POSTGRESQL_RETIRED",
                "retired PostgreSQL readiness contract; native migration required"));
        if (contract == "native-store-integrity")
            return NativeIntegrityAsync(dep, ct);
        if (contract.StartsWith("loopback-tcp-", StringComparison.Ordinal)
            && int.TryParse(contract["loopback-tcp-".Length..], out var port))
            return LoopbackTcpAsync(dep, port, ct);
        return Task.FromResult(Fault(dep, "STARTUP_CONTRACT_UNKNOWN",
            $"unsupported readiness_contract:{contract}"));
    }

    private async Task<ProbeResult> NativeIntegrityAsync(
        DependencyDeclaration dep, CancellationToken ct)
    {
        if (_nativeIntegrity is null)
            return Fault(dep, "NATIVE_STORE_VERIFIER_UNAVAILABLE",
                "native authority verifier is required");
        var sw = Stopwatch.StartNew();
        try
        {
            ct.ThrowIfCancellationRequested();
            var verified = await _nativeIntegrity(dep, ct).ConfigureAwait(false);
            return verified
                ? Ok(dep, $"{dep.Identity}-start", dep.Identity, sw, "ready:native-integrity")
                : NotReady(dep, $"{dep.Identity}-start", dep.Identity, sw,
                    "NATIVE_STORE_INTEGRITY_FAILED", "native authority integrity failed");
        }
        catch (Exception) when (!ct.IsCancellationRequested)
        {
            return NotReady(dep, $"{dep.Identity}-start", dep.Identity, sw,
                "NATIVE_STORE_INTEGRITY_FAILED", "native authority verifier failed");
        }
    }

    private async Task<ProbeResult> LoopbackTcpAsync(
        DependencyDeclaration dep, int port, CancellationToken ct)
    {
        var sw = Stopwatch.StartNew();
        var timeout = TimeSpan.FromSeconds(
            _manifest.ProbeConstant("vectord_probe_timeout", 0.5));
        var ok = await StartupProbes.ProbeTcpAsync("127.0.0.1", port,
            timeout, ct).ConfigureAwait(false);

        var onDemand = dep.Activation == "on-demand";
        if (!ok && onDemand)
        {
            // §10.7: absent-but-installed is deferred, not degraded.
            return new ProbeResult
            {
                Phase = $"{dep.Identity}-start",
                Label = $"啟動 {dep.Identity}",
                Critical = false,
                OnDemand = true,
                Installed = true,
                Ready = false,
                State = "deferred",
                FaultCode = $"{dep.Identity.ToUpperInvariant()}_UNREACHABLE",
                Message = "on-demand deferred",
                DurationMs = (int)sw.ElapsedMilliseconds,
            };
        }
        return ok
            ? Ok(dep, $"{dep.Identity}-start", dep.Identity, sw, "ready")
            : NotReady(dep, $"{dep.Identity}-start", dep.Identity, sw,
                $"{dep.Identity.ToUpperInvariant()}_UNREACHABLE",
                "unreachable");
    }

    private static ProbeResult Ok(
        DependencyDeclaration dep, string phase, string label,
        Stopwatch sw, string message,
        (string Key, string Value)? extra = null) => new()
    {
        Phase = phase,
        Label = label,
        Critical = dep.IsCoreCritical,
        OnDemand = dep.Activation == "on-demand",
        Installed = true,
        Ready = true,
        State = "ok",
        FaultCode = "",
        Message = message,
        DurationMs = (int)sw.ElapsedMilliseconds,
    };

    private static ProbeResult NotReady(
        DependencyDeclaration dep, string phase, string label,
        Stopwatch sw, string faultCode, string message) => new()
    {
        Phase = phase,
        Label = label,
        Critical = dep.IsCoreCritical,
        OnDemand = dep.Activation == "on-demand",
        Installed = true,
        Ready = false,
        State = "fault",
        FaultCode = faultCode,
        Message = message,
        DurationMs = (int)sw.ElapsedMilliseconds,
    };

    private static ProbeResult Fault(
        DependencyDeclaration dep, string faultCode, string message) => new()
    {
        Phase = $"{dep.Identity}-start",
        Label = dep.Identity,
        Critical = dep.IsCoreCritical,
        Ready = false,
        State = "fault",
        FaultCode = faultCode,
        Message = message,
        DurationMs = 0,
    };
}
