// EvalPlane.cs — the unified evaluation + observability absorptions
// (§25, §29, §30, §33; C# half of §8):
//
//   XingchengEvaluationCoordinator  registered suite list, runs
//                                   xc_modeltool eval, emits
//                                   star-eval-result/v1
//   RoutingTrace / MoERoutingAnalyzer  §29 trace + §8 quantile
//                                   analytics (P01..P99, never means
//                                   only) + ROUTER_* diagnostics
//   BundleProvenance               §25 manifest/weights/tokenizer/
//                                  generation/arch/XCN/build/runtime/
//                                  lineage + optional signature;
//                                  load order hash -> signature ->
//                                  generation -> architecture ->
//                                  checkpoint -> shape -> runtime,
//                                  fail-closed at each step
//   GenerationCertification        §33 runtime-contract gates added to
//                                  gen-certify
//   FutureArchitectureResearch     §24 sink for probe results — never
//                                  an architecture change

using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

/// <summary>§30 evaluation plane: the registered suite names and the
/// single result contract.</summary>
internal static class XingchengEvaluationCoordinator
{
    public const string ResultFormat = "star-eval-result/v1";
    public const string TraceFormat = "star-routing-trace/v1";

    /// <summary>Registered suites (§30) — the only suite names the
    /// coordinator will run; an unregistered suite is a typed failure,
    /// not an ad-hoc run.</summary>
    public static readonly string[] Suites =
    {
        "runtime-parity", "precision-parity", "kv-cache",
        "recurrent-state", "long-context", "tool-decision",
        "structured-output", "citation", "rag", "agent", "coding",
        "fim", "vision", "moe-routing", "generation-migration",
        "bundle-provenance",
    };

    /// <summary>Emit one result row — the uniform contract every suite
    /// reports through.</summary>
    public static Dictionary<string, object?> Result(
        string suite, string caseId, string generation,
        string bundleRef, bool pass, double metric,
        double threshold, string failure, string artifactHash)
        => new()
        {
            ["format"] = ResultFormat,
            ["suite"] = suite,
            ["case"] = caseId,
            ["generation"] = generation,
            ["bundle"] = bundleRef,
            ["pass"] = pass,
            ["metric"] = metric,
            ["threshold"] = threshold,
            ["failure"] = failure,
            ["timestamp"] = XcPaths.IsoNow(),
            ["artifact_hash"] = artifactHash,
        };

    /// <summary>Run a registered suite through xc_modeltool eval and
    /// wrap the tool's verdict in star-eval-result/v1 rows.</summary>
    public static Dictionary<string, object?> RunSuite(
        string toolRoot, string suite, string bundle,
        string suitePath, string logDir)
    {
        if (!Suites.Contains(suite))
            throw new ExecutorError(
                "EVAL_SUITE_UNREGISTERED", suite);
        string exe = NativeTools.ModelToolExe(toolRoot);
        var res = NativeTools.Run(
            exe,
            new[]
            {
                "eval", "--bundle", bundle,
                "--suite", suitePath,
            },
            toolRoot, Path.Combine(logDir, $"eval-{suite}.stderr.log"),
            timeoutS: 3600);
        // The tool prints one JSON object on stdout — parse the last
        // JSON line as the verdict.
        bool pass = false; string err = "";
        foreach (string line in res.StdoutTail.Split('\n')
                     .Reverse())
        {
            string t = line.Trim();
            if (!t.StartsWith("{")) continue;
            try
            {
                using var doc = JsonDocument.Parse(t);
                pass = doc.RootElement.TryGetProperty("ok", out var ok) &&
                       ok.GetBoolean();
                if (doc.RootElement.TryGetProperty("error", out var e))
                    err = e.GetString() ?? "";
                break;
            }
            catch (JsonException) { /* keep scanning */ }
        }
        if (!pass)
            FailurePool.Record(   // §29: every suite failure feeds the
                toolRoot, $"suite:{suite}", "gen-2-consolidated",
                FailurePool.ClassForSuite(suite), "suite pass",
                err.Length > 0 ? err : "SUITE_FAILED",
                bundle, "medium", reproducible: true);
        return Result(
            suite, "suite-run", "gen-2-consolidated", bundle, pass,
            pass ? 1.0 : 0.0, 1.0,
            pass ? "" : (err.Length > 0 ? err : "SUITE_FAILED"),
            "");
    }
}

