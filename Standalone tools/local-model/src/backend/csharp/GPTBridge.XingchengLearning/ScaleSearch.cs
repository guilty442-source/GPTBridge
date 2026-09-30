// ScaleSearch.cs — HardwareAwareScaleSearch (efficiency/scale directive
// §2-§6, §49-§52). Liquid-style hardware-in-the-loop scaling: the model
// shape is never hand-picked — a hardware profile produces candidate
// shapes, each is memory-estimated, hardware-aligned and ranked. The
// canonical architecture mechanism (xc-fused-1 schedule, Top-2, shared
// expert, DeltaNet/attention kinds) is invariant: only shape numbers
// move, never topology.
//
//   Search            §2-§6: profile -> candidate shapes -> memory
//                     estimate -> alignment score -> ranked shortlist;
//                     infeasible working sets never become candidates.
//   ScoreCandidate    §50-§52: capability/hardware ratio accounting +
//                     NAIVE_SCALING detection (params×N & cost×N).
//
// Emits: star-hardware-scale-search/v1, star-scale-candidate/v1.
// CAPABILITY_TRAINING_FROZEN: planning only — no weights touched.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class ScaleSearch
{
    public const string SearchFormat = "star-hardware-scale-search/v1";
    public const string CandidateFormat = "star-scale-candidate/v1";

    // §49 the three compared candidate families.
    public static readonly string[] CandidateKinds =
        { "DENSE_SCALE", "DEPTH_SCALE", "EXPERT_SCALE" };

    // §3 shape dimensions the search may move — everything else
    // (schedule ratio, top_k, shared expert, layer kinds, tokenizer,
    // context architecture) is pinned by xc-fused-1.
    public static readonly string[] ShapeFields =
    {
        "hidden", "layers", "heads", "kv_heads",
        "expert_intermediate", "expert_count", "context_target",
        "precision",
    };

    // §30 minimum efficient routed-expert intermediate width — below
    // this, grouped-GEMM launch overhead dominates; candidates are
    // rejected before scoring.
    public const long MinEfficientExpertWidth = 256;

    private static long Num(JsonElement r, string k, long d = 0) =>
        r.TryGetProperty(k, out var v) && v.ValueKind ==
            JsonValueKind.Number && v.TryGetInt64(out long n) ? n : d;
    private static double DNum(JsonElement r, string k, double d = 0) =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.Number ? v.GetDouble() : d;

    // --------------------------------------------------- estimation --

    /// <summary>§5 runtime-memory estimate for a shape. Any candidate
    /// whose inference or training working set cannot live on the
    /// hardware is rejected before ranking — memory is an architecture
    /// constraint, not an afterthought.</summary>
    public static Dictionary<string, object?> MemoryEstimate(
        long hidden, long layers, long kvHeads, long headDim,
        long expertIntermediate, long expertCount, long sharedExperts,
        long contextTarget, double weightBytes,
        long deltaStateBytesPerLayer, long prefixCacheBytes,
        long vocabSize, bool forTraining)
    {
        // dense weights: embed + per-layer (attn + shared + norms)
        long embed = hidden * vocabSize;
        long perLayerDense =
            hidden * hidden * 4 +               // qkv+o approx
            hidden * expertIntermediate * 3 * sharedExperts +
            hidden * 8;                          // norms + gates
        long expertParams =
            layers / 2 * expertCount *
            expertIntermediate * hidden * 3;    // MoE every other layer
        long weights =
            (embed + perLayerDense * layers + expertParams) *
            (long)weightBytes;
        long kv = layers * kvHeads * headDim * 2 /*k+v*/ *
                  contextTarget * 1 /*kv-int8 eligible*/;
        long deltaState = deltaStateBytesPerLayer * layers;
        long routedExpertBytes = expertParams * (long)weightBytes;
        long workspace = Math.Max(64L << 20,
            (hidden * hidden + hidden * contextTarget) * 2);
        long inference = weights + kv + deltaState +
                         prefixCacheBytes + workspace;
        long training = 0;
        if (forTraining)
        {
            long denseParams = embed + perLayerDense * layers;
            long adam = denseParams * 8 +        // m+v fp32
                        denseParams * 4;         // grads
            long acts = layers * contextTarget * hidden * 2 * 16;
            training = inference + adam + acts;
        }
        return new Dictionary<string, object?>
        {
            ["weights_bytes"] = weights,
            ["kv_bytes"] = kv,
            ["delta_state_bytes"] = deltaState,
            ["prefix_cache_bytes"] = prefixCacheBytes,
            ["routed_expert_bytes"] = routedExpertBytes,
            ["workspace_bytes"] = workspace,
            ["inference_working_set_bytes"] = inference,
            ["training_working_set_bytes"] = training,
        };
    }

    // --------------------------------------------------- alignment --

    /// <summary>§4 hardware alignment score 0..1. Shapes that waste
    /// silicon (odd widths, misaligned expert intermediates, huge state
    /// footprint) score below clean shapes regardless of parameter
    /// count.</summary>
    private static Dictionary<string, object?> AlignmentScore(
        long hidden, long heads, long kvHeads, long expertIntermediate,
        long expertCount, long layers, long deltaStatePerLayer,
        long kvBytes, HardwareCaps caps)
    {
        double cuda = 1.0;
        if (hidden % 128 != 0) cuda -= 0.25;
        else if (hidden % 256 != 0) cuda -= 0.10;
        if (heads <= 0 || hidden % Math.Max(1, heads) != 0) cuda -= 0.3;
        if (kvHeads <= 0 || heads % Math.Max(1, kvHeads) != 0)
            cuda -= 0.2;
        cuda = Math.Clamp(cuda, 0, 1);

        double simd = 1.0;                     // AVX-512 lane = 16 fp32
        if (hidden % 64 != 0) simd -= 0.3;
        if (expertIntermediate % 64 != 0) simd -= 0.3;
        simd = Math.Clamp(simd, 0, 1);

        double gemm = 1.0;
        if (expertIntermediate % 128 != 0) gemm -= 0.3;
        if (expertCount > 0 && expertCount % 8 != 0) gemm -= 0.2;
        gemm = Math.Clamp(gemm, 0, 1);

        double mem = 1.0;
        long footprint = deltaStatePerLayer * layers + kvBytes;
        if (caps.VramBytes > 0 &&
            footprint > caps.VramBytes / 2) mem -= 0.4;
        else if (caps.VramBytes > 0 &&
                 footprint > caps.VramBytes / 4) mem -= 0.15;
        mem = Math.Clamp(mem, 0, 1);

        double score = 0.30 * cuda + 0.20 * simd +
                       0.25 * gemm + 0.25 * mem;
        return new Dictionary<string, object?>
        {
            ["cuda_alignment"] = Math.Round(cuda, 3),
            ["avx512_alignment"] = Math.Round(simd, 3),
            ["memory_alignment"] = Math.Round(mem, 3),
            ["gemm_efficiency"] = Math.Round(gemm, 3),
            ["hardware_alignment_score"] = Math.Round(score, 4),
        };
    }

    // ------------------------------------------------------ search --

    /// <summary>§2 hardware-in-the-loop candidate search. Input:
    /// {hardware:{...HardwareCaps}, base_shape:{hidden,layers,heads,
    /// kv_heads,expert_intermediate,expert_count,shared_experts,
    /// context_target,vocab_size,delta_state_bytes_per_layer,
    /// prefix_cache_bytes}, target_param_scale (default 2),
    /// for_training, candidate_count}. Emits a ranked shortlist —
    /// every candidate kind competes (§49), never a default.</summary>
    public static Dictionary<string, object?> Search(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("SCALE_SHAPE_INEFFICIENT",
                "search input must be an object");
        var caps = HardwareCaps.From(
            el.TryGetProperty("hardware", out var hw) &&
            hw.ValueKind == JsonValueKind.Object ? hw : el);
        if (!el.TryGetProperty("base_shape", out var bs) ||
            bs.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("SCALE_SHAPE_INEFFICIENT",
                "base_shape required");

        long hidden = Num(bs, "hidden", 1024),
             layers = Num(bs, "layers", 24),
             heads = Num(bs, "heads", 16),
             kvHeads = Num(bs, "kv_heads", Math.Max(1, heads / 4)),
             ei = Num(bs, "expert_intermediate", 2048),
             ec = Num(bs, "expert_count", 8),
             shared = Num(bs, "shared_experts", 1),
             ctx = Num(bs, "context_target", 4096),
             vocab = Num(bs, "vocab_size", 8192),
             ds = Num(bs, "delta_state_bytes_per_layer", 1 << 20),
             prefix = Num(bs, "prefix_cache_bytes", 256L << 20);
        double wb = DNum(bs, "weight_bytes_per_param", 2.0);
        double targetScale = DNum(el, "target_param_scale", 2.0);
        // §61: STANDARD balances capability/latency; EXTREME_SPARSE
        // ranks active-efficiency first and opens the 256E sweep.
        string scaleClass =
            el.TryGetProperty("scale_class", out var sc) &&
            sc.ValueKind == JsonValueKind.String
                ? sc.GetString()!.ToUpperInvariant() : "STANDARD";
        if (scaleClass != "STANDARD" && scaleClass != "EXTREME_SPARSE")
            throw new ExecutorError("SCALE_TIER_INVALID",
                $"scale_class '{scaleClass}' — STANDARD or " +
                "EXTREME_SPARSE only (§61)");
        bool forTraining =
            el.TryGetProperty("for_training", out var ft) &&
            ft.ValueKind == JsonValueKind.True;
        int candCount = (int)Math.Clamp(
            Num(el, "candidate_count", 12), 1, 64);
        long headDim = hidden / Math.Max(1, heads);
        long baseParams = hidden * vocab +
            (hidden * hidden * 4 +
             hidden * ei * 3 * shared + hidden * 8) * layers +
            layers / 2 * ec * ei * hidden * 3;

        var candidates = new List<Dictionary<string, object?>>();
        var rejected = new List<Dictionary<string, object?>>();

        void Consider(string kind, long h, long l, long hd, long kvh,
                      long eiw, long ecnt, long ctxT)
        {
            if (eiw < MinEfficientExpertWidth && ecnt > 0)
            {
                rejected.Add(new Dictionary<string, object?>
                {
                    ["kind"] = kind,
                    ["shape"] = $"h{h} l{l} ei{eiw} e{ecnt}",
                    ["reject"] = "SCALE_SHAPE_INEFFICIENT",
                    ["reason"] = "expert_intermediate below " +
                        $"minimum_efficient_expert_width " +
                        $"({MinEfficientExpertWidth})",
                });
                return;
            }
            var mem = MemoryEstimate(h, l, kvh, headDim, eiw, ecnt,
                shared, ctxT, wb, ds, prefix, vocab, forTraining);
            long fit = forTraining
                ? (long)mem["training_working_set_bytes"]!
                : (long)mem["inference_working_set_bytes"]!;
            long budget = caps.VramBytes + caps.RamBytes;
            if (budget > 0 && fit > budget)
            {
                rejected.Add(new Dictionary<string, object?>
                {
                    ["kind"] = kind,
                    ["shape"] = $"h{h} l{l} ei{eiw} e{ecnt}",
                    ["reject"] = "SCALE_SHAPE_INEFFICIENT",
                    ["reason"] = $"working_set {fit} exceeds " +
                                 $"resident budget {budget}",
                });
                return;
            }
            long pars = h * vocab +
                (h * h * 4 + h * eiw * 3 * shared + h * 8) * l +
                l / 2 * ecnt * eiw * h * 3;
            long activePars = pars - l / 2 * Math.Max(0, ecnt - 2) *
                              eiw * h * 3;   // Top-2 routed
            var align = AlignmentScore(h, hd, kvh, eiw, ecnt, l, ds,
                (long)mem["kv_bytes"]!, caps);
            double hscore =
                Convert.ToDouble(align["hardware_alignment_score"]);
            // §50 capacity/hardware ratio proxy: params reachable per
            // active FLOP, weighted by alignment.
            double ratio = activePars > 0
                ? hscore * (double)pars / activePars : 0;
            candidates.Add(new Dictionary<string, object?>
            {
                ["format"] = CandidateFormat,
                ["kind"] = kind,
                ["shape"] = new Dictionary<string, object?>
                {
                    ["hidden"] = h, ["layers"] = l, ["heads"] = hd,
                    ["kv_heads"] = kvh,
                    ["expert_intermediate"] = eiw,
                    ["expert_count"] = ecnt,
                    ["context_target"] = ctxT,
                },
                ["total_params"] = pars,
                ["active_params"] = activePars,
                ["memory_estimate"] = mem,
                ["alignment"] = align,
                ["capacity_hardware_ratio"] = Math.Round(ratio, 4),
                // §51 naive-scaling flag: uniform blow-up of params,
                // active FLOPs and memory marks a bad candidate.
                ["naive_scaling"] =
                    pars >= baseParams * 4 &&
                    activePars >= baseParams * 4,
            });
        }

        // §3 sweep — canonical schedule preserved: layer count always
        // a multiple of the 3:1 delta:full block (4-layer period).
        long Period(long l) => l - l % 4;
        // DENSE: width × sqrt(scale), keep depth band.
        for (double w = 1.0; w <= targetScale; w += 0.5)
        {
            long h = ((long)(hidden * Math.Sqrt(w)) / 64) * 64;
            if (h <= hidden || h % heads != 0) continue;
            Consider("DENSE_SCALE", h, layers, heads, kvHeads,
                     ei, ec, ctx);
            if (candidates.Count >= candCount) break;
        }
        // DEPTH: same width, deeper 3:1 periods (§12 schedule intact).
        for (long l = layers + 4; l <= layers * targetScale; l += 4)
        {
            Consider("DEPTH_SCALE", hidden, Period(l), heads, kvHeads,
                     ei, ec, ctx);
            if (candidates.Count >= candCount * 2) break;
        }
        // EXPERT: same core, routed count up — Top-2 stays fixed (§26).
        // §21-24: EXTREME_SPARSE opens the 256E stretch candidate.
        long[] expertSteps = scaleClass == "EXTREME_SPARSE"
            ? new[] { 16L, 32L, 64L, 128L, 256L }
            : new[] { 16L, 32L, 64L, 128L };
        foreach (long n in expertSteps)
        {
            if (n <= ec) continue;
            Consider("EXPERT_SCALE", hidden, layers, heads, kvHeads,
                     ei, n, ctx);
            long eiFine = Math.Max(MinEfficientExpertWidth,
                ei / 2 / 64 * 64);
            if (eiFine != ei)
                Consider("EXPERT_SCALE", hidden, layers, heads, kvHeads,
                         eiFine, n * 2, ctx);
        }

        // §61 ranking: EXTREME_SPARSE orders by capacity/active ratio
        // (params reachable per active param, alignment-weighted);
        // STANDARD orders by aligned capability headroom — total
        // capacity per resident byte, so a candidate that buys params
        // at flat working-set still wins.
        var ranked = candidates
            .OrderByDescending(c => scaleClass == "EXTREME_SPARSE"
                ? Convert.ToDouble(c["capacity_hardware_ratio"])
                : Convert.ToDouble(c["capacity_hardware_ratio"]) *
                  Math.Log2(1.0 + (double)
                      Convert.ToInt64(c["active_params"]) /
                      Convert.ToInt64(c["total_params"]) * 8.0))
            .Take(candCount)
            .ToList();
        for (int i = 0; i < ranked.Count; i++)
            ranked[i]["rank"] = i + 1;
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = SearchFormat,
            ["base_params"] = baseParams,
            ["hardware"] = caps.ToDict(),
            ["target_param_scale"] = targetScale,
            ["scale_class"] = scaleClass,
            ["for_training"] = forTraining,
            ["canonical_invariants"] = new Dictionary<string, object?>
            {
                ["architecture"] = "xc-fused-1",
                ["schedule"] = "delta3:full1 period preserved",
                ["top_k"] = 2, ["shared_expert"] = "ALWAYS_HOT",
            },
            ["candidates"] = ranked.Cast<object?>().ToList(),
            ["rejected"] = rejected.Cast<object?>().ToList(),
            ["selected"] = ranked.Count > 0 ? ranked[0] : null,
        };
    }

    // -------------------------------------------------- scorecard --

    /// <summary>§50-§52 scorecard for one candidate: capability gain vs
    /// cost + the parameter-efficiency metric family. A candidate whose
    /// cost grows with params is flagged NAIVE_SCALING and cannot be
    /// listed as an efficient-scale success (§51).</summary>
    public static Dictionary<string, object?> ScoreCandidate(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("SCALE_SHAPE_INEFFICIENT",
                "scorecard input must be an object");
        double cap = DNum(el, "capability_gain", 0);
        double trainCost = DNum(el, "training_cost_gpuh", 0);
        double inferCost = DNum(el, "inference_cost_per_mtok", 0);
        double vram = DNum(el, "vram_bytes", 0);
        double ram = DNum(el, "ram_bytes", 0);
        double flops = DNum(el, "active_flops_per_token", 0);
        double energy = DNum(el, "energy_wh_per_mtok", 0);
        long total = Num(el, "total_params", 1),
             active = Num(el, "active_params", 1),
             trainable = Num(el, "trainable_params", 1);
        double baseTotal = DNum(el, "baseline_total_params", total);
        double baseFlops = DNum(el, "baseline_active_flops", flops);
        bool naive = total >= baseTotal * 4 &&
                     flops >= baseFlops * 4 &&
                     vram >= 4 * DNum(el, "baseline_vram_bytes", vram) &&
                     ram >= 4 * DNum(el, "baseline_ram_bytes", ram);
        double ratio = 0;
        if (cap > 0 && inferCost > 0)
            ratio = cap / inferCost *
                    ((double)total / Math.Max(1, active));
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = CandidateFormat,
            ["capacity_hardware_ratio"] = Math.Round(ratio, 6),
            ["naive_scaling"] = naive,
            ["naive_scaling_rejected"] = naive,
            ["efficiency"] = new Dictionary<string, object?>
            {
                ["capability_per_total_param"] = cap / Math.Max(1, total),
                ["capability_per_active_param"] =
                    cap / Math.Max(1, active),
                ["capability_per_trainable_param"] =
                    cap / Math.Max(1, trainable),
                ["capability_per_gb_vram"] =
                    vram > 0 ? cap / (vram / (1 << 30)) : 0,
                ["capability_per_gb_ram"] =
                    ram > 0 ? cap / (ram / (1 << 30)) : 0,
                ["capability_per_tflop"] =
                    flops > 0 ? cap / (flops / 1e12) : 0,
                ["capability_per_watt_hour"] =
                    energy > 0 ? cap / energy : 0,
            },
            ["costs"] = new Dictionary<string, object?>
            {
                ["training_cost_gpuh"] = trainCost,
                ["inference_cost_per_mtok"] = inferCost,
                ["ttft_ms"] = DNum(el, "ttft_ms", 0),
                ["itl_ms"] = DNum(el, "itl_ms", 0),
                ["tps"] = DNum(el, "tps", 0),
            },
        };
    }
}
