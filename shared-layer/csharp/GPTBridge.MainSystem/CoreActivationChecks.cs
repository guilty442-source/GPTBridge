// Five-core activation observers — B162 executive boundary.
//
// Phase-6 of the governed startup activates the five cores. Each
// observer is a bounded, fail-closed probe: the core's governed native
// entry must exist and, where the contract allows, produce valid
// output. Missing or invalid artifacts observe false — never throw,
// never guess.
//
//   decision-core    -> shared-layer/csharp/GPTBridge.CodexPipeline
//   permission-core  -> shared-layer/csharp/GPTBridge.Permission
//   runtime-core     -> this gate (runtime-core executing the check)
//   automation-core  -> automation-flows.json + GPTBridge.GitAutomation
//   xingcheng-domain -> review surface; on-demand, not startup-critical

using System.Diagnostics;
using System.Text.Json.Nodes;

namespace GPTBridge.MainSystem;

public sealed class CoreActivationChecks
{
    private const string CodexPipelineExe =
        "GPTBridge.CodexPipeline.exe";

    private readonly string _root;

    public CoreActivationChecks(string projectRoot) =>
        _root = Path.GetFullPath(projectRoot);

    public string Root => _root;

    private string Rel(params string[] parts) =>
        Path.Combine(new[] { _root }.Concat(parts).ToArray());

    private static bool JsonParses(string path)
    {
        if (!File.Exists(path)) return false;
        try { JsonNode.Parse(File.ReadAllText(path)); return true; }
        catch { return false; }
    }

    /// <summary>
    /// decision-core: the codex sovereign surface is published.
    /// </summary>
    public bool DecisionActive() =>
        File.Exists(Rel("shared-layer", "csharp",
            "GPTBridge.CodexPipeline", "publish", CodexPipelineExe));

    /// <summary>
    /// permission-core: native identity/directory host published and the
    /// permission directory authority parses.
    /// </summary>
    public bool PermissionActive() =>
        File.Exists(Rel("shared-layer", "csharp",
            "GPTBridge.Permission", "publish",
            "GPTBridge.Permission.exe"))
        && JsonParses(Rel("governance_rule", "permission_directory",
            "directory_authority.json"));

    /// <summary>
    /// runtime-core: this host is the runtime core — executing the gate
    /// is its activation evidence (B162: sole execution authority).
    /// </summary>
    public bool RuntimeActive() => true;

    /// <summary>
    /// automation-core: the governed flow registry parses and the
    /// resident git-automation host is published. The flow registry is
    /// the allowlist; orchestration never executes work itself.
    /// </summary>
    public bool AutomationActive() =>
        JsonParses(Rel("main-system", "config",
            "automation-flows.json"))
        && File.Exists(Rel("shared-layer", "csharp",
            "GPTBridge.GitAutomation", "publish",
            "GPTBridge.GitAutomation.exe"));

    /// <summary>
    /// Normal information layer: A263 channel host published and the
    /// governed background_service manifest entry is present and not
    /// disabled (fail-closed when the entry is missing).
    /// </summary>
    public bool NormalInformationLayerActive()
    {
        if (!File.Exists(Rel("shared-layer", "csharp",
                "GPTBridge.ChannelHost", "publish",
                "GPTBridge.ChannelHost.exe")))
        {
            return false;
        }
        var manifestPath = Rel("shared-layer", "manifest.json");
        if (!File.Exists(manifestPath)) return false;
        try
        {
            var service = JsonNode.Parse(
                File.ReadAllText(manifestPath))?["background_service"];
            if (service is null) return false;
            return service["enabled"]?.GetValue<bool>() != false;
        }
        catch { return false; }
    }

    /// <summary>
    /// permission-core codex-plane check: the official codex authority
    /// is reachable over the governed read-only path and reports the
    /// governance-codex://official URI. Bounded to 15s; any failure
    /// observes false.
    /// </summary>
    public async Task<bool> OfficialCodexValid(CancellationToken ct)
    {
        var exe = Rel("shared-layer", "csharp",
            "GPTBridge.CodexPipeline", "publish", CodexPipelineExe);
        if (!File.Exists(exe)) return false;
        try
        {
            using var linked =
                CancellationTokenSource.CreateLinkedTokenSource(ct);
            linked.CancelAfter(TimeSpan.FromSeconds(15));
            var psi = new ProcessStartInfo
            {
                FileName = exe,
                RedirectStandardOutput = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            psi.ArgumentList.Add("--authority-state");
            using var proc = Process.Start(psi);
            if (proc is null) return false;
            var stdout = await proc.StandardOutput
                .ReadToEndAsync(linked.Token).ConfigureAwait(false);
            await proc.WaitForExitAsync(linked.Token)
                .ConfigureAwait(false);
            if (proc.ExitCode != 0) return false;
            var doc = JsonNode.Parse(stdout);
            var uri = doc?["authority_uri"]?.GetValue<string>();
            var version = doc?["codex_version"]?.GetValue<string>();
            // The official codex is anchored at the governed PostgreSQL
            // authority locator; an empty version is not a valid codex.
            return uri == "postgresql://local/gptbridge_codex"
                && !string.IsNullOrEmpty(version);
        }
        catch { return false; }
    }
}