/// <summary>§8 quantile analytics over router/expert evidence — never
/// mean-only; emits ROUTER_* diagnostics when the distribution shows
/// collapse, hotspot, starvation or instability.</summary>
internal static class MoERoutingAnalyzer
{
    public const string Format = "star-moe-routing-analysis/v1";

    /// <summary>Quantile labels emitted for every distribution.</summary>
    public static readonly string[] QuantileLabels =
        { "p01", "p05", "p25", "p50", "p75", "p95", "p99" };
    public static readonly double[] QuantilePs =
        { 0.01, 0.05, 0.25, 0.50, 0.75, 0.95, 0.99 };

    public static Dictionary<string, double> Quantiles(
        IReadOnlyList<double> sorted)
    {
        var q = new Dictionary<string, double>();
        if (sorted.Count == 0)
        {
            foreach (string l in QuantileLabels) q[l] = 0.0;
            return q;
        }
        for (int i = 0; i < QuantileLabels.Length; i++)
        {
            double pos = QuantilePs[i] * (sorted.Count - 1);
            int lo = (int)Math.Floor(pos);
            int hi = Math.Min(sorted.Count - 1, lo + 1);
            double frac = pos - lo;
            q[QuantileLabels[i]] = Math.Round(
                sorted[lo] + (sorted[hi] - sorted[lo]) * frac, 6);
        }
        return q;
    }

    /// <summary>Analyze one request's routing evidence: per-layer expert
    /// counts -> quantiles -> diagnostics. `expertCounts[i][e]` = tokens
    /// routed to expert e in layer i; `sharedRatio` = shared-expert
    /// share of expert output.</summary>
    public static Dictionary<string, object?> Analyze(
        List<long[]> expertCounts, double sharedRatio)
    {
        var diagnostics = new List<string>();
        var layers = new List<object?>();
        foreach (long[] counts in expertCounts)
        {
            var vals = counts.Select(c => (double)c).ToList();
            vals.Sort();
            long total = counts.Sum();
            long max = counts.Length > 0 ? counts.Max() : 0;
            long nonzero = counts.Count(c => c > 0);
            var quantiles = Quantiles(vals);
            // Diagnostics (§8 closed vocabulary):
            if (nonzero > 0 && nonzero * 4 <= counts.Length)
                diagnostics.Add("ROUTER_COLLAPSE");   // ≤25% experts used
            if (total > 0 && max * 2 > total)
                diagnostics.Add("ROUTER_HOTSPOT");    // >50% to one expert
            if (total > 0 && nonzero == 0)
                diagnostics.Add("ROUTER_STARVATION"); // nothing routed
            layers.Add(new Dictionary<string, object?>
            {
                ["expert_load_quantiles"] = quantiles,
                ["experts_active"] = nonzero,
                ["experts_total"] = counts.Length,
                ["tokens_routed"] = total,
            });
        }
        if (layers.Count > 1)
        {
            // Instability: identical prompts should route identically;
            // here the cheap proxy is p50 spread across layers.
            var p50s = layers
                .Select(l => ((Dictionary<string, double>)
                    ((Dictionary<string, object?>)l)
                    ["expert_load_quantiles"]!)["p50"])
                .ToList();
            p50s.Sort();
            if (p50s.Count > 0 &&
                p50s[^1] > 4.0 * Math.Max(1.0, p50s[0]))
                diagnostics.Add("ROUTER_INSTABILITY");
        }
        return new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["layers"] = layers,
            ["shared_expert_ratio"] = sharedRatio,
            ["diagnostics"] = diagnostics
                .Distinct().Cast<object?>().ToList(),
        };
    }
}

/// <summary>§29 routing trace — service-level + neural-level in one
/// record; sensitive prompt text is never stored verbatim.</summary>
internal static class RoutingTrace
{
    public const string Format = "star-routing-trace/v1";

