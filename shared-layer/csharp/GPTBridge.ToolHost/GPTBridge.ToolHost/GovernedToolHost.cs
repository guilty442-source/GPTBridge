// Governed tool host: worker loop + health surface, mirroring
// GovernedRuntimeWorkerMixin/_worker semantics against IToolTransport.
//
// Boundary (E4): this host never issues tokens and never touches the
// transport store — every governed op is delegated through the proxy
// ops surface, which keeps token issuance, route authorization and the
// store inside the Python governance plane.
using System.Collections.Concurrent;
using System.Text.Json.Nodes;

namespace GPTBridge.ToolHost;

/// <summary>Tool executor contract: (event, result) per governed command.</summary>
public interface IGovernedCommandExecutor
{
    /// <summary>
    /// Execute one governed command. Return the response event name and
    /// result object; throw PermissionDeniedException for denied commands.
    /// </summary>
    Task<(string Event, JsonObject Result)> ExecuteAsync(
        string command, JsonObject payload, string requestId,
        CancellationToken cancellationToken);

    /// <summary>Extra keys merged into the host health snapshot.</summary>
    JsonObject Health() => new();
}

public sealed record ChannelHealth
{
    public string ChannelId { get; init; } = "";
    public bool LastOk { get; init; }
    public string? LastRequestAt { get; init; }
    public int ConsecutiveFailures { get; init; }
    public bool Degraded => ConsecutiveFailures >= 3;

    public JsonObject AsJson() => new()
    {
        ["channel_id"] = ChannelId,
        ["last_ok"] = LastOk,
        ["last_request_at"] = LastRequestAt,
        ["consecutive_failures"] = ConsecutiveFailures,
        ["degraded"] = Degraded,
    };
}

public sealed class GovernedToolHost
{
    private const string LocalCleanupCommand = "toolbox_run_local_cleanup";
    private const string GovernanceMainActor = "governance/main-system";

    private readonly GovernedEnvironment _env;
    private readonly IGovernedCommandExecutor _executor;
    private readonly Func<GovernedEnvironment, IToolTransport> _transportFactory;
    private readonly string _version;
    private readonly string[] _processingChannels;
    private readonly IReadOnlyDictionary<string, SubmitBinding>
        _submitChannels;

    private readonly ConcurrentDictionary<string, CancellationTokenSource>
        _inFlight = new();
    private readonly ConcurrentDictionary<string, ChannelHealth>
        _channelHealth = new();
    private readonly ConcurrentDictionary<string, object?> _waiters = new();
    private readonly CancellationTokenSource _shutdown = new();
    private readonly DateTimeOffset _startTime = DateTimeOffset.UtcNow;
    // Idle-reap signal: last activity tick (UTC) + live WS client count,
    // published on /metrics so the backend reaper can distinguish an
    // unused runtime from one quietly serving an open tool window.
    private long _lastActivityTicks = DateTimeOffset.UtcNow.Ticks;
    private long _activeWs;

    private IToolTransport? _transport;
    private volatile JsonObject? _lastNotification;

    public GovernedToolHost(
        GovernedEnvironment env,
        IGovernedCommandExecutor executor,
        string version,
        Func<GovernedEnvironment, IToolTransport>? transportFactory = null,
        string[]? processingChannels = null,
        IReadOnlyDictionary<string, SubmitBinding>? submitChannels = null)
    {
        _env = env;
        _executor = executor;
        _version = version;
        _transportFactory = transportFactory
            ?? (e => TransportProxyClient.Start(e));
        _processingChannels = processingChannels ?? ["system"];
        _submitChannels = submitChannels
            ?? new Dictionary<string, SubmitBinding>();
    }

    public CancellationToken ShutdownToken => _shutdown.Token;
    public int Port => _env.Port;
    /// <summary>Bound transport, if the worker loop has started one.</summary>
    public IToolTransport? Transport => _transport;
    public event Action? Stopped;

    public void RequestShutdown() => _shutdown.Cancel();

    /// <summary>Any command/connection activity resets the idle clock.</summary>
    public void RecordActivity() =>
        Interlocked.Exchange(
            ref _lastActivityTicks, DateTimeOffset.UtcNow.Ticks);

