// ModuleSensitivity.cs — §21/§22 future-training metadata.
//
//   ModuleSensitivityRegistry  per-module signal/noise/capability
//                              affinity ledger for a future targeted
//                              fine-tuning study (Storm lesson).
//                              Metadata only — no freeze-percentage,
//                              no spectrum FT, no weight update may
//                              flow from it while the freeze holds.
//   MODEL_MERGE_ENABLED = false  SLERP/TIES/DARE/weight-average can
//                              never produce an active model — the
//                              generation lineage + checkpoint
//                              integrity contracts supersede merges.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class ModuleSensitivity
{
    public const string Format = "star-module-sensitivity/v1";
    public const string RelDir =
        "xingcheng/runtime/state/module-sensitivity";
    public const bool ModelMergeEnabled = false;   // §22 — hard gate

    public static readonly string[] Affinities =
        { "language", "reasoning", "coding", "tool", "rag",
          "persona", "creative", "vision", "grounding" };

    private static string Dir(string toolRoot)
        => Path.Combine(toolRoot,
                        RelDir.Replace('/', Path.DirectorySeparatorChar));

    /// <summary>Record a sensitivity observation for a module/layer.
    /// Frozen-phase safe: the record describes a module, it can never
    /// select weights to update.</summary>
    public static Dictionary<string, object?> Record(
        string toolRoot, JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object ||
            !el.TryGetProperty("module_id", out var mid) ||
            !el.TryGetProperty("layer_id", out var lid))
            throw new ExecutorError("MODULE_SENSITIVITY_INVALID",
                "needs module_id + layer_id");
        var rec = new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["module_id"] = mid.GetString(),
            ["layer_id"] = lid.GetInt64(),
            ["signal_strength"] =
                el.TryGetProperty("signal_strength", out var ss)
                    ? ss.GetDouble() : 0.0,
            ["noise_level"] =
                el.TryGetProperty("noise_level", out var nl)
                    ? nl.GetDouble() : 0.0,
            ["capability_affinity"] =
                el.TryGetProperty("capability_affinity", out var ca)
                    ? ca.GetString() : "",
            ["training_history"] =
                el.TryGetProperty("training_history", out var th)
                    ? ModelLifecycle.Decode(th) : new List<object?>(),
            ["regression_history"] =
                el.TryGetProperty("regression_history", out var rh)
                    ? ModelLifecycle.Decode(rh) : new List<object?>(),
            ["training_effect"] = "metadata_only",
            ["freeze_note"] = "CAPABILITY_TRAINING_FROZEN",
        };
        string affinity = (string)rec["capability_affinity"]!;
        if (affinity.Length > 0 && !Affinities.Contains(affinity))
            throw new ExecutorError("MODULE_SENSITIVITY_INVALID",
                $"bad capability_affinity {affinity}");
        Directory.CreateDirectory(Dir(toolRoot));
        ModelLifecycle.AtomicWrite(
            Path.Combine(Dir(toolRoot),
                (string)rec["module_id"]! + ".json"),
            CanonicalJson.PrettyDict(rec) + "\n");
        return rec;
    }

    /// <summary>§22 merge gate — every merge request fails closed.</summary>
    public static Dictionary<string, object?> MergeRequest(
        JsonElement el)
    {
        string method = el.ValueKind == JsonValueKind.Object &&
                        el.TryGetProperty("method", out var m)
            ? m.GetString() ?? "" : "";
        throw new ExecutorError("MODEL_MERGE_DISABLED",
            $"merge method {method} rejected: " +
            "generation lineage + checkpoint integrity supersede " +
            "SLERP/TIES/DARE/weight-average merges; only an " +
            "EXPERIMENTAL candidate generation with full certify may " +
            "study this later");
    }
}