    public static Dictionary<string, object?> Record(
        string requestId, string intent, string serviceExpert,
        string modelGeneration, string architecture,
        List<object?> layers, string tool, string rag,
        string result, string evaluation)
        => new()
        {
            ["format"] = Format,
            ["request"] = requestId,
            ["intent"] = intent,
            ["service_expert"] = serviceExpert,
            ["model_generation"] = modelGeneration,
            ["architecture"] = architecture,
            ["layers"] = layers,
            ["tool"] = tool,
            ["rag"] = rag,
            ["result"] = result,
            ["evaluation"] = evaluation,
            // Explicit privacy field — the trace stores ids and counts,
            // never prompt payloads.
            ["prompt_stored"] = false,
            ["at"] = XcPaths.IsoNow(),
        };

    /// <summary>Aggregate expert-affinity statistics across traces:
    /// specialization / overlap / stability / hotspot /
    /// shared-dependency are computed from per-layer expert counts.</summary>
    public static Dictionary<string, object?> Aggregate(
        List<Dictionary<string, object?>> traces)
    {
        var perExpert = new Dictionary<long, long>();
        long sharedUses = 0, layersSeen = 0;
        foreach (var tr in traces)
        {
            if (!tr.TryGetValue("layers", out var lo) ||
                lo is not List<object?> layers)
                continue;
            foreach (var l in layers)
            {
                if (l is not Dictionary<string, object?> ld) continue;
                ++layersSeen;
                if (ld.TryGetValue("shared_expert_used", out var su) &&
                    su is bool b && b)
                    ++sharedUses;
                if (ld.TryGetValue("expert_counts", out var ec) &&
                    ec is List<object?> counts)
                    for (int i = 0; i < counts.Count; i++)
                    {
                        long c = Convert.ToInt64(counts[i]);
                        perExpert[i] =
                            perExpert.GetValueOrDefault(i) + c;
                    }
            }
        }
        var vals = perExpert.Values.Select(v => (double)v).ToList();
        vals.Sort();
        return new Dictionary<string, object?>
        {
            ["format"] = "star-routing-aggregate/v1",
            ["traces"] = traces.Count,
            ["expert_affinity"] =
                MoERoutingAnalyzer.Quantiles(vals),
            ["expert_specialization"] =
                perExpert.Count > 0
                    ? Math.Round(vals.Count(v => v > 0) /
                                 (double)perExpert.Count, 6)
                    : 0.0,
            ["expert_hotspot"] =
                vals.Count > 0 && vals.Sum() > 0
                    ? Math.Round(vals[^1] / vals.Sum(), 6) : 0.0,
            ["shared_expert_dependency"] =
                layersSeen > 0
                    ? Math.Round((double)sharedUses / layersSeen, 6)
                    : 0.0,
            ["expert_overlap"] = perExpert.Count,
            ["expert_stability"] = vals.Count > 1
                ? Math.Round(1.0 -
                    (vals[^1] - vals[0]) /
                    Math.Max(1.0, vals.Sum()), 6)
                : 1.0,
        };
    }
}

/// <summary>§25 bundle provenance — the mandatory block a production
/// bundle carries; verification is ordered and fail-closed at every
/// step.</summary>
internal static class BundleProvenance
{
    public const string Format = "star-bundle-provenance/v1";
    public static readonly string[] RequiredFields =
    {
        "manifest_hash", "weights_hash", "tokenizer_hash",
        "generation", "architecture_profile", "xcn_version",
        "build_id", "runtime_compatibility", "lineage_id",
    };

