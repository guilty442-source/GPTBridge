// Startup gate executor — C# port of phases_execution._run_startup_phases.
//
// Executes the bootstrap gates and the certified dependency DAG inside a
// bounded worker pool (AA2: never one worker per declaration; the cap is
// STARTUP_PHASE_MAX_WORKERS=8), in dependency-aware submission order, then
// evaluates the gate: every critical bootstrap ready, DAG acyclic and
// classification-clean, every core-critical dependency ready, and the
// whole gate inside STARTUP_GATE_DEADLINE_SECONDS.
//
// The result contract mirrors the Python report: deterministic order
// (manifest order, not completion order), per-phase duration_ms, fault
// codes, and the gate_ok verdict.

using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text.Json.Nodes;

namespace GPTBridge.MainSystem;

public sealed class StartupGateResult
{
    public required IReadOnlyList<JsonObject> Results { get; init; }
    public required JsonObject Classification { get; init; }
    public required JsonObject CoreReady { get; init; }
    public bool GateOk { get; init; }
    public int TotalMs { get; init; }
    public bool DeadlineExceeded { get; init; }
    public required string GenerationId { get; init; }

    public JsonObject AsJson() => new()
    {
        ["generation_id"] = GenerationId,
        ["gate_ok"] = GateOk,
        ["total_ms"] = TotalMs,
        ["deadline_exceeded"] = DeadlineExceeded,
        ["results"] = new JsonArray(Results.ToArray<JsonNode>()),
        ["classification"] = Classification,
        ["core_ready"] = CoreReady,
    };
}

public sealed class StartupGate
{
    // AA2: bound the startup worker pool regardless of manifest size.
    private const int StartupPhaseMaxWorkers = 8;

    private readonly StartupManifest _manifest;
    private readonly DependencyProbes _probes;
    private readonly Func<string, CancellationToken, Task<ProbeResult>>?
        _bootstrapHandlers;

    public StartupGate(
        StartupManifest manifest,
        DependencyProbes? probes = null,
        Func<string, CancellationToken, Task<ProbeResult>>?
            bootstrapHandlers = null)
    {
        _manifest = manifest;
        _probes = probes ?? new DependencyProbes(manifest);
        _bootstrapHandlers = bootstrapHandlers;
    }

    public async Task<StartupGateResult> RunAsync(
        string generationId, CancellationToken ct)
    {
        var totalStart = Stopwatch.GetTimestamp();
        var declarations = _manifest.Dependencies;
        var dag = new DependencyDag(declarations);
        var classification = GovernedStartup
            .VerifyDependencyClassification(dag, _manifest);

        var bootstrapResults = new ConcurrentDictionary<string, ProbeResult>();
        var dependencyResults =
            new ConcurrentDictionary<string, ProbeResult>();

        var workers = Math.Max(1, Math.Min(StartupPhaseMaxWorkers,
            _manifest.BootstrapPhases.Count + declarations.Count));
        var orderedDeps = dag.StartOrder();
        var phaseByIdentity = declarations.ToDictionary(
            d => d.Identity, d => $"{d.Identity}-start");

        using var semaphore = new SemaphoreSlim(workers, workers);
        var tasks = new List<Task>();

        foreach (var phase in _manifest.BootstrapPhases)
        {
            var captured = phase;
            tasks.Add(Task.Run(async () =>
            {
                await semaphore.WaitAsync(ct).ConfigureAwait(false);
                try
                {
                    bootstrapResults[captured] =
                        await RunBootstrapPhaseAsync(captured, ct)
                            .ConfigureAwait(false);
                }
                finally { semaphore.Release(); }
            }, ct));
        }
        foreach (var dep in orderedDeps)
        {
            var captured = dep;
            tasks.Add(Task.Run(async () =>
            {
                await semaphore.WaitAsync(ct).ConfigureAwait(false);
                try
                {
                    var result = await StartupProbes.WithRetryAsync(
                        captured,
                        c => _probes.ProbeAsync(captured, c), ct)
                        .ConfigureAwait(false);
                    dependencyResults[captured.Identity] = result;
                }
                catch (Exception error) when (!(error is OperationCanceledException && ct.IsCancellationRequested))
                {
                    dependencyResults[captured.Identity] = new ProbeResult
                    {
                        Phase = phaseByIdentity[captured.Identity],
                        Label = captured.Identity,
                        Critical = captured.IsCoreCritical,
                        Ready = false,
                        State = "fault",
                        FaultCode = "STARTUP_DEPENDENCY_EXCEPTION",
                        Message = $"{error.GetType().Name}: {error.Message}",
                        DurationMs = 0,
                    };
                }
                finally { semaphore.Release(); }
            }, ct));
        }
        try { await Task.WhenAll(tasks).ConfigureAwait(false); }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        { /* cancellation lands on gate_ok below */ }

        // Deterministic report order: manifest order, not completion order.
        var results = new List<JsonObject>();
        foreach (var phase in _manifest.BootstrapPhases)
            if (bootstrapResults.TryGetValue(phase, out var r))
            {
                var o = r.AsJson();
                results.Add(o);
            }
        foreach (var dep in declarations)
            if (dependencyResults.TryGetValue(dep.Identity, out var r))
            {
                var o = r.AsJson();
                o["criticality"] = dep.Criticality;
                o["required_by"] = dep.RequiredBy;
                results.Add(o);
            }

        var gateOk = !ct.IsCancellationRequested;
        foreach (var phase in _manifest.BootstrapPhases)
        {
            if (!bootstrapResults.TryGetValue(phase, out var r)
                || (r.Critical && !r.Ready))
                gateOk = false;
        }
        if (!dag.IsAcyclic || classification["ok"]?.GetValue<bool>() != true)
            gateOk = false;
        foreach (var dep in declarations)
            if (dep.IsCoreCritical
                && !(dependencyResults.TryGetValue(dep.Identity, out var r)
                     && r.Ready))
                gateOk = false;

        var totalMs = (int)Stopwatch.GetElapsedTime(totalStart)
            .TotalMilliseconds;
        var gateDeadlineMs =
            _manifest.ProbeConstant("startup_gate_deadline_seconds", 90.0)
            * 1000.0;
        var deadlineExceeded = totalMs > gateDeadlineMs;
        if (deadlineExceeded) gateOk = false;

        var observed = new Dictionary<string, bool>
        {
            ["all-core-critical-dependencies-ready"] = dag.CoreCritical.All(
                d => dependencyResults.TryGetValue(d.Identity, out var r)
                     && r.Ready),
        };
        var coreReady = GovernedStartup.VerifyCoreReady(
            _manifest.CoreReadyConditions, observed);

        return new StartupGateResult
        {
            Results = results,
            Classification = classification,
            CoreReady = coreReady,
            GateOk = gateOk,
            TotalMs = totalMs,
            DeadlineExceeded = deadlineExceeded,
            GenerationId = generationId,
        };
    }

    private async Task<ProbeResult> RunBootstrapPhaseAsync(
        string phase, CancellationToken ct)
    {
        if (_bootstrapHandlers is not null)
            return await _bootstrapHandlers(phase, ct).ConfigureAwait(false);
        // Default bootstrap gates are local preflight checks; the full
        // governed handlers stay Python-side during migration (parity
        // window). An unmapped gate reports deferred, never silently ok.
        return new ProbeResult
        {
            Phase = phase,
            Label = phase,
            Critical = false,
            Ready = false,
            State = "deferred",
            FaultCode = "BOOTSTRAP_PHASE_DELEGATED",
            Message = "delegated:python-authoritative-during-migration",
            DurationMs = 0,
        };
    }
}
