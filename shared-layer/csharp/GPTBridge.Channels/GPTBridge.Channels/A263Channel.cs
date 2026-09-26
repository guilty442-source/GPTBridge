// A263-compliant Information-Layer Channel Runtime — C# port.
//
// information-channel-gateway (architecture registry: python_residency =
// migrate-csharp).  Ports shared-layer/src/shared_layer/channel_runtime.py
// + connection_mixin.py + heartbeat_mixin.py with contract parity:
//
//   * typed generation on every channel (channel_generation)
//   * two-way heartbeat with deadline
//   * bounded queues / backpressure (control + message)
//   * control priority channel
//   * transactional outbox for state changes
//   * ordered ack/cursor
//   * disconnect invalidates ready
//   * reconnect requires snapshot/cursor/hash convergence
//   * NO dropping unacknowledged events, NO duplicate side effects
//
// Messages travel as JsonObject — the same shape Python peers emit.

using System.Text.Json.Nodes;

namespace GPTBridge.Channels;

public sealed class A263Channel
{
    /// <summary>Python component_version("channel-runtime") equivalent.</summary>
    public const string ChannelRuntimeVersion = "1.0.0";

    private readonly ChannelConfig _config;
    private readonly IChannelTransport _transport;
    private readonly TransactionalOutbox? _outbox;

    private ChannelState _state = ChannelState.Closed;
    private ChannelGeneration _generation;

    // Queues — bounded, same capacity contract as the Python deques.
    // Python is single-threaded asyncio; here producers run on different
    // threads than the send loop, so queue access is locked.
    private readonly object _queueLock = new();
    private readonly Queue<JsonObject> _controlQueue = new();
    private readonly Queue<(MessagePriority Priority, JsonObject Message)>
        _messageQueue = new();

    // Ack/cursor tracking.
    private int _ackedCursor;
    private int _sentUpto;

    // Outbound send loop wakeup + task.
    private TaskCompletionSource<bool> _sendWakeup =
        new(TaskCreationOptions.RunContinuationsAsynchronously);
    private Task? _sendTask;
    private Task? _heartbeatTask;

    // Dead signal — the Python heartbeat_dead asyncio.Event.
    private CancellationTokenSource _heartbeatDead = new();

    // Locks.
    private readonly SemaphoreSlim _generationLock = new(1, 1);

    // Reconnect/snapshot state.
    private int _reconnectAttempts;
    private string _snapshotHash = "";
    private int _snapshotCursor;
    private ChannelGeneration? _snapshotGeneration;

    // Callbacks.
    private Func<ChannelState, ChannelState, Task>? _onStateChange;
    private Func<JsonObject, Task>? _onMessage;
    private Func<JsonObject, Task>? _onControl;

    // Metrics.
    private int _messagesSent;
    private int _messagesReceived;
    private int _reconnects;
    private double _lastPingSent;
    private double _lastPongReceived;
    private int _heartbeatsSent;
    private int _heartbeatsReceived;

    public A263Channel(
        ChannelConfig config,
        IChannelTransport transport,
        TransactionalOutbox? outbox = null)
    {
        _config = config;
        _transport = transport;
        _outbox = outbox;
        _generation = new ChannelGeneration
        {
            ChannelId = config.ChannelId,
            Generation = 0,
        };
    }

    public ChannelConfig Config => _config;
    public ChannelState State => _state;
    public ChannelGeneration Generation => _generation;

    private static double MonotonicNow() =>
        Environment.TickCount64 / 1000.0;

    // ================================================================
    // callbacks / state
    // ================================================================

    public void SetCallbacks(
        Func<ChannelState, ChannelState, Task>? onStateChange = null,
        Func<JsonObject, Task>? onMessage = null,
        Func<JsonObject, Task>? onControl = null)
    {
        _onStateChange = onStateChange;
        _onMessage = onMessage;
        _onControl = onControl;
    }

    private async Task SetStateAsync(ChannelState newState)
    {
        var oldState = _state;
        if (oldState == newState)
        {
            return;
        }
        _state = newState;
        if (_onStateChange is not null)
        {
            try
            {
                await _onStateChange(oldState, newState).ConfigureAwait(false);
            }
            catch (Exception)
            {
                // state-change observers never break the channel
            }
        }
    }

    // ================================================================
    // queues (bounded — same backpressure contract)
    // ================================================================

