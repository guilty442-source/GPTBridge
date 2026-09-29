// Generic governed tool host entry point (deferred transport).
//
// Role: stand in for the per-tool native entry
// (manifest.runtime.native_entry) until each tool's own executor and
// the transport-store successor land. The host validates the governed
// environment injected by the main-system backend
// (tools/env.rs::source_runtime_env), serves the governed
// /health + /metrics + /shutdown + authenticated-WS surface via
// ToolHostServer, and idles the worker loop on a deferred transport.
//
// Honesty boundary (E4): this process never mints tokens, never reads
// GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP, and never touches the
// transport store — there is no store client. Claims drain empty
// instead of fabricating a binding, and the deferred state is
// surfaced in the health snapshot so observers see the truth.
using System.Net;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using GPTBridge.ToolHost;

namespace GPTBridge.ToolHost.App;

/// <summary>
/// Store-free IToolTransport: hello reports the deferred marker, every
/// claim returns null (no queue exists yet), side ops answer false/null.
/// When the real star-governed-transport-proxy/v1 sidecar ships, swap
/// the factory to TransportProxyClient.Start and delete this class.
/// </summary>
internal sealed class DeferredStoreTransport : IToolTransport
{
    public event Action? Disconnected;
    public Task<JsonObject> HelloAsync(
        string toolId, string workspaceInstanceId,
        IReadOnlyDictionary<string, string> channels,
        CancellationToken ct = default) => Task.FromResult(new JsonObject
    {
        ["ok"] = true,
        ["deferred"] = true,
        ["reason"] = "transport-store-native-successor-pending",
    });

    public Task<JsonObject?> ClaimAsync(
        string channel, CancellationToken ct = default)
        => Task.FromResult<JsonObject?>(null);

    public Task<bool> RespondAsync(
        string channel, string requestId, JsonNode? response,
        CancellationToken ct = default) => Task.FromResult(false);

    public Task<bool> RequestCancelledAsync(
        string channel, string requestId, CancellationToken ct = default)
        => Task.FromResult(false);

    public Task<bool> ProgressAsync(
        string channel, string requestId, JsonNode? payload,
        CancellationToken ct = default) => Task.FromResult(false);

    public Task<JsonNode?> NotificationStampAsync(
        string channel, CancellationToken ct = default)
        => Task.FromResult<JsonNode?>(null);

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

/// <summary>
/// Placeholder executor: unreachable today (no store claims), reports
/// its deferred state honestly if ever invoked.
/// </summary>
internal sealed class DeferredExecutor : IGovernedCommandExecutor
{
    private readonly string _toolId;

    public DeferredExecutor(string toolId) => _toolId = toolId;

    public Task<(string Event, JsonObject Result)> ExecuteAsync(
        string command, JsonObject payload, string requestId,
        CancellationToken cancellationToken)
        => Task.FromResult(($"{command}_result", new JsonObject
        {
            ["ok"] = false,
            ["tool_id"] = _toolId,
            ["error_code"] = "TOOL_EXECUTOR_PENDING_NATIVE_PORT",
            ["message"] = "Tool business executor is pending its native "
                + "port; the governed host itself is up.",
        }));

    public JsonObject Health() => new()
    {
        ["transport_state"] = "deferred:store-successor-pending",
        ["executor_state"] = "deferred:business-port-pending",
    };
}

internal static class Program
{
    private static readonly Regex TokenPattern = new(
        "^[a-f0-9]{64}$", RegexOptions.Compiled);
    private static readonly Regex ToolIdPattern = new(
        "^[a-z0-9][a-z0-9_-]{1,63}$", RegexOptions.Compiled);