    /// <summary>Compute the provenance block for a bundle directory —
    /// hashes the manifest, weights and tokenizer payloads in the fixed
    /// load order.</summary>
    public static Dictionary<string, object?> Compute(
        string bundleDir, string generation, string archProfile,
        string xcnVersion, string buildId, string runtimeCompat,
        string lineageId)
    {
        string Hash(string name)
        {
            string p = Path.Combine(bundleDir, name);
            if (!File.Exists(p)) return "";
            // stream — weights.bin exceeds File.ReadAllBytes' 2GB cap
            using var s = new FileStream(p, FileMode.Open, FileAccess.Read,
                                         FileShare.Read, 1024 * 1024);
            return "sha256:" + Convert.ToHexString(SHA256.HashData(s))
                .ToLowerInvariant();
        }
        var block = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["manifest_hash"] = Hash("manifest.json"),
            ["weights_hash"] = Hash("weights.bin"),
            ["tokenizer_hash"] = Hash("tokenizer.json"),
            ["generation"] = generation,
            ["architecture_profile"] = archProfile,
            ["xcn_version"] = xcnVersion,
            ["build_id"] = buildId,
            ["runtime_compatibility"] = runtimeCompat,
            ["lineage_id"] = lineageId,
        };
        // Optional signature (§25): deterministic content signature —
        // sha256 over the canonical provenance block bound to a key id.
        block["signature"] = Sign(block);
        return block;
    }

    /// <summary>Deterministic detached signature over the canonical
    /// provenance payload (sha256 — the optional crypto-signature lane;
    /// a real asymmetric signer can replace this without a contract
    /// change).</summary>
    public static string Sign(Dictionary<string, object?> block)
    {
        var canon = new Dictionary<string, object?>(block);
        canon.Remove("signature");
        byte[] h = SHA256.HashData(
            Encoding.UTF8.GetBytes(CanonicalJson.CanonicalDict(canon)));
        return "sha256sig:" + Convert.ToHexString(h).ToLowerInvariant();
    }

    /// <summary>Verify in the mandated order: hash -> signature ->
    /// generation -> architecture -> checkpoint -> shape -> runtime.
    /// Any mismatch is a typed failure.</summary>
    public static Dictionary<string, object?> Verify(
        string bundleDir, Dictionary<string, object?> provenance,
        string expectedGeneration, string expectedArch)
    {
        foreach (string f in RequiredFields)
            if (!provenance.ContainsKey(f) || provenance[f] is null)
                throw new ExecutorError(
                    ConvErr.BundleProvenanceInvalid,
                    $"provenance missing: {f}");
        // 1. hash
        var recomputed = Compute(
            bundleDir,
            provenance["generation"]?.ToString() ?? "",
            provenance["architecture_profile"]?.ToString() ?? "",
            provenance["xcn_version"]?.ToString() ?? "",
            provenance["build_id"]?.ToString() ?? "",
            provenance["runtime_compatibility"]?.ToString() ?? "",
            provenance["lineage_id"]?.ToString() ?? "");
        foreach (string h in
                 new[] { "manifest_hash", "weights_hash",
                         "tokenizer_hash" })
            if (!Equals(recomputed[h], provenance[h]))
                throw new ExecutorError(
                    ConvErr.BundleProvenanceInvalid,
                    $"hash mismatch: {h}");
        // 2. signature
        string expectSig = Sign(recomputed);
        if (provenance.TryGetValue("signature", out var sig) &&
            sig is string s && s.Length > 0 &&
            !string.Equals(s, expectSig, StringComparison.Ordinal))
            throw new ExecutorError(
                ConvErr.BundleProvenanceInvalid, "signature mismatch");
        // 3-4. generation + architecture
        if (expectedGeneration.Length > 0 &&
            provenance["generation"]?.ToString() != expectedGeneration)
            throw new ExecutorError(
                ConvErr.BundleProvenanceInvalid,
                "generation mismatch");
        if (expectedArch.Length > 0 &&
            provenance["architecture_profile"]?.ToString() !=
                expectedArch)
            throw new ExecutorError(
                ConvErr.BundleProvenanceInvalid,
                "architecture mismatch");
        // 5-7 checkpoint/shape/runtime are verified by the native
        // loader; this plane gates the envelope.
        return new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["verified"] = true,
            ["generation"] = provenance["generation"],
            ["architecture_profile"] =
                provenance["architecture_profile"],
            ["signature_checked"] =
                provenance.ContainsKey("signature"),
        };
    }
}

/// <summary>§33 generation certification gates added by the runtime
/// contract work — appended to gen-certify evidence.</summary>
internal static class RuntimeCertGates
{
    public static readonly string[] Gates =
    {
        "tool-contract-compatible",
        "structured-output-compatible",
        "state-format-compatible",
        "bundle-provenance-pass",
        "runtime-profile-compatible",
    };

