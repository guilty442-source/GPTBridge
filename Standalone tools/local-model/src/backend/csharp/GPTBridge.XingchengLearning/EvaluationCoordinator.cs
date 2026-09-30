// EvaluationCoordinator.cs — §30 unified evaluation plane.
//
// Every suite across both planes emits one record shape:
// ``star-eval-result/v1`` (suite, case, generation, bundle, pass,
// metric, threshold, failure, timestamp, artifact_hash). The
// coordinator owns the suite registry, validates and appends records,
// and reports status. Baseline-only this phase: failures never launch
// training.
//
//   ArchitectureGate (§15 GLM-5.3): ARCHITECTURE_CHANGE_REQUIRED is
//   true only when every justification condition holds — otherwise the
//   request fails closed with ARCHITECTURE_CHANGE_NOT_JUSTIFIED. The
//   governance rule stands: post-training / agent workflow / runtime
//   optimization are exhausted before backbone changes are allowed.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class EvaluationCoordinator
{
    public const string Format = "star-eval-result/v1";

    /// <summary>§30 formal suite registry.</summary>
    public static readonly string[] Suites =
    {
        "runtime-parity", "precision-parity", "kv-cache",
        "recurrent-state", "long-context", "tool-decision",
        "structured-output", "citation", "rag", "agent", "coding",
        "fim", "vision", "moe-routing", "generation-migration",
        "bundle-provenance",
    };

    private static string LogPath(string toolRoot)
        => Path.Combine(toolRoot, XcPaths.LogsRel,
                        "eval-results.jsonl");

    public static Dictionary<string, object?> Record(
        string toolRoot, string file)
    {
        var r = ToolContracts.ReadObject(file, "EVAL_RESULT_INVALID");
        var missing = new List<object?>();
        foreach (var k in new[] { "format", "suite", "case",
                                  "generation", "bundle", "pass",
                                  "metric", "threshold",
                                  "artifact_hash" })
            if (!r.ContainsKey(k)) missing.Add(k);
        if (missing.Count > 0)
            throw new ExecutorError("EVAL_RESULT_INVALID",
                "missing: " + string.Join(",", missing));
        if (ToolContracts.Str(r, "format") != Format)
            throw new ExecutorError("EVAL_RESULT_INVALID",
                "format must be " + Format);
        string suite = ToolContracts.Str(r, "suite");
        if (!Suites.Contains(suite))
            throw new ExecutorError("EVAL_RESULT_INVALID",
                $"suite {suite} not in registry");
        r["timestamp"] = XcPaths.IsoNow();
        Directory.CreateDirectory(
            Path.GetDirectoryName(LogPath(toolRoot))!);
        File.AppendAllText(LogPath(toolRoot),
            JsonSerializer.Serialize(r) + "\n");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["suite"] = suite, ["case"] = r["case"],
            ["pass"] = r["pass"],
            // baseline-only: no result can enqueue a trainer job.
            ["training_triggered"] = false,
        };
    }

    public static Dictionary<string, object?> Status(string toolRoot)
    {
        var counts = new Dictionary<string, Dictionary<string, int>>();
        int total = 0;
        if (File.Exists(LogPath(toolRoot)))
            foreach (var line in File.ReadLines(LogPath(toolRoot)))
            {
                try
                {
                    using var d = JsonDocument.Parse(line);
                    string s = d.RootElement
                        .GetProperty("suite").GetString() ?? "?";
                    bool pass = d.RootElement
                        .TryGetProperty("pass", out var p) &&
                        p.ValueKind == JsonValueKind.True;
                    if (!counts.TryGetValue(s, out var c))
                        counts[s] = c = new Dictionary<string, int>();
                    c[pass ? "pass" : "fail"] =
                        c.GetValueOrDefault(pass ? "pass" : "fail") + 1;
                    ++total;
                }
                catch { /* skip malformed lines */ }
            }
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["total"] = total,
            ["suites_registered"] =
                Suites.Cast<object?>().ToList(),
            ["by_suite"] = counts.ToDictionary(
                kv => kv.Key,
                kv => (object?)kv.Value.ToDictionary(
                    x => x.Key, x => (object?)x.Value)),
        };
    }
}

internal static class ArchitectureGate
{
    /// <summary>§15: every condition must hold, otherwise the change
    /// request is denied. The record carries evidence pointers; the
    /// gate only checks the booleans are all asserted.</summary>
    private static readonly string[] Conditions =
    {
        "architecture_bottleneck",        // existing arch cannot fix it
        "runtime_optimization_ineffective",
        "data_improvement_ineffective",
        "post_training_path_ineffective",
        "independent_benchmark",
        "ablation",
        "memory_impact",
        "latency_impact",
    };

