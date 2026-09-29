// Parity tests for the C# LISTEN/NOTIFY listener port against the
// Python contract in shared-layer/src/shared_layer/transport_notify.py.

using System.Collections.Concurrent;
using GPTBridge.Channels;

namespace GPTBridge.Channels.Tests;

public sealed class NotifyListenerTests
{
    private sealed class FakeNotifyTransport : INotifyTransport
    {
        public readonly ConcurrentQueue<string> Listened = new();
        private readonly System.Threading.Channels.Channel<NotifyMessage>
            _incoming = System.Threading.Channels.Channel
                .CreateUnbounded<NotifyMessage>();
        public int OpenCount;
        public int DisposeCount;
        public bool ThrowOnOpen;

        public Task OpenAsync(CancellationToken cancellationToken)
        {
            if (ThrowOnOpen)
                throw new InvalidOperationException("fake-open-fail");
            Interlocked.Increment(ref OpenCount);
            return Task.CompletedTask;
        }

        public Task ListenAsync(
            string pgChannel, CancellationToken cancellationToken)
        {
            Listened.Enqueue(pgChannel);
            return Task.CompletedTask;
        }

        public void Push(string channel, string payload) =>
            _incoming.Writer.TryWrite(new NotifyMessage(channel, payload));

        public async Task<NotifyMessage?> WaitAsync(
            TimeSpan slice, CancellationToken cancellationToken)
        {
            using var linked = CancellationTokenSource
                .CreateLinkedTokenSource(cancellationToken);
            linked.CancelAfter(slice);
            try
            {
                return await _incoming.Reader
                    .ReadAsync(linked.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
                when (!cancellationToken.IsCancellationRequested)
            {
                return null;
            }
        }

        public ValueTask DisposeAsync()
        {
            Interlocked.Increment(ref DisposeCount);
            return ValueTask.CompletedTask;
        }
    }

    private sealed class Harness : IAsyncDisposable
    {
        public readonly List<FakeNotifyTransport> Transports = new();
        public readonly NotifyListener Listener;

        public Harness(
            TimeSpan? callbackDeadline = null,
            Action<FakeNotifyTransport>? onCreate = null)
        {
            Listener = new NotifyListener(
                ct =>
                {
                    var transport = new FakeNotifyTransport();
                    onCreate?.Invoke(transport);
                    lock (Transports)
                        Transports.Add(transport);
                    return Task.FromResult<INotifyTransport>(transport);
                },
                minBackoff: TimeSpan.FromMilliseconds(10),
                maxBackoff: TimeSpan.FromMilliseconds(100),
                pollSlice: TimeSpan.FromMilliseconds(20),
                callbackDeadline: callbackDeadline);
        }

        public FakeNotifyTransport Current
        {
            get
            {
                lock (Transports)
                    return Transports[^1];
            }
        }

        public int Count
        {
            get
            {
                lock (Transports)
                    return Transports.Count;
            }
        }

        public async ValueTask DisposeAsync() => await Listener.DisposeAsync();
    }

    private static async Task WaitForAsync(
        Func<bool> condition, string what, TimeSpan? timeout = null)
    {
        var deadline = DateTime.UtcNow +
            (timeout ?? TimeSpan.FromSeconds(5));
        while (!condition())
        {
            Assert.True(
                DateTime.UtcNow < deadline,
                $"timed out waiting for {what}");
            await Task.Delay(10);
        }
    }

    [Fact]
    public void SubscribeRejectsInvalidChannel()
    {
        var listener = new NotifyListener(
            _ => Task.FromResult<INotifyTransport>(new FakeNotifyTransport()));
        var error = Assert.Throws<ArgumentException>(
            () => listener.Subscribe("BAD-CHAN!", (_, _) => { }));
        Assert.Contains("INVALID_NOTIFY_CHANNEL", error.Message);
    }

    [Fact]
    public void StartReturnsFalseWithoutSubscribers()
    {
        var h = new Harness();
        Assert.False(h.Listener.Start());
        Assert.False(h.Listener.Status().Running);
    }

    [Fact]
    public async Task NotifyDispatchesChannelAndPayload()
    {
        await using var h = new Harness();
        var received = new ConcurrentQueue<(string, string)>();
        h.Listener.Subscribe("alpha",
            (channel, payload) => received.Enqueue((channel, payload)));
        Assert.True(h.Listener.Start());

        await WaitForAsync(
            () => h.Count >= 1
                && h.Current.Listened.Contains("tool_request_alpha"),
            "LISTEN tool_request_alpha");

        h.Current.Push("tool_request_alpha", "req-1");
        await WaitForAsync(() => !received.IsEmpty, "dispatch");
        Assert.Equal(("alpha", "req-1"), received.Single());
    }

    [Fact]
    public async Task DuplicateSubscribeIsNoop()
    {
        await using var h = new Harness();
        var count = 0;
        Action<string, string> callback = (_, _) =>
            Interlocked.Increment(ref count);
        h.Listener.Subscribe("alpha", callback);
        h.Listener.Subscribe("alpha", callback);
        h.Listener.Start();
        await WaitForAsync(
            () => h.Count >= 1
                && h.Current.Listened.Contains("tool_request_alpha"),
            "LISTEN");
        h.Current.Push("tool_request_alpha", "req-1");
        await WaitForAsync(() => count >= 1, "one dispatch");
        await Task.Delay(100);
        Assert.Equal(1, count);
    }

    [Fact]
    public async Task NewChannelTriggersResubscribeSession()
    {
        await using var h = new Harness();
        h.Listener.Subscribe("alpha", (_, _) => { });
        h.Listener.Start();
        await WaitForAsync(
            () => h.Count >= 1
                && h.Current.Listened.Contains("tool_request_alpha"),
            "first session");

        h.Listener.Subscribe("beta", (_, _) => { });
        await WaitForAsync(() => h.Count >= 2, "re-LISTEN session");
        Assert.Contains("tool_request_alpha", h.Current.Listened);
        Assert.Contains("tool_request_beta", h.Current.Listened);
    }

    [Fact]
    public async Task UnsubscribeLastCallbackDropsChannel()
    {
        await using var h = new Harness();
        Action<string, string> callback = (_, _) => { };
        h.Listener.Subscribe("alpha", callback);
        h.Listener.Start();
        await WaitForAsync(
            () => h.Count >= 1
                && h.Current.Listened.Contains("tool_request_alpha"),
            "first session");
        var first = h.Current;

        h.Listener.Unsubscribe("alpha", callback);
        Assert.Empty(h.Listener.SubscribedChannels());
        // Resubscribe ends the LISTEN session; the loop parks with no
        // connection instead of holding an idle one.
        await WaitForAsync(() => first.DisposeCount >= 1, "session close");
    }

    [Fact]
    public async Task SlowCallbackCountsDispatchStall()
    {
        await using var h = new Harness(
            callbackDeadline: TimeSpan.FromMilliseconds(30));
        var gate = new ManualResetEventSlim();
        h.Listener.Subscribe("alpha",
            (_, _) => gate.Wait(TimeSpan.FromSeconds(10)));
        h.Listener.Start();
        await WaitForAsync(
            () => h.Count >= 1
                && h.Current.Listened.Contains("tool_request_alpha"),
            "LISTEN");
        h.Current.Push("tool_request_alpha", "req-1");
        await WaitForAsync(
            () => h.Listener.Status().DispatchStalls >= 1, "stall");
        gate.Set();
    }

    [Fact]
    public async Task InflightCapDropsWakeHints()
    {
        await using var h = new Harness(
            callbackDeadline: TimeSpan.FromMilliseconds(20));
        var gate = new ManualResetEventSlim();
        // 9 wedging subscribers: 8 hit the per-callback deadline, the 9th
        // is dropped by the inflight cap (8) — 9 stalls total.  Each
        // callback captures the loop index so the compiler cannot fold
        // them into one shared delegate (dedupe is by reference parity).
        for (var i = 0; i < 9; i++)
        {
            var index = i;
            h.Listener.Subscribe("alpha",
                (_, _) => { _ = index; gate.Wait(TimeSpan.FromSeconds(10)); });
        }
        h.Listener.Start();
        await WaitForAsync(
            () => h.Count >= 1
                && h.Current.Listened.Contains("tool_request_alpha"),
            "LISTEN");
        h.Current.Push("tool_request_alpha", "req-1");
        try
        {
            await WaitForAsync(
                () => h.Listener.Status().DispatchStalls >= 9,
                "8 deadline stalls + 1 cap drop",
                TimeSpan.FromSeconds(15));
        }
        finally
        {
            gate.Set();
        }
    }

    [Fact]
    public async Task ReconnectsWithBackoffAfterTransportFailure()
    {
        var attempts = 0;
        await using var h = new Harness(onCreate: transport =>
            transport.ThrowOnOpen = Interlocked.Increment(ref attempts) == 1);
        h.Listener.Subscribe("alpha", (_, _) => { });
        h.Listener.Start();
        await WaitForAsync(
            () => h.Count >= 2
                && h.Current.Listened.Contains("tool_request_alpha"),
            "reconnect + LISTEN");
        Assert.True(h.Listener.Status().Running);
    }

    [Fact]
    public async Task StatusMirrorsPythonShape()
    {
        await using var h = new Harness();
        h.Listener.Subscribe("alpha", (_, _) => { });
        h.Listener.Start();
        await WaitForAsync(
            () => h.Count >= 1
                && h.Current.Listened.Contains("tool_request_alpha"),
            "LISTEN");
        var status = h.Listener.Status();
        Assert.True(status.Running);
        Assert.Equal(new[] { "alpha" }, status.Channels.ToArray());
        var dict = status.AsDict();
        Assert.True(dict.ContainsKey("running"));
        Assert.True(dict.ContainsKey("channels"));
        Assert.True(dict.ContainsKey("last_error"));
        Assert.True(dict.ContainsKey("dispatch_stalls"));
        Assert.True(dict["running"]!.GetValue<bool>());
    }
}
