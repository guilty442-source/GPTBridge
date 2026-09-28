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
    public required string PythonExecutable { get; init; }
    /// <summary>
    /// Sidecar entry. Default is the governed transport proxy module —
    /// spawned as ``python -m governance_rule.execution.tool_runtime.
    /// transport_proxy`` so its relative imports resolve (spec P2). The
    /// GPTBRIDGE_TOOLHOST_PROXY_ENTRY override is the wire-fixture seam
    /// used by interop smoke tests and is a plain script path.
    /// </summary>
    public string ProxyEntry =>
        string.IsNullOrWhiteSpace(
            Environment.GetEnvironmentVariable("GPTBRIDGE_TOOLHOST_PROXY_ENTRY"))
            ? "governance_rule.execution.tool_runtime.transport_proxy"
            : Environment.GetEnvironmentVariable(
                "GPTBRIDGE_TOOLHOST_PROXY_ENTRY")!.Trim();

    /// <summary>True when ProxyEntry is a module name (``-m`` form).</summary>
    public bool ProxyIsModule =>
        string.IsNullOrWhiteSpace(
            Environment.GetEnvironmentVariable("GPTBRIDGE_TOOLHOST_PROXY_ENTRY"));

    /// <summary>
    /// Sidecar import roots: project root (``governance_rule``) plus the
    /// shared-layer src tree (``shared_layer``) — mirrors the path set a
    /// governed source runtime builds in its entry module.
    /// </summary>
    public string ProxyPythonPath =>
        ProjectRoot + ";" + Path.Combine(
            ProjectRoot, "shared-layer", "src");

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
        string? declaredToolId = null)
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

        var python = Path.Combine(
            root, "main-system", ".venv", "Scripts", "python.exe");
        if (!fileExists(python))
            python = Path.Combine(
                root, "main-system", ".venv", "Scripts", "pythonw.exe");
        if (!fileExists(python))
            throw new PermissionDeniedException();

        return new GovernedEnvironment
        {
            ToolId = toolId,
            ProjectRoot = root,
            ToolRoot = toolRoot,
            SessionToken = token,
            Port = port,
            ShutdownToken = shutdownToken,
            PythonExecutable = python,
        };
    }
}