    /// <summary>WS client attach/detach — an open tool window holds a
    /// connection, so a positive count always reads as "in use".</summary>
    public void WsConnected()
    {
        Interlocked.Increment(ref _activeWs);
        RecordActivity();
    }

    public void WsDisconnected() => Interlocked.Decrement(ref _activeWs);

    /// <summary>Cancel an in-flight request (WS toolbox_cancel_tool_run).</summary>
    public bool CancelRequest(string requestId)
    {
        if (_inFlight.TryGetValue(requestId, out var cts))
        {
            cts.Cancel();
            return true;
        }
        return false;
    }

    /// <summary>
    /// WS command lane (design §10): true when the executor declares the
    /// command on its surface. Only owned commands may be dispatched —
    /// everything else stays PERMISSION_DENIED at the server boundary.
    /// </summary>
    public bool OwnsWsCommand(string command) =>
        _executor is IWsCommandSurface surface
        && surface.OwnsCommand(command);

    /// <summary>
    /// Dispatch an authenticated WS command to the executor surface.
    /// Registered in _inFlight so toolbox_cancel_tool_run cancels it the
    /// same way store-claimed requests are cancelled; the returned task
    /// completes with (event, result) for the server to emit.
    /// </summary>
    public async Task<(string Event, JsonObject Result)> DispatchWsAsync(
        string command,
        JsonObject payload,
        string requestId,
        Func<JsonObject, Task>? emitProgress,
        CancellationToken ct)
    {
        if (_executor is not IWsCommandSurface surface
            || !surface.OwnsCommand(command))
            throw new PermissionDeniedException();
        RecordActivity();
        using var requestCts = CancellationTokenSource
            .CreateLinkedTokenSource(ct, _shutdown.Token);
        _inFlight[requestId] = requestCts;
        try
        {
            return await surface
                .ExecuteWsAsync(
                    command, payload, requestId, emitProgress,
                    requestCts.Token)
                .ConfigureAwait(false);
        }
        finally
        {
            _inFlight.TryRemove(requestId, out _);
        }
    }

    public JsonObject HealthSnapshot()
    {
        var channelHealth = new JsonObject();
        foreach (var (id, health) in _channelHealth)
            channelHealth[id] = health.AsJson();
        var channelRoutes = new JsonObject();
        foreach (var id in _processingChannels)
            channelRoutes[id] = $"{id}-channel/{_env.ToolId}";
        var snapshot = new JsonObject
        {
            ["ok"] = true,
            ["role"] = "governed-tool-runtime",
            ["sovereign_id"] = "main-system",
            ["authority"] = "sub-sovereign-tool",
            ["scope"] = "tool-local",
            ["duty"] = new JsonArray("execute-governed-commands"),
            ["subordinate_to"] = new JsonArray("main-system"),
            ["version"] = _version,
            ["tool_id"] = _env.ToolId,
            ["runtime_scope"] = "independent-tool",
            ["governance_ready"] = true,
            ["workspace_instance_id"] = _env.WorkspaceInstanceId(),
            ["channels"] = new JsonArray(
                _processingChannels.Select(c => (JsonNode)c).ToArray()),
            ["channel_routes"] = channelRoutes,
            ["channel_health"] = channelHealth,
            ["runtime_host"] = "csharp-toolhost",
        };
        foreach (var (key, value) in _executor.Health())
            snapshot[key] = value?.DeepClone();
        return snapshot;
    }

    public JsonObject MetricsSnapshot()
    {
        var lastActivityTicks = Interlocked.Read(ref _lastActivityTicks);
        return new()
        {
            ["channel_health"] = new JsonObject(
                _channelHealth.Select(p =>
                    new KeyValuePair<string, JsonNode?>(
                        p.Key, p.Value.AsJson())).ToArray()),
            ["worker_queue_size"] = _waiters.Count,
            ["processing_channels"] = new JsonArray(
                _processingChannels.Select(c => (JsonNode)c).ToArray()),
            ["notification_queue_size"] = 0,
            ["last_notification"] = _lastNotification?.DeepClone(),
            ["uptime_seconds"] =
                (DateTimeOffset.UtcNow - _startTime).TotalSeconds,
            ["in_flight_requests"] = _inFlight.Count,
            ["active_ws_connections"] = Interlocked.Read(ref _activeWs),
            ["last_activity_at"] = new DateTimeOffset(
                lastActivityTicks, TimeSpan.Zero).ToString("O"),
        };
    }

