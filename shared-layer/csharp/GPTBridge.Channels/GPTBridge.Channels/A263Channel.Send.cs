using System.Text.Json.Nodes;

namespace GPTBridge.Channels;

public sealed partial class A263Channel
{

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
}
