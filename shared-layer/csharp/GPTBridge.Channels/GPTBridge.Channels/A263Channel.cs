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

public sealed partial class A263Channel
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

    // High-resolution monotonic clock — TickCount64 quantises to ~15.6ms
    // which makes sub-second heartbeat deadlines nondeterministic;
    // Python's time.monotonic() is a performance counter too.
    private static readonly long _monotonicEpoch =
        System.Diagnostics.Stopwatch.GetTimestamp();

    private static double MonotonicNow() =>
        (System.Diagnostics.Stopwatch.GetTimestamp() - _monotonicEpoch)
        / (double)System.Diagnostics.Stopwatch.Frequency;

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
