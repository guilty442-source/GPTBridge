// Collectors.cs — role-database source collectors.
//
// Ports of self_learning.collect_verified_examples /
// collect_preference_pairs — now served from the native metadata plane
// (§54-§55: the per-scope role corpora are `role_training_example` /
// `role_preference_pair` records in xstore; PostgreSQL is no longer a
// runtime dependency). Scope failures skip the scope rather than
// aborting the collection.

namespace GPTBridge.XingchengLearning;

internal static class Collectors
{
    /// <summary>active=1 AND quality_score>=min_quality examples, by scope.</summary>
    public static Dictionary<string, List<Dictionary<string, object?>>>
        CollectVerifiedExamples(string toolRoot, double minQuality = XcPaths.MinQuality)
    {
        var byScope = new Dictionary<string, List<Dictionary<string, object?>>>(
            StringComparer.Ordinal);
        NativeMetadataClient meta;
        try { meta = new NativeMetadataClient(toolRoot); }
        catch (MetadataError) { return byScope; } // plane down -> skip all
        foreach (string scope in XcPaths.Scopes.OrderBy(s => s, StringComparer.Ordinal))
        {
            List<Dictionary<string, object?>> rows;
            try
            {
                rows = meta.Query(
                    NativeMetadataClient.Types.RoleExample,
                    new Dictionary<string, object?> { ["scope"] = scope },
                    500000)
                    .Where(r => TransformerTrainingRepository
                                    .Truthy(r["active"]) &&
                                TransformerTrainingRepository
                                    .Num(r, "quality_score") >= minQuality)
                    .OrderBy(r => TransformerTrainingRepository
                                      .Int64(r, "revision"))
                    .ToList();
            }
            catch (Exception)
            {
                continue; // unreadable scope -> skip, fail-closed downstream
            }
            var records = new List<Dictionary<string, object?>>();
            foreach (var row in rows)
            {
                string prompt = (row["input_text"]?.ToString() ?? "").Trim();
                string completion = (row["target_text"]?.ToString() ?? "").Trim();
                if (prompt.Length == 0 || completion.Length == 0)
                    continue;
                records.Add(new Dictionary<string, object?>
                {
                    ["revision"] = Convert.ToInt32(row["revision"] ?? 0),
                    ["example_id"] = row["example_id"]?.ToString() ?? "",
                    ["intent"] = row["intent"]?.ToString() ?? "",
                    ["input_text"] = prompt,
                    ["target_text"] = completion,
                    ["source_type"] = row["source_type"]?.ToString() ?? "",
                    ["quality_score"] = Convert.ToDouble(row["quality_score"] ?? 0.0),
                });
            }
            if (records.Count > 0)
                byScope[scope] = records;
        }
        return byScope;
    }

    /// <summary>paired=1 preference rows across all scopes.</summary>
    public static List<Dictionary<string, object?>> CollectPreferencePairs(string toolRoot)
    {
        var pairs = new List<Dictionary<string, object?>>();
        NativeMetadataClient meta;
        try { meta = new NativeMetadataClient(toolRoot); }
        catch (MetadataError) { return pairs; }
        foreach (string scope in XcPaths.Scopes.OrderBy(s => s, StringComparer.Ordinal))
        {
            List<Dictionary<string, object?>> rows;
            try
            {
                rows = meta.Query(
                    NativeMetadataClient.Types.RolePair,
                    new Dictionary<string, object?> { ["scope"] = scope },
                    500000)
                    .Where(r => TransformerTrainingRepository
                                    .Truthy(r["paired"]))
                    .OrderBy(r => TransformerTrainingRepository
                                      .Int64(r, "revision"))
                    .ToList();
            }
            catch (Exception)
            {
                continue;
            }
            foreach (var row in rows)
            {
                string prompt = (row["prompt_text"]?.ToString() ?? "").Trim();
                string chosen = (row["chosen_text"]?.ToString() ?? "").Trim();
                string rejected = (row["rejected_text"]?.ToString() ?? "").Trim();
                if (prompt.Length == 0 || chosen.Length == 0 || rejected.Length == 0)
                    continue;
                pairs.Add(new Dictionary<string, object?>
                {
                    ["revision"] = Convert.ToInt32(row["revision"] ?? 0),
                    ["pair_id"] = row["pair_id"]?.ToString() ?? "",
                    ["intent"] = row["intent"]?.ToString() ?? "",
                    ["prompt_text"] = prompt,
                    ["chosen_text"] = chosen,
                    ["rejected_text"] = rejected,
                    ["scope"] = scope,
                });
            }
        }
        pairs.Sort((a, b) =>
        {
            int byScope = string.CompareOrdinal(
                (string?)a["scope"], (string?)b["scope"]);
            return byScope != 0
                ? byScope
                : Convert.ToInt32(a["revision"]).CompareTo(
                    Convert.ToInt32(b["revision"]));
        });
        return pairs;
    }

    /// <summary>Native inference-exclusion signal: the model-service
    /// descriptor's pid is alive (engine session up). Missing descriptor =
    /// inactive; unreadable descriptor = indeterminate (fail-closed).</summary>
    public static bool? InferenceActive(string toolRoot)
    {
        string descriptorPath =
            Path.Combine(toolRoot, XcPaths.ModelServiceRel);
        if (!File.Exists(descriptorPath))
            return false;
        int pid;
        try
        {
            using var doc = System.Text.Json.JsonDocument.Parse(
                File.ReadAllText(descriptorPath));
            pid = doc.RootElement.GetProperty("pid").GetInt32();
        }
        catch (Exception)
        {
            return null; // indeterminate -> blocked upstream
        }
        try
        {
            using var proc = System.Diagnostics.Process.GetProcessById(pid);
            return !proc.HasExited;
        }
        catch (ArgumentException)
        {
            return false; // pid reused/dead -> session over (stale descriptor)
        }
        catch (Exception)
        {
            return null;
        }
    }
}
