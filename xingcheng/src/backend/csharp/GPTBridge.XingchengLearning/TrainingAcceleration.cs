// TrainingAcceleration.cs — Xingcheng NativeTrainingAccelerationPlane
// (acceleration directive §0-§74), C# contract/orchestration layer.
// The sole KPI is TIME_TO_QUALIFIED_MODEL (§0): a strategy wins when it
// reaches the capability threshold sooner — tokens/sec is an
// ingredient, never the verdict.
//
//   Telemetry          §1 + §65 — the fixed metric set and the
//                      step-time breakdown contract.
//   BottleneckClassify §66 — step breakdown -> one bottleneck class;
//                      the class decides the next optimization.
//   EvalTiers          §37-§40 — FAST/REGRESSION/FULL cadence
//                      contract; every-step full eval is denied.
//   PilotLadder        §42-§43 — 50/200/400/600-step ladder; LR pilot
//                      <=3 candidates x 20-50 steps.
//   BatchPlan          §7-§10 — TrainingBatchPlanner: bucket +
//                      free VRAM -> microbatch + grad-accum +
//                      workspace; accumulation is a VRAM fallback,
//                      not a default.
//   PrecisionPolicy    §2-§3 — BF16 compute primary, FP32 sensitive
//                      accumulation, FP64 oracle-only; FP8/FP4 are
//                      not a primary lane this phase.
//   DistillArtifact    §48-§51 — top-N logits + residual mass; one
//                      artifact shared by every student candidate.
//   TimeToQuality      §0/§68-§69 — strategy comparison by
//                      time-to-threshold + CapabilityGain/GPU-s.
//   SpeedGate          §67 — a speed change that drops capability is
//                      FAIL regardless of the speedup.
//
// CAPABILITY_TRAINING_FROZEN: contracts and gates only — the planner
// emits plans, it never runs a trainer job.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class TrainingAcceleration
{
    public const string TelemetryFormat = "star-training-telemetry/v1";
    public const string BottleneckFormat =
        "star-bottleneck-classify/v1";
    public const string EvalTierFormat = "star-eval-tier-policy/v1";
    public const string PilotFormat = "star-training-pilot/v1";
    public const string BatchPlanFormat =
        "star-training-batch-plan/v1";
    public const string PrecisionFormat =
        "star-training-precision/v1";
    public const string DistillFormat =
        "star-distillation-artifact/v1";
    public const string T2QFormat = "star-time-to-quality/v1";
    public const string SpeedGateFormat = "star-speed-gate/v1";

    // §1 the fixed per-run metric set — "step/s" alone is not a
    // telemetry record.
    public static readonly string[] MetricFields =
    {
        "raw_tokens_per_sec", "effective_tokens_per_sec", "step_ms",
        "forward_ms", "backward_ms", "optimizer_ms", "data_wait_ms",
        "h2d_ms", "eval_ms", "checkpoint_ms", "gpu_busy_ratio",
        "tensor_core_ratio", "vram_peak", "ram_peak",
        "trainable_params", "time_to_target",
        "time_to_best_checkpoint",
    };
    // §65 the step-time components that must account for step_ms.
    public static readonly string[] StepParts =
    {
        "forward_ms", "backward_ms", "optimizer_ms", "data_wait_ms",
        "h2d_ms", "eval_ms", "checkpoint_ms",
    };
    // §66 bottleneck classes — classification precedes optimization.
    public static readonly string[] BottleneckClasses =
    {
        "DATA_BOUND", "CPU_BOUND", "LAUNCH_BOUND", "MEMORY_BOUND",
        "COMPUTE_BOUND", "OPTIMIZER_BOUND", "EVAL_BOUND",
        "CHECKPOINT_BOUND",
    };
    // §7 fixed sequence buckets — same bucket = same tensor shape =
    // CUDA-graphable (§31-§32).
    public static readonly int[] SeqBuckets =
        { 128, 256, 512, 1024, 2048 };
    // §42 capability-training pilot ladder.
    public static readonly int[] PilotSteps = { 50, 200, 400, 600 };

    private static double DNum(JsonElement r, string k,
        double d = double.NaN) =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.Number ? v.GetDouble() : d;
    private static long Num(JsonElement r, string k, long d = 0) =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.Number &&
        v.TryGetInt64(out long n) ? n : d;
    private static string Str(JsonElement r, string k,
        string d = "") =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? d : d;

    // --------------------------------------------------- telemetry --

    /// <summary>§63/§100 (capability unification): when a plan request
    /// names a capability, resolve it through the canonical registry
    /// (unknown names fail closed) and stamp the request with the
    /// resolved id, its §55-§57 execution profile and its §61 resource
    /// hint — the plan never selects kernels or demands resources.
    /// </summary>
    private static void WithCapability(JsonElement el,
        Dictionary<string, object?> result)
    {
        string raw = Str(el, "capability");
        if (raw.Length == 0) return;
        string? cap = CapabilityRegistry.Resolve(raw)
            ?? throw new ExecutorError("CAPABILITY_UNKNOWN", raw);
        result["capability_id"] = cap;
        var profile = CapabilityRuntimeProfile.Emit(cap);
        result["runtime_profile"] = profile["execution_profile"];
        result["resource_hint"] = profile["resource_hint"];
    }

    /// <summary>Validate a §1 telemetry record: every field present and
    /// numeric; the §65 step parts must account for >=95% of step_ms
    /// (5% slack for untracked overhead — anything larger means the
    /// breakdown is lying).</summary>
    public static Dictionary<string, object?> ValidateTelemetry(
        JsonElement el)
    {
        var missing = MetricFields
            .Where(k => !el.TryGetProperty(k, out var v) ||
                        v.ValueKind != JsonValueKind.Number)
            .Cast<object?>().ToList();
        if (missing.Count > 0)
            return new Dictionary<string, object?>
            {
                ["ok"] = false, ["format"] = TelemetryFormat,
                ["verdict"] = "TELEMETRY_INCOMPLETE",
                ["missing"] = missing,
                ["rule"] = "the full §1 metric set is required — " +
                           "blind optimization is denied (§65)",
            };
        double step = DNum(el, "step_ms", 0);
        double parts = StepParts.Sum(p => DNum(el, p, 0));
        double covered = step > 0 ? parts / step : 0;
        var violations = new List<object?>();
        if (step > 0 && covered < 0.95)
            violations.Add(
                $"step breakdown covers {covered:P0} of step_ms — " +
                "the missing share must be attributed before " +
                "optimizing (§65)");
        if (DNum(el, "effective_tokens_per_sec") >
            DNum(el, "raw_tokens_per_sec") + 1e-6)
            violations.Add("effective_tokens_per_sec > raw — padding " +
                           "accounting is inconsistent (§6)");
        return new Dictionary<string, object?>
        {
            ["ok"] = violations.Count == 0,
            ["format"] = TelemetryFormat,
            ["fields_present"] = MetricFields.Length,
            ["step_coverage"] = Math.Round(covered, 4),
            ["violations"] = violations,
            ["verdict"] = violations.Count == 0
                ? "TELEMETRY_VALID" : "TELEMETRY_INCOMPLETE",
        };
    }

    /// <summary>§66 bottleneck auto-classification from a telemetry
    /// record. The largest step component wins, with data_wait/h2d
    /// folding into DATA_BOUND; a healthy step where compute dominates
    /// reports COMPUTE_BOUND as information, not a defect.</summary>
    public static Dictionary<string, object?> BottleneckClassify(
        JsonElement el)
    {
        double fwd = DNum(el, "forward_ms", 0),
               bwd = DNum(el, "backward_ms", 0),
               opt = DNum(el, "optimizer_ms", 0),
               dat = DNum(el, "data_wait_ms", 0),
               h2d = DNum(el, "h2d_ms", 0),
               ev = DNum(el, "eval_ms", 0),
               ck = DNum(el, "checkpoint_ms", 0),
               step = DNum(el, "step_ms", 0);
        double io = dat + h2d;
        double compute = fwd + bwd;
        var shares = new (string cls, double ms)[]
        {
            ("DATA_BOUND", io), ("OPTIMIZER_BOUND", opt),
            ("EVAL_BOUND", ev), ("CHECKPOINT_BOUND", ck),
            ("COMPUTE_BOUND", compute),
        };
        var top = shares.OrderByDescending(s => s.ms).First();
        // §12: data wait above ~5% of the step is the first thing to
        // fix, even when another class is nominally larger.
        string verdict = top.cls;
        if (step > 0 && io / step > 0.05 && io >= compute * 0.25)
            verdict = "DATA_BOUND";
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = BottleneckFormat,
            ["bottleneck"] = verdict,
            ["shares"] = shares.Select(s => (object?)
                new Dictionary<string, object?>
                {
                    ["class"] = s.cls,
                    ["ms"] = Math.Round(s.ms, 3),
                    ["share"] = step > 0
                        ? Math.Round(s.ms / step, 4) : 0,
                }).ToList(),
            ["data_wait_share"] = step > 0
                ? Math.Round(io / step, 4) : 0,
            ["rule"] = "classify before optimizing — 'blind " +
                       "optimization' is denied (§66)",
        };
    }

    // --------------------------------------------------- eval tiers --

    /// <summary>§37-§40 eval cadence contract. FAST_EVAL every 25-50
    /// steps, REGRESSION ~100, FULL_CERT only at best/final/promotion
    /// candidates. A config that runs full eval per step is
    /// EVAL_BOUND by construction and rejected.</summary>
    public static Dictionary<string, object?> EvalTiers(JsonElement el)
    {
        var violations = new List<object?>();
        long fastEvery = Num(el, "fast_eval_every", 0);
        long regEvery = Num(el, "regression_eval_every", 0);
        long fullEvery = Num(el, "full_eval_every", -1);
        if (fastEvery < 20 || fastEvery > 50)
            violations.Add($"fast_eval_every {fastEvery} outside " +
                           "25-50 step band (§38)");
        if (regEvery < 80 || regEvery > 200)
            violations.Add($"regression_eval_every {regEvery} " +
                           "outside ~100 step band (§39)");
        if (fullEvery >= 0 && fullEvery < 500 &&
            !el.TryGetProperty("full_on_candidates_only", out _))
            violations.Add("full eval must run at candidate points, " +
                           "not on a step cadence (§40)");
        return new Dictionary<string, object?>
        {
            ["ok"] = violations.Count == 0,
            ["format"] = EvalTierFormat,
            ["tiers"] = new[]
            {
                "FAST_EVAL: active capability + loss + small holdout " +
                    "+ numerical sanity (§38)",
                "REGRESSION_EVAL: core-capability smoke regression " +
                    "(§39)",
                "FULL_CERT: best/final/promotion candidates only " +
                    "(§40)",
            },
            ["violations"] = violations,
            ["verdict"] = violations.Count == 0
                ? "EVAL_TIERS_VALID" : "EVAL_TIERS_INVALID",
        };
    }

    // ------------------------------------------------------ pilot ---

    /// <summary>§42-§43 pilot contract: capability training starts at
    /// 50 steps and only escalates on evidence; the LR pilot is <=3
    /// candidates at 20-50 steps each — a grid search is denied.</summary>
    public static Dictionary<string, object?> PilotLadder(
        JsonElement el)
    {
        var violations = new List<object?>();
        long steps = Num(el, "planned_steps", -1);
        if (steps > 600)
            violations.Add($"planned_steps {steps} exceeds the pilot " +
                           "ladder max 600 (§42)");
        if (el.TryGetProperty("lr_candidates", out var lrs) &&
            lrs.ValueKind == JsonValueKind.Array &&
            lrs.GetArrayLength() > 3)
            violations.Add("LR pilot allows at most 3 candidates " +
                           "(§43)");
        if (el.TryGetProperty("lr_pilot_steps", out var lps) &&
            lps.ValueKind == JsonValueKind.Number)
        {
            long n = lps.GetInt64();
            if (n < 20 || n > 50)
                violations.Add($"lr_pilot_steps {n} outside " +
                               "20-50 (§43)");
        }
        var result = new Dictionary<string, object?>
        {
            ["ok"] = violations.Count == 0,
            ["format"] = PilotFormat,
            ["ladder"] = PilotSteps.Cast<object?>().ToList(),
            ["rule"] = "escalate on evidence — never launch a " +
                       "thousand-step run first (§41-§42)",
            ["violations"] = violations,
            ["verdict"] = violations.Count == 0
                ? "PILOT_VALID" : "PILOT_INVALID",
        };
        WithCapability(el, result);
        return result;
    }

    // ------------------------------------------------- batch plan ---

    /// <summary>§8-§10 TrainingBatchPlanner. Inputs: sequence bucket,
    /// free VRAM, trainable params, optimizer bytes, per-token
    /// activation estimate. Microbatch is grown until the workspace
    /// fills; gradient accumulation only covers what VRAM cannot —
    /// more accumulation than needed is a launch-overhead loss (§10).</summary>
    public static Dictionary<string, object?> BatchPlan(JsonElement el)
    {
        long bucket = Num(el, "sequence_bucket", 512);
        if (!SeqBuckets.Contains((int)bucket))
            throw new ExecutorError("TRAINING_STAGE_INVALID",
                $"sequence_bucket {bucket} not in the fixed bucket " +
                $"set {{{string.Join(",", SeqBuckets)}}} (§7)");
        long freeVram = Num(el, "free_vram_bytes", 0);
        long trainable = Num(el, "trainable_params", 0);
        long optBytes = Num(el, "optimizer_state_bytes",
                            trainable * 8);      // m+v fp32
        long actPerToken = Num(el, "activation_bytes_per_token",
                               4L << 20);
        long targetBatchTokens = Num(el, "target_batch_tokens",
                                     bucket * 8);
        if (freeVram <= 0)
            throw new ExecutorError("TRAINING_STAGE_INVALID",
                "free_vram_bytes required (§8)");
        long weightBytes = trainable * 2;        // bf16 resident copy
        long usable = Math.Max(0,
            freeVram - weightBytes - optBytes);
        long tokensFit = actPerToken > 0 ? usable / actPerToken : 0;
        long microTokens = Math.Clamp(
            Math.Max(bucket, tokensFit / bucket * bucket),
            bucket, targetBatchTokens);
        long accum = microTokens > 0
            ? (long)Math.Ceiling(
                (double)targetBatchTokens / microTokens) : 0;
        var plan = new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = BatchPlanFormat,
            ["sequence_bucket"] = bucket,
            ["microbatch_tokens"] = microTokens,
            ["microbatch_sequences"] = microTokens / bucket,
            ["gradient_accumulation"] = accum,
            ["workspace_bytes"] =
                microTokens * actPerToken + weightBytes + optBytes,
            ["vram_headroom_bytes"] = freeVram -
                (microTokens * actPerToken + weightBytes + optBytes),
            ["accumulation_is_vram_fallback"] = accum > 1,
        };
        if (accum > 8)
        {
            plan["ok"] = false;
            plan["verdict"] = "MEMORY_BOUND";
            plan["note"] = "accumulation >8 means the bucket/target " +
                           "combination cannot pay for itself in " +
                           "throughput (§10)";
        }
        else plan["verdict"] = accum <= 1
            ? "MAX_MICROBATCH" : "ACCUMULATING";
        WithCapability(el, plan);
        return plan;
    }

    /// <summary>§42–§43 Resource-Aware TrainingBatchPlanner: the plan's
    /// envelope is the ResourceGrant, not the driver. effective VRAM =
    /// min(driver_available, grant cap) (§41); CPU threads clamp to
    /// cpu_threads_max; no legal plan → INSUFFICIENT_GRANTED_RESOURCES,
    /// never quota breach (§43). A plan that still exceeds the grant
    /// after sizing fails closed ACCELERATION_PLAN_OVER_GRANT (§103,
    /// acceptance scenario H).</summary>
    public static Dictionary<string, object?> GrantBoundBatchPlan(
        JsonElement el, ResourceGrant grant)
    {
        long freeVram = Num(el, "free_vram_bytes", 0);
        long driverTotal = Num(el, "vram_total_bytes", 0);
        long effective = grant.EffectiveVramBytes(freeVram, driverTotal);
        var merged = new Dictionary<string, object?>();
        foreach (var p in el.EnumerateObject())
            merged[p.Name] = p.Value;
        merged["free_vram_bytes"] = effective;
        merged["grant_id"] = grant.GrantId;
        int threads = (int)Num(el, "cpu_threads", 0);
        if (grant.CpuThreadsMax > 0 &&
            (threads <= 0 || threads > grant.CpuThreadsMax))
            merged["cpu_threads"] = threads = grant.CpuThreadsMax;
        var plan = BatchPlan(JsonSerializer.SerializeToElement(merged));
        var violations = grant.OverGrantViolations(
            planCpuThreads: threads,
            planVramBytes: Convert.ToInt64(
                plan.GetValueOrDefault("workspace_bytes") ?? 0L),
            planStreams: (int)Num(el, "stream_count", 0),
            planBackgroundThreads: (int)Num(el, "background_threads", 0),
            planIoRead: Num(el, "io_read_bytes", 0),
            planIoWrite: Num(el, "io_write_bytes", 0),
            planRamBytes: Num(el, "ram_bytes", 0));
        if (violations.Count > 0)
            throw new ExecutorError(ResourceErrors.PlanOverGrant,
                "acceleration plan exceeds resource grant: " +
                string.Join("; ", violations));
        if (plan.TryGetValue("verdict", out var v) &&
            v as string == "MEMORY_BOUND")
            throw new ExecutorError(ResourceErrors.InsufficientGranted,
                "no legal plan fits inside the granted VRAM envelope " +
                "(§43 — shed scope, never exceed quota)");
        plan["format"] = ResourceContracts.PlanFormat;
        plan["grant_bound"] = true;
        plan["effective_vram_bytes"] = effective;
        return plan;
    }

    // --------------------------------------------------- precision --

    /// <summary>§2-§3 training precision contract — emitted, never
    /// negotiated: BF16 compute primary on CUDA 8.6 tensor cores, FP32
    /// for sensitive accumulation/router/optimizer-critical state,
    /// FP64 reserved to oracle/gradcheck/numerical certification.
    /// FP8/FP4 training is out of the primary lane this phase.</summary>
    public static Dictionary<string, object?> PrecisionPolicy()
        => new()
        {
            ["ok"] = true, ["format"] = PrecisionFormat,
            ["compute"] = "BF16 tensor core (§2-§3)",
            ["sensitive_accumulation"] = "FP32",
            ["router"] = "FP32",
            ["optimizer_critical_state"] = "FP32",
            ["fp64_role"] =
                "oracle / gradcheck / numerical certification only",
            ["auxiliary"] = "INT8 eligible auxiliary path",
            ["excluded_primary"] = new[] { "FP8", "FP4" },
            ["certification_rule"] =
                "BF16 becomes training-primary only after the §4 " +
                "oracle comparison (loss/gradient/logit/state) " +
                "passes — certification precedes promotion",
        };

    // ----------------------------------------------- distill cache --

    /// <summary>§48-§51 distillation-artifact contract: teacher output
    /// is cached ONCE per (teacher, data snapshot) — top-N logits +
    /// residual mass + selected hidden/routing/compact-reasoning
    /// targets. Full-vocab logits, per-epoch teacher reruns and
    /// per-LR-trial artifacts are all denied.</summary>
    public static Dictionary<string, object?> ValidateDistillArtifact(
        JsonElement el)
    {
        var violations = new List<object?>();
        if (Num(el, "top_n", 0) <= 0)
            violations.Add("top_n required — teacher caches are " +
                           "top-N, not full-vocab (§50)");
        if (el.TryGetProperty("full_vocab_logits", out var fv) &&
            fv.ValueKind == JsonValueKind.True)
            violations.Add("full vocabulary logits stored — keep " +
                           "top-N + residual mass only (§50)");
        if (!el.TryGetProperty("teacher_id", out var ti) ||
            (ti.GetString() ?? "").Length == 0)
            violations.Add("teacher_id missing — artifacts key on " +
                           "(teacher, snapshot) for reuse (§51)");
        if (!el.TryGetProperty("data_snapshot_id", out var ds) ||
            (ds.GetString() ?? "").Length == 0)
            violations.Add("data_snapshot_id missing — required " +
                           "for cross-candidate reuse (§51)");
        if (el.TryGetProperty("rerun_per_epoch", out var re) &&
            re.ValueKind == JsonValueKind.True)
            violations.Add("teacher rerun per epoch is denied — " +
                           "the artifact is precomputed (§48)");
        return new Dictionary<string, object?>
        {
            ["ok"] = violations.Count == 0,
            ["format"] = DistillFormat,
            ["violations"] = violations,
            ["verdict"] = violations.Count == 0
                ? "DISTILL_ARTIFACT_VALID"
                : "DISTILL_ARTIFACT_INVALID",
        };
    }

    // ---------------------------------------------- time-to-quality --

    /// <summary>§0/§68-§69: compare two training strategies by
    /// time-to-qualified-model, not tokens/sec. The record carries the
    /// tokens each strategy needs to hit the capability threshold and
    /// its throughput; the winner is the smaller projected wall time,
    /// tie-broken by CapabilityGain/GPU-second.</summary>
    public static Dictionary<string, object?> TimeToQuality(
        JsonElement el)
    {
        var items = new List<(string name, double t, double eff)>();
        if (el.TryGetProperty("strategies", out var arr) &&
            arr.ValueKind == JsonValueKind.Array)
            foreach (var s in arr.EnumerateArray())
            {
                string name = Str(s, "name", "?");
                double tps = DNum(s, "tokens_per_sec", 0);
                double tokensToTarget =
                    DNum(s, "tokens_to_target", 0);
                double gain = DNum(s, "capability_gain", 0);
                double t = tps > 0 && tokensToTarget > 0
                    ? tokensToTarget / tps
                    : double.PositiveInfinity;
                double eff = t > 0 && double.IsFinite(t)
                    ? gain / t : 0;
                items.Add((name, t, eff));
            }
        if (items.Count < 2)
            throw new ExecutorError("TRAINING_STAGE_INVALID",
                "at least two strategies required (§68)");
        var winner = items
            .OrderBy(i => i.t).ThenByDescending(i => i.eff).First();
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = T2QFormat,
            ["kpi"] = "TIME_TO_QUALIFIED_MODEL",
            ["strategies"] = items.Select(i => (object?)
                new Dictionary<string, object?>
                {
                    ["name"] = i.name,
                    ["projected_seconds"] =
                        double.IsFinite(i.t) ? Math.Round(i.t, 1)
                                             : (object?)null!,
                    ["capability_gain_per_gpu_s"] =
                        Math.Round(i.eff, 8),
                }).ToList(),
            ["winner"] = winner.name,
            ["rule"] = "tokens/sec never decides alone — a slower " +
                       "pipeline reaching the threshold with fewer " +
                       "tokens wins (§68-§69)",
        };
    }

    // ------------------------------------------------- speed gate ---

    /// <summary>§67 capability-preservation gate for a speed change
    /// (fusion/BF16/graph/sparse-grad/QAT/recompute): the change is
    /// FAIL when capability regresses, however large the speedup.</summary>
    public static Dictionary<string, object?> SpeedGate(JsonElement el)
    {
        double before = DNum(el, "capability_before", double.NaN);
        double after = DNum(el, "capability_after", double.NaN);
        double speedup = DNum(el, "speedup_ratio", 1.0);
        if (double.IsNaN(before) || double.IsNaN(after))
            throw new ExecutorError("TRAINING_STAGE_INVALID",
                "capability_before/after required — a speed change " +
                "without capability evidence cannot pass (§67)");
        bool drop = after < before - 1e-9;
        var result = new Dictionary<string, object?>
        {
            ["ok"] = !drop,
            ["format"] = SpeedGateFormat,
            ["change"] = Str(el, "change", "unspecified"),
            ["capability_before"] = before,
            ["capability_after"] = after,
            ["speedup_ratio"] = speedup,
            ["verdict"] = drop
                ? "SPEED_REGRESSION_FAIL"
                : "SPEED_GATE_PASS",
            ["rule"] = "+30% speed with a capability drop is FAIL " +
                       "(§67)",
        };
        WithCapability(el, result);
        return result;
    }
}