    public string WorkspaceInstanceId => _env.WorkspaceInstanceId();
    public string SessionToken => _env.SessionToken;
    public string ShutdownTokenValue => _env.ShutdownToken;
    public string ToolId => _env.ToolId;

    private void RecordChannelHealth(string channelId, bool ok)
    {
        _channelHealth.AddOrUpdate(
            channelId,
            _ => new ChannelHealth
            {
                ChannelId = channelId,
                LastOk = ok,
                LastRequestAt = DateTimeOffset.UtcNow.ToString("O"),
                ConsecutiveFailures = ok ? 0 : 1,
            },
            (_, current) => new ChannelHealth
            {
                ChannelId = channelId,
                LastOk = ok,
                LastRequestAt = DateTimeOffset.UtcNow.ToString("O"),
                ConsecutiveFailures =
                    ok ? 0 : current.ConsecutiveFailures + 1,
            });
    }

    private static string IsoNow() =>
        DateTimeOffset.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ");

    /// <summary>
    /// Worker loop: claim → dispatch executor → respond, mirroring
    /// GovernedRuntimeWorkerMixin._worker. Idle periods poll
    /// notification_stamp for wake acceleration with 0.25→0.5s backoff.
    /// </summary>
    public async Task RunWorkerAsync(CancellationToken external = default)
    {
        var transport = _transport ??= _transportFactory(_env);
        transport.Disconnected += () => _shutdown.Cancel();
        var modes = _processingChannels
            .ToDictionary(c => c, _ => "process");
        foreach (var channel in _submitChannels.Keys)
            modes[channel] = "submit";
        await transport.HelloAsync(
            _env.ToolId,
            _env.WorkspaceInstanceId(),
            modes,
            _submitChannels.Count > 0 ? _submitChannels : null,
            external).ConfigureAwait(false);

        using var linked = CancellationTokenSource
            .CreateLinkedTokenSource(external, _shutdown.Token);
        var ct = linked.Token;
        var idlePoll = TimeSpan.FromMilliseconds(250);
        var maxIdle = TimeSpan.FromMilliseconds(500);
        var stamps = new Dictionary<string, string?>();

        while (!ct.IsCancellationRequested)
        {
            JsonObject? request = null;
            string? activeChannel = null;
            foreach (var channelId in _processingChannels)
            {
                activeChannel = channelId;
                try
                {
                    request = await transport
                        .ClaimAsync(channelId, ct).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    break;
                }
                catch (ProxyErrorException)
                {
                    RecordChannelHealth(channelId, ok: false);
                    try { await Task.Delay(500, ct).ConfigureAwait(false); }
                    catch (OperationCanceledException) { }
                    break;
                }
                if (request is not null)
                    break;
            }
            if (ct.IsCancellationRequested)
                break;
            if (request is null)
            {
                // Stamp-driven wake: cheap write-stamp probe per channel;
                // a changed stamp means a queued request is waiting.
                var changed = false;
                foreach (var channelId in _processingChannels)
                {
                    try
                    {
                        var stamp = await transport
                            .NotificationStampAsync(channelId, ct)
                            .ConfigureAwait(false);
                        var key = stamp?.ToJsonString();
                        if (key is not null
                            && (!stamps.TryGetValue(channelId, out var prev)
                                || prev != key))
                        {
                            stamps[channelId] = key;
                            changed = true;
                            _lastNotification = new JsonObject
                            {
                                ["payload"] = "transport-store-changed",
                                ["received_at"] = IsoNow(),
                            };
                        }
                    }
                    catch (ProxyErrorException) { /* stamp is best-effort */ }
                    catch (OperationCanceledException) { break; }
                }
                if (changed)
                {
                    idlePoll = TimeSpan.FromMilliseconds(250);
                    continue;
                }
                try { await Task.Delay(idlePoll, ct).ConfigureAwait(false); }
                catch (OperationCanceledException) { break; }
                idlePoll = idlePoll + idlePoll / 2;
                if (idlePoll > maxIdle)
                    idlePoll = maxIdle;
                continue;
            }
            idlePoll = TimeSpan.FromMilliseconds(250);
            await HandleRequestAsync(
                transport, activeChannel ?? "system", request, ct)
                .ConfigureAwait(false);
        }
    }

