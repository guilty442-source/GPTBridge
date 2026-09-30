using System.Text;
using System.Security.Cryptography;
using GPTBridge.ToolHost;

namespace GPTBridge.ToolHost.Tests;

public class GovernedEnvironmentTests
{
    private static string Token() => new string('a', 64);

    private static GovernedEnvironment LoadEnv(
        Dictionary<string, string> vars,
        string toolId = "vaultly",
        string root = @"E:\GPTBridge",
        string toolDir = @"E:\GPTBridge\Standalone tools\vaultly")
    {
        var files = new Dictionary<string, string>
        {
            [Path.Combine(toolDir, "manifest.json")] =
                "{\"id\":\"" + toolId + "\"}",
            [Path.Combine(root, "native", "tool_runtime", "bin",
                "proxy-sidecar.exe")] = "",
        };
        return GovernedEnvironment.Load(
            getenv: k => vars.TryGetValue(k, out var v) ? v : null,
            fileExists: p => files.ContainsKey(p),
            dirExists: p => p == root || p == toolDir,
            readFile: p => files[p]);
    }

    private static Dictionary<string, string> BaseVars(
        string toolDir = @"E:\GPTBridge\Standalone tools\vaultly")
        => new()
        {
            ["GPTBRIDGE_GOVERNANCE_PROJECT_ROOT"] = @"E:\GPTBridge",
            ["GPTBRIDGE_GOVERNED_RUNTIME_TOOL_ID"] = "vaultly",
            ["GPTBRIDGE_TOOL_DIR"] = toolDir,
            ["GPTBRIDGE_IPC_SESSION_TOKEN"] = Token(),
            ["GPTBRIDGE_IPC_PORT"] = "18233",
            ["GPTBRIDGE_SHUTDOWN_TOKEN"] = "shtok",
            ["GPTBRIDGE_TOOLHOST_PROXY_ENTRY"] =
                @"native\tool_runtime\bin\proxy-sidecar.exe",
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
        var vars = BaseVars(toolDir: @"D:\elsewhere\vaultly");
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
        Assert.Equal("vaultly", env.ToolId);
        Assert.Equal(18233, env.Port);
        // The sidecar executable is injected via env and resolves to an
        // absolute .exe path inside the project root (native lane — the
        // Python transport_proxy module default is retired, B162).
        Assert.Equal(
            Path.Combine(
                @"E:\GPTBridge", "native", "tool_runtime", "bin",
                "proxy-sidecar.exe"),
            env.SidecarExecutable);
    }

    [Fact]
    public void Load_missing_sidecar_entry_denied()
    {
        var vars = BaseVars();
        vars.Remove("GPTBRIDGE_TOOLHOST_PROXY_ENTRY");
        Assert.Throws<PermissionDeniedException>(() => LoadEnv(vars));
    }

    [Fact]
    public void Load_sidecar_outside_root_denied()
    {
        var vars = BaseVars();
        vars["GPTBRIDGE_TOOLHOST_PROXY_ENTRY"] =
            @"..\outside\proxy-sidecar.exe";
        Assert.Throws<PermissionDeniedException>(() => LoadEnv(vars));
    }

    [Fact]
    public void Load_non_exe_sidecar_denied()
    {
        var vars = BaseVars();
        vars["GPTBRIDGE_TOOLHOST_PROXY_ENTRY"] =
            @"native\tool_runtime\bin\proxy-sidecar.py";
        Assert.Throws<PermissionDeniedException>(() => LoadEnv(vars));
    }

    [Fact]
    public void WorkspaceInstanceId_matches_spec_formula()
    {
        // Spec: sha256(normcase(str(root)).replace("\\","/"))[:24]
        // normcase on Windows lowercases; root "E:\GPTBridge" → "e:/gptbridge"
        var expected = Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes("e:/gptbridge")))
            .ToLowerInvariant()[..24];
        var env = LoadEnv(BaseVars());
        Assert.Equal(expected, env.WorkspaceInstanceId());
    }
}
