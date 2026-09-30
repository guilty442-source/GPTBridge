// TrainingAcceleration.cs — NativeTrainingAccelerationPlane
// (acceleration directive §0-§74). The KPI is
// TIME_TO_QUALIFIED_MODEL, not step/s: telemetry, bottleneck
// classification, eval tiers, pilot ladder, batch planning,
// precision policy, distillation-artifact validation and the speed
// gate all serve that single metric. Contracts only — every method
// is pure validation/derivation over recorded evidence; no weights,
// no training side effects.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class TrainingAcceleration
{
    /// <summary>§7/§31-§32 same-bucket sequences share tensor shapes,
    /// so they are CUDA-graphable and padding waste is bounded.</summary>
    public static readonly int[] SeqBuckets =
        { 128, 256, 512, 1024, 2048, 4096 };

    static long Num(JsonElement el, string k, long d = 0)
    {
        if (el.ValueKind == JsonValueKind.Object &&
            el.TryGetProperty(k, out var p))
        {
            if (p.ValueKind == JsonValueKind.Number &&
                p.TryGetInt64(out long v)) return v;
        }
        return d;
    }

    static double NumF(JsonElement el, string k, double d = 0.0)
    {
        if (el.ValueKind == JsonValueKind.Object &&
            el.TryGetProperty(k, out var p) &&
            p.ValueKind == JsonValueKind.Number)
            return p.GetDouble();
        return d;
    }

    static string Str(JsonElement el, string k)
    {
        if (el.ValueKind == JsonValueKind.Object &&
            el.TryGetProperty(k, out var p) &&
            p.ValueKind == JsonValueKind.String)
            return p.GetString() ?? "";
        return "";
    }

    static void Need(JsonElement el, string errCode,
                     params string[] fields)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(errCode, "record is not an object");
        foreach (string f in fields)
            if (!el.TryGetProperty(f, out var p) ||
                p.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined)
                throw new ExecutorError(errCode, $"missing field: {f}");
    }

    /// <summary>Validate one training-telemetry record: the plane cannot
    /// reason about acceleration on a record that does not say what it
    /// measured.</summary>
    public static Dictionary<string, object?> ValidateTelemetry(
        JsonElement el)
    {
        Need(el, "TELEMETRY_INCOMPLETE",
             "step", "loss", "tokens_per_s", "step_time_ms",
             "data_wait_ms", "host_transfer_ms", "kernel_ms",
             "optimizer_ms", "comm_ms", "eval_tokens_per_s");
        long stepTime = Num(el, "step_time_ms");
        long parts = Num(el, "data_wait_ms") + Num(el, "host_transfer_ms")
                   + Num(el, "kernel_ms") + Num(el, "optimizer_ms")
                   + Num(el, "comm_ms");
        if (parts > stepTime && stepTime > 0)
            throw new ExecutorError("TELEMETRY_INCOMPLETE",
                "stage times exceed step_time_ms — clock overlap " +
                "must be declared, not silently summed");
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-training-telemetry/v1",
            ["step"] = Num(el, "step"),
            ["tokens_per_s"] = NumF(el, "tokens_per_s"),
            ["accounted_share"] = stepTime > 0
                ? (double)parts / stepTime : 0.0,
        };
    }

    /// <summary>Classify the dominant stage from measured stage times —
    /// honest "unclassified" when no stage holds the majority.</summary>
    public static Dictionary<string, object?> BottleneckClassify(
        JsonElement el)
    {
        Need(el, "TELEMETRY_INCOMPLETE",
             "step_time_ms", "data_wait_ms", "host_transfer_ms",
             "kernel_ms", "optimizer_ms", "comm_ms");
        double total = NumF(el, "step_time_ms");
        var stages = new (string name, double ms)[]
        {
            ("data_pipeline", NumF(el, "data_wait_ms")),
            ("host_transfer", NumF(el, "host_transfer_ms")),
            ("kernel", NumF(el, "kernel_ms")),
            ("optimizer", NumF(el, "optimizer_ms")),
            ("comm", NumF(el, "comm_ms")),
        };
        var top = stages.OrderByDescending(s => s.ms).First();
        double share = total > 0 ? top.ms / total : 0.0;
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["bottleneck"] = share >= 0.4 ? top.name : "unclassified",
            ["dominant_share"] = share,
            ["stages"] = stages.Select(s =>
                (object?)new Dictionary<string, object?>
                {
                    ["stage"] = s.name, ["ms"] = s.ms,
                    ["share"] = total > 0 ? s.ms / total : 0.0,
                }).ToList(),
        };
    }

    /// <summary>Eval tiers: cheap tiers run every step; expensive tiers
    /// run rarely. Tiers must be non-decreasing in cost and interval —
    /// an expensive tier running every step defeats the plane.</summary>
    public static Dictionary<string, object?> EvalTiers(JsonElement el)
    {
        Need(el, "EVAL_TIERS_INVALID", "tiers");
        if (el.GetProperty("tiers").ValueKind != JsonValueKind.Array)
            throw new ExecutorError("EVAL_TIERS_INVALID",
                "tiers must be an array");
        long prevInterval = 0, prevCost = 0;
        int n = 0;
        foreach (var t in el.GetProperty("tiers").EnumerateArray())
        {
            Need(t, "EVAL_TIERS_INVALID", "name", "every_n_steps",
                 "est_cost_s");
            long iv = Num(t, "every_n_steps");
            long cost = Num(t, "est_cost_s");
            if (iv <= 0 || cost < 0)
                throw new ExecutorError("EVAL_TIERS_INVALID",
                    "tier interval/cost must be positive");
            if (n > 0 && (iv < prevInterval || cost < prevCost))
                throw new ExecutorError("EVAL_TIERS_INVALID",
                    "tiers must be non-decreasing in interval and cost");
            prevInterval = iv; prevCost = cost; n++;
        }
        if (n == 0)
            throw new ExecutorError("EVAL_TIERS_INVALID", "no tiers");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["tier_count"] = n,
            ["max_interval"] = prevInterval,
        };
    }

    /// <summary>Pilot ladder: probe → pilot → full. Every stage must
    /// declare its budget and stop criteria up front — a stage that
    /// cannot say when it stops is not a ladder rung.</summary>
    public static Dictionary<string, object?> PilotLadder(JsonElement el)
    {
        Need(el, "PILOT_INVALID", "stages");
        var order = new[] { "probe", "pilot", "full" };
        var seen = new List<string>();
        foreach (var s in el.GetProperty("stages").EnumerateArray())
        {
            Need(s, "PILOT_INVALID", "name", "max_steps", "lr",
                 "stop_criteria");
            seen.Add(Str(s, "name"));
        }
        for (int i = 0; i < seen.Count && i < order.Length; i++)
            if (seen[i] != order[i])
                throw new ExecutorError("PILOT_INVALID",
                    $"stage {i} must be '{order[i]}', got '{seen[i]}'");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["stages"] = seen.Cast<object?>().ToList(),
        };
    }

    /// <summary>Batch plan: snap seq_len to a bucket, then derive the
    /// micro-batch count that fills target tokens per step.</summary>
    public static Dictionary<string, object?> BatchPlan(JsonElement el)
    {
        Need(el, "TRAINING_STAGE_INVALID", "seq_len",
             "target_tokens_per_step", "micro_batch_size");
        long seq = Num(el, "seq_len");
        int bucket = SeqBuckets.FirstOrDefault(b => b >= seq, -1);
        if (bucket < 0)
            throw new ExecutorError("TRAINING_STAGE_INVALID",
                $"seq_len {seq} exceeds largest bucket {SeqBuckets[^1]}");
        long micro = Num(el, "micro_batch_size");
        long target = Num(el, "target_tokens_per_step");
        if (micro <= 0 || target <= 0)
            throw new ExecutorError("TRAINING_STAGE_INVALID",
                "micro_batch_size/target_tokens_per_step must be >0");
        long perStep = micro * bucket;
        long accum = Math.Max(1, (target + perStep - 1) / perStep);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-sequence-buckets/v1",
            ["bucket"] = bucket,
            ["micro_batch_size"] = micro,
            ["grad_accum_steps"] = accum,
            ["tokens_per_step"] = perStep * accum,
            ["padding_waste"] = bucket > 0
                ? (double)(bucket - seq) / bucket : 0.0,
        };
    }

    /// <summary>Fixed training-precision policy — master FP32, compute
    /// BF16 where parity evidence exists, router/accumulation FP32.</summary>
    public static Dictionary<string, object?> PrecisionPolicy() =>
        new()
        {
            ["ok"] = true,
            ["format"] = "star-training-precision/v1",
            ["master_weights"] = "FP32",
            ["accumulation"] = "FP32",
            ["router"] = "FP32",
            ["compute_candidate"] = "BF16",
            ["rule"] = "BF16 compute requires parity evidence; " +
                       "silent promotion is denied",
        };

    /// <summary>Distillation artifact contract: a distill record must
    /// name teacher, student, dataset digest, logit retention and
    /// temperature — anything less cannot be reproduced or gated.</summary>
    public static Dictionary<string, object?> ValidateDistillArtifact(
        JsonElement el)
    {
        Need(el, "DISTILL_ARTIFACT_INVALID",
             "teacher_id", "student_id", "dataset_sha256",
             "logit_top_n", "temperature", "capability_retention");
        long topN = Num(el, "logit_top_n");
        double temp = NumF(el, "temperature");
        if (topN <= 0 || temp <= 0.0)
            throw new ExecutorError("DISTILL_ARTIFACT_INVALID",
                "logit_top_n and temperature must be positive");
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-distill-artifact/v1",
            ["teacher_id"] = Str(el, "teacher_id"),
            ["student_id"] = Str(el, "student_id"),
            ["logit_top_n"] = topN,
            ["temperature"] = temp,
        };
    }

    /// <summary>The plane KPI: time-to-qualified-model. Sums measured
    /// step time until the recorded eval floor is reached; reports
    /// not_qualified honestly when the floor was never reached.</summary>
    public static Dictionary<string, object?> TimeToQuality(
        JsonElement el)
    {
        Need(el, "TRAINING_STAGE_INVALID",
             "cumulative_step_time_s", "eval_floor_reached",
             "eval_floor_name");
        bool reached = el.GetProperty("eval_floor_reached")
            .ValueKind == JsonValueKind.True;
        double t = NumF(el, "cumulative_step_time_s");
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["kpi"] = "TIME_TO_QUALIFIED_MODEL",
            ["qualified"] = reached,
            ["eval_floor"] = Str(el, "eval_floor_name"),
            ["time_to_quality_s"] = reached ? t : (object?)null!,
            ["verdict"] = reached ? "QUALIFIED" : "NOT_QUALIFIED",
        };
    }

    /// <summary>Speed gate: candidate throughput vs recorded baseline.
    /// A candidate slower than tolerance is a regression, not an
    /// acceleration — SPEED_REGRESSION, fail-closed.</summary>
    public static Dictionary<string, object?> SpeedGate(JsonElement el)
    {
        Need(el, "TRAINING_STAGE_INVALID",
             "baseline_tokens_per_s", "candidate_tokens_per_s",
             "tolerance");
        double base_ = NumF(el, "baseline_tokens_per_s");
        double cand = NumF(el, "candidate_tokens_per_s");
        double tol = NumF(el, "tolerance");
        if (base_ <= 0 || tol < 0)
            throw new ExecutorError("TRAINING_STAGE_INVALID",
                "baseline must be positive, tolerance non-negative");
        double delta = (cand - base_) / base_;
        bool pass = delta >= -tol;
        if (!pass)
            throw new ExecutorError("SPEED_REGRESSION",
                $"candidate {cand:F1} tok/s vs baseline {base_:F1} " +
                $"tok/s ({delta:P1} < -{tol:P1})");
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["baseline_tokens_per_s"] = base_,
            ["candidate_tokens_per_s"] = cand,
            ["delta"] = delta,
            ["within_tolerance"] = true,
        };
    }
}
