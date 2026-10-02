// CudaTrainingPlane.cs — Xingcheng NativeCudaTrainingPlane contract
// (training-acceleration directive §0-§72), C# governance layer over
// the native CUDA training runtime. Complements TrainingAcceleration
// (telemetry/bottleneck/eval/pilot/batch-plan) with the device-side
// contracts: precision map, stream lanes, arenas, graph cache key,
// MoE/sparsity rules, residency, phases and prohibitions.
//
// The sole KPI is TIME_TO_QUALIFIED_MODEL (§0/§65): a strategy wins
// when it reaches the capability threshold + regression PASS sooner.
// GPU utilization is an ingredient, never the verdict (§64/§72).
//
// Contracts only — every method is pure emission/validation over
// recorded evidence; no weights, no training side effects.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CudaTrainingPlane
{
    public const string PlaneFormat = "star-cuda-training-plane/v1";
    public const string PrecisionMapFormat =
        "star-train-precision-map/v1";
    public const string GraphKeyFormat =
        "star-cuda-training-graph-key/v1";
    public const string AdamWFormat = "star-fused-adamw/v1";

    // §19/§33 fixed stream lanes — created once per run, never per
    // step.
    public static readonly string[] StreamLanes =
    {
        "TRAIN_H2D", "TRAIN_COMPUTE", "CHECKPOINT_D2H",
        "EXPERT_PREFETCH", "EVAL",
    };

    // §9/§10 arena tiers — every training GPU allocation is
    // stream-ordered pool memory in exactly one tier; hot-path
    // cudaMalloc/cudaFree is denied (§8/§71).
    public static readonly string[] ArenaTiers =
    {
        "ACTIVATION", "GRADIENT", "OPTIMIZER", "WORKSPACE",
    };

    // §30 graph-cache key — capture is only valid while every field
    // is stable; a shape change invalidates the cache.
    public static readonly string[] GraphKeyFields =
    {
        "sequence_bucket", "microbatch", "precision",
        "architecture_hash", "trainable_map_hash",
    };

    // §3 required production-training precision by tensor class.
    // FP64 remains oracle/gradcheck/parity/debug only (§2).
    private static readonly (string cls, string prec)[] PrecisionReq =
    {
        ("weights_compute", "BF16"), ("activations", "BF16"),
        ("gemm", "BF16_TENSOR_CORE"), ("attention", "BF16"),
        ("delta", "BF16"), ("shared_expert", "BF16"),
        ("routed_expert", "BF16"), ("router", "FP32"),
        ("norm_accumulation", "FP32"), ("loss_accumulation", "FP32"),
        ("optimizer_moments", "FP32"),
    };

    // §66-§69 phased delivery order.
    private static readonly (int id, string name, string[] items)[]
        Phases =
    {
        (1, "bf16_pipeline", new[]
        {
            "bf16_training_path", "training_telemetry",
            "pinned_data_pipeline", "arena_allocator",
            "pilot_300m_50step", "before_after_report",
        }),
        (2, "sparse_optimizer", new[]
        {
            "grouped_moe", "sparse_backward", "sparse_optimizer",
            "fused_adamw", "capability_run_300m_200step",
        }),
        (3, "graph_and_overlap", new[]
        {
            "cuda_graph", "kernel_fusion", "async_checkpoint",
            "eval_tiers", "time_to_qualified_model",
        }),
    };

    private static string Str(JsonElement r, string k,
        string d = "") =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? d : d;
    private static long Num(JsonElement r, string k, long d = 0) =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.Number &&
        v.TryGetInt64(out long n) ? n : d;

    // ----------------------------------------------------- plane ---

    // §0-§72 the whole plane contract in one emission — what the
    // runtime must do, what it must never do, and what has actually
    // landed.
    public static Dictionary<string, object?> Contract() =>
        new()
        {
            ["ok"] = true,
            ["format"] = PlaneFormat,
            ["kpi"] = "TIME_TO_QUALIFIED_MODEL",
            ["architecture_frozen"] = true,
            ["frozen_items"] = new object?[]
            {
                "HybridCausalDecoder", "Delta_x3_FullAttn_x1",
                "Top2_routing", "SharedExpert", "Tokenizer",
                "XCN10", "ModelTensorContract",
            },
            ["precision_map"] = PrecisionReq.Select(p =>
                (object?)new Dictionary<string, object?>
                {
                    ["class"] = p.cls, ["precision"] = p.prec,
                }).ToList(),
            ["fp64_role"] = "oracle_gradcheck_parity_debug_only",
            ["precision_gate"] = "bf16-training-cert",
            ["cuda_target"] = new Dictionary<string, object?>
            {
                ["sm_native"] = "sm_86",
                ["ptx_fallback"] = true,
                ["generic_ptx_only_denied"] = true,
            },
            ["stream_lanes"] = StreamLanes.Cast<object?>().ToList(),
            ["arena_tiers"] = ArenaTiers.Cast<object?>().ToList(),
            ["memory_pool"] = new Dictionary<string, object?>
            {
                ["allocator"] = "cudaMallocAsync/cudaFreeAsync",
                ["model"] = "stream_ordered_pool",
                ["hot_path_malloc_denied"] = true,
            },
            ["graph_key_fields"] =
                GraphKeyFields.Cast<object?>().ToList(),
            ["moe_rules"] = new object?[]
            {
                "grouped_by_expert_gemm",
                "sparse_backward_active_only",
                "inactive_expert_zero_work_hard_rule",
            },
            ["sparse_optimizer"] = new object?[]
            {
                "trainable_tensor_registry",
                "frozen_no_gradient_no_optimizer_state",
                "adamw_traverse_registry_only",
            },
            ["residency_20b"] = new Dictionary<string, object?>
            {
                ["active_compute_max"] = "1B",
                ["trainable_preferred"] = "100M-500M",
                ["unused_experts"] = "ram_nvme",
                ["full_optimizer_load_denied"] = true,
            },
            ["npu_rule"] = "inference_only_no_training",
            ["cpu_rule"] = "io_batchprep_scheduler_checkpoint_only",
            ["phases"] = Phases.Select(p =>
                (object?)new Dictionary<string, object?>
                {
                    ["phase"] = p.id, ["name"] = p.name,
                    ["items"] = p.items.Cast<object?>().ToList(),
                }).ToList(),
            ["prohibitions"] = new object?[]
            {
                "python_training", "pytorch", "jax",
                "full_model_fp64", "fake_fp8", "fake_fp4",
                "per_step_full_eval", "per_step_full_checkpoint",
                "frozen_tensor_optimizer_state",
                "inactive_expert_backward",
                "hot_path_malloc_free",
                "routine_device_synchronize",
                "repeated_teacher_inference",
            },
            ["landed"] = new object?[]
            {
                "fused_adamw_kernel_certified",
                "sqsum_norm_kernel_certified",
                "trainer_env_gate_XINGCHENG_TRAINER_CUDA_OPT",
                "kernel_optimizations_bit_identical",
                "telemetry_validate", "bottleneck_classify",
                "eval_tiers", "pilot_ladder", "batch_plan",
                "precision_policy", "distill_artifact",
                "time_to_quality", "speed_gate",
            },
        };

    // ------------------------------------------------ precision ----

    // §2-§3 emit the required precision map, or validate a caller's
    // map against it — a wrong class/precision pairing is a hard
    // fail (production training must not quietly drift to FP64
    // GEMM or fake low precision).
    public static Dictionary<string, object?> PrecisionMap(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            return new()
            {
                ["ok"] = true, ["format"] = PrecisionMapFormat,
                ["required"] = PrecisionReq.Select(p =>
                    (object?)new Dictionary<string, object?>
                    {
                        ["class"] = p.cls,
                        ["precision"] = p.prec,
                    }).ToList(),
                ["fp64_role"] =
                    "oracle_gradcheck_parity_debug_only",
            };
        var bad = new List<object?>();
        foreach (var (cls, prec) in PrecisionReq)
        {
            string got = Str(el, cls, "");
            if (got != prec)
                bad.Add(new Dictionary<string, object?>
                {
                    ["class"] = cls, ["required"] = prec,
                    ["got"] = got == "" ? "MISSING" : got,
                });
        }
        return new()
        {
            ["ok"] = bad.Count == 0,
            ["format"] = PrecisionMapFormat,
            ["verdict"] = bad.Count == 0
                ? "PRECISION_MAP_VALID" : "PRECISION_MAP_INVALID",
            ["mismatches"] = bad,
        };
    }

    // ------------------------------------------------ graph key ----

    // §30-§32 validate a CUDA-graph cache key: sequence bucket must
    // be a fixed bucket (same bucket = same shape = graphable),
    // precision must be the certified map's compute precision, and
    // both hashes must be present — a missing hash means a stale
    // graph can silently serve the wrong trainable map.
    public static Dictionary<string, object?> GraphKey(
        JsonElement el)
    {
        var bad = new List<object?>();
        long bucket = Num(el, "sequence_bucket", -1);
        if (!TrainingAcceleration.SeqBuckets.Contains((int)bucket))
            bad.Add(new Dictionary<string, object?>
            {
                ["field"] = "sequence_bucket",
                ["rule"] = "fixed bucket required",
                ["got"] = bucket,
            });
        if (Num(el, "microbatch", 0) <= 0)
            bad.Add(new Dictionary<string, object?>
            {
                ["field"] = "microbatch", ["rule"] = ">0 required",
            });
        string prec = Str(el, "precision");
        if (prec != "BF16")
            bad.Add(new Dictionary<string, object?>
            {
                ["field"] = "precision",
                ["rule"] = "certified map compute precision only",
                ["got"] = prec == "" ? "MISSING" : prec,
            });
        foreach (var h in new[] { "architecture_hash",
                                  "trainable_map_hash" })
            if (Str(el, h) == "")
                bad.Add(new Dictionary<string, object?>
                {
                    ["field"] = h, ["rule"] = "required",
                });
        return new()
        {
            ["ok"] = bad.Count == 0,
            ["format"] = GraphKeyFormat,
            ["verdict"] = bad.Count == 0
                ? "GRAPH_KEY_VALID" : "GRAPH_KEY_INVALID",
            ["violations"] = bad,
            ["invalidation_rule"] =
                "any key field change drops the cached graph (§30)",
        };
    }

    // ------------------------------------------------- fused adamw --

    // §26/§67 status of NativeCudaFusedAdamW: kernel identity plus
    // the certification evidence — parity vs the trainer's exact
    // scalar semantics and the measured per-step delta. Numbers are
    // the harness record, not a promise.
    public static Dictionary<string, object?> FusedAdamWStatus() =>
        new()
        {
            ["ok"] = true,
            ["format"] = AdamWFormat,
            ["kernels"] = new object?[]
            {
                "xc_adamw_fused", "xc_sqsum_part",
            },
            ["fused_ops"] = new object?[]
            {
                "gradient_scale", "first_moment", "second_moment",
                "bias_correction", "decoupled_weight_decay",
                "parameter_update",
            },
            ["certification"] = new Dictionary<string, object?>
            {
                ["oracle"] = "trainer_scalar_adamw",
                ["elements"] = 1_000_000,
                ["steps"] = 5,
                ["max_dev"] = 3.755e-06,
                ["sqsum_rel_err"] = 3.043e-08,
                ["verdict"] = "PARITY_PASS",
            },
            ["measured"] = new Dictionary<string, object?>
            {
                ["gpu_step_ms_incl_g_h2d"] = 0.594,
                ["cpu_step_ms"] = 14.105,
                ["speedup"] = 23.7,
                ["device"] = "cc8.6",
            },
            ["integration"] = new Dictionary<string, object?>
            {
                ["env_gate"] = "XINGCHENG_TRAINER_CUDA_OPT",
                ["default"] = "cpu_scalar",
                ["fallback"] = "per_tensor_scalar_on_any_device_miss",
                ["residency"] = "w_m_v_device_resident",
                ["per_step_traffic"] = "g_h2d+w_d2h",
            },
            ["promotion_rule"] =
                "production default only after bf16-training-cert " +
                "promotes the device path (§4/§67)",
        };
}