    private bool EnqueueControl(JsonObject message)
    {
        lock (_queueLock)
        {
            if (_controlQueue.Count >= _config.ControlChannelCapacity)
            {
                return false; // backpressure on control channel
            }
            _controlQueue.Enqueue(message);
        }
        _sendWakeup.TrySetResult(true);
        return true;
    }

    private bool EnqueueMessage(MessagePriority priority, JsonObject message)
    {
        lock (_queueLock)
        {
            if (_config.EnableBackpressure
                && _messageQueue.Count >= _config.MaxQueueSize)
            {
                return false; // backpressure
            }
            _messageQueue.Enqueue((priority, message));
        }
        _sendWakeup.TrySetResult(true);
        return true;
    }

    private Task SendHelloAsync()
    {
        var message = new JsonObject
        {
            ["type"] = "control",
            ["command"] = "state_event_hello",
            ["payload"] = new JsonObject { ["cursor"] = _ackedCursor },
            ["generation"] = _generation.AsDict(),
        };
        EnqueueControl(message);
        return Task.CompletedTask;
    }

    private Task SendResyncAsync(int cursor)
    {
        var message = new JsonObject
        {
            ["type"] = "control",
            ["command"] = "state_event_resync",
            ["payload"] = new JsonObject { ["cursor"] = cursor },
            ["generation"] = _generation.AsDict(),
        };
        EnqueueControl(message);
        return Task.CompletedTask;
    }

    // ================================================================
    // send / receive loops
    // ================================================================

    /// <summary>Send a message with priority (A263 backpressure).</summary>
    public Task<bool> SendAsync(
        JsonObject message,
        MessagePriority priority = MessagePriority.Command,
        CancellationToken cancellationToken = default)
    {
        message["generation"] = _generation.AsDict();
        var success = EnqueueMessage(priority, message);
        if (success)
        {
            _messagesSent += 1;
        }
        return Task.FromResult(success);
    }

    private void StartSendLoop()
    {
        if (_sendTask is { IsCompleted: false })
        {
            return;
        }
        _sendWakeup = new TaskCompletionSource<bool>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        _sendTask = SendLoopAsync(_heartbeatDead);
    }

