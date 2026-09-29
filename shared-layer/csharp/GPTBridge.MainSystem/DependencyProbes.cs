// Dependency phase probes — the per-identity handlers the Python
// PhaseMixin exposes (_phase_postgresql / _phase_vectord / _phase_ollama).
//
// Each probe maps a manifest declaration to a ProbeResult. Unknown
// identities resolve through the generic loopback-tcp contract so new
// manifest entries need no source changes (A191/A192).

using System.Diagnostics;
using System.Text.Json.Nodes;
using Npgsql;

namespace GPTBridge.MainSystem;

public sealed class DependencyProbes
{
    private readonly StartupManifest _manifest;
    private readonly Func<string?> _dsn;

    /// <param name="dsn">
    /// Governed-DSN resolver for the runtime purpose; the Python path uses
    /// shared_layer.security.dsn_policy.resolve_dsn(RUNTIME) with the
    /// GPTBRIDGE_POSTGRES_DSN env fallback — inject the resolved value.
    /// </param>
    public DependencyProbes(StartupManifest manifest, Func<string?>? dsn = null)
    {
        _manifest = manifest;
        _dsn = dsn ?? (() => Environment
            .GetEnvironmentVariable("GPTBRIDGE_POSTGRES_DSN"));
    }

    /// <summary>Dispatch by readiness_contract; never throws.</summary>
    public Task<ProbeResult> ProbeAsync(
        DependencyDeclaration dep, CancellationToken ct)
    {
        var contract = dep.ReadinessContract;
        if (contract == "tcp-or-dsn-select-1")
            return PostgresAsync(dep, ct);
        if (contract.StartsWith("loopback-tcp-", StringComparison.Ordinal)
            && int.TryParse(contract["loopback-tcp-".Length..], out var port))
            return LoopbackTcpAsync(dep, port, ct);
        return Task.FromResult(Fault(dep, "STARTUP_CONTRACT_UNKNOWN",
            $"unsupported readiness_contract:{contract}"));
    }

    private async Task<ProbeResult> PostgresAsync(
        DependencyDeclaration dep, CancellationToken ct)
    {
        var sw = Stopwatch.StartNew();
        var dsn = (_dsn() ?? "").Trim();
        var connectTimeout = TimeSpan.FromSeconds(
            _manifest.ProbeConstant("postgres_connect_timeout", 1.0));
        var attempts = Math.Max(1,
            (int)_manifest.ProbeConstant("postgres_probe_attempts", 3));
        var delay = TimeSpan.FromSeconds(
            _manifest.ProbeConstant("postgres_probe_delay", 0.5));

        if (dsn.Length > 0)
        {
            for (var attempt = 0; attempt < attempts; attempt++)
            {
                try
                {
                    var builder = new NpgsqlConnectionStringBuilder(dsn)
                    {
                        Timeout = (int)Math.Max(1, connectTimeout.TotalSeconds),
                    };
                    await using var conn = new NpgsqlConnection(
                        builder.ConnectionString);
                    await conn.OpenAsync(ct).ConfigureAwait(false);
                    await using var cmd = new NpgsqlCommand("SELECT 1", conn);
                    await cmd.ExecuteScalarAsync(ct).ConfigureAwait(false);
                    return Ok(dep, "postgresql-start", "PostgreSQL",
                        sw, "ready:dsn-select-1",
                        extra: ("dsn_source", "governed-or-env"));
                }
                catch (Exception) when (!ct.IsCancellationRequested)
                {
                    if (attempt + 1 < attempts)
                        await Task.Delay(delay, ct).ConfigureAwait(false);
                }
            }
        }

        // No DSN anywhere (or the DSN path failed) — degrade to TCP probe.
        var tcpOk = await StartupProbes.ProbeTcpAsync("127.0.0.1",
            _manifest.Port("postgresql", 5432), connectTimeout, ct)
            .ConfigureAwait(false);
        if (tcpOk)
            return Ok(dep, "postgresql-start", "PostgreSQL", sw,
                "ready:tcp-probe",
                extra: ("dsn_source", dsn.Length > 0 ? "failed" : "none"));
        return NotReady(dep, "postgresql-start", "PostgreSQL", sw,
            "POSTGRES_UNREACHABLE", "unreachable");
    }

    private async Task<ProbeResult> LoopbackTcpAsync(
        DependencyDeclaration dep, int port, CancellationToken ct)
    {
        var sw = Stopwatch.StartNew();
        var timeout = dep.Identity == "ollama"
            ? TimeSpan.FromSeconds(
                _manifest.ProbeConstant("ollama_probe_timeout", 0.5))
            : TimeSpan.FromSeconds(
                _manifest.ProbeConstant("vectord_probe_timeout", 0.5));
        var ok = await StartupProbes.ProbeTcpAsync("127.0.0.1", port,
            timeout, ct).ConfigureAwait(false);

        var onDemand = dep.Activation == "on-demand";
        if (!ok && onDemand)
        {
            // §10.7: absent-but-installed is deferred, not degraded.
            var installed = OllamaInstalled(dep.Identity);
            return new ProbeResult
            {
                Phase = $"{dep.Identity}-start",
                Label = $"啟動 {dep.Identity}",
                Critical = false,
                OnDemand = true,
                Installed = installed,
                Ready = false,
                State = installed ? "deferred" : "degraded",
                FaultCode = $"{dep.Identity.ToUpperInvariant()}_UNREACHABLE",
                Message = installed
                    ? "on-demand deferred (installed)" : "not installed",
                DurationMs = (int)sw.ElapsedMilliseconds,
            };
        }
        return ok
            ? Ok(dep, $"{dep.Identity}-start", dep.Identity, sw, "ready")
            : NotReady(dep, $"{dep.Identity}-start", dep.Identity, sw,
                $"{dep.Identity.ToUpperInvariant()}_UNREACHABLE",
                "unreachable");
    }

    private static bool OllamaInstalled(string identity)
    {
        if (identity != "ollama") return true;
        var local = Environment.GetFolderPath(
            Environment.SpecialFolder.LocalApplicationData);
        if (File.Exists(Path.Combine(
                local, "Programs", "Ollama", "ollama.exe")))
            return true;
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH")
                     ?? "").Split(Path.PathSeparator))
        {
            try
            {
                if (File.Exists(Path.Combine(dir, "ollama.exe")))
                    return true;
            }
            catch (ArgumentException) { /* malformed PATH entry */ }
        }
        return false;
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