    /// <summary>
    /// Env validation mirroring GovernedEnvironment.Load against the
    /// contract the Rust backend actually injects (tools/env.rs):
    /// ToolId binds GPTBRIDGE_GOVERNED_RUNTIME_TOOL_ID (the identity the
    /// /health gate expects), while the manifest check pins
    /// GPTBRIDGE_TOOL_ID — the owner tool whose sealed manifest sits at
    /// GPTBRIDGE_TOOL_DIR. No proxy entry is required because the
    /// deferred transport spawns nothing.
    /// </summary>
    private static GovernedEnvironment LoadEnvironment(out string version)
    {
        static string Get(string key) =>
            (Environment.GetEnvironmentVariable(key) ?? "").Trim();

        var rawRoot = Get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT");
        if (rawRoot.Length == 0)
            throw new PermissionDeniedException();
        var root = Path.GetFullPath(rawRoot);
        if (!Directory.Exists(root))
            throw new PermissionDeniedException();

        var ownerId = Get("GPTBRIDGE_TOOL_ID");
        if (!ToolIdPattern.IsMatch(ownerId) || ownerId == "main-system")
            throw new PermissionDeniedException();
        var governedId = Get("GPTBRIDGE_GOVERNED_RUNTIME_TOOL_ID");
        var toolId = ToolIdPattern.IsMatch(governedId)
            && governedId != "main-system"
            ? governedId : ownerId;

        var rawToolDir = Get("GPTBRIDGE_TOOL_DIR");
        var toolRoot = Path.GetFullPath(
            rawToolDir.Length > 0 ? rawToolDir : Path.Combine(root, ownerId));
        var relative = Path.GetRelativePath(root, toolRoot);
        if (relative == ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar)
            || Path.IsPathRooted(relative))
            throw new PermissionDeniedException();

        var manifestPath = Path.Combine(toolRoot, "manifest.json");
        version = "1.0.0";
        if (!File.Exists(manifestPath))
            throw new PermissionDeniedException();
        try
        {
            using var manifest = JsonDocument.Parse(
                File.ReadAllText(manifestPath));
            var rootEl = manifest.RootElement;
            var manifestId =
                rootEl.TryGetProperty("id", out var idEl)
                && idEl.ValueKind == JsonValueKind.String
                    ? idEl.GetString() : null;
            if (manifestId != ownerId)
                throw new PermissionDeniedException();
            if (rootEl.TryGetProperty("version", out var vEl)
                && vEl.ValueKind == JsonValueKind.String
                && vEl.GetString() is { Length: > 0 } v)
                version = v;
        }
        catch (JsonException)
        {
            throw new PermissionDeniedException();
        }

        var token = Get("GPTBRIDGE_IPC_SESSION_TOKEN").ToLowerInvariant();
        if (!TokenPattern.IsMatch(token))
            throw new PermissionDeniedException();
        if (!int.TryParse(Get("GPTBRIDGE_IPC_PORT"), out var port)
            || port < 1024 || port > 65535)
            throw new PermissionDeniedException();

        return new GovernedEnvironment
        {
            ToolId = toolId,
            ProjectRoot = root,
            ToolRoot = toolRoot,
            SessionToken = token,
            Port = port,
            ShutdownToken = Get("GPTBRIDGE_SHUTDOWN_TOKEN"),
            // No transport-proxy sidecar exists yet; the deferred
            // transport spawns nothing. Bound to the host exe itself so
            // the invariant "sidecar is an exe inside the root" still
            // holds for whatever reads the env record.
            SidecarExecutable = Environment.ProcessPath ?? string.Empty,
        };
    }

    public static async Task<int> Main()
    {
        GovernedEnvironment env;
        string version;
        try
        {
            env = LoadEnvironment(out version);
        }
        catch (PermissionDeniedException)
        {
            Console.Error.WriteLine(
                "[toolhost] PERMISSION_DENIED: governed environment "
                + "validation failed");
            return 13;
        }

        await using var host = new GovernedToolHost(
            env,
            new DeferredExecutor(env.ToolId),
            version,
            transportFactory: _ => new DeferredStoreTransport(),
            processingChannels: ["system"]);
        await using var server = new ToolHostServer(host);
        try
        {
            server.Start();
        }
        catch (HttpListenerException exc)
        {
            Console.Error.WriteLine(
                $"[toolhost] listener failed: {exc.Message}");
            return 13;
        }
        await host.RunWorkerAsync().ConfigureAwait(false);
        return 0;
    }
}
