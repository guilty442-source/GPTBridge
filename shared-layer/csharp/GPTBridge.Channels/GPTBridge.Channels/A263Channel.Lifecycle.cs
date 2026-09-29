using System.Text.Json.Nodes;

namespace GPTBridge.Channels;

public sealed partial class A263Channel
{

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

}
