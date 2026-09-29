// Transport LISTEN/NOTIFY consumer — §10.63/G26/INT-4 (event over polling).
//
// C# port of shared-layer/src/shared_layer/transport_notify.py
// (python_residency target: csharp-channel).  One dedicated connection
// LISTENs tool_request_<channel> for every subscribed channel id and
// re-dispatches request ids to subscriber callbacks.
//
// Contract parity with the Python side:
//   * the notification is only a wake-hint — loss is covered by the
//     subscriber's periodic polling (fail-closed fallback); the listener
//     itself reconnects with bounded backoff and never disables polling
//   * callbacks run on listener threads — subscribers must re-dispatch
//     into their own event loop (cheap re-dispatch contract)
//   * lazy: no connection while there are no subscribers; adding the
//     first callback for a new channel triggers a re-LISTEN
//   * dispatch is bounded: 2 worker slots, 5 s per-callback deadline,
//     inflight cap 8; saturated queues drop the hint and count stalls
//   * status() mirrors the Python dict shape

using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace GPTBridge.Channels;

/// <summary>Listener status projection — mirrors Python status().</summary>
public sealed record NotifyListenerStatus(
    bool Running,
    IReadOnlyList<string> Channels,
    string LastError,
    int DispatchStalls)
{
    /// <summary>Wire projection with the Python dict key names.</summary>
    public JsonObject AsDict() => new()
    {
        ["running"] = Running,
        ["channels"] = new JsonArray(Channels.Select(c => (JsonNode)c).ToArray()),
        ["last_error"] = LastError,
        ["dispatch_stalls"] = DispatchStalls,
    };
}

/// <summary>LISTEN <c>tool_request_&lt;channel&gt;</c>; payload → callbacks.</summary>
public sealed class NotifyListener : IAsyncDisposable
{
    /// <summary>PG channel prefix (wire contract with the producer).</summary>
    public const string NotifyChannelPrefix = "tool_request_";

    private static readonly Regex ChannelName =
        new("^[a-z0-9_]+$", RegexOptions.Compiled);

    // Dispatch bounds — identical to the Python constants.
    private static readonly TimeSpan CallbackDeadline = TimeSpan.FromSeconds(5.0);
    private const int DispatchInflightCap = 8;
    private const int DispatchWorkers = 2;

    private readonly Func<CancellationToken, Task<INotifyTransport>>
        _transportFactory;
    private readonly TimeSpan _minBackoff;
    private readonly TimeSpan _maxBackoff;
    private readonly TimeSpan _pollSlice;
    private readonly TimeSpan _callbackDeadline;

    private readonly object _lock = new();
    private readonly Dictionary<string, List<Action<string, string>>>
        _subscribers = new(StringComparer.Ordinal);
    private readonly CancellationTokenSource _stop = new();
    private readonly SemaphoreSlim _resubscribe = new(0, 1);
    private readonly SemaphoreSlim _dispatchWorkers =
        new(DispatchWorkers, DispatchWorkers);
    private Task? _loop;
    private string _lastError = "";
    private int _dispatchStalls;
    private int _dispatchInflight;

    /// <param name="transportFactory">
    /// Opens one dedicated LISTEN session per call (injectable for tests;
    /// production uses <see cref="ForPostgreSql"/>).
    /// </param>
    public NotifyListener(
        Func<CancellationToken, Task<INotifyTransport>> transportFactory,
        TimeSpan? minBackoff = null,
        TimeSpan? maxBackoff = null,
        TimeSpan? pollSlice = null,
        TimeSpan? callbackDeadline = null)
    {
        _transportFactory = transportFactory
            ?? throw new ArgumentNullException(nameof(transportFactory));
        _minBackoff = Max(minBackoff ?? TimeSpan.FromSeconds(1.0),
            TimeSpan.FromSeconds(0.1));
        _maxBackoff = Max(maxBackoff ?? TimeSpan.FromSeconds(60.0),
            _minBackoff);
        _pollSlice = Max(pollSlice ?? TimeSpan.FromSeconds(1.0),
            TimeSpan.FromSeconds(0.1));
        _callbackDeadline = callbackDeadline ?? CallbackDeadline;
    }