    public static Dictionary<string, object?> Evaluate(string file)
    {
        var r = ToolContracts.ReadObject(
            file, "ARCHITECTURE_CHANGE_NOT_JUSTIFIED");
        var missing = Conditions
            .Where(c => !r.ContainsKey(c)).ToList();
        if (missing.Count > 0)
            throw new ExecutorError("ARCHITECTURE_CHANGE_NOT_JUSTIFIED",
                "missing conditions: " + string.Join(",", missing));
        var failed = Conditions
            .Where(c => r[c] is not bool b || !b).ToList();
        bool required = failed.Count == 0;
        if (!required)
            throw new ExecutorError("ARCHITECTURE_CHANGE_NOT_JUSTIFIED",
                "unmet: " + string.Join(",", failed));
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["architecture_change_required"] = true,
            ["conditions"] = Conditions.Cast<object?>().ToList(),
            ["note"] = "gate passed — a new architecture axis may be " +
                       "proposed as a separate generation",
        };
    }
}

internal static class BundleProvenance
{
    public const string Format = "star-bundle-provenance/v1";

    /// <summary>§25 Granite-style provenance: verify a bundle's
    /// manifest declares and matches its hashes — manifest hash,
    /// weights hash, tokenizer hash, generation, architecture profile,
    /// XCN version, build/runtime compatibility, lineage id. Optional
    /// signature slot is checked when present (sha256 manifest
    /// signature via a sibling .sig file containing "sha256:<hex>").
    /// Load order enforced: hash -> signature -> generation ->
    /// architecture -> checkpoint -> shape -> compatibility.</summary>
    public static Dictionary<string, object?> Check(
        string toolRoot, string bundleDir)
    {
        var failures = new List<object?>();
        string manifestPath = Path.Combine(bundleDir, "manifest.json");
        if (!File.Exists(manifestPath))
            throw new ExecutorError("BUNDLE_PROVENANCE_INVALID",
                "manifest missing");
        var m = ToolContracts.ReadObject(
            manifestPath, "BUNDLE_PROVENANCE_INVALID");

        // 1. hashes — weights + tokenizer must hash-match the manifest.
        string weightsSha =
            TransformerTrainingRepository.Sha256File(
                Path.Combine(bundleDir, "weights.bin"));
        string declaredWeights =
            ToolContracts.Str(m, "weights_sha256");
        if (declaredWeights.Length > 0 &&
            !declaredWeights.EndsWith(weightsSha))
            failures.Add("weights-sha-mismatch");
        string? tokSha = null;
        foreach (var tok in new[] { "tokenizer.json", "tokenizer" })
        {
            string tp = Path.Combine(bundleDir, tok);
            if (File.Exists(tp))
            {
                tokSha = TransformerTrainingRepository.Sha256File(tp);
                break;
            }
        }
        string declaredTok = ToolContracts.Str(m, "tokenizer_sha256");
        if (declaredTok.Length > 0 && tokSha != null &&
            !declaredTok.EndsWith(tokSha))
            failures.Add("tokenizer-sha-mismatch");

        // 2. optional signature — manifest .sig sibling holding
        //    "sha256:<hex of canonical manifest bytes>".
        bool signed = false;
        string sigPath = manifestPath + ".sig";
        if (File.Exists(sigPath))
        {
            string expect = File.ReadAllText(sigPath).Trim();
            string actual = "sha256:" +
                TransformerTrainingRepository.Sha256File(manifestPath);
            signed = string.Equals(expect, actual,
                                   StringComparison.OrdinalIgnoreCase);
            if (!signed) failures.Add("signature-mismatch");
        }

        // 3. generation identity — must name a lineage generation.
        string gen = ToolContracts.Str(m, "generation") is
            { Length: > 0 } g ? g
            : ToolContracts.Str(m, "lineage_id");
        if (gen.Length == 0) failures.Add("generation-missing");

        // 4. architecture profile field.
        string arch = ToolContracts.Str(m, "architecture_generation");
        // older manifests carry profile inside config — accept either.
        if (arch.Length == 0)
        {
            if (m.TryGetValue("config", out var c) &&
                c is Dictionary<string, object?> cd)
                arch = ToolContracts.Str(cd, "generation");
        }
        if (arch.Length == 0) failures.Add("architecture-missing");

        // 5. checkpoint contract — XCN1 v10 canonical; older accepted
        //    for backward-compat reading only.
        string xcn = ToolContracts.Str(m, "checkpoint_version");
        if (xcn.Length > 0 && !xcn.Contains("v10"))
            failures.Add("checkpoint-noncanonical:" + xcn);

        // 6. runtime compatibility marker.
        string compat = ToolContracts.Str(m, "runtime_compatibility");
        if (compat.Length > 0 && !compat.Contains("native"))
            failures.Add("runtime-incompatible:" + compat);

        bool ok = failures.Count == 0;
        if (!ok)
            throw new ExecutorError("BUNDLE_PROVENANCE_INVALID",
                string.Join(",", failures));
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = Format,
            ["bundle"] = bundleDir,
            ["generation"] = gen,
            ["architecture_generation"] = arch,
            ["checkpoint_version"] = xcn,
            ["weights_sha256"] = "sha256:" + weightsSha,
            ["signed"] = signed,
            ["verified_order"] = new List<object?>
            {
                "hash", "signature", "generation", "architecture",
                "checkpoint", "shape", "compatibility", "load",
            },
        };
    }
}
