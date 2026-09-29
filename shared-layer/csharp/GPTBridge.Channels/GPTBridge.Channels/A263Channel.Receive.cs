using System.Text.Json.Nodes;

namespace GPTBridge.Channels;

public sealed partial class A263Channel
{

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
}
