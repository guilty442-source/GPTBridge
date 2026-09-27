// star-governed-transport-proxy/v1 — P2 stdio sidecar client.
//
// Spawns `python -B -s transport_proxy.py` as a child process which
// inherits this process's governed environment (the bootstrap env is
// consumed inside the Python governance plane — it is never parsed here).
// Wire protocol: UTF-8 JSONL, one request/response per line, responses
// correlated by caller-supplied `id`. All failures surface as
// ProxyErrorException with the spec's closed error-code set.
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.ToolHost;

public sealed class TransportProxyClient : IToolTransport
{
    private const int MaxLineBytes = 2 * 1024 * 1024;

    private readonly Process _process;
    private readonly StreamWriter _stdin;
    private readonly SemaphoreSlim _writeLock = new(1, 1);
    private readonly ConcurrentDictionary<
        string, TaskCompletionSource<JsonNode?>> _pending = new();
    private readonly Task _reader;
    private int _disposed;

    public event Action? Disconnected;

    private TransportProxyClient(Process process)
    {
        _process = process;
        _stdin = process.StandardInput;
        _stdin.AutoFlush = true;
        _reader = Task.Run(ReadLoopAsync);
    }

    public static TransportProxyClient Start(GovernedEnvironment env)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = env.PythonExecutable,
            WorkingDirectory = env.ToolRoot,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardInputEncoding = new UTF8Encoding(false),
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
        };
        startInfo.ArgumentList.Add("-B");
        startInfo.ArgumentList.Add("-s");
        startInfo.ArgumentList.Add(env.ProxyEntry);
        // Inherit the governed environment verbatim — including
        // GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP, which the sidecar consumes
        // exactly like GovernedToolRuntime.load_authentication.
        var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException("sidecar spawn failed");
        // Drain stderr so the child never blocks on a full pipe.
        _ = Task.Run(async () =>
        {
            try { await process.StandardError.ReadToEndAsync(); }
            catch { /* best-effort drain */ }
        });
        return new TransportProxyClient(process);
    }

    private async Task ReadLoopAsync()
    {
        try
        {
            var stdout = _process.StandardOutput;
            string? raw;
            while ((raw = await stdout.ReadLineAsync()) is not null)
            {
                if (raw.Length == 0 || Encoding.UTF8.GetByteCount(raw) > MaxLineBytes)
                    continue;
                JsonObject? message;
                try { message = JsonNode.Parse(raw) as JsonObject; }
                catch (JsonException) { continue; }
                if (message is null)
                    continue;
                var id = message["id"]?.GetValue<string>();
                if (string.IsNullOrEmpty(id))
                    continue;
                if (!_pending.TryRemove(id, out var tcs))
                    continue;
                var ok = message["ok"]?.GetValue<bool>() == true;
                if (ok)
                {
                    tcs.TrySetResult(message["result"]?.DeepClone());
                }
                else
                {
                    var err = message["error"] as JsonObject;
                    tcs.TrySetException(new ProxyErrorException(
                        err?["code"]?.GetValue<string>() ?? "TRANSPORT_ERROR",
                        err?["message"]?.GetValue<string>() ?? ""));
                }
            }
        }
        catch (Exception)
        {
            // Fall through: EOF/IO failure both mean the sidecar is gone.
        }
        finally
        {
            var lost = new ProxyErrorException(
                "PROXY_DISCONNECTED", "transport proxy terminated");
            foreach (var pair in _pending)
            {
                if (_pending.TryRemove(pair.Key, out var tcs))
                    tcs.TrySetException(lost);
            }
            Disconnected?.Invoke();
        }
    }

    private async Task<JsonNode?> CallAsync(
        string op, JsonObject args, CancellationToken ct)
    {
        if (Volatile.Read(ref _disposed) != 0)
            throw new ProxyErrorException("PROXY_DISCONNECTED", "disposed");
        var id = Guid.NewGuid().ToString("N");
        var envelope = new JsonObject
        {
            ["v"] = 1,
            ["id"] = id,
            ["op"] = op,
            ["args"] = args,
        };
        var tcs = new TaskCompletionSource<JsonNode?>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = tcs;
        try
        {
            var line = envelope.ToJsonString() + "\n";
            await _writeLock.WaitAsync(ct).ConfigureAwait(false);
            try
            {
                await _stdin.WriteAsync(line).ConfigureAwait(false);
            }
            finally
            {
                _writeLock.Release();
            }
        }
        catch (Exception) when (ct.IsCancellationRequested)
        {
            _pending.TryRemove(id, out _);
            throw;
        }
        catch (Exception exc)
        {
            _pending.TryRemove(id, out _);
            throw new ProxyErrorException(
                "PROXY_DISCONNECTED", exc.Message);
        }
        using var registration = ct.Register(
            () => tcs.TrySetCanceled(ct));
        return await tcs.Task.ConfigureAwait(false);
    }

    public async Task<JsonObject> HelloAsync(
        string toolId,
        string workspaceInstanceId,
        IReadOnlyDictionary<string, string> channels,
        CancellationToken ct = default)
    {
        var modes = new JsonObject();
        foreach (var (channelId, mode) in channels)
            modes[channelId] = mode;
        var result = await CallAsync("hello", new JsonObject
        {
            ["tool_id"] = toolId,
            ["workspace_instance_id"] = workspaceInstanceId,
            ["channels"] = modes,
        }, ct).ConfigureAwait(false);
        return result as JsonObject
            ?? throw new ProxyErrorException("BAD_ENVELOPE", "hello result");
    }

    public async Task<JsonObject?> ClaimAsync(
        string channel, CancellationToken ct = default)
    {
        var result = await CallAsync("claim", new JsonObject
        {
            ["channel"] = channel,
        }, ct).ConfigureAwait(false);
        return (result as JsonObject)?["request"] as JsonObject;
    }

    public async Task<bool> RespondAsync(
        string channel, string requestId, JsonNode? response,
        CancellationToken ct = default)
    {
        var result = await CallAsync("respond", new JsonObject
        {
            ["channel"] = channel,
            ["request_id"] = requestId,
            ["response"] = response?.DeepClone(),
        }, ct).ConfigureAwait(false);
        return result?.GetValue<bool>() == true;
    }

    public async Task<bool> RequestCancelledAsync(
        string channel, string requestId, CancellationToken ct = default)
    {
        var result = await CallAsync("request_cancelled", new JsonObject
        {
            ["channel"] = channel,
            ["request_id"] = requestId,
        }, ct).ConfigureAwait(false);
        return result?.GetValue<bool>() == true;
    }

    public async Task<bool> ProgressAsync(
        string channel, string requestId, JsonNode? payload,
        CancellationToken ct = default)
    {
        var result = await CallAsync("progress", new JsonObject
        {
            ["channel"] = channel,
            ["request_id"] = requestId,
            ["payload"] = payload?.DeepClone(),
        }, ct).ConfigureAwait(false);
        return result?.GetValue<bool>() == true;
    }

    public async Task<JsonNode?> NotificationStampAsync(
        string channel, CancellationToken ct = default)
    {
        return await CallAsync("notification_stamp", new JsonObject
        {
            ["channel"] = channel,
        }, ct).ConfigureAwait(false);
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
            return;
        try { _process.StandardInput.Close(); }
        catch { /* already closed */ }
        try
        {
            using var waitCts = new CancellationTokenSource(
                TimeSpan.FromSeconds(5));
            await _process.WaitForExitAsync(waitCts.Token)
                .ConfigureAwait(false);
        }
        catch (Exception)
        {
            try { _process.Kill(entireProcessTree: true); }
            catch { /* best-effort */ }
        }
        try { await _reader.ConfigureAwait(false); }
        catch { /* reader faults are surfaced via Disconnected */ }
        _process.Dispose();
        _writeLock.Dispose();
    }
}
