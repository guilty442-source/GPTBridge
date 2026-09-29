using System.Text.Json.Nodes;

namespace GPTBridge.Channels;

public sealed partial class A263Channel
{

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
}
