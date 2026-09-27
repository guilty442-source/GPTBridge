using System.Text;
using System.Security.Cryptography;
using GPTBridge.ToolHost;

namespace GPTBridge.ToolHost.Tests;

public class GovernedEnvironmentTests
{
    private static string Token() => new string('a', 64);

    private static GovernedEnvironment LoadEnv(
        Dictionary<string, string> vars,
        string toolId = "system-rescue",
        string root = @"E:\GPTBridge",
        string toolDir = @"E:\GPTBridge\Standalone tools\system-rescue")
    {
        var files = new Dictionary<string, string>
        {
            [Path.Combine(toolDir, "manifest.json")] =
                "{\"id\":\"" + toolId + "\"}",
            [Path.Combine(root, "main-system", ".venv", "Scripts",
                "python.exe")] = "",
        };
        return GovernedEnvironment.Load(
            getenv: k => vars.TryGetValue(k, out var v) ? v : null,
            fileExists: p => files.ContainsKey(p),
            dirExists: p => p == root || p == toolDir,
            readFile: p => files[p]);
    }

    private static Dictionary<string, string> BaseVars(
        string toolDir = @"E:\GPTBridge\Standalone tools\system-rescue")
        => new()
        {
            ["GPTBRIDGE_GOVERNANCE_PROJECT_ROOT"] = @"E:\GPTBridge",
            ["GPTBRIDGE_GOVERNED_RUNTIME_TOOL_ID"] = "system-rescue",
            ["GPTBRIDGE_TOOL_DIR"] = toolDir,
            ["GPTBRIDGE_IPC_SESSION_TOKEN"] = Token(),
            ["GPTBRIDGE_IPC_PORT"] = "18233",
            ["GPTBRIDGE_SHUTDOWN_TOKEN"] = "shtok",
        };

    [Fact]
    public void Load_missing_root_env_denied()
    {
        Assert.Throws<PermissionDeniedException>(
            () => GovernedEnvironment.Load(getenv: _ => null));
    }

    [Fact]
    public void Load_bad_token_denied()
    {
        var vars = BaseVars();
        vars["GPTBRIDGE_IPC_SESSION_TOKEN"] = "not-hex";
        Assert.Throws<PermissionDeniedException>(() => LoadEnv(vars));
    }

    [Fact]
    public void Load_bad_port_denied()
    {
        var vars = BaseVars();
        vars["GPTBRIDGE_IPC_PORT"] = "80";
        Assert.Throws<PermissionDeniedException>(() => LoadEnv(vars));
    }

    [Fact]
    public void Load_tool_root_outside_root_denied()
    {
        var vars = BaseVars(toolDir: @"D:\elsewhere\system-rescue");
        Assert.Throws<PermissionDeniedException>(() => LoadEnv(vars));
    }

    [Fact]
    public void Load_manifest_id_mismatch_denied()
    {
        var vars = BaseVars();
        vars["GPTBRIDGE_GOVERNED_RUNTIME_TOOL_ID"] = "other-tool";
        Assert.Throws<PermissionDeniedException>(() => LoadEnv(vars));
    }

    [Fact]
    public void Load_happy_path()
    {
        var env = LoadEnv(BaseVars());
        Assert.Equal("system-rescue", env.ToolId);
        Assert.Equal(18233, env.Port);
        Assert.EndsWith("transport_proxy.py", env.ProxyEntry);
    }

    [Fact]
    public void WorkspaceInstanceId_matches_python_formula()
    {
        // Python: sha256(normcase(str(root)).replace("\\","/"))[:24]
        // normcase on Windows lowercases; root "E:\GPTBridge" → "e:/gptbridge"
        var expected = Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes("e:/gptbridge")))
            .ToLowerInvariant()[..24];
        var env = LoadEnv(BaseVars());
        Assert.Equal(expected, env.WorkspaceInstanceId());
    }
}
