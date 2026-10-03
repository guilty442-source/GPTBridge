using GPTBridge.Channels;

namespace GPTBridge.Channels.Tests;

public sealed class NativeNotifyTransportTests : IDisposable
{
    private readonly string _root = Path.Combine(Path.GetTempPath(), "gptbridge-notify-" + Guid.NewGuid().ToString("N"));
    private async Task<NativeNotifyTransport> Open(string channel = "tool_request_test")
    {
        var transport = new NativeNotifyTransport(_root);
        await transport.OpenAsync(default);
        await transport.ListenAsync(channel, default);
        return transport;
    }
    [Fact]
    public void RetiredDsnCannotBecomeNativeRoot()
    {
        Assert.Throws<ArgumentException>(() => new NativeNotifyTransport("Host=localhost;Database=transport"));
    }
    [Fact]
    public async Task BroadcastReachesIndependentSessions()
    {
        await using var first = await Open();
        await using var second = await Open();
        await NativeNotifyTransport.PublishAsync(_root, "tool_request_test", "request-1");
        Assert.Equal("request-1", (await first.WaitAsync(TimeSpan.FromSeconds(1), default))?.Payload);
        Assert.Equal("request-1", (await second.WaitAsync(TimeSpan.FromSeconds(1), default))?.Payload);
        Assert.Null(await first.WaitAsync(TimeSpan.Zero, default));
    }
    [Fact]
    public async Task ExistingHintsAreNotReplayedButNewHintsAreDelivered()
    {
        await NativeNotifyTransport.PublishAsync(_root, "tool_request_test", "old");
        await using var transport = await Open();
        Assert.Null(await transport.WaitAsync(TimeSpan.Zero, default));
        await NativeNotifyTransport.PublishAsync(_root, "tool_request_test", "new");
        Assert.Equal("new", (await transport.WaitAsync(TimeSpan.FromSeconds(1), default))?.Payload);
    }
    [Fact]
    public async Task UnsubscribedChannelsDoNotDispatch()
    {
        await using var transport = await Open();
        await NativeNotifyTransport.PublishAsync(_root, "tool_request_other", "ignored");
        Assert.Null(await transport.WaitAsync(TimeSpan.FromMilliseconds(30), default));
    }
    [Fact]
    public async Task InvalidAndOversizedHintsAreRejected()
    {
        await Assert.ThrowsAsync<ArgumentException>(() => NativeNotifyTransport.PublishAsync(_root, "../bad", "x"));
        await Assert.ThrowsAsync<ArgumentException>(() => NativeNotifyTransport.PublishAsync(_root, "tool_request_test", new string('x', 17000)));
        await using var transport = await Open();
        await File.WriteAllTextAsync(Path.Combine(_root, "tool_request_test.json"), "{\"Id\":\"bad\",\"Channel\":\"tool_request_test\",\"Payload\":\"x\"}");
        await Assert.ThrowsAsync<InvalidDataException>(() => transport.WaitAsync(TimeSpan.Zero, default));
    }
    [Fact]
    public async Task CancellationIsNotReportedAsTimeout()
    {
        await using var transport = await Open();
        using var cancellation = new CancellationTokenSource();
        cancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => transport.WaitAsync(TimeSpan.FromSeconds(1), cancellation.Token));
    }
    [Fact]
    public async Task NativeListenerDispatchesRealPublishedHint()
    {
        await using var listener = NotifyListener.ForNative(_root, pollSlice: TimeSpan.FromMilliseconds(100));
        var received = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        listener.Subscribe("test", (_, request) => received.TrySetResult(request));
        Assert.True(listener.Start());
        // Repeat until connected: hints are intentionally not a durable queue.
        for (var attempt = 0; attempt < 30 && !received.Task.IsCompleted; attempt++)
        {
            await NativeNotifyTransport.PublishAsync(_root, "tool_request_test", "smoke-request");
            await Task.Delay(50);
        }
        Assert.Equal("smoke-request", await received.Task.WaitAsync(TimeSpan.FromSeconds(2)));
    }
    public void Dispose()
    {
        if (Directory.Exists(_root)) Directory.Delete(_root, recursive: true);
    }
}