    /// <summary>Production listener over a PostgreSQL DSN.</summary>
    public static NotifyListener ForPostgreSql(
        string dsn,
        TimeSpan? minBackoff = null,
        TimeSpan? maxBackoff = null,
        TimeSpan? pollSlice = null) =>
        new(
            ct =>
            {
                var transport = new NpgsqlNotifyTransport(dsn);
                return Task.FromResult<INotifyTransport>(transport);
            },
            minBackoff, maxBackoff, pollSlice);

    // -- subscription --------------------------------------------------

    /// <summary>
    /// Subscribe to <c>tool_request_&lt;channelId&gt;</c>.  The callback
    /// runs on a listener dispatch worker as
    /// <c>callback(channelId, requestId)</c> — keep it a cheap
    /// re-dispatcher.
    /// </summary>
    public void Subscribe(string channelId, Action<string, string> callback)
    {
        ArgumentNullException.ThrowIfNull(callback);
        var channel = (channelId ?? "").Trim();
        if (!ChannelName.IsMatch(channel))
            throw new ArgumentException(
                $"INVALID_NOTIFY_CHANNEL:{channelId}", nameof(channelId));
        bool newChannel;
        lock (_lock)
        {
            var subs = _subscribers.TryGetValue(channel, out var list)
                ? list
                : _subscribers[channel] = new List<Action<string, string>>();
            if (subs.Contains(callback))
                return;
            subs.Add(callback);
            newChannel = subs.Count == 1;
        }
        if (newChannel)
            SignalResubscribe();
    }

    /// <summary>Remove one callback; last removal drops the LISTEN.</summary>
    public void Unsubscribe(string channelId, Action<string, string> callback)
    {
        ArgumentNullException.ThrowIfNull(callback);
        bool removedChannel;
        lock (_lock)
        {
            var key = (channelId ?? "").Trim();
            removedChannel = false;
            if (_subscribers.TryGetValue(key, out var subs)
                && subs.Remove(callback) && subs.Count == 0)
            {
                _subscribers.Remove(key);
                removedChannel = true;
            }
        }
        if (removedChannel)
            SignalResubscribe();
    }

    /// <summary>Sorted subscribed channel ids (Python parity).</summary>
    public IReadOnlyList<string> SubscribedChannels()
    {
        lock (_lock)
            return _subscribers.Keys.OrderBy(k => k, StringComparer.Ordinal)
                .ToArray();
    }

    // -- lifecycle -----------------------------------------------------

    /// <summary>Start the listen loop; false when there are no subscribers.</summary>
    public bool Start()
    {
        if (SubscribedChannels().Count == 0)
            return false;
        lock (_lock)
        {
            if (_loop is { IsCompleted: false })
                return true;
            _loop = Task.Run(RunAsync);
        }
        return true;
    }

