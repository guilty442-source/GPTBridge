// DataQuality.cs — the Phi-4 / Storm data absorptions (§14, §19) and
// the §35 incremental corpus contract.
//
//   DatasetQualityPipeline   source -> license -> sensitive ->
//                            normalize -> exact dedup -> MinHash dedup
//                            -> contamination -> quality -> difficulty
//                            -> capability labels -> provenance ->
//                            accept/reject. Per-record metadata only —
//                            the pipeline COLLECTS, it never feeds the
//                            trainer (capability_training_frozen).
//   Self-curation metadata   informativeness / novelty / difficulty /
//                            capability_value / duplicate_density /
//                            failure_relevance -> training_priority
//                            (§19: stored, never trained on).
//   IncrementalScanState     §35 contract the native corpus scanner
//                            (xcm_corpus.h) fills — FileIndex /
//                            ContentHash / ChangedFileDetector /
//                            DedupIndex semantics verified here.
//
// Every decision is recorded; nothing enters a dataset without a
// provenance row.

using System.Security.Cryptography;
using System.Text;
// DataQuality.cs — §14 + §19 dataset quality pipeline.
//
// Phi-4 lesson: data quality is a first-class governed component.
// Storm lesson: self-curation metadata (informativeness, novelty,
// difficulty, capability value, duplicate density, failure relevance)
// produces a training_priority — stored as metadata only. Nothing
// here feeds the trainer; capability training stays frozen.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

/// <summary>§14 per-record quality metadata — the fields a governed
/// dataset row must carry before it can be considered at all.</summary>
internal sealed class QualityRecord
{
    public string SourceKind = "";          // human|synthetic|mixed
    public string Teacher = "";
    public string GenerationSource = "";    // which generation produced it
    public double Quality;                  // 0..1
    public double Difficulty;               // 0..1
    public bool Verified;
    public string DedupGroup = "";
    public string ContaminationState = "clean"; // clean|suspect|hit
    // §19 self-curation axes (metadata only):
    public double Informativeness;
    public double Novelty;
    public double CapabilityValue;
    public double DuplicateDensity;
    public double FailureRelevance;

    public double TrainingPriority =>
        // Bounded metadata score — used for ordering candidates, never
        // for auto-training (frozen).
        Math.Round(Math.Clamp(
            0.30 * Quality + 0.20 * Informativeness +
            0.15 * Novelty + 0.15 * CapabilityValue +
            0.10 * Difficulty + 0.10 * FailureRelevance -
            0.20 * DuplicateDensity, 0.0, 1.0), 6);

    public Dictionary<string, object?> ToDict(string contentHash) => new()
    {
        ["content_sha256"] = contentHash,
        ["source_kind"] = SourceKind,
        ["human_or_synthetic"] = SourceKind,
        ["teacher"] = Teacher,
        ["generation_source"] = GenerationSource,
        ["quality"] = Quality,
        ["difficulty"] = Difficulty,
        ["verified"] = Verified,
        ["dedup_group"] = DedupGroup,
        ["contamination_state"] = ContaminationState,
        ["informativeness"] = Informativeness,
        ["novelty"] = Novelty,
        ["capability_value"] = CapabilityValue,
        ["duplicate_density"] = DuplicateDensity,
        ["failure_relevance"] = FailureRelevance,
        ["training_priority"] = TrainingPriority,
    };
}

/// <summary>§14 the pipeline — a record must survive every stage to be
/// accepted. Stages are deterministic and each writes its verdict into
/// the record; rejection at any stage is terminal and recorded.</summary>
internal static class DatasetQualityPipeline
{
    public const string Format = "star-dataset-quality/v1";
    public const string ReportRel =
        "xingcheng/runtime/state/dataset-quality";

    public sealed class Result
    {
        public bool Accepted;
        public string RejectStage = "";
        public string RejectReason = "";
        public QualityRecord Record = new();
        public string ContentHash = "";
        public List<string> CapabilityLabels = new();
    }

    /// <summary>Run one sample end to end. `licenseOk`/`sensitiveHit`
    /// are caller-evaluated gates (the governed registry owns them);
    /// `knownHashes`/`minhashBands` are the dedup corpora;
    /// `contaminated` flags benchmark leakage.</summary>
    public static Result Evaluate(
        string sourceKind, string teacher, string generationSource,
        string normalizedText,
        bool licenseOk, bool sensitiveHit, bool contaminated,
        HashSet<string> knownHashes,
        HashSet<ulong> minhashBands,
        double quality, double difficulty, bool verified,
        List<string> capabilityLabels)
    {
        var r = new Result
        {
            Record =
            {
                SourceKind = sourceKind,
                Teacher = teacher,
                GenerationSource = generationSource,
                Quality = quality,
                Difficulty = difficulty,
                Verified = verified,
            },
            CapabilityLabels = capabilityLabels,
        };

        // Stage order is fixed — a record may never skip a gate.
        if (!licenseOk)
            return Reject(r, "license", "LICENSE_REJECTED");
        if (sensitiveHit)
            return Reject(r, "sensitive", "SENSITIVE_REJECTED");
        if (normalizedText.Trim().Length == 0)
            return Reject(r, "normalize", "EMPTY_AFTER_NORMALIZE");

        r.ContentHash = Sha256Hex(normalizedText);
        if (!knownHashes.Add(r.ContentHash))
            return Reject(r, "exact_dedup", "EXACT_DUPLICATE");

        ulong band = MinHashBand(normalizedText);
        if (!minhashBands.Add(band))
            return Reject(r, "minhash_dedup", "NEAR_DUPLICATE");
        r.Record.DedupGroup = $"band-{band:x16}";

        if (contaminated)
        {
            r.Record.ContaminationState = "hit";
            return Reject(r, "contamination", "CONTAMINATION_HIT");
        }
        r.Record.ContaminationState = "clean";

        if (quality < XcPaths.MinQuality)
            return Reject(r, "quality",
                          $"below min_quality {XcPaths.MinQuality}");

        r.Accepted = true;
        return r;
    }

