// Governed environment contract for native tool hosts.
// Mirrors GovernedToolRuntime.workspace_root/_init_token_and_port: every
// failure is fail-closed PERMISSION_DENIED. The host NEVER reads or
// forwards GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP beyond inheriting it into
// the transport-proxy sidecar environment (E4 boundary).
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace GPTBridge.ToolHost;

public sealed class PermissionDeniedException : Exception
{
    public PermissionDeniedException() : base("PERMISSION_DENIED") { }
}

public sealed class GovernedEnvironment
{
    private static readonly Regex TokenPattern = new(
        "^[a-f0-9]{64}$", RegexOptions.Compiled);
    private static readonly Regex ToolIdPattern = new(
        "^[a-z0-9][a-z0-9_-]{1,63}$", RegexOptions.Compiled);

    public required string ToolId { get; init; }
    public required string ProjectRoot { get; init; }
    public required string ToolRoot { get; init; }
    public required string SessionToken { get; init; }
    public required int Port { get; init; }
    public required string ShutdownToken { get; init; }
    /// <summary>
    /// Resolved native sidecar executable speaking
    /// star-governed-transport-proxy/v1 over stdio. The Python
    /// transport_proxy module lane is retired (B162/B167/B38); the
    /// executable is injected via GPTBRIDGE_TOOLHOST_PROXY_ENTRY, mirroring
    /// the native tool_host config.proxy_command_line seam — absent or
    /// invalid values fail closed with PERMISSION_DENIED in Load() unless
    /// the caller opted into the deferred-transport mode.
    /// </summary>
    public required string SidecarExecutable { get; init; }

    /// <summary>True when Load() ran with <c>sidecarOptional</c> and no
    /// proxy entry was injected: the host must run a
    /// <see cref="DeferredStoreTransport"/> claim lane (the Go lane's
    /// govenv.go optional-sidecar convention) instead of spawning a
    /// sidecar. SidecarExecutable is then bound to the host exe itself
    /// so the "sidecar is an exe inside the root" invariant still holds
    /// for whatever reads the env record.</summary>
    public bool SidecarDeferred { get; init; }

    public string WorkspaceInstanceId()
    {
        // Python: sha256(normcase(str(root)).replace("\\","/"))[:24]
        var normalized = ProjectRoot.Replace('\\', '/').ToLowerInvariant();
        return Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(normalized)))
            .ToLowerInvariant()[..24];
    }

    public static GovernedEnvironment Load(
        Func<string, string?>? getenv = null,
        Func<string, bool>? fileExists = null,
        Func<string, bool>? dirExists = null,
        Func<string, string>? readFile = null,
        string? declaredToolId = null,
        bool sidecarOptional = false)
    {
        getenv ??= Environment.GetEnvironmentVariable;
        fileExists ??= File.Exists;
        dirExists ??= Directory.Exists;
        readFile ??= File.ReadAllText;

        var rawRoot = (getenv("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT") ?? "").Trim();
        if (rawRoot.Length == 0)
            throw new PermissionDeniedException();
        var root = Path.GetFullPath(rawRoot);
        if (!dirExists(root))
            throw new PermissionDeniedException();

        var toolId = (getenv("GPTBRIDGE_GOVERNED_RUNTIME_TOOL_ID")
            ?? declaredToolId ?? "").Trim();
        if (ToolIdPattern.IsMatch(toolId) is false || toolId == "main-system")
            throw new PermissionDeniedException();

        var rawToolDir = (getenv("GPTBRIDGE_TOOL_DIR") ?? "").Trim();
        var toolRoot = Path.GetFullPath(
            rawToolDir.Length > 0 ? rawToolDir : Path.Combine(root, toolId));
        // tool_root must be inside project root.
        var relative = Path.GetRelativePath(root, toolRoot);
        if (relative == ".." || relative.StartsWith(".." + Path.DirectorySeparatorChar)
            || Path.IsPathRooted(relative))
            throw new PermissionDeniedException();
        // manifest.json must exist and id must match (sealed-identity binding
        // is enforced again inside the sidecar by load_authentication).
        var manifestPath = Path.Combine(toolRoot, "manifest.json");
        if (!fileExists(manifestPath))
            throw new PermissionDeniedException();
        try
        {
            using var manifest = JsonDocument.Parse(readFile(manifestPath));
            var manifestId = manifest.RootElement
                .TryGetProperty("id", out var idEl) && idEl.ValueKind == JsonValueKind.String
                ? idEl.GetString() : null;
            if (manifestId != toolId)
                throw new PermissionDeniedException();
        }
        catch (JsonException)
        {
            throw new PermissionDeniedException();
        }

        var token = (getenv("GPTBRIDGE_IPC_SESSION_TOKEN") ?? "")
            .Trim().ToLowerInvariant();
        if (!TokenPattern.IsMatch(token))
            throw new PermissionDeniedException();
        if (!int.TryParse((getenv("GPTBRIDGE_IPC_PORT") ?? "").Trim(),
                out var port) || port < 1024 || port > 65535)
            throw new PermissionDeniedException();
        var shutdownToken = (getenv("GPTBRIDGE_SHUTDOWN_TOKEN") ?? "").Trim();

        // Native sidecar (star-governed-transport-proxy/v1 over stdio) is
        // injected via env — no implicit default exists now that the
        // Python transport_proxy module is retired. When the caller marks
        // the sidecar optional (the Go lane's deferred-claim convention)
        // an absent entry yields a deferred environment whose transport
        // is the store-less DeferredStoreTransport; a present-but-invalid
        // entry still fails closed.
        var rawEntry = (getenv("GPTBRIDGE_TOOLHOST_PROXY_ENTRY") ?? "").Trim();
        var sidecarDeferred = false;
        string sidecar;
        if (rawEntry.Length == 0)
        {
            if (!sidecarOptional)
                throw new PermissionDeniedException();
            sidecarDeferred = true;
            sidecar = Environment.ProcessPath ?? string.Empty;
            if (sidecar.Length == 0)
                throw new PermissionDeniedException();
        }
        else
        {
            sidecar = Path.GetFullPath(
                Path.IsPathRooted(rawEntry)
                    ? rawEntry : Path.Combine(root, rawEntry));
            var sidecarRel = Path.GetRelativePath(root, sidecar);
            if (sidecarRel == ".." || sidecarRel.StartsWith(".." + Path.DirectorySeparatorChar)
                || Path.IsPathRooted(sidecarRel)
                || !string.Equals(Path.GetExtension(sidecar), ".exe",
                    StringComparison.OrdinalIgnoreCase)
                || !fileExists(sidecar))
                throw new PermissionDeniedException();
        }

        return new GovernedEnvironment
        {
            ToolId = toolId,
            ProjectRoot = root,
            ToolRoot = toolRoot,
            SessionToken = token,
            Port = port,
            ShutdownToken = shutdownToken,
            SidecarExecutable = sidecar,
            SidecarDeferred = sidecarDeferred,
        };
    }
}
