// SftDataset.cs — ``star-transformer-sft/v1`` snapshot exporter and
// ``star-transformer-dpo/v1`` preference bridge ports.
//
// Deterministic sha256-permille split, snapshot JSONL serialization,
// content digest and manifest — identical to the retired Python lane so
// dataset identities (content_sha256 -> dataset_id) remain reproducible.

using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class SftDataset
{
    public const string SftFormatVersion = "star-transformer-sft/v1";
    public const string PreferenceSnapshotFormat = "star-transformer-dpo/v1";
    public const string PreferenceSourceType = "preference-pair-governed";
    public const double PairQuality = 0.9;

    private static readonly Dictionary<string, string> ScopeMap =
        new(StringComparer.OrdinalIgnoreCase)
        {
            ["main"] = "main",
            ["investment"] = "investment",
            ["mathematical"] = "mathematical",
            ["coding"] = "coding",
        };

    private static string Sha256Text(string text)
        => TransformerTrainingRepository.Sha256Text(text);

    public static string SftText(string prompt, string completion)
        => $"{prompt.Trim()}\n\n{completion.Trim()}";

    public static Dictionary<string, object?> SerializeSftExample(
        IReadOnlyDictionary<string, object?> example)
    {
        string prompt = (TransformerTrainingRepository.Str(example, "input_text") ?? "").Trim();
        string completion = (TransformerTrainingRepository.Str(example, "target_text") ?? "").Trim();
        if (prompt.Length == 0 || completion.Length == 0)
            throw new ArgumentException(
                "sft example requires non-empty input_text/target_text");
        string text = SftText(prompt, completion);
        return new Dictionary<string, object?>
        {
            ["source"] = TransformerTrainingRepository.Str(example, "source_type") ?? "",
            ["sha256"] = Sha256Text(text),
            ["text"] = text,
            ["prompt"] = prompt,
            ["completion"] = completion,
            ["intent"] = TransformerTrainingRepository.Str(example, "intent") ?? "",
            ["source_example_id"] =
                TransformerTrainingRepository.Str(example, "example_id") ?? "",
            ["source_revision"] =
                TransformerTrainingRepository.Int(example, "revision"),
            ["quality_score"] =
                TransformerTrainingRepository.Num(example, "quality_score"),
            // §5 freeze-phase provenance: every kept record carries the
            // producing generation/model/version context so it can be
            // replayed after capability training unfreezes — without the
            // producing generation still existing.
            ["generation"] =
                TransformerTrainingRepository.Str(example, "generation") ?? "",
            ["model_version"] =
                TransformerTrainingRepository.Str(example, "model_version") ?? "",
            ["failure_type"] =
                TransformerTrainingRepository.Str(example, "failure_type") ?? "",
            ["collected_at"] =
                TransformerTrainingRepository.Str(example, "collected_at") ?? "",
            ["validation_state"] = "collected",
        };
    }

    /// <summary>Write snapshot JSONL and return the registration bundle
    /// (``snapshot_path``, ``snapshot_sha256``, ``content_sha256``,
    /// ``examples``, ``source_manifest``, ``manifest``).</summary>
    public static Dictionary<string, object?> BuildSftDataset(
        string outputPath,
        IReadOnlyDictionary<string, List<Dictionary<string, object?>>> examplesByScope,
        int valPermille = 50,
        string generation = "",
        string modelVersion = "")
    {
        string target = Path.GetFullPath(outputPath);
        Directory.CreateDirectory(Path.GetDirectoryName(target)!);
        if (valPermille <= 0 || valPermille >= 1000)
            throw new ArgumentException("val_permille must be in (0, 1000)");

        var registered = new List<Dictionary<string, object?>>();
        var records = new List<Dictionary<string, object?>>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var (scope, examples) in examplesByScope)
        {
            if (!ScopeMap.TryGetValue(scope, out string? databaseScope))
                throw new ArgumentException($"unsupported database_scope: {scope}");
            foreach (var example in examples)
            {
                var record = SerializeSftExample(example);
                if (generation.Length > 0 &&
                    (string?)record["generation"] == "")
                    record["generation"] = generation;
                if (modelVersion.Length > 0 &&
                    (string?)record["model_version"] == "")
                    record["model_version"] = modelVersion;
                string hash = (string)record["sha256"]!;
                if (!seen.Add(hash))
                    continue;
                string split =
                    Convert.ToInt32(hash[..8], 16) % 1000 < valPermille
                        ? "validation" : "train";
                record["split"] = split;
                records.Add(record);
                registered.Add(new Dictionary<string, object?>
                {
                    ["split"] = split,
                    ["owner_model_id"] =
                        TransformerTrainingRepository.Str(example, "owner_model_id")
                        ?? "star-main-native-model",
                    ["database_scope"] = databaseScope,
                    ["source_example_id"] = record["source_example_id"],
                    ["source_revision"] = record["source_revision"],
                    ["content_sha256"] = hash,
                    ["source_type"] = record["source"],
                    ["quality_score"] = record["quality_score"],
                    ["generation"] = record["generation"],
                    ["model_version"] = record["model_version"],
                });
            }
        }

        if (records.Count == 0)
            throw new ArgumentException(
                "no approved language training examples supplied");
        if (!records.Any(r => (string?)r["split"] == "train") ||
            !records.Any(r => (string?)r["split"] == "validation"))
            throw new ArgumentException(
                "dataset requires train and validation examples");

        using (var writer = new StreamWriter(target, append: false,
                                             new UTF8Encoding(false)))
            foreach (var record in records)
            {
                // json.dumps(record, ensure_ascii=False) — insertion order.
                writer.WriteLine(CanonicalJson.PlainDict(record));
            }

        // content digest: sha256 of json.dumps(sorted(hashes), sort_keys=True)
        // — default separators (", ") for a bare array of strings.
        var sortedHashes = records.Select(r => (string)r["sha256"]!)
            .OrderBy(h => h, StringComparer.Ordinal).ToList();
        var sb = new StringBuilder("[");
        for (int i = 0; i < sortedHashes.Count; i++)
        {
            if (i > 0) sb.Append(", ");
            CanonicalJson.WriteValue(sortedHashes[i], sb, canonical: false, depth: 0);
        }
        sb.Append(']');
        string contentSha256 = Sha256Text(sb.ToString());

        string snapshotSha256 = TransformerTrainingRepository.Sha256File(target);
        var scopes = examplesByScope
            .OrderBy(kv => kv.Key, StringComparer.Ordinal)
            .ToDictionary(kv => kv.Key, kv => (object?)kv.Value.Count);
        var manifest = new Dictionary<string, object?>
        {
            ["format_version"] = SftFormatVersion,
            ["snapshot_path"] = target,
            ["snapshot_sha256"] = snapshotSha256,
            ["content_sha256"] = contentSha256,
            ["example_count"] = records.Count,
            ["train_count"] = records.Count(r => (string?)r["split"] == "train"),
            ["validation_count"] = records.Count(r => (string?)r["split"] == "validation"),
            ["scopes"] = scopes,
        };
        return new Dictionary<string, object?>
        {
            ["format_version"] = SftFormatVersion,
            ["snapshot_path"] = target,
            ["snapshot_sha256"] = snapshotSha256,
            ["content_sha256"] = contentSha256,
            ["examples"] = registered,
            ["source_manifest"] = new Dictionary<string, object?>
            {
                ["format"] = SftFormatVersion,
                ["scopes"] = scopes,
                ["val_permille"] = valPermille,
            },
            ["manifest"] = manifest,
        };
    }

    // ------------------------------------------------------- DPO bridge --

    private static string RecordHash(string prompt, string chosen, string rejected)
        => Sha256Text($"{prompt}\0{chosen}\0{rejected}");

    public static Dictionary<string, object?> BuildPairsSnapshot(
        IReadOnlyList<Dictionary<string, object?>> pairs,
        string outputPath,
        int valPermille = 200,
        string generation = "",
        string modelVersion = "")
    {
        string target = Path.GetFullPath(outputPath);
        Directory.CreateDirectory(Path.GetDirectoryName(target)!);
        var seen = new HashSet<string>(StringComparer.Ordinal);
        var records = new List<Dictionary<string, object?>>();
        foreach (var pair in pairs)
        {
            string prompt = (
                TransformerTrainingRepository.Str(pair, "prompt_text") ??
                TransformerTrainingRepository.Str(pair, "prompt") ?? "").Trim();
            string chosen = (
                TransformerTrainingRepository.Str(pair, "chosen_text") ??
                TransformerTrainingRepository.Str(pair, "chosen") ?? "").Trim();
            string rejected = (
                TransformerTrainingRepository.Str(pair, "rejected_text") ??
                TransformerTrainingRepository.Str(pair, "rejected") ?? "").Trim();
            if (prompt.Length == 0 || chosen.Length == 0 || rejected.Length == 0)
                continue;
            string digest = RecordHash(prompt, chosen, rejected);
            if (!seen.Add(digest))
                continue;
            string split = Convert.ToInt32(digest[..8], 16) % 1000 < valPermille
                ? "validation" : "train";
            string pid =
                TransformerTrainingRepository.Str(pair, "pair_id") ?? "";
            records.Add(new Dictionary<string, object?>
            {
                ["source"] = "language_preference_pair:" +
                    (pid.Length > 32 ? pid[..32] : pid),
                ["sha256"] = digest,
                ["text"] = $"{prompt}\n\n{chosen}",
                ["prompt"] = prompt,
                ["chosen"] = chosen,
                ["rejected"] = rejected,
                ["intent"] =
                    TransformerTrainingRepository.Str(pair, "intent") ?? "preference",
                ["quality_score"] = PairQuality,
                ["split"] = split,
                // §5 provenance — see SerializeSftExample.
                ["generation"] =
                    TransformerTrainingRepository.Str(pair, "generation")
                    ?? generation,
                ["model_version"] =
                    TransformerTrainingRepository.Str(pair, "model_version")
                    ?? modelVersion,
                ["failure_type"] =
                    TransformerTrainingRepository.Str(pair, "failure_type")
                    ?? "",
                ["collected_at"] =
                    TransformerTrainingRepository.Str(pair, "collected_at")
                    ?? "",
                ["validation_state"] = "collected",
            });
        }
        if (records.Count == 0)
            throw new ArgumentException("PREFERENCE_SNAPSHOT_EMPTY");
        if (records.Count > 1 && !records.Any(r => (string?)r["split"] == "validation"))
            records[^1]["split"] = "validation";

        using (var writer = new StreamWriter(target, append: false,
                                             new UTF8Encoding(false)))
            foreach (var record in records)
                writer.WriteLine(CanonicalJson.PlainDict(record));

        string fileDigest = TransformerTrainingRepository.Sha256File(target);
        var manifest = new Dictionary<string, object?>
        {
            ["format_version"] = PreferenceSnapshotFormat,
            ["created_at"] = XcPaths.IsoNow(),
            ["snapshot_path"] = target,
            ["snapshot_sha256"] = fileDigest,
            ["pairs"] = records.Count,
            ["train_count"] = records.Count(r => (string?)r["split"] == "train"),
            ["validation_count"] =
                records.Count(r => (string?)r["split"] == "validation"),
        };
        File.WriteAllText(
            Path.Combine(Path.GetDirectoryName(target)!, "preference_manifest.json"),
            CanonicalJson.PrettyDict(manifest) + "\n", new UTF8Encoding(false));
        return manifest;
    }

    public static Dictionary<string, object?> RegisterPairsSnapshot(
        TransformerTrainingRepository repository,
        IReadOnlyDictionary<string, object?> manifest,
        string ownerModelId = "star-main-native-model",
        string databaseScope = "main",
        string createdBy = "star-main-native-model",
        string generation = "",
        string modelVersion = "")
    {
        var data = manifest;
        if ((string?)data.GetValueOrDefault("format_version") !=
            PreferenceSnapshotFormat)
            throw new ArgumentException("PREFERENCE_MANIFEST_FORMAT_MISMATCH");
        string snapshotPath = Path.GetFullPath((string)data["snapshot_path"]!);
        string toolRoot = Path.GetFullPath(repository.ToolRoot);
        if (!snapshotPath.StartsWith(toolRoot + Path.DirectorySeparatorChar,
                                     StringComparison.Ordinal))
            throw new UnauthorizedAccessException("PREFERENCE_SNAPSHOT_SCOPE_DENIED");
        if (!File.Exists(snapshotPath))
            throw new FileNotFoundException("PREFERENCE_SNAPSHOT_MISSING");
        string actualSha = TransformerTrainingRepository.Sha256File(snapshotPath);
        if (actualSha != (string?)data["snapshot_sha256"])
            throw new ArgumentException("PREFERENCE_SNAPSHOT_DRIFT");

        var examples = new List<Dictionary<string, object?>>();
        foreach (string line in File.ReadLines(snapshotPath))
        {
            string trimmed = line.Trim();
            if (trimmed.Length == 0) continue;
            using var doc = JsonDocument.Parse(trimmed);
            string recordHash = doc.RootElement.GetProperty("sha256").GetString()!;
            string split = doc.RootElement.TryGetProperty("split", out var s)
                ? s.GetString() ?? "train" : "train";
            examples.Add(new Dictionary<string, object?>
            {
                ["split"] = split,
                ["owner_model_id"] = ownerModelId,
                ["database_scope"] = databaseScope,
                ["source_example_id"] = $"pref-{recordHash[..24]}",
                ["source_revision"] = 1,
                ["content_sha256"] = recordHash,
                ["source_type"] = PreferenceSourceType,
                ["quality_score"] = PairQuality,
                ["generation"] =
                    doc.RootElement.TryGetProperty("generation", out var g)
                        ? g.GetString() ?? generation : generation,
                ["model_version"] =
                    doc.RootElement.TryGetProperty("model_version", out var mv)
                        ? mv.GetString() ?? modelVersion : modelVersion,
            });
        }
        string contentSha256 = TransformerTrainingRepository.Sha256Text(
            string.Join("", examples.Select(e => (string)e["content_sha256"]!)
                .OrderBy(h => h, StringComparer.Ordinal)));
        var prefManifest = new Dictionary<string, object?>();
        foreach (string key in new[] { "created_at", "pairs", "train_count", "validation_count" })
            if (data.TryGetValue(key, out object? v))
                prefManifest[key] = v;
        return repository.CreateDataset(
            contentSha256: contentSha256,
            snapshotPath: snapshotPath,
            snapshotSha256: actualSha,
            examples: examples,
            sourceManifest: new Dictionary<string, object?>
            {
                ["bridge"] = PreferenceSnapshotFormat,
                ["preference_manifest"] = prefManifest,
            },
            createdBy: createdBy);
    }
}
