using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

internal static class SnapshotEvidence
{
    public static string Require(GitResult result)
    {
        if (result.Code != 0 || result.TimedOut)
            throw new InvalidOperationException(result.TimedOut
                ? "GIT_SNAPSHOT_TIMEOUT" : $"GIT_SNAPSHOT_FAILED:{result.Code}");
        return result.Stdout;
    }

    public static JsonArray Names(GitResult result)
    {
        var array = new JsonArray();
        foreach (var line in Require(result).Split('\n'))
            if (line.Trim().Length > 0) array.Add(line.Trim());
        return array;
    }
}