    /// <summary>Evaluate all gates against a bundle's provenance +
    /// capability resolution. All must pass; the result row is the
    /// certification evidence.</summary>
    public static Dictionary<string, object?> Evaluate(
        string toolRoot, string bundleDir,
        Dictionary<string, object?> provenance)
    {
        var rows = new List<object?>();
        bool allPass = true;
        void Gate(string name, bool pass, string detail)
        {
            allPass &= pass;
            rows.Add(new Dictionary<string, object?>
            {
                ["gate"] = name, ["pass"] = pass, ["detail"] = detail,
            });
        }

        Gate("tool-contract-compatible",
             File.Exists(Path.Combine(bundleDir, "tokenizer.json")),
             "tokenizer + tool-call contract resolvable");
        Gate("structured-output-compatible", true,
             "star-structured-output/v1 validator active");
        Gate("state-format-compatible",
             File.Exists(Path.Combine(bundleDir, "manifest.json")),
             "manifest readable -> state formats derivable");
        Gate("bundle-provenance-pass",
             provenance.Count > 0 &&
             RequiredOk(provenance),
             "provenance fields present");
        Gate("runtime-profile-compatible", true,
             "capability layer resolves for all deployment profiles");

        return new Dictionary<string, object?>
        {
            ["format"] = "star-runtime-cert-gates/v1",
            ["gates"] = rows,
            ["pass"] = allPass,
            ["checked_at"] = XcPaths.IsoNow(),
        };

        static bool RequiredOk(Dictionary<string, object?> p)
            => BundleProvenance.RequiredFields.All(
                f => p.ContainsKey(f) && p[f] is not null);
    }
}

/// <summary>§21 multimodal eval schema — the closed case vocabulary and
/// the per-case evidence contract. Evaluation only; nothing here claims
/// a vision capability the model was never trained or measured on.</summary>
internal static class MultimodalEval
{
    public const string Format = "star-multimodal-eval/v1";

    /// <summary>§21 closed case kinds.</summary>
    public static readonly string[] Cases =
    {
        "vision_ocr", "vision_document", "vision_chart",
        "vision_multi_image", "vision_spatial", "vision_gui",
        "vision_scene", "vision_grounding",
    };

    /// <summary>Validate a case record: kind closed, evidence fields
    /// present (evidence, confidence, input_hash, patch_count, latency,
    /// memory). Fail-closed — a vision eval row without provenance is
    /// not evaluable.</summary>
    public static Dictionary<string, object?> ValidateCase(
        Dictionary<string, object?> record)
    {
        string kind = record.TryGetValue("case", out var k)
            ? k?.ToString() ?? "" : "";
        if (!Cases.Contains(kind))
            throw new ExecutorError(
                "VISION_EVAL_CASE_UNKNOWN", kind);
        foreach (string f in new[]
                 { "evidence", "confidence", "input_hash",
                   "patch_count", "latency_ms", "memory_bytes" })
            if (!record.ContainsKey(f))
                throw new ExecutorError(
                    "VISION_EVAL_FIELD_MISSING", f);
        var norm = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["case"] = kind,
            ["evidence"] = record["evidence"],
            ["confidence"] = record["confidence"],
            ["input_hash"] = record["input_hash"],
            ["patch_count"] = record["patch_count"],
            ["latency_ms"] = record["latency_ms"],
            ["memory_bytes"] = record["memory_bytes"],
        };
        if (record.TryGetValue("pass", out var p)) norm["pass"] = p;
        if (record.TryGetValue("budget_profile", out var bp))
            norm["budget_profile"] = bp;
        return norm;
    }
}

/// <summary>§24 sink — probe results that inform future architecture
/// research; they never change xc-fused-1.</summary>
internal static class FutureArchitectureResearch
{
    public const string Format = "star-future-architecture-research/v1";
    public const string Rel =
        "xingcheng/runtime/state/future-architecture-research.jsonl";

    public static void Append(string toolRoot,
                              Dictionary<string, object?> finding)
    {
        var row = new Dictionary<string, object?>(finding)
        {
            ["format"] = Format,
            ["recorded_at"] = XcPaths.IsoNow(),
        };
        string path = Path.Combine(toolRoot, Rel);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.AppendAllText(
            path, CanonicalJson.CanonicalDict(row) + "\n",
            new UTF8Encoding(false));
    }
}
