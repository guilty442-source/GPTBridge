using System.Diagnostics;
using System.Text.Json;
using StarBusinessLogic.Infrastructure;

namespace StarBusinessLogic.Application;

// P13/G30-P7-3: C# business orchestration owns the lifecycle *decision loop*
// for the business path.  Execution stays governed: start/stop go through the
// authenticated IPC command surface (toolbox_* -> permission gate -> toolbox
// service), and engine-resource release goes through the model-service
// contract (POST /v1/release -> AutoReleaseManager).  This class never spawns
// or kills processes directly (A177).
public sealed record LifecycleStepResult(
    string Name,
    bool Ok,
    long LatencyMs,
    string Detail,
    string? ErrorCode = null
);

public sealed record LifecycleRehearsalReport(
    string Schema,
    string RecordedAt,
    string ProjectRoot,
    string ToolRoot,
    IReadOnlyList<LifecycleStepResult> Steps,
    bool OverallOk
);

public sealed class LifecycleOrchestrator
{
    /// <summary>Lifecycle decision-loop deadline for activation (descriptor + status ready).</summary>
    public static readonly TimeSpan DefaultActivationDeadline = TimeSpan.FromSeconds(120);

    private static readonly TimeSpan ProbeTimeout = TimeSpan.FromSeconds(3);

    private readonly string _projectRoot;
    private readonly string _toolRoot;
    private readonly GovernedIpcClient _ipc;
    private readonly Func<TimeSpan, CancellationToken, Task> _delay;
    private readonly Func<CancellationToken, Task<bool>> _serviceUp;

    public LifecycleOrchestrator(
        string projectRoot,
        string toolRoot,
        GovernedIpcClient? ipc = null,
        Func<TimeSpan, CancellationToken, Task>? delay = null,
        Func<CancellationToken, Task<bool>>? serviceUp = null)
    {
        _projectRoot = projectRoot ?? throw new ArgumentNullException(nameof(projectRoot));
        _toolRoot = toolRoot ?? throw new ArgumentNullException(nameof(toolRoot));
        _ipc = ipc ?? new GovernedIpcClient(GovernedIpcDiscovery.Endpoint(projectRoot));
        _delay = delay ?? ((t, ct) => Task.Delay(t, ct));
        _serviceUp = serviceUp ?? IsServiceUpAsync;
    }

    /// <summary>Read-only proof the governed channel answers lifecycle queries.</summary>
    public async Task<JsonElement> ListToolsAsync(CancellationToken cancellationToken = default)
        => await _ipc.ExecuteAsync("toolbox_list_tools", new Dictionary<string, object?>(), cancellationToken).ConfigureAwait(false);

