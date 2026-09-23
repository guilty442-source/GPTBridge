using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace StarBusinessLogic.Infrastructure;

// Governed IPC channel client (main-system command surface).
//
//   ws://127.0.0.1:<port>/?token=<session-token>&instance=<workspace-instance-id>
//
// Mirrors ipc/server_tokens.py:
//   * token precedence: GPTBRIDGE_IPC_SESSION_TOKEN env (64-hex) →
//     $GPTBRIDGE_IPC_STATE_ROOT/session-token → %LOCALAPPDATA%/GPTBridge/ipc/session-token
//   * instance id: sha256(normcase(abspath(project_root)) with '/' separators)[:24]
//   * port: GPTBRIDGE_IPC_PORT env (1024-65535) else 8765
//
// Wire contract: client sends {"command": ..., "payload": {...}}; the server
// emits {"event": "<command>_result", "payload": {...}} and heartbeat_ping
// events which must be answered with {"command": "heartbeat_pong"} (20 s
// pong timeout server-side).
//
// Fail-closed: discovery/handshake/protocol failures throw; callers surface
// the error instead of fabricating a result.  Loopback only.
public static class GovernedIpcDiscovery
{
    public const int DefaultPort = 8765;
    private static readonly System.Text.RegularExpressions.Regex TokenPattern =
        new("^[a-f0-9]{64}$", System.Text.RegularExpressions.RegexOptions.Compiled);

    public static string SessionToken()
    {
        var configured = (Environment.GetEnvironmentVariable("GPTBRIDGE_IPC_SESSION_TOKEN") ?? "").Trim().ToLowerInvariant();
        if (TokenPattern.IsMatch(configured)) return configured;

        var stateRoot = (Environment.GetEnvironmentVariable("GPTBRIDGE_IPC_STATE_ROOT") ?? "").Trim();
        string tokenPath;
        if (!string.IsNullOrEmpty(stateRoot))
        {
            tokenPath = Path.Combine(Path.GetFullPath(stateRoot), "session-token");
        }
        else
        {
            var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (string.IsNullOrEmpty(localAppData))
                localAppData = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "AppData", "Local");
            tokenPath = Path.Combine(localAppData, "GPTBridge", "ipc", "session-token");
        }
        string token;
        try { token = File.ReadAllText(tokenPath).Trim().ToLowerInvariant(); }
        catch (IOException ex) { throw new InvalidOperationException("IPC_SESSION_TOKEN_UNAVAILABLE", ex); }
        catch (UnauthorizedAccessException ex) { throw new InvalidOperationException("IPC_SESSION_TOKEN_UNAVAILABLE", ex); }
        if (!TokenPattern.IsMatch(token))
            throw new InvalidOperationException("IPC_SESSION_TOKEN_INVALID");
        return token;
    }

    public static string WorkspaceInstanceId(string projectRoot)
    {
        if (string.IsNullOrWhiteSpace(projectRoot)) throw new ArgumentException("PROJECT_ROOT_REQUIRED", nameof(projectRoot));
        // Python: os.path.normcase(str(Path(root).expanduser().absolute())).replace("\\", "/")
        // Windows normcase = lowercase + '\\' separators; .absolute() does not
        // collapse '..' — GetFullPath is the closest managed equivalent and is
        // safe for the normalized roots this deployment passes in.
        var normalized = Path.GetFullPath(projectRoot).Replace('\\', '/').TrimEnd('/').ToLowerInvariant();
        var hash = SHA256.HashData(Encoding.UTF8.GetBytes(normalized));
        return Convert.ToHexString(hash).ToLowerInvariant()[..24];
    }

    public static int Port()
    {
        var configured = (Environment.GetEnvironmentVariable("GPTBRIDGE_IPC_PORT") ?? "").Trim();
        if (string.IsNullOrEmpty(configured)) return DefaultPort;
        if (!int.TryParse(configured, out var port) || port < 1024 || port > 65535)
            throw new InvalidOperationException("IPC_PORT_INVALID");
        return port;
    }

    public static Uri Endpoint(string projectRoot)
    {
        var token = SessionToken();
        var instance = WorkspaceInstanceId(projectRoot);
        return new Uri($"ws://127.0.0.1:{Port()}/?token={token}&instance={instance}");
    }

    /// <summary>Derive the project root for a standalone tool layout.</summary>
    public static string ResolveProjectRoot(string? explicitRoot, string? toolRoot)
    {
        var env = (explicitRoot ?? Environment.GetEnvironmentVariable("GPTBRIDGE_PROJECT_ROOT") ?? "").Trim();
        if (!string.IsNullOrEmpty(env)) return Path.GetFullPath(env);
        if (string.IsNullOrWhiteSpace(toolRoot)) throw new InvalidOperationException("PROJECT_ROOT_UNAVAILABLE");
        var tool = Path.GetFullPath(toolRoot);
        // <root>/Standalone tools/<tool> → two levels up is the project root.
        if (string.Equals(Path.GetFileName(Path.GetDirectoryName(tool.TrimEnd(Path.DirectorySeparatorChar))), "Standalone tools", StringComparison.OrdinalIgnoreCase))
            return Path.GetFullPath(Path.Combine(tool, "..", ".."));
        return tool;
    }
}