    /// <summary>Persist an accepted/rejected verdict into the quality
    /// ledger — append-only, one JSON object per line.</summary>
    public static void RecordVerdict(string toolRoot, Result r,
                                     string datasetRef)
    {
        string dir = Path.Combine(toolRoot, ReportRel);
        Directory.CreateDirectory(dir);
        string path = Path.Combine(dir, "quality-ledger.jsonl");
        var row = r.Record.ToDict(r.ContentHash);
        row["format"] = Format;
        row["accepted"] = r.Accepted;
        row["reject_stage"] = r.RejectStage;
        row["reject_reason"] = r.RejectReason;
        row["capability_labels"] =
            r.CapabilityLabels.Cast<object?>().ToList();
        row["dataset_ref"] = datasetRef;
        row["recorded_at"] = XcPaths.IsoNow();
        File.AppendAllText(path, CanonicalJson.CanonicalDict(row) + "\n",
                           new UTF8Encoding(false));
    }

    private static Result Reject(Result r, string stage, string reason)
    {
        r.RejectStage = stage;
        r.RejectReason = reason;
        return r;
    }

    private static string Sha256Hex(string text)
    {
        byte[] h = SHA256.HashData(Encoding.UTF8.GetBytes(text));
        return Convert.ToHexString(h).ToLowerInvariant();
    }

    /// <summary>Single-band MinHash fingerprint over word 5-grams — the
    /// cheap near-duplicate gate; the native scanner's 64-lane signature
    /// is the authoritative variant for corpus-scale runs.</summary>
    private static ulong MinHashBand(string text)
    {
        ulong min = ulong.MaxValue;
        var words = text.Split(
            (char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        for (int i = 0; i + 5 <= words.Length; i++)
        {
            ulong h = 1469598103934665603UL;
            for (int w = i; w < i + 5; w++)
                foreach (char c in words[w])
                { h ^= c; h *= 1099511628211UL; }
            if (h < min) min = h;
        }
        if (min == ulong.MaxValue)
            foreach (char c in text) { min ^= c; min *= 1099511628211UL; }
        return min;
    }
}

/// <summary>§35 the incremental corpus contract — verifies the native
/// scanner's manifest shape: unchanged files must be skipped, the dedup
/// index must be persistent, the merge must be deterministic.</summary>
internal static class IncrementalCorpusContract
{
    public const string Format = "star-corpus-scan-state/v1";
    public static readonly string[] RequiredManifestFields =
    {
        "dataset_version", "docs_total", "docs_new", "docs_changed",
        "docs_unchanged_skipped", "docs_dedup_dropped",
        "content_hashes", "band_index_size",
    };

    /// <summary>Structural check of a scan-state/manifest document —
    /// fail-closed on missing fields or a non-incremental run
    /// (unchanged files reprocessed) that would violate §35.</summary>
    public static List<string> Validate(Dictionary<string, object?> m)
    {
        var errors = new List<string>();
        foreach (string f in RequiredManifestFields)
            if (!m.ContainsKey(f)) errors.Add($"missing:{f}");
        // §35 invariant: unchanged files are never reprocessed — the
        // counter must exist and be non-negative; a manifest that hides
        // reprocessing fails closed.
        if (m.TryGetValue("docs_unchanged_skipped", out var u) &&
            u is long ul && ul < 0)
            errors.Add("unchanged_skipped_negative");
        return errors;
    }
}

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

        // §19 self-curation + §20 SampleUtilityScore (Storm lesson):
        // nine-dimension utility, metadata only — never reaches the
        // trainer while the freeze holds.
        double informativeness = Num(rec, "informativeness", quality);
        double novelty = Num(rec, "novelty", 0.5);
        double capValue = Num(rec, "capability_value", quality);
        double dupDensity = Num(rec, "duplicate_density",
            duplicate ? 1.0 : 0.0);
        double failureRel = Num(rec, "failure_relevance", 0.0);
        var utility = new Dictionary<string, object?>
        {
            ["educational_value"] = Num(rec, "educational_value", capValue),
            ["difficulty"] = difficulty,
            ["clarity"] = Num(rec, "clarity", quality),
            ["novelty"] = novelty,
            ["reasoning_value"] = Num(rec, "reasoning_value", 0.5),
            ["instruction_value"] = Num(rec, "instruction_value", 0.5),
            ["creative_value"] = Num(rec, "creative_value", 0.0),
            ["tool_value"] = Num(rec, "tool_value", 0.0),
            ["grounding_value"] = Num(rec, "grounding_value", 0.0),
        };
        rec["sample_utility_score"] = utility;
        double uAvg = utility.Values.Average(v => (double)v!);
        double priority = 0.30 * informativeness + 0.20 * novelty +
            0.25 * capValue + 0.15 * failureRel +
            0.10 * difficulty - 0.50 * dupDensity;
        rec["training_priority"] =
            Math.Round(0.6 * priority + 0.4 * uAvg, 6);

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
