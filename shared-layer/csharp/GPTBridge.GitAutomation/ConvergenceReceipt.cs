using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

/// <summary>
/// C50 cross-domain joined receipt (worker: devin). The governance
/// engine owns neither domain truth: this module only joins the Git
/// plane receipt (sync outcome + main head) with the SQL plane receipt
/// (authority version vs mirror parity) under one correlation identity
/// and one intended generation. Convergence is claimed only when both
/// planes agree on that generation; unknown or divergent sides stay
/// incomplete (fail-closed) and the last accepted state is preserved.
/// </summary>
internal static class ConvergenceReceipt
{
    public const string Format = "star-governance-convergence/v1";
    private const string StateRelative =
        "main-system/runtime/state/governance-convergence.json";

    public static JsonObject Join(
        string projectRoot, string gitStatus, JsonObject? sql)
    {
        var parity = sql?["parity_after"] as JsonObject
            ?? sql?["parity_before"] as JsonObject;
        var generation = parity?["sql_version"]?.GetValue<string>();
        var sqlInSync = parity?["in_sync"]?.GetValue<bool>() == true;
        var gitOk = gitStatus.StartsWith("synchronized",
            StringComparison.Ordinal);
        var converged = gitOk && sqlInSync && generation is not null;
        var reason = converged ? "converged"
            : !gitOk ? "git-plane-incomplete"
            : generation is null ? "sql-plane-unknown"
            : "generation-mismatch";
        return new JsonObject
        {
            ["format"] = Format,
            ["correlation_id"] = Guid.NewGuid().ToString("N"),
            ["intended_generation"] = generation,
            ["issued_at"] = Canon.EpochSeconds(),
            ["git_plane"] = new JsonObject
            {
                ["status"] = gitStatus,
                ["main_head"] = Git.RevParse(projectRoot, "refs/heads/main"),
            },
            ["sql_plane"] = new JsonObject
            {
                ["sql_version"] = generation,
                ["mirror_version"] = parity?["mirror_version"]?
                    .GetValue<string>(),
                ["in_sync"] = parity?["in_sync"]?.DeepClone(),
                ["refreshed"] = sql?["refreshed"]?.DeepClone(),
            },
            ["converged"] = converged,
            ["reason"] = reason,
        };
    }

    /// <summary>Persist the joined receipt. A failed converged claim
    /// never overwrites the last converged receipt (last accepted
    /// state is preserved as <c>last_converged</c>).</summary>
    public static JsonObject Record(
        string projectRoot, string gitStatus, JsonObject? sql)
    {
        var receipt = Join(projectRoot, gitStatus, sql);
        var path = Path.Combine(projectRoot,
            StateRelative.Replace('/', Path.DirectorySeparatorChar));
        try
        {
            JsonNode? lastConverged = receipt["converged"]!
                .GetValue<bool>() ? receipt.DeepClone() : null;
            if (lastConverged is null && File.Exists(path))
                lastConverged = (JsonNode.Parse(File.ReadAllText(path))
                    as JsonObject)?["last_converged"]?.DeepClone();
            var state = new JsonObject
            {
                ["latest"] = receipt.DeepClone(),
                ["last_converged"] = lastConverged,
            };
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            using var document = JsonDocument.Parse(state.ToJsonString());
            Canon.WriteJsonAtomic(
                path, Canon.Indented(document.RootElement) + "\n");
        }
        catch (IOException) { /* evidence write failure never fails sync */ }
        catch (UnauthorizedAccessException) { }
        catch (JsonException) { }
        return receipt;
    }
}