    /// <summary>Stop the loop and close the LISTEN session (5 s join cap).</summary>
    public async Task StopAsync()
    {
        Task? loop;
        lock (_lock)
        {
            loop = _loop;
            _loop = null;
        }
        _stop.Cancel();
        SignalResubscribe();
        if (loop is not null)
        {
            try
            {
                await loop.WaitAsync(TimeSpan.FromSeconds(5.0))
                    .ConfigureAwait(false);
            }
            catch (Exception)
            {
                // Timeout/cancellation: the loop is daemon-grade; a wedged
                // session is dropped, never joined past the cap.
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        await StopAsync().ConfigureAwait(false);
        _stop.Dispose();
        _resubscribe.Dispose();
        _dispatchWorkers.Dispose();
    }

    /// <summary>Status — same keys as the Python status() dict.</summary>
    public NotifyListenerStatus Status()
    {
        Task? loop;
        lock (_lock)
            loop = _loop;
        return new NotifyListenerStatus(
            Running: loop is { IsCompleted: false },
            Channels: SubscribedChannels(),
            LastError: _lastError,
            DispatchStalls: _dispatchStalls);
    }

    // -- listener loop -------------------------------------------------

    private void SignalResubscribe()
    {
        try
        {
            _resubscribe.Release();
        }
        catch (SemaphoreFullException)
        {
            // Already signaled — the loop re-LISTENs once anyway.
        }
    }

    private bool ResubscribeSignaled() => _resubscribe.Wait(0);

    private async Task RunAsync()
    {
        var ct = _stop.Token;
        var backoff = _minBackoff;
        while (!ct.IsCancellationRequested)
        {
            var channels = SubscribedChannels();
            if (channels.Count == 0)
            {
                // No subscribers — hold no connection; wait for subscribe
                // or stop (5 s cap is a pure fallback, signals wake early).
                try
                {
                    await _resubscribe.WaitAsync(TimeSpan.FromSeconds(5.0), ct)
                        .ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    break;
                }
                continue;
            }
            try
            {
                await ListenOnceAsync(channels, ct).ConfigureAwait(false);
                backoff = _minBackoff;
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                break;
            }
            catch (Exception error)
            {
                _lastError = $"{error.GetType().Name}: {error.Message}";
                try
                {
                    await Task.Delay(backoff, ct).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    break;
                }
                var doubled = TimeSpan.FromTicks(backoff.Ticks * 2);
                backoff = doubled > _maxBackoff ? _maxBackoff : doubled;
            }
        }
    }

    private async Task ListenOnceAsync(
        IReadOnlyList<string> channels, CancellationToken ct)
    {
        await using var transport = await _transportFactory(ct)
            .ConfigureAwait(false);
        await transport.OpenAsync(ct).ConfigureAwait(false);
        foreach (var channel in channels)
            await transport
                .ListenAsync(NotifyChannelPrefix + channel, ct)
                .ConfigureAwait(false);
        // Session LISTENs cover the current set; drop queued signals so a
        // stale subscription change cannot force an immediate reconnect.
        while (ResubscribeSignaled())
        {
        }
        while (!ct.IsCancellationRequested && !ResubscribeSignaled())
        {
            var message = await transport
                .WaitAsync(_pollSlice, ct)
                .ConfigureAwait(false);
            if (message is { } notify)
                Dispatch(notify.Channel, notify.Payload);
        }
    }

    private void Dispatch(string pgChannel, string payload)
    {
        var channel = pgChannel.StartsWith(NotifyChannelPrefix,
            StringComparison.Ordinal)
            ? pgChannel[NotifyChannelPrefix.Length..]
            : pgChannel;
        Action<string, string>[] callbacks;
        lock (_lock)
            callbacks = _subscribers.TryGetValue(channel, out var subs)
                ? subs.ToArray()
                : Array.Empty<Action<string, string>>();
        foreach (var callback in callbacks)
        {
            if (Volatile.Read(ref _dispatchInflight) >= DispatchInflightCap)
            {
                // Workers wedged — drop the wake-hint rather than grow an
                // unbounded queue (subscriber polling covers the loss).
                Interlocked.Increment(ref _dispatchStalls);
                continue;
            }
            Interlocked.Increment(ref _dispatchInflight);
            var run = Task.Run(async () =>
            {
                await _dispatchWorkers.WaitAsync().ConfigureAwait(false);
                try
                {
                    callback(channel, payload);
                }
                finally
                {
                    _dispatchWorkers.Release();
                    Interlocked.Decrement(ref _dispatchInflight);
                }
            });
            bool completed;
            try
            {
                // Sequential per-callback deadline — Python parity with
                // submit(...).result(timeout=5): a wedged callback keeps
                // running on a bounded worker and counts a stall.
                completed = run.Wait(_callbackDeadline);
            }
            catch (Exception)
            {
                // AggregateException wraps callback failures — callback
                // errors never kill the dispatch loop.
                completed = run.IsCompleted;
            }
            if (!completed)
                Interlocked.Increment(ref _dispatchStalls);
        }
    }

    private static TimeSpan Max(TimeSpan left, TimeSpan right) =>
        left > right ? left : right;
}
