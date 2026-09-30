// DatasetQuality.cs — §14 Phi-4 + §19 Storm absorption: data quality
// as a first-class pipeline. Collects only — never trains.
//
//   source -> license -> sensitive -> normalize -> exact dedup ->
//   near-dup (MinHash band signature reuse from corpus artifacts) ->
//   contamination -> quality -> difficulty -> capability labels ->
//   provenance -> self-curation priority -> accept/reject
//
// Output record ``star-dataset-quality/v1`` appends to
// dataset-quality.jsonl. Every record carries source_kind,
// human_or_synthetic, teacher, generation_source, quality, difficulty,
// verified, dedup_group, contamination_state plus the §19 self-curation
// fields (informativeness, novelty, capability_value, duplicate_density,
// failure_relevance -> training_priority). Metadata only — the trainer
// stays sealed under capability_training_frozen.

using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class DatasetQuality
{
    public const string Format = "star-dataset-quality/v1";

    private static string LogsDir(string toolRoot)
        => Path.Combine(toolRoot, XcPaths.LogsRel);
    private static string SeenPath(string toolRoot)
        => Path.Combine(LogsDir(toolRoot), "dataset-quality-seen.jsonl");

    /// <summary>Score + label a candidate record. Input json fields:
    /// text (required), source_id, source_kind, human_or_synthetic,
    /// teacher, generation_source, capability hint, failure_type,
    /// verified, license, sensitivity.</summary>
    public static Dictionary<string, object?> Evaluate(
        string toolRoot, string recordFile)
    {
        var r = ToolContracts.ReadObject(recordFile, "DQ_RECORD_INVALID");
        string text = ToolContracts.Str(r, "text");
        if (text.Length == 0)
            throw new ExecutorError("DQ_RECORD_INVALID", "text empty");
        string license = ToolContracts.Str(r, "license");
        string sensitivity =
            ToolContracts.Str(r, "sensitivity") is { Length: > 0 } s
                ? s : "internal";
        var rejects = new List<object?>();
        if (license.Length == 0) rejects.Add("license-missing");
        if (sensitivity is not ("internal" or "public"))
            rejects.Add("sensitivity-" + sensitivity);

        // normalize + hashes (same NFC contract as the corpus lane).
        string nfc = text.Normalize(NormalizationForm.FormC);
        bool nfcChanged = !string.Equals(text, nfc, StringComparison.Ordinal);
        string sha = "sha256:" +
            TransformerTrainingRepository.Sha256Text(nfc);

        // exact dedup vs the seen-ledger (content hash -> dedup_group).
        Directory.CreateDirectory(LogsDir(toolRoot));
        string seen = SeenPath(toolRoot);
        string dedupGroup = "";
        bool exactDup = false;
        if (File.Exists(seen))
            foreach (var line in File.ReadLines(seen))
            {
                if (line.StartsWith(sha + " "))
                {
                    exactDup = true;
                    dedupGroup = line[(sha.Length + 1)..];
                    break;
                }
            }
        if (!exactDup)
        {
            dedupGroup = "dg-" + sha[7..15];
            File.AppendAllText(seen, sha + " " + dedupGroup + "\n");
        }
        if (exactDup) rejects.Add("exact-duplicate");

        // contamination: deny-listed governed content must never enter
        // training data — reuse the corpus path fragments.
        string sourceId = ToolContracts.Str(r, "source_id");
        string rel = ToolContracts.Str(r, "relpath")
            .Replace('\\', '/').ToLowerInvariant();
        bool contaminated =
            rel.Contains("governance_rule/codex") ||
            rel.Contains("permission") ||
            rel.Contains("/audit/");
        if (contaminated) rejects.Add("contamination-deny-path");

        // --- scores (deterministic heuristics; no model calls) ---
        int len = nfc.Length;
        int alpha = 0, digit = 0, cjk = 0, ws = 0;
        foreach (char ch in nfc)
        {
            if (char.IsLetter(ch) && ch < 0x80) ++alpha;
            else if (char.IsDigit(ch)) ++digit;
            else if (ch >= 0x4E00 && ch <= 0x9FFF) ++cjk;
            else if (char.IsWhiteSpace(ch)) ++ws;
        }
        double density = len > 0 ? (alpha + digit + cjk) / (double)len : 0;
        double quality = Math.Round(
            Math.Min(1.0, 0.2 + 0.5 * density +
                     Math.Min(0.3, len / 4000.0)), 4);
        double difficulty = Math.Round(
            Math.Min(1.0, len / 8000.0 + digit / (double)Math.Max(1, len) +
                     (rel.EndsWith(".cs") || rel.EndsWith(".cpp") ||
                      rel.EndsWith(".rs") ? 0.3 : 0.0)), 4);

        // capability labels — deterministic keyword sets.
        var labels = new List<object?>();
        string low = nfc.ToLowerInvariant();
        if (cjk * 5 > len / 4) labels.Add("zh-tw");
        if (low.Contains("```") || rel.EndsWith(".cs") ||
            rel.EndsWith(".cpp") || rel.EndsWith(".rs"))
            labels.Add("coding");
        if (digit * 8 > alpha) labels.Add("math");
        if (low.Contains("read") || cjk > 0) labels.Add("reading");
        string capHint = ToolContracts.Str(r, "capability");
        if (capHint.Length > 0 && !labels.Contains(capHint))
            labels.Add(capHint);
        if (labels.Count == 0) labels.Add("general");

        // --- §19 self-curation metadata ---
        double informativeness = quality;
        double novelty = exactDup ? 0.0 :
            Math.Round(0.5 + Math.Min(0.5, len / 10000.0), 4);
        double failureRelevance =
            ToolContracts.Str(r, "failure_type").Length > 0 ? 0.9 : 0.2;
        double capabilityValue = Math.Round(
            0.4 * informativeness + 0.3 * novelty +
            0.3 * failureRelevance, 4);
        double duplicateDensity = exactDup ? 1.0 : 0.0;
        double trainingPriority = Math.Round(
            Math.Max(0.0, Math.Min(1.0,
                0.45 * capabilityValue + 0.35 * quality +
                0.20 * difficulty - 0.5 * duplicateDensity)), 4);

        bool verified = r.TryGetValue("verified", out var vb) &&
                        vb is bool b && b;
        if (!verified && quality < 0.5) rejects.Add("low-quality");

        var record = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["at"] = XcPaths.IsoNow(),
            ["source_id"] = sourceId,
            ["relpath"] = ToolContracts.Str(r, "relpath"),
            ["source_kind"] =
                ToolContracts.Str(r, "source_kind") is { Length: > 0 } sk
                    ? sk : "unknown",
            ["human_or_synthetic"] =
                ToolContracts.Str(r, "human_or_synthetic") is
                    { Length: > 0 } hs ? hs : "unknown",
            ["teacher"] = ToolContracts.Str(r, "teacher"),
            ["generation_source"] =
                ToolContracts.Str(r, "generation_source") is
                    { Length: > 0 } gs
                    ? gs : GenerationMigration.CurrentGeneration(toolRoot),
            ["sha256"] = sha,
            ["nfc_changed"] = nfcChanged,
            ["quality"] = quality,
            ["difficulty"] = difficulty,
            ["verified"] = verified,
            ["dedup_group"] = dedupGroup,
            ["contamination_state"] =
                contaminated ? "denied" : "clear",
            ["capability_labels"] = labels,
            ["informativeness"] = informativeness,
            ["novelty"] = novelty,
            ["capability_value"] = capabilityValue,
            ["duplicate_density"] = duplicateDensity,
            ["failure_relevance"] = failureRelevance,
            ["training_priority"] = trainingPriority,
            ["decision"] = rejects.Count == 0 ? "accept" : "reject",
            ["rejects"] = rejects,
        };
        File.AppendAllText(
            Path.Combine(LogsDir(toolRoot), "dataset-quality.jsonl"),
            JsonSerializer.Serialize(record) + "\n");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["decision"] = record["decision"],
            ["quality"] = quality, ["difficulty"] = difficulty,
            ["training_priority"] = trainingPriority,
            ["capability_labels"] = labels,
            ["rejects"] = rejects,
            ["trained"] = false,   // frozen — collection only
        };
    }
}
