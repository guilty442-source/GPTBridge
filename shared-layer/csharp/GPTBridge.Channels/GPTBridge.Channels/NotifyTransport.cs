// Transport LISTEN/NOTIFY transport seam — information layer (C27).
//
// C# port of the connection surface inside
// shared-layer/src/shared_layer/transport_notify.py
// (python_residency target: csharp-channel).  The seam is injectable so
// the listener loop is unit-testable without a live PostgreSQL; the
// production adapter wraps Npgsql on a dedicated connection — never the
// lane pool, same as the Python dedicated_connection contract.
//
// Contract parity with the Python side:
//   * LISTEN tool_request_<channel>; payload is a wake-hint only —
//     subscriber-side periodic polling covers loss (fail-closed)
//   * WaitAsync returns null on poll-slice timeout, letting the loop
//     check stop/resubscribe flags between slices
//   * one transport instance == one LISTEN session; resubscribe starts a
//     new session (the Python listener reconnects the same way)

using Npgsql;

namespace GPTBridge.Channels;

/// <summary>One LISTEN/NOTIFY notification: PG channel + payload.</summary>
public readonly record struct NotifyMessage(string Channel, string Payload);

/// <summary>Dedicated LISTEN session abstraction (information layer).</summary>
public interface INotifyTransport : IAsyncDisposable
{
    /// <summary>Open the dedicated connection.</summary>
    Task OpenAsync(CancellationToken cancellationToken);

    /// <summary>LISTEN one fully-qualified PG channel name.</summary>
    Task ListenAsync(string pgChannel, CancellationToken cancellationToken);

    /// <summary>
    /// Next notification, or <c>null</c> when <paramref name="slice"/>
    /// elapsed with no traffic (poll-slice timeout, not an error).
    /// </summary>
    Task<NotifyMessage?> WaitAsync(
        TimeSpan slice, CancellationToken cancellationToken);
}

/// <summary>Npgsql-backed <see cref="INotifyTransport"/>.</summary>
public sealed class NpgsqlNotifyTransport : INotifyTransport
{
    private readonly string _dsn;
    private NpgsqlConnection? _connection;
    private readonly Queue<NotifyMessage> _pending = new();
    private readonly object _pendingLock = new();

    public NpgsqlNotifyTransport(string dsn)
    {
        if (string.IsNullOrWhiteSpace(dsn))
            throw new ArgumentException("NOTIFY_DSN_EMPTY", nameof(dsn));
        _dsn = dsn;
    }

    public async Task OpenAsync(CancellationToken cancellationToken)
    {
        var connection = new NpgsqlConnection(_dsn);
        connection.Notification += (_, args) =>
        {
            lock (_pendingLock)
            {
                _pending.Enqueue(new NotifyMessage(args.Channel, args.Payload));
            }
        };
        await connection.OpenAsync(cancellationToken).ConfigureAwait(false);
        _connection = connection;
    }

    public async Task ListenAsync(
        string pgChannel, CancellationToken cancellationToken)
    {
        var connection = _connection
            ?? throw new InvalidOperationException("NOTIFY_TRANSPORT_NOT_OPEN");
        // Channel names are regex-validated upstream ([a-z0-9_]+ plus the
        // tool_request_ prefix); identifier quoting is defence in depth.
        await using var command = new NpgsqlCommand(
            $"LISTEN \"{pgChannel}\"", connection);
        await command.ExecuteNonQueryAsync(cancellationToken)
            .ConfigureAwait(false);
    }

    public async Task<NotifyMessage?> WaitAsync(
        TimeSpan slice, CancellationToken cancellationToken)
    {
        lock (_pendingLock)
        {
            if (_pending.Count > 0)
                return _pending.Dequeue();
        }
        var connection = _connection
            ?? throw new InvalidOperationException("NOTIFY_TRANSPORT_NOT_OPEN");
        using var sliceCts = CancellationTokenSource
            .CreateLinkedTokenSource(cancellationToken);
        sliceCts.CancelAfter(slice);
        try
        {
            await connection.WaitAsync(sliceCts.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
            when (sliceCts.IsCancellationRequested
                && !cancellationToken.IsCancellationRequested)
        {
            return null; // poll-slice timeout — not an error
        }
        lock (_pendingLock)
        {
            return _pending.Count > 0 ? _pending.Dequeue() : null;
        }
    }

    public async ValueTask DisposeAsync()
    {
        var connection = _connection;
        _connection = null;
        if (connection is not null)
            await connection.DisposeAsync().ConfigureAwait(false);
    }
}