/// <summary>Minimal text-frame transport so the protocol loop is testable.</summary>
public interface IIpcTransport : IAsyncDisposable
{
    Task ConnectAsync(Uri uri, CancellationToken cancellationToken);
    Task SendTextAsync(string text, CancellationToken cancellationToken);
    /// <summary>Next text message, or null when the peer closed the socket.</summary>
    Task<string?> ReceiveTextAsync(CancellationToken cancellationToken);
    Task CloseAsync(CancellationToken cancellationToken);
}

public sealed class WebSocketIpcTransport : IIpcTransport
{
    private readonly ClientWebSocket _ws = new();

    public async Task ConnectAsync(Uri uri, CancellationToken cancellationToken)
    {
        if (uri.Scheme != Uri.UriSchemeWs || uri.Host is not ("127.0.0.1" or "localhost" or "::1"))
            throw new InvalidOperationException("IPC_ENDPOINT_MUST_BE_LOOPBACK");
        await _ws.ConnectAsync(uri, cancellationToken).ConfigureAwait(false);
    }

    public async Task SendTextAsync(string text, CancellationToken cancellationToken)
    {
        var bytes = Encoding.UTF8.GetBytes(text);
        await _ws.SendAsync(bytes, WebSocketMessageType.Text, endOfMessage: true, cancellationToken).ConfigureAwait(false);
    }

    public async Task<string?> ReceiveTextAsync(CancellationToken cancellationToken)
    {
        var buffer = new byte[64 * 1024];
        var stream = new MemoryStream();
        WebSocketReceiveResult result;
        do
        {
            result = await _ws.ReceiveAsync(buffer, cancellationToken).ConfigureAwait(false);
            if (result.MessageType == WebSocketMessageType.Close) return null;
            if (result.MessageType == WebSocketMessageType.Binary)
                throw new InvalidOperationException("IPC_BINARY_FRAME_UNSUPPORTED");
            stream.Write(buffer, 0, result.Count);
            if (stream.Length > 1024 * 1024)
                throw new InvalidOperationException("IPC_MESSAGE_TOO_LARGE");
        } while (!result.EndOfMessage);
        return Encoding.UTF8.GetString(stream.ToArray());
    }

    public async Task CloseAsync(CancellationToken cancellationToken)
    {
        if (_ws.State is WebSocketState.Open or WebSocketState.CloseReceived)
        {
            try { await _ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "done", cancellationToken).ConfigureAwait(false); }
            catch (WebSocketException) { /* peer already gone */ }
            catch (OperationCanceledException) { /* bounded close elapsed */ }
        }
    }

    public async ValueTask DisposeAsync()
    {
        _ws.Dispose();
        await ValueTask.CompletedTask;
    }
}

public sealed class GovernedIpcClient
{
    private static readonly TimeSpan DefaultCommandTimeout = TimeSpan.FromSeconds(30);
    private readonly Uri _endpoint;
    private readonly Func<IIpcTransport> _transportFactory;

    public GovernedIpcClient(Uri endpoint, Func<IIpcTransport>? transportFactory = null)
    {
        _endpoint = endpoint;
        _transportFactory = transportFactory ?? (() => new WebSocketIpcTransport());
    }

    /// <summary>Send one governed command and await its &lt;command&gt;_result payload.</summary>
    public async Task<JsonElement> ExecuteAsync(
        string command,
        IReadOnlyDictionary<string, object?> payload,
        CancellationToken cancellationToken = default,
        TimeSpan? timeout = null)
    {
        if (string.IsNullOrWhiteSpace(command)) throw new ArgumentException("IPC_COMMAND_REQUIRED", nameof(command));
        var resultEvent = command + "_result";
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        linked.CancelAfter(timeout ?? DefaultCommandTimeout);

        await using var transport = _transportFactory();
        await transport.ConnectAsync(_endpoint, linked.Token).ConfigureAwait(false);

        var outbound = JsonSerializer.Serialize(new { command, payload });
        await transport.SendTextAsync(outbound, linked.Token).ConfigureAwait(false);

        while (true)
        {
            string? raw;
            try { raw = await transport.ReceiveTextAsync(linked.Token).ConfigureAwait(false); }
            catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
            {
                throw new InvalidOperationException($"IPC_RESULT_TIMEOUT:{command}");
            }
            if (raw is null) throw new InvalidOperationException($"IPC_CONNECTION_CLOSED:{command}");

            JsonDocument doc;
            try { doc = JsonDocument.Parse(raw); }
            catch (JsonException ex) { throw new InvalidOperationException("IPC_MESSAGE_INVALID", ex); }
            using (doc)
            {
                var evt = doc.RootElement.TryGetProperty("event", out var e) ? e.GetString() : null;
                if (evt == "heartbeat_ping")
                {
                    await transport.SendTextAsync("{\"command\":\"heartbeat_pong\",\"payload\":{}}", linked.Token).ConfigureAwait(false);
                    continue;
                }
                if (evt == resultEvent)
                {
                    return doc.RootElement.TryGetProperty("payload", out var p) ? p.Clone() : JsonDocument.Parse("{}").RootElement.Clone();
                }
                // COMMAND_RECEIVED, runtime_status_push, unrelated results — ignore.
            }
        }
    }
}
