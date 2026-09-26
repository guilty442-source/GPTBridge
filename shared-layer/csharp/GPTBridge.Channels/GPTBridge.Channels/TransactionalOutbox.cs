// Transactional Outbox — A195/A263.
//
// C# port of shared-layer/src/shared_layer/transactional_outbox.py.
// Ordered event sequences with cursor-based replay, same contract:
// append assigns sequence = latest + 1, idempotency_key is
// "{channel_id}:{sequence}", fetch_after(cursor, limit) returns the
// ordered window, replay_from re-emits through the callback (or relies
// on the send-loop via SentUpto when no callback is registered).

using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.Channels;

public sealed class TransactionalOutbox
{
    public string ChannelId { get; }
    private int _sequence;
    private readonly SortedDictionary<int, OutboxEvent> _events = new();
    private readonly SemaphoreSlim _lock = new(1, 1);
    private readonly Func<OutboxEvent, Task>? _callback;

    public TransactionalOutbox(
        string channelId,
        int maxSequence = 0,
        Func<OutboxEvent, Task>? callback = null)
    {
        ChannelId = channelId;
        _sequence = maxSequence;
        _callback = callback;
    }

    public int GetLatestSequence() => _sequence;

    public async Task<OutboxEvent> AppendAsync(
        string entityId,
        string entityType,
        string operation,
        JsonObject payload,
        string stateHash = "",
        CancellationToken cancellationToken = default)
    {
        // Parity with the Python side: the payload must be JSON
        // encodable; fall back to {"repr": ...} when it is not.
        try
        {
            _ = payload.ToJsonString();
        }
        catch (InvalidOperationException)
        {
            payload = new JsonObject { ["repr"] = payload.ToString() };
        }
        catch (JsonException)
        {
            payload = new JsonObject { ["repr"] = payload.ToString() };
        }

        await _lock.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            _sequence += 1;
            var evt = new OutboxEvent
            {
                Sequence = _sequence,
                EntityId = entityId,
                EntityType = entityType,
                Operation = operation,
                Payload = payload,
                StateHash = stateHash,
                IdempotencyKey = $"{ChannelId}:{_sequence}",
            };
            _events[_sequence] = evt;
            if (_callback is not null)
            {
                try
                {
                    await _callback(evt).ConfigureAwait(false);
                }
                catch (Exception)
                {
                    // callback failures never block the append
                }
            }
            return evt;
        }
        finally
        {
            _lock.Release();
        }
    }

    public async Task<List<OutboxEvent>> FetchAfterAsync(
        int cursor, int limit, CancellationToken cancellationToken = default)
    {
        await _lock.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var events = new List<OutboxEvent>();
            var end = Math.Min(cursor + 1 + limit, _sequence + 1);
            for (var seq = cursor + 1; seq < end; seq++)
            {
                if (_events.TryGetValue(seq, out var evt))
                {
                    events.Add(evt);
                }
            }
            return events;
        }
        finally
        {
            _lock.Release();
        }
    }

    /// <summary>
    /// Replay events from cursor through the registered callback.  With no
    /// callback the outbox drains via FetchAfterAsync so the channel
    /// send-loop picks events up through SentUpto (see A263Channel).
    /// </summary>
    public async Task ReplayFromAsync(
        int cursor, CancellationToken cancellationToken = default)
    {
        var events = await FetchAfterAsync(cursor, 1000, cancellationToken)
            .ConfigureAwait(false);
        if (_callback is null)
        {
            return;
        }
        foreach (var evt in events)
        {
            try
            {
                await _callback(evt).ConfigureAwait(false);
            }
            catch (Exception)
            {
                // replay callback failures never abort the sweep
            }
        }
    }
}