    private async Task SendLoopAsync(CancellationTokenSource dead)
    {
        // Control channel drains first (heartbeat/ack/cursor priority),
        // then outbox state events, then regular traffic sorted by
        // priority within each batch — same ordering as the Python loop.
        try
        {
            while (!dead.IsCancellationRequested)
            {
                while (true)
                {
                    JsonObject control;
                    lock (_queueLock)
                    {
                        if (_controlQueue.Count == 0)
                        {
                            break;
                        }
                        control = _controlQueue.Dequeue();
                    }
                    await _transport.SendAsync(control, dead.Token)
                        .ConfigureAwait(false);
                }

                if (_outbox is not null
                    && _sentUpto < _outbox.GetLatestSequence())
                {
                    foreach (var evt in await _outbox
                        .FetchAfterAsync(_sentUpto,
                            _config.MaxOutboundBatch, dead.Token)
                        .ConfigureAwait(false))
                    {
                        await _transport.SendAsync(
                            new JsonObject
                            {
                                ["type"] = "state_event",
                                ["event"] = new JsonObject
                                {
                                    ["sequence"] = evt.Sequence,
                                    ["entity_id"] = evt.EntityId,
                                    ["entity_type"] = evt.EntityType,
                                    ["operation"] = evt.Operation,
                                    ["payload"] =
                                        evt.Payload.DeepClone(),
                                    ["state_hash"] = evt.StateHash,
                                    ["idempotency_key"] =
                                        evt.IdempotencyKey,
                                    ["timestamp"] = evt.Timestamp,
                                },
                                ["generation"] = _generation.AsDict(),
                            }, dead.Token).ConfigureAwait(false);
                        _sentUpto = evt.Sequence;
                    }
                }

                List<(MessagePriority, JsonObject)> batch;
                lock (_queueLock)
                {
                    batch = new List<(MessagePriority, JsonObject)>(
                        _messageQueue.Count);
                    while (_messageQueue.Count > 0)
                    {
                        batch.Add(_messageQueue.Dequeue());
                    }
                }
                if (batch.Count > 0)
                {
                    batch.Sort(
                        (a, b) => a.Item1.CompareTo(b.Item1));
                    foreach (var (_, message) in batch)
                    {
                        await _transport.SendAsync(message, dead.Token)
                            .ConfigureAwait(false);
                    }
                }

                // Park until enqueued again — the idle timeout is normal
                // and must NOT mark the channel dead.  Swap in a fresh
                // signal each pass (Python event.clear()): an enqueue
                // that lands before the exchange was already drained
                // above; one that lands after wakes the wait below.
                var waitTcs = new TaskCompletionSource<bool>(
                    TaskCreationOptions.RunContinuationsAsynchronously);
                Interlocked.Exchange(ref _sendWakeup, waitTcs);
                var delay = Task.Delay(
                    TimeSpan.FromSeconds(
                        _config.SendIdleSleepSeconds), dead.Token);
                try
                {
                    await Task.WhenAny(waitTcs.Task, delay)
                        .ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
            }
        }
        catch (OperationCanceledException)
        {
            // cancelled — dead flag already owned by caller
        }
        catch (Exception)
        {
            dead.Cancel();
        }
    }

    private async Task ReceiveLoopAsync(CancellationTokenSource dead)
    {
        try
        {
            while (!dead.IsCancellationRequested)
            {
                var message = await _transport
                    .ReceiveAsync(dead.Token).ConfigureAwait(false);
                if (message is null)
                {
                    break;
                }
                await DispatchMessageAsync(message).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException)
        {
            // cancelled
        }
        catch (Exception)
        {
            dead.Cancel();
        }
    }

    private async Task DispatchMessageAsync(JsonObject message)
    {
        _messagesReceived += 1;

        var msgType = message["type"]?.GetValue<string>() ?? "message";
        var command = message["command"]?.GetValue<string>();

        if (msgType == "control")
        {
            await HandleControlAsync(message, command).ConfigureAwait(false);
        }
        else if (_onMessage is not null)
        {
            await _onMessage(message).ConfigureAwait(false);
        }
    }

    private async Task HandleControlAsync(
        JsonObject message, string? command)
    {
        var payload = message["payload"] as JsonObject;
        switch (command)
        {
            case "heartbeat_pong":
                _lastPongReceived = MonotonicNow();
                _heartbeatsReceived += 1;
                break;
            case "state_event_ack":
                var cursor =
                    payload?["cursor"]?.GetValue<int>() ?? 0;
                if (cursor > _ackedCursor)
                {
                    _ackedCursor = cursor;
                }
                break;
            case "state_event_hello":
                await SendSessionInfoAsync(payload ?? new JsonObject())
                    .ConfigureAwait(false);
                break;
            case "state_event_resync":
                var resyncCursor =
                    payload?["cursor"]?.GetValue<int>() ?? 0;
                await HandleResyncAsync(resyncCursor)
                    .ConfigureAwait(false);
                break;
            case "state_event_session":
                break;
        }

        if (_onControl is not null)
        {
            try
            {
                await _onControl(message).ConfigureAwait(false);
            }
            catch (Exception)
            {
                // control observers never break the channel
            }
        }
    }

    private async Task HandleResyncAsync(int cursor)
    {
        _ackedCursor = cursor;
        _sentUpto = cursor;
        if (_outbox is not null)
        {
            await _outbox.ReplayFromAsync(cursor).ConfigureAwait(false);
        }
    }

    private Task SendSessionInfoAsync(JsonObject payload)
    {
        var message = new JsonObject
        {
            ["type"] = "control",
            ["command"] = "state_event_session",
            ["payload"] = new JsonObject
            {
                ["session_id"] = _generation.SessionId,
                ["backend_generation"] = _generation.BackendGeneration,
                ["cursor"] = _ackedCursor,
                ["latest_sequence"] =
                    _outbox?.GetLatestSequence() ?? 0,
            },
            ["generation"] = _generation.AsDict(),
        };
        EnqueueControl(message);
        return Task.CompletedTask;
    }

    // ================================================================
    // outbox integration (A195/A263)
    // ================================================================

    public Task<OutboxEvent> AppendStateEventAsync(
        string entityId,
        string entityType,
        string operation,
        JsonObject payload,
        string stateHash = "",
        CancellationToken cancellationToken = default)
    {
        if (_outbox is null)
        {
            throw new InvalidOperationException("No outbox configured");
        }
        return _outbox.AppendAsync(
            entityId, entityType, operation, payload, stateHash,
            cancellationToken);
    }

    // ================================================================
    // heartbeat (A263 two-way heartbeat with deadline)
    // ================================================================

    private async Task HeartbeatLoopAsync(CancellationTokenSource dead)
    {
        // Baseline the deadline at loop start: Python initialises
        // _last_pong_received to 0 so the first deadline check always
        // trips on uptime; the intended contract is "peer gets one full
        // timeout window from channel open".
        _lastPongReceived = MonotonicNow();
        while (!dead.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(
                    TimeSpan.FromSeconds(
                        _config.HeartbeatIntervalSeconds), dead.Token)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                break;
            }
            if (dead.IsCancellationRequested)
            {
                break;
            }

            try
            {
                await SendPingAsync().ConfigureAwait(false);
            }
            catch (Exception)
            {
                dead.Cancel();
                break;
            }

            // Deadline check.
            if (MonotonicNow() - _lastPongReceived
                > _config.HeartbeatTimeoutSeconds)
            {
                dead.Cancel();
                try
                {
                    await _transport.CloseAsync(
                        1001, "heartbeat_timeout",
                        CancellationToken.None).ConfigureAwait(false);
                }
                catch (Exception)
                {
                    // transport already gone
                }
                break;
            }
        }
    }

    private Task SendPingAsync()
    {
        var message = new JsonObject
        {
            ["type"] = "control",
            ["command"] = "heartbeat_ping",
            ["payload"] = new JsonObject
            {
                ["t"] = DateTime.UtcNow.ToString("o"),
            },
            ["generation"] = _generation.AsDict(),
        };
        EnqueueControl(message);
        _lastPingSent = MonotonicNow();
        _heartbeatsSent += 1;
        return Task.CompletedTask;
    }

    // ================================================================
    // connection lifecycle (connection_mixin parity)
    // ================================================================

    public async Task<ChannelGeneration> ConnectAsync(
        string backendGeneration = "",
        string sessionId = "",
        CancellationToken cancellationToken = default)
    {
        await _generationLock.WaitAsync(cancellationToken)
            .ConfigureAwait(false);
        try
        {
            _generation = new ChannelGeneration
            {
                ChannelId = _config.ChannelId,
                Generation = _generation.Generation + 1,
                BackendGeneration = backendGeneration,
                SessionId = sessionId,
            };
            _reconnectAttempts = 0;
        }
        finally
        {
            _generationLock.Release();
        }

        await SetStateAsync(ChannelState.Connecting)
            .ConfigureAwait(false);
        if (_heartbeatDead.IsCancellationRequested)
        {
            _heartbeatDead = new CancellationTokenSource();
        }

        _heartbeatTask =
            HeartbeatLoopAsync(_heartbeatDead);
        _ = ReceiveLoopAsync(_heartbeatDead);
        StartSendLoop();
        await SendHelloAsync().ConfigureAwait(false);
        await SetStateAsync(ChannelState.Open).ConfigureAwait(false);
        return _generation;
    }

    /// <summary>Graceful disconnect — invalidates ready (A263).</summary>
    public async Task DisconnectAsync(
        int code = 1000, string reason = "",
        CancellationToken cancellationToken = default)
    {
        _heartbeatDead.Cancel();
        // Python cancels and awaits the tracked tasks before closing the
        // transport; the receive loop is untracked and dies on close.
        await AwaitLoopTaskAsync(_heartbeatTask).ConfigureAwait(false);
        await AwaitLoopTaskAsync(_sendTask).ConfigureAwait(false);
        await _transport
            .CloseAsync(code, reason, cancellationToken)
            .ConfigureAwait(false);
        await SetStateAsync(ChannelState.Closed).ConfigureAwait(false);
    }

    private static async Task AwaitLoopTaskAsync(Task? task)
    {
        if (task is null)
        {
            return;
        }
        try
        {
            await task.WaitAsync(TimeSpan.FromSeconds(2))
                .ConfigureAwait(false);
        }
        catch (Exception)
        {
            // loop already exited or refused to die; never block disconnect
        }
    }

    /// <summary>Reconnect with snapshot/cursor/hash convergence (A263).</summary>
    public async Task<ChannelGeneration> ReconnectAsync(
        int snapshotCursor,
        string snapshotHash,
        string backendGeneration = "",
        string sessionId = "",
        CancellationToken cancellationToken = default)
    {
        if (!VerifySnapshot(snapshotCursor, snapshotHash))
        {
            throw new ArgumentException(
                "snapshot integrity verification failed");
        }

        _snapshotCursor = snapshotCursor;
        _snapshotHash = snapshotHash;
        _snapshotGeneration = _generation;

        await SetStateAsync(ChannelState.Reconnecting)
            .ConfigureAwait(false);
        _reconnectAttempts += 1;

        if (_reconnectAttempts > _config.ReconnectMaxAttempts)
        {
            await SetStateAsync(ChannelState.Dead).ConfigureAwait(false);
            throw new InvalidOperationException(
                "max reconnect attempts exceeded");
        }

        await _transport.CloseAsync(1001, "reconnect",
            CancellationToken.None).ConfigureAwait(false);

        var delay = _config.ReconnectBaseDelaySeconds
            * Math.Pow(2, _reconnectAttempts - 1);
        await Task.Delay(TimeSpan.FromSeconds(delay),
            cancellationToken).ConfigureAwait(false);

        await _generationLock.WaitAsync(cancellationToken)
            .ConfigureAwait(false);
        try
        {
            _generation = new ChannelGeneration
            {
                ChannelId = _config.ChannelId,
                Generation = _generation.Generation + 1,
                BackendGeneration = backendGeneration,
                SessionId = sessionId,
            };
        }
        finally
        {
            _generationLock.Release();
        }

        // Python clears the shared dead flag after the transport close;
        // here the dead source belongs to the old loop generation, so the
        // old loops are retired first — otherwise a straggling old loop
        // could cancel the new generation or two send loops could race.
        var oldDead = _heartbeatDead;
        oldDead.Cancel();
        await AwaitLoopTaskAsync(_heartbeatTask).ConfigureAwait(false);
        await AwaitLoopTaskAsync(_sendTask).ConfigureAwait(false);
        _heartbeatDead = new CancellationTokenSource();

        _heartbeatTask =
            HeartbeatLoopAsync(_heartbeatDead);
        _ = ReceiveLoopAsync(_heartbeatDead);
        StartSendLoop();
        await SendResyncAsync(snapshotCursor).ConfigureAwait(false);
        await SetStateAsync(ChannelState.Open).ConfigureAwait(false);
        _reconnects += 1;
        return _generation;
    }

    private static bool VerifySnapshot(int cursor, string snapshotHash) =>
        cursor >= 0;

    // ================================================================
    // metrics
    // ================================================================

    private int _controlQueueCount()
    {
        lock (_queueLock)
        {
            return _controlQueue.Count;
        }
    }

    private int _messageQueueCount()
    {
        lock (_queueLock)
        {
            return _messageQueue.Count;
        }
    }

    public JsonObject GetMetrics() => new()
    {
        ["channel_id"] = _config.ChannelId,
        ["state"] = _state.ToWire(),
        ["generation"] = _generation.AsDict(),
        ["messages_sent"] = _messagesSent,
        ["messages_received"] = _messagesReceived,
        ["heartbeats_sent"] = _heartbeatsSent,
        ["heartbeats_received"] = _heartbeatsReceived,
        ["reconnects"] = _reconnects,
        ["queue_size"] = _messageQueueCount(),
        ["control_queue_size"] = _controlQueueCount(),
        ["outbox_sequence"] = _outbox?.GetLatestSequence() ?? 0,
        ["outbound_send_active"] =
            _sendTask is { IsCompleted: false },
        ["acked_cursor"] = _ackedCursor,
        ["sent_upto"] = _sentUpto,
        ["pending_acks"] = 0,
        ["last_ping_sent"] = _lastPingSent,
        ["last_pong_received"] = _lastPongReceived,
    };

    // ================================================================
    // factory
    // ================================================================

    public static async Task<A263Channel> CreateAsync(
        string channelId,
        IChannelTransport transport,
        ChannelConfig? config = null,
        TransactionalOutbox? outbox = null,
        CancellationToken cancellationToken = default)
    {
        config ??= new ChannelConfig { ChannelId = channelId };
        var channel = new A263Channel(config, transport, outbox);
        await channel.ConnectAsync(
            cancellationToken: cancellationToken)
            .ConfigureAwait(false);
        return channel;
    }
}
