// DataQuality.cs — §14 + §19 dataset quality pipeline.
//
// Phi-4 lesson: data quality is a first-class governed component.
// Storm lesson: self-curation metadata (informativeness, novelty,
// difficulty, capability value, duplicate density, failure relevance)
// produces a training_priority — stored as metadata only. Nothing
// here feeds the trainer; capability training stays frozen.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class DataQuality
{
    public const string Format = "star-dataset-quality/v1";
    public const string RelDir =
        "xingcheng/runtime/state/data-quality";

    /// <summary>Pipeline stages (§14): source -> license -> sensitive
    /// -> normalize -> exact dedup -> minhash dedup -> contamination
    /// -> quality -> difficulty -> capability labels -> provenance ->
    /// accept/reject.</summary>
    public static readonly string[] Stages =
        { "source", "license_check", "sensitive_check", "normalize",
          "exact_dedup", "minhash_dedup", "contamination_check",
          "quality_score", "difficulty_score", "capability_labels",
          "provenance", "accept_reject" };

    private static readonly string[] RecordRequired =
        { "source_kind", "human_or_synthetic", "teacher",
          "generation_source", "quality", "difficulty", "verified",
          "dedup_group", "contamination_state" };

    /// <summary>Score one candidate record and emit the quality +
    /// curation record. Accept requires: license pass, sensitive pass,
    /// not a duplicate member, contamination_state != contaminated,
    /// quality >= minQuality. Returns the stored record.</summary>
    public static Dictionary<string, object?> Evaluate(
        string toolRoot, JsonElement input, double minQuality = 0.0)
    {
        var rec = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["recorded_at"] = XcPaths.IsoNow(),
            ["generation"] =
                GenerationMigration.CurrentGeneration(toolRoot),
            ["freeze_note"] =
                "collect-only; CAPABILITY_TRAINING_FROZEN",
        };
        foreach (var p in input.EnumerateObject())
            rec[p.Name] = ModelLifecycle.Decode(p.Value);
        foreach (var k in RecordRequired)
            if (!rec.ContainsKey(k))
                throw new ExecutorError(
                    "DATA_QUALITY_INVALID",
                    $"missing field {k}");

        bool licensePass = Truthy(rec, "license_pass", true);
        bool sensitivePass = Truthy(rec, "sensitive_pass", true);
        bool duplicate =
            (rec["dedup_group"] as string ?? "").Length > 0 &&
            Truthy(rec, "dedup_member", false);
        bool contaminated = Equals(
            rec["contamination_state"], "contaminated");
        double quality = Num(rec, "quality", 0.0);
        double difficulty = Num(rec, "difficulty", 0.0);

        // §19 self-curation metadata -> training_priority (metadata
        // only; never reaches the trainer while the freeze holds).
        double informativeness = Num(rec, "informativeness", quality);
        double novelty = Num(rec, "novelty", 0.5);
        double capValue = Num(rec, "capability_value", quality);
        double dupDensity = Num(rec, "duplicate_density",
            duplicate ? 1.0 : 0.0);
        double failureRel = Num(rec, "failure_relevance", 0.0);
        double priority = 0.30 * informativeness + 0.20 * novelty +
            0.25 * capValue + 0.15 * failureRel +
            0.10 * difficulty - 0.50 * dupDensity;
        rec["training_priority"] = Math.Round(priority, 6);

        bool accept = licensePass && sensitivePass && !duplicate &&
            !contaminated && quality >= minQuality;
        rec["decision"] = accept ? "accept" : "reject";
        var reject = new List<object?>();
        if (!licensePass) reject.Add("license");
        if (!sensitivePass) reject.Add("sensitive");
        if (duplicate) reject.Add("dedup");
        if (contaminated) reject.Add("contamination");
        if (quality < minQuality) reject.Add("quality");
        rec["reject_reasons"] = reject;
        rec["stages"] = Stages.Cast<object?>().ToList();

        Directory.CreateDirectory(Path.Combine(
            toolRoot, RelDir.Replace('/', Path.DirectorySeparatorChar)));
        string id = "dq-" + DateTime.UtcNow.ToString("yyyyMMdd-HHmmss")
            + "-" + Guid.NewGuid().ToString("N")[..8];
        ModelLifecycle.AtomicWrite(
            Path.Combine(toolRoot,
                RelDir.Replace('/', Path.DirectorySeparatorChar),
                id + ".json"),
            CanonicalJson.PrettyDict(rec) + "\n");
        rec["ok"] = true;
        rec["record_id"] = id;
        return rec;
    }

    private static bool Truthy(
        Dictionary<string, object?> d, string k, bool dflt)
        => d.TryGetValue(k, out var v) && v is bool b ? b : dflt;

    private static double Num(
        Dictionary<string, object?> d, string k, double dflt)
        => d.TryGetValue(k, out var v) && v is IConvertible c
            ? Convert.ToDouble(c) : dflt;
}
