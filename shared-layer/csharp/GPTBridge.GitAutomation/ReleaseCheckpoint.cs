using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

/// <summary>release_checkpoint parity — durable merge evidence.</summary>
internal static class ReleaseCheckpoint
{
    public static JsonObject Record(
        string projectRoot, string mainPath, string auditResult = "",
        string actor = "governance/release")
    {
        var local = Git.RevParse(mainPath, "refs/heads/main");
        var origin = Git.RevParse(mainPath, "refs/remotes/origin/main");
        var timestamp = Canon.UtcNow();
        var checkpoint = new JsonObject
        {
            ["timestamp"] = timestamp,
            ["main_sha"] = local,
            ["origin_sha"] = origin,
            ["governance_audit"] = auditResult,
            ["audit_sequence"] = null,
            ["queue_id"] = "",
            ["sync_state"] = local.Length == 0 || origin.Length == 0
                ? "MISSING_REF"
                : local == origin ? "IN_SYNC" : "LOCAL_AHEAD",
        };
        var dir = Path.Combine(Git.CommonDir(projectRoot),
            "gptbridge-automation", "releases");
        Directory.CreateDirectory(dir);
        var name = timestamp.Replace(":", "") + "-" +
                   (local.Length > 0 ? local[..12] : "none");
        var path = Path.Combine(dir, name + ".json");
        using var document = JsonDocument.Parse(checkpoint.ToJsonString());
        Canon.WriteJsonAtomic(
            path, Canon.Indented(document.RootElement));
        checkpoint["path"] = path;
        return checkpoint;
    }
}