    /// <summary>Ensure the model service is running; activates it through the
    /// governed toolbox path when it is down (or the descriptor is stale).</summary>
    public async Task<LifecycleStepResult> EnsureModelServiceAsync(
        CancellationToken cancellationToken = default,
        TimeSpan? activationDeadline = null)
    {
        var sw = Stopwatch.StartNew();
        // Descriptor alone is not proof of life — channel_runtime removes it on
        // clean stop, but a crash leaves a stale descriptor pointing at a dead
        // port.  "Up" = descriptor valid AND /v1/status answers.
        if (await _serviceUp(cancellationToken).ConfigureAwait(false))
            return new LifecycleStepResult("ensure-model-service", true, sw.ElapsedMilliseconds, "already-running");

        var requestId = $"csharp-lifecycle-{Guid.NewGuid():N}";
        JsonElement result;
        try
        {
            result = await _ipc.ExecuteAsync(
                "toolbox_start_tool",
                new Dictionary<string, object?>
                {
                    ["tool_id"] = "local-model",
                    ["background"] = true,
                    ["request_id"] = requestId,
                },
                cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is InvalidOperationException or OperationCanceledException)
        {
            return Fail(sw, "ensure-model-service", ex);
        }
        if (!result.TryGetProperty("ok", out var ok) || ok.ValueKind != JsonValueKind.True)
        {
            var code = result.TryGetProperty("error_code", out var ec) ? ec.GetString() : "START_FAILED";
            return new LifecycleStepResult("ensure-model-service", false, sw.ElapsedMilliseconds, $"start rejected: {code}", code);
        }

        var deadline = activationDeadline ?? DefaultActivationDeadline;
        while (sw.Elapsed < deadline)
        {
            if (await _serviceUp(cancellationToken).ConfigureAwait(false))
                return new LifecycleStepResult("ensure-model-service", true, sw.ElapsedMilliseconds, $"activated via governed toolbox path (request {requestId})");
            await _delay(TimeSpan.FromMilliseconds(500), cancellationToken).ConfigureAwait(false);
        }
        return new LifecycleStepResult("ensure-model-service", false, sw.ElapsedMilliseconds, "activation deadline elapsed", "ACTIVATION_TIMEOUT");
    }

    /// <summary>Engine-resource release through the model-service contract.</summary>
    public async Task<LifecycleStepResult> ReleaseModelAsync(string? key = null, CancellationToken cancellationToken = default)
    {
        var sw = Stopwatch.StartNew();
        try
        {
            using var client = ModelServiceLocator.CreateClient(_toolRoot);
            var released = await client.ReleaseAsync(key, cancellationToken).ConfigureAwait(false);
            return new LifecycleStepResult("release-model", true, sw.ElapsedMilliseconds, $"released=[{string.Join(",", released)}]");
        }
        catch (Exception ex) when (ex is InvalidOperationException or HttpRequestException or OperationCanceledException or TaskCanceledException)
        {
            return Fail(sw, "release-model", ex);
        }
    }

    /// <summary>Governed stop of the model-owner tool.</summary>
    public async Task<LifecycleStepResult> StopModelServiceAsync(CancellationToken cancellationToken = default)
    {
        var sw = Stopwatch.StartNew();
        try
        {
            var result = await _ipc.ExecuteAsync(
                "toolbox_stop_tool",
                new Dictionary<string, object?>
                {
                    ["tool_id"] = "local-model",
                    ["request_id"] = $"csharp-lifecycle-{Guid.NewGuid():N}",
                },
                cancellationToken).ConfigureAwait(false);
            var ok = result.TryGetProperty("ok", out var okEl) && okEl.ValueKind == JsonValueKind.True;
            return new LifecycleStepResult("stop-model-service", ok, sw.ElapsedMilliseconds,
                ok ? "stopped via governed toolbox path" : $"stop rejected: {DescribeError(result)}",
                ok ? null : DescribeError(result));
        }
        catch (Exception ex) when (ex is InvalidOperationException or OperationCanceledException)
        {
            return Fail(sw, "stop-model-service", ex);
        }
    }

    /// <summary>P13 migration rehearsal: prove the C# orchestrator can drive the
    /// full lifecycle decision loop over the governed surfaces.  Activation and
    /// infer legs are opt-in because they start the model for real.</summary>
    public async Task<LifecycleRehearsalReport> RehearseAsync(
        bool activateIfDown = false,
        bool runInfer = false,
        CancellationToken cancellationToken = default)
    {
        var steps = new List<LifecycleStepResult>();
        var sw = Stopwatch.StartNew();

        // 1. Governed channel proof: authenticated lifecycle query.
        try
        {
            var tools = await ListToolsAsync(cancellationToken).ConfigureAwait(false);
            var count = tools.ValueKind == JsonValueKind.Object && tools.TryGetProperty("tools", out var t) && t.ValueKind == JsonValueKind.Array
                ? t.GetArrayLength() : -1;
            steps.Add(new LifecycleStepResult("governed-channel-list", true, sw.ElapsedMilliseconds, $"toolbox_list_tools answered ({count} tools)"));
        }
        catch (Exception ex) when (ex is InvalidOperationException or OperationCanceledException)
        {
            steps.Add(Fail(sw, "governed-channel-list", ex));
            return Report(steps);
        }

        // 2. Descriptor probe + liveness — an observation leg, so it is Ok
        // whenever it reports the true state (up / absent / stale).
        var discovered = TryDiscover();
        var up = false;
        if (discovered is null)
        {
            steps.Add(new LifecycleStepResult("model-service-probe", true, sw.ElapsedMilliseconds,
                "descriptor absent (service down)"));
        }
        else
        {
            up = await _serviceUp(cancellationToken).ConfigureAwait(false);
            steps.Add(new LifecycleStepResult(
                "model-service-probe", true, sw.ElapsedMilliseconds,
                up ? $"service up pid={discovered.Pid} port-ok"
                   : $"stale descriptor pid={discovered.Pid} (process dead, port refused)"));
        }

        // 3. Activation leg (opt-in — real governed start of local-model).
        var rehearsalStartedService = false;
        if (!up)
        {
            if (!activateIfDown)
            {
                steps.Add(new LifecycleStepResult("activate-model-service", true, sw.ElapsedMilliseconds,
                    "skipped (service down; pass activateIfDown to exercise the governed start leg)"));
            }
            else
            {
                var ensure = await EnsureModelServiceAsync(cancellationToken).ConfigureAwait(false);
                steps.Add(ensure);
                if (!ensure.Ok) return Report(steps);
                up = true;
                rehearsalStartedService = ensure.Detail.StartsWith("activated", StringComparison.Ordinal);
            }
        }

        // 4. Status leg over the model-service contract.
        if (up)
        {
            try
            {
                using var client = ModelServiceLocator.CreateClient(_toolRoot);
                var status = await client.StatusAsync(cancellationToken).ConfigureAwait(false);
                steps.Add(new LifecycleStepResult("model-service-status", true, sw.ElapsedMilliseconds,
                    $"status keys=[{string.Join(",", status.Keys)}]"));
            }
            catch (Exception ex) when (ex is InvalidOperationException or HttpRequestException or OperationCanceledException or TaskCanceledException)
            {
                steps.Add(Fail(sw, "model-service-status", ex));
            }
        }

        // 5. Infer leg (opt-in — real model round trip).
        if (runInfer && up)
        {
            try
            {
                using var client = ModelServiceLocator.CreateClient(_toolRoot);
                var resp = await client.InferAsync(
                    new ModelInferenceRequest("你好", "", "Conversation", MaxNewTokens: 8),
                    cancellationToken).ConfigureAwait(false);
                steps.Add(new LifecycleStepResult("infer", true, sw.ElapsedMilliseconds,
                    $"infer ok model={resp.ModelId} tokens={resp.TokenIds.Count}"));
            }
            catch (Exception ex) when (ex is InvalidOperationException or HttpRequestException or OperationCanceledException or TaskCanceledException)
            {
                steps.Add(Fail(sw, "infer", ex));
            }
        }

        // 6. Release leg (engine-resource unload; only when the service is up).
        if (up)
        {
            steps.Add(await ReleaseModelAsync(cancellationToken: cancellationToken).ConfigureAwait(false));
        }

        // 7. Rollback leg: a service the rehearsal itself activated is stopped
        // again through the governed path — the drill leaves no residue.
        if (rehearsalStartedService)
        {
            steps.Add(await StopModelServiceAsync(cancellationToken).ConfigureAwait(false));
        }

        return Report(steps);
    }

    /// <summary>Liveness probe: descriptor valid AND /v1/status answers within
    /// a bounded window.  Stale descriptors (dead pid, refused port) read as
    /// down — the orchestrator never treats a tombstone as running.</summary>
    private async Task<bool> IsServiceUpAsync(CancellationToken cancellationToken)
    {
        if (TryDiscover() is null) return false;
        try
        {
            using var client = ModelServiceLocator.CreateClient(_toolRoot, ProbeTimeout);
            await client.StatusAsync(cancellationToken).ConfigureAwait(false);
            return true;
        }
        catch (Exception ex) when (ex is InvalidOperationException or HttpRequestException or OperationCanceledException or TaskCanceledException)
        {
            return false;
        }
    }

    private ModelServiceEndpoint? TryDiscover()
    {
        try { return ModelServiceLocator.Discover(_toolRoot); }
        catch (InvalidOperationException) { return null; }
    }

    private static string DescribeError(JsonElement result)
        => result.TryGetProperty("error_code", out var ec) ? ec.GetString() ?? "UNKNOWN" : "UNKNOWN";

    private static LifecycleStepResult Fail(Stopwatch sw, string name, Exception ex)
        => new(name, false, sw.ElapsedMilliseconds, ex.Message, ex.Message.Split(':')[0]);

    private LifecycleRehearsalReport Report(IReadOnlyList<LifecycleStepResult> steps)
        => new(
            "star-lifecycle-rehearsal/v1",
            DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ"),
            _projectRoot,
            _toolRoot,
            steps,
            steps.All(s => s.Ok));
}
