// Native transient wake hints; request authority remains in the governed store.
using System.Text.Json;
using System.Text.RegularExpressions;
namespace GPTBridge.Channels;
public readonly record struct NotifyMessage(string Channel, string Payload);
public interface INotifyTransport : IAsyncDisposable
{
    Task OpenAsync(CancellationToken cancellationToken);
    Task ListenAsync(string channel, CancellationToken cancellationToken);
    Task<NotifyMessage?> WaitAsync(TimeSpan slice, CancellationToken cancellationToken);
}
public sealed class NativeNotifyTransport : INotifyTransport
{
    private static readonly Regex ChannelName = new("^tool_request_[a-z0-9_]+$", RegexOptions.Compiled);
    private readonly string _root;
    private readonly Dictionary<string, string> _seen = new(StringComparer.Ordinal);
    private readonly SemaphoreSlim _signal = new(0, 1);
    private FileSystemWatcher? _watcher;
    private bool _disposed;
    private sealed record Hint(string Id, string Channel, string Payload);
    public NativeNotifyTransport(string root)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(root);
        if (!Path.IsPathFullyQualified(root)) throw new ArgumentException("NOTIFY_ROOT_MUST_BE_ABSOLUTE", nameof(root));
        _root = Path.GetFullPath(root);
    }
    public Task OpenAsync(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        ObjectDisposedException.ThrowIf(_disposed, this);
        if (_watcher is not null) throw new InvalidOperationException("NOTIFY_ALREADY_OPEN");
        Directory.CreateDirectory(_root);
        _watcher = new FileSystemWatcher(_root, "tool_request_*.json")
        { NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite };
        _watcher.Changed += (_, _) => Signal();
        _watcher.Created += (_, _) => Signal();
        _watcher.Renamed += (_, _) => Signal();
        _watcher.Error += (_, _) => Signal();
        _watcher.EnableRaisingEvents = true;
        return Task.CompletedTask;
    }
    public Task ListenAsync(string channel, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested(); EnsureOpen(); ValidateChannel(channel);
        if (!_seen.ContainsKey(channel)) _seen[channel] = Read(channel)?.Id ?? "";
        return Task.CompletedTask;
    }
    public async Task<NotifyMessage?> WaitAsync(TimeSpan slice, CancellationToken cancellationToken)
    {
        EnsureOpen(); cancellationToken.ThrowIfCancellationRequested();
        var next = Scan();
        if (next is not null) return next;
        await _signal.WaitAsync(slice, cancellationToken).ConfigureAwait(false);
        cancellationToken.ThrowIfCancellationRequested();
        return Scan();
    }
    private NotifyMessage? Scan()
    {
        foreach (var channel in _seen.Keys.ToArray())
        {
            var hint = Read(channel);
            if (hint is null || hint.Id == _seen[channel]) continue;
            _seen[channel] = hint.Id;
            return new NotifyMessage(channel, hint.Payload);
        }
        return null;
    }
    private Hint? Read(string channel)
    {
        try
        {
            using var stream = new FileStream(Path.Combine(_root, channel + ".json"), FileMode.Open,
                FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            if (stream.Length > 16384) throw new InvalidDataException("NOTIFY_HINT_TOO_LARGE");
            var hint = JsonSerializer.Deserialize<Hint>(stream);
            if (hint is null || !Guid.TryParseExact(hint.Id, "N", out _) || hint.Channel != channel || hint.Payload is null)
                throw new InvalidDataException("NOTIFY_HINT_INVALID");
            return hint;
        }
        catch (FileNotFoundException) { return null; }
    }
    // Atomic publication broadcasts to independent subscribers; bursts may coalesce.
    // Periodic request polling remains mandatory and covers any missed hint.
    public static async Task PublishAsync(string root, string channel, string payload,
        CancellationToken cancellationToken = default)
    {
        ValidateChannel(channel); ArgumentException.ThrowIfNullOrWhiteSpace(root);
        if (!Path.IsPathFullyQualified(root)) throw new ArgumentException("NOTIFY_ROOT_MUST_BE_ABSOLUTE", nameof(root));
        ArgumentNullException.ThrowIfNull(payload);
        var bytes = JsonSerializer.SerializeToUtf8Bytes(new Hint(Guid.NewGuid().ToString("N"), channel, payload));
        if (bytes.Length > 16384) throw new ArgumentException("NOTIFY_HINT_TOO_LARGE", nameof(payload));
        var directory = Path.GetFullPath(root); Directory.CreateDirectory(directory);
        var temporary = Path.Combine(directory, ".hint-" + Guid.NewGuid().ToString("N"));
        try
        {
            await File.WriteAllBytesAsync(temporary, bytes, cancellationToken).ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            File.Move(temporary, Path.Combine(directory, channel + ".json"), overwrite: true);
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }
    private static void ValidateChannel(string channel)
    {
        if (channel is null || !ChannelName.IsMatch(channel)) throw new ArgumentException("INVALID_NOTIFY_CHANNEL", nameof(channel));
    }
    private void EnsureOpen()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        if (_watcher is null) throw new InvalidOperationException("NOTIFY_TRANSPORT_NOT_OPEN");
    }
    private void Signal()
    {
        try { _signal.Release(); }
        catch (SemaphoreFullException) { }
        catch (ObjectDisposedException) { }
    }
    public ValueTask DisposeAsync()
    {
        _disposed = true; _watcher?.Dispose(); _watcher = null; _signal.Dispose();
        return ValueTask.CompletedTask;
    }
}
