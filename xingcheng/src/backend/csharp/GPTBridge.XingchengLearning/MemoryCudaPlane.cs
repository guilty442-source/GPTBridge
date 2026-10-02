// MemoryCudaPlane.cs — C# contract plane for NativeMemoryCudaPlane
// (memory/CUDA directive). The native lane (xcm_memplane.h) owns the
// allocations; this owns policy, validation, and the contracts the
// governed layer enforces.
//
//   PrecisionPolicy     §1/§2/§22/§61: FP64 = oracle only, BF16 =
//                       production compute on sm_86, router/norm
//                       accumulate FP32, KV INT8, FP8/FP4
//                       DISABLED_BY_HARDWARE — no production FP8/FP4
//                       kernel work this generation.
//   StreamPolicy        §17/§18: fixed stream lanes, decode has the
//                       highest latency priority; device-wide sync is
//                       a lifecycle event, not a sync primitive.
//   PressureLadder      §11/§32: the exact ordered eviction list —
//                       prefix (reconstructable) evicts before active
//                       state; reject_request is the last step, OOM
//                       is never the answer.
//   PrefillChunkPlan    §42/§43: adaptive chunking that keeps
//                       delta-state semantics exact (commit per
//                       chunk, numerically identical to one-shot).
//   AlignmentScore      §23: TensorCore-friendliness of a shape.
//   KernelDispatchKey   §63: (cc, precision, shape, batch, op) — the
//                       single registry key; no scattered if/else.
//   ValidateTelemetry   §54/§55: star-cuda-memory-telemetry/v1.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class MemoryCudaPlane
{
    public const string TelemetryFormat =
        "star-cuda-memory-telemetry/v1";

    // --------------------------------------------------- precision --

    /// <summary>§2 production precision map for sm_86. FP8/FP4 are
    /// DISABLED_BY_HARDWARE — the dispatch keeps the artifact formats
    /// but must not spend production kernels on them.</summary>
    public static readonly Dictionary<string, string> PrecisionPolicy =
        new(StringComparer.Ordinal)
        {
            ["common_gemm"] = "BF16",
            ["attention"] = "BF16",
            ["delta_weights"] = "BF16",
            ["embedding"] = "BF16",
            ["shared_expert"] = "BF16",
            ["hot_routed_expert"] = "BF16",
            ["router"] = "FP32",
            ["norm_accumulate"] = "FP32",
            ["norm_storage"] = "BF16_CANDIDATE",
            ["kv"] = "INT8",
            ["delta_state"] = "BF16_CANDIDATE",
            ["cold_expert_storage"] = "INT8_INT4",
            ["fp8"] = "DISABLED_BY_HARDWARE",
            ["fp4"] = "DISABLED_BY_HARDWARE",
            ["fp64_role"] = "ORACLE_ONLY",
        };

    public static Dictionary<string, object?> PrecisionReport() => new()
    {
        ["ok"] = true,
        ["format"] = "star-precision-policy/v1",
        ["target_cc"] = "sm_86",
        ["production_primary"] = "BF16",
        ["fp64_role"] = "ORACLE_ONLY",
        ["policy"] = PrecisionPolicy.ToDictionary(
            kv => kv.Key, kv => (object?)kv.Value),
        ["tensor_core_lanes"] = new object?[]
            { "bf16", "fp16", "int8" }.ToList(),
    };

    // -------------------------------------------------- pressure ----

    /// <summary>§11 the ladder, in order. Index 0 = first response;
    /// index 7 = last resort. prefix (reconstructable) leaves before
    /// any active state (§32).</summary>
    public static readonly string[] Ladder =
    {
        "evict_cold_prefix", "reduce_warm_prefix",
        "evict_routed_expert", "shrink_expert_hotset",
        "reduce_batch", "reduce_prefill_chunk",
        "spill_eligible_state", "reject_request",
    };

    public static int LadderIndex(string step)
        => Array.IndexOf(Ladder, step);

    /// <summary>Given a deficit and the reclaimable bytes per step,
    /// return the ordered steps needed — never jumps to reject while
    /// an earlier step can satisfy the deficit.</summary>
    public static Dictionary<string, object?> PlanPressure(
        long deficitBytes, Dictionary<string, long> reclaimable)
    {
        var steps = new List<object?>();
        long remaining = deficitBytes;
        for (int i = 0; i < Ladder.Length && remaining > 0; ++i)
        {
            string s = Ladder[i];
            long avail = reclaimable.GetValueOrDefault(s, 0);
            if (s == "reject_request") avail = 0;
            long take = Math.Min(avail, remaining);
            remaining -= take;
            steps.Add(new Dictionary<string, object?>
            {
                ["order"] = i, ["step"] = s,
                ["reclaimed_bytes"] = take,
            });
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["deficit_bytes"] = deficitBytes,
            ["covered"] = remaining <= 0,
            ["residual_bytes"] = Math.Max(0, remaining),
            ["must_reject"] = remaining > 0,
            ["steps"] = steps,
        };
    }

    // ------------------------------------------------ prefill chunk --

    /// <summary>§42: pick the largest chunk that fits free VRAM while
    /// active decode traffic and state growth stay protected.
    /// Chunk size is one of 128/256/512/1024; the result never breaks
    /// delta-state semantics (commit per chunk, §43).</summary>
    public static readonly long[] ChunkCandidates =
        { 128, 256, 512, 1024 };

    public static Dictionary<string, object?> PrefillChunkPlan(
        long freeVramBytes, long bytesPerToken, long activeDecodeKbs)
    {
        if (bytesPerToken <= 0)
            throw new ExecutorError("MEMPLANE_BUDGET_EXCEEDED",
                "bytes_per_token must be > 0");
        long usable = freeVramBytes - activeDecodeKbs * 1024;
        long chosen = 128;
        foreach (long c in ChunkCandidates)
            if (c * bytesPerToken <= usable) chosen = c;
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-prefill-chunk/v1",
            ["adaptive_prefill_chunk"] = chosen,
            ["delta_state_semantics"] = "commit_per_chunk",
            ["numerical_equivalence"] = "one_shot_prefill",
        };
    }

    // -------------------------------------------- tensor-core align --

    /// <summary>§23 alignment score: multiples of the bf16 tile granule
    /// (8) score 1.0, multiples of 4 score 0.5, everything else 0 —
    /// scale profiles must not pick terrible matrix sizes to save a
    /// few parameters.</summary>
    public static double AlignmentScore(long dim)
    {
        if (dim <= 0) return 0;
        if (dim % 8 == 0) return 1.0;
        if (dim % 4 == 0) return 0.5;
        if (dim % 2 == 0) return 0.25;
        return 0;
    }

    // ------------------------------------------------- telemetry ----

    /// <summary>§54/§55 schema check for the native telemetry emit.</summary>
    public static Dictionary<string, object?> ValidateTelemetry(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("MEMPLANE_TELEMETRY_INVALID",
                "telemetry must be object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != TelemetryFormat)
            throw new ExecutorError("MEMPLANE_TELEMETRY_INVALID",
                $"expected format {TelemetryFormat}");
        foreach (var k in new[]
                 { "device_pool_used", "device_pool_peak",
                   "workspace_peak", "pinned_host_bytes",
                   "h2d_bytes", "d2h_bytes", "d2d_bytes", "tiers" })
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError("MEMPLANE_TELEMETRY_INVALID",
                    $"missing field {k}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = TelemetryFormat,
            ["cuda_present"] =
                el.TryGetProperty("cuda_present", out var cp) &&
                cp.ValueKind == JsonValueKind.True,
        };
    }
}