    private async Task HandleRequestAsync(
        IToolTransport transport,
        string channelId,
        JsonObject request,
        CancellationToken ct)
    {
        var requestId = request["request_id"]?.GetValue<string>() ?? "";
        var payload = request["payload"] as JsonObject;
        var command = "";
        JsonObject result;
        var cancelled = false;
        RecordActivity();
        try
        {
            if (payload is null)
                throw new PermissionDeniedException();
            command = payload["_governed_command"]?.GetValue<string>()
                ?.Trim() ?? "";
            payload.Remove("_governed_command");
            if (command.Length == 0)
                throw new PermissionDeniedException();
            payload["_governed_requester_actor"] =
                request["requester_actor"]?.GetValue<string>() ?? "";

            if (command == LocalCleanupCommand)
            {
                // Governance-owned hygiene sweep: the C# host delegates
                // rather than reimplementing tool-local cleanup semantics.
                if (payload["_governed_requester_actor"]
                        ?.GetValue<string>() != GovernanceMainActor)
                    throw new PermissionDeniedException();
                result = new JsonObject
                {
                    ["ok"] = true,
                    ["tool_id"] = _env.ToolId,
                    ["operation"] = "local-self-cleanup",
                    ["delegated"] = true,
                    ["host"] = "csharp-toolhost",
                };
            }
            else
            {
                using var requestCts = CancellationTokenSource
                    .CreateLinkedTokenSource(ct);
                _inFlight[requestId] = requestCts;
                try
                {
                    var execution = _executor.ExecuteAsync(
                        command, payload, requestId, requestCts.Token);
                    // Cancel poll: the requester marks the store row
                    // cancelled (cancel_tool_execution); the worker
                    // detects it here and abandons the request without
                    // responding — same as Python cancelled_during_execution.
                    while (!execution.IsCompleted)
                    {
                        var polled = await Task
                            .WhenAny(execution, Task.Delay(100, ct))
                            .ConfigureAwait(false);
                        if (polled == execution)
                            break;
                        bool requestCancelled;
                        try
                        {
                            requestCancelled = await transport
                                .RequestCancelledAsync(
                                    channelId, requestId, ct)
                                .ConfigureAwait(false);
                        }
                        catch (ProxyErrorException)
                        {
                            requestCancelled = false;
                        }
                        if (requestCancelled)
                        {
                            requestCts.Cancel();
                            try { await execution.ConfigureAwait(false); }
                            catch (OperationCanceledException) { }
                            cancelled = true;
                            break;
                        }
                    }
                    if (cancelled)
                        return;
                    var (_, executorResult) = await execution
                        .ConfigureAwait(false);
                    result = executorResult;
                }
                finally
                {
                    _inFlight.TryRemove(requestId, out _);
                }
            }
            result["request_id"] = requestId;
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            return;
        }
        catch (Exception)
        {
            result = new JsonObject
            {
                ["ok"] = false,
                ["tool_id"] = _env.ToolId,
                ["request_id"] = requestId,
                ["error_code"] = "PERMISSION_DENIED",
                ["message"] = "PERMISSION_DENIED",
            };
        }

        try
        {
            await transport.RespondAsync(channelId, requestId, result, ct)
                .ConfigureAwait(false);
            RecordChannelHealth(channelId, ok: true);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // Shutdown in progress — the store lease reclaims the request.
        }
        catch (ProxyErrorException)
        {
            RecordChannelHealth(channelId, ok: false);
        }
    }

    public async ValueTask DisposeAsync()
    {
        _shutdown.Cancel();
        // Cancel every in-flight WS request before tearing down the
        // executor — executors own live children (model workers) whose
        // shutdown must not race a still-running command.
        foreach (var cts in _inFlight.Values)
        {
            try { cts.Cancel(); } catch { /* disposed */ }
        }
        if (_transport is not null)
            await _transport.DisposeAsync().ConfigureAwait(false);
        if (_executor is IAsyncDisposable asyncDisposable)
            await asyncDisposable.DisposeAsync().ConfigureAwait(false);
        else if (_executor is IDisposable disposable)
            disposable.Dispose();
        _shutdown.Dispose();
        Stopped?.Invoke();
    }
}
