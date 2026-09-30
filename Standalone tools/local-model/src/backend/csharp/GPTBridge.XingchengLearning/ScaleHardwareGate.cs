// ScaleHardwareGate.cs — NativeScaleEfficiencyPlane governance lane
// (scale directive §2/§23-§26/§48/§49). Governance only: evaluates
// scale candidates against measured hardware and emits certifications.
// It never creates weights, never changes xc-fused-1, and never marks
// a capability change — scale_profile / capability_version /
// weight_version / runtime_version stay independent identities (§45).
//
//   ScaleProfile            star-scale-profile/v1 validation — the
//                           five residency/tier identities and the
//                           seven mandatory parameter metrics (§2/§3).
//   PrecisionMap            star-precision-map/v1 validation —
//                           per-component storage/execution precision
//                           separation (§12/§23).
//   ScaleHardwareGate       star-scale-hardware-gate/v1 verdict —
//                           TRAIN_AND_INFER / INFER_ONLY /
//                           EXPERT_OFFLOAD_REQUIRED /
//                           QUANTIZATION_REQUIRED /
//                           SCALE_REDUCE_REQUIRED /
//                           HARDWARE_INSUFFICIENT (§24).
//   TrainingWorkingSet      peak working-set estimate — active
//                           weights + gradients + optimizer +
//                           activations + MTP + routing + workspace;
//                           the training gate is PEAK WORKING SET,
//                           never full model size (§26).
//   ResourceCert            star-resource-cert/v1 record for every
//                           production bundle (§48).
//   ScalePromotionGate      star-scale-promotion-gate/v1 — all seven
//                           gates must pass; capability gain alone
//                           never promotes (§49).

using System.Text.Json;
using System.Text.RegularExpressions;

namespace GPTBridge.XingchengLearning;

internal static class ScaleHardwareGate
{
    public const string ProfileFormat = "star-scale-profile/v1";
    public const string GateFormat = "star-scale-hardware-gate/v1";
    public const string PrecisionMapFormat = "star-precision-map/v1";
    public const string WorksetFormat = "star-training-workset/v1";
    public const string CertFormat = "star-resource-cert/v1";
    public const string PromotionFormat = "star-scale-promotion-gate/v1";

    // §3: a scale report that shows only "32B" is not a report.
    public static readonly string[] ParamMetrics =
    {
        "total_params", "unique_params", "active_params",
        "gpu_resident_params", "ram_resident_params",
        "nvme_cold_params", "trainable_params",
    };

    private static readonly Regex ProfileName =
        new(@"^xc-(?!fused)[a-z0-9][a-z0-9\-]*$",
            RegexOptions.Compiled);

    private static long Num(JsonElement r, string k, long d = 0)
    {
        if (!r.TryGetProperty(k, out var v)) return d;
        if (v.ValueKind == JsonValueKind.Number &&
            v.TryGetInt64(out long n)) return n;
        return d;
    }

    private static double DNum(JsonElement r, string k, double d = 0)
    {
        if (!r.TryGetProperty(k, out var v)) return d;
        if (v.ValueKind == JsonValueKind.Number) return v.GetDouble();
        return d;
    }

    // ---------------------------------------------------- §2/§3 -----
    /// <summary>Validate a star-scale-profile/v1 record. Architecture
    /// and scale are separate identities — "xc-fused-1b" style hybrid
    /// names are rejected, as is any profile that changes the
    /// architecture or lacks the seven parameter metrics.</summary>
    public static Dictionary<string, object?> ValidateProfile(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("SCALE_PROFILE_INVALID",
                "profile must be an object");
        string profile = el.TryGetProperty("scale_profile", out var sp)
            ? sp.GetString() ?? "" : "";
        string arch = el.TryGetProperty("architecture_profile",
            out var ap) ? ap.GetString() ?? "" : "";
        if (!ProfileName.IsMatch(profile))
            throw new ExecutorError("SCALE_PROFILE_INVALID",
                $"scale_profile '{profile}' invalid — must be xc-* " +
                "and must NOT embed the architecture name " +
                "(xc-fused-* is forbidden)");
        if (arch != "xc-fused-1")
            throw new ExecutorError("SCALE_PROFILE_INVALID",
                $"architecture_profile '{arch}' — only xc-fused-1 is " +
                "canonical this phase");
        var missing = ParamMetrics
            .Where(m => !el.TryGetProperty(m, out _))
            .ToList();
        if (missing.Count > 0)
            throw new ExecutorError("SCALE_PROFILE_INVALID",
                "missing parameter metrics: " +
                string.Join(",", missing));
        long total = Num(el, "total_params"),
             active = Num(el, "active_params"),
             gpuRes = Num(el, "gpu_resident_params"),
             ramRes = Num(el, "ram_resident_params"),
             nvme = Num(el, "nvme_cold_params");
        if (total <= 0 || active <= 0 || active > total)
            throw new ExecutorError("SCALE_PROFILE_INVALID",
                "total/active params inconsistent");
        var d = new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = ProfileFormat,
            ["scale_profile"] = profile,
            ["architecture_profile"] = arch,
            ["weight_version"] =
                el.TryGetProperty("weight_version", out var wv)
                    ? wv.GetString() : null,
            ["runtime_version"] =
                el.TryGetProperty("runtime_version", out var rv)
                    ? rv.GetString() : null,
            ["capability_version"] =
                el.TryGetProperty("capability_version", out var cv)
                    ? cv.GetString() : null,
            ["capacity_amplification"] =
                Math.Round((double)total / active, 4),
            ["gpu_amplification"] = gpuRes > 0
                ? Math.Round((double)total / gpuRes, 4) : 0.0,
            ["ram_amplification"] = ramRes > 0
                ? Math.Round((double)total / ramRes, 4) : 0.0,
        };
        foreach (var m in ParamMetrics) d[m] = Num(el, m);
        long accounted = gpuRes + ramRes + nvme;
        d["residency_accounted_params"] = accounted;
        d["residency_covers_total"] = accounted >= total ||
            gpuRes + ramRes + nvme >= total;
        // §4: a candidate must improve at least two amplification
        // metrics vs the incumbent — the incumbent values ride along.
        var improved = new List<string>();
        if (el.TryGetProperty("incumbent", out var inc) &&
            inc.ValueKind == JsonValueKind.Object)
        {
            double iCap = DNum(inc, "capacity_amplification", 0);
            double iGpu = DNum(inc, "gpu_amplification", 0);
            double iRam = DNum(inc, "ram_amplification", 0);
            if ((double)d["capacity_amplification"]! > iCap)
                improved.Add("capacity_amplification");
            if ((double)d["gpu_amplification"]! > iGpu)
                improved.Add("gpu_amplification");
            if ((double)d["ram_amplification"]! > iRam)
                improved.Add("ram_amplification");
            d["amplification_improved"] = improved;
            d["amplification_gate"] = improved.Count >= 2
                ? "PASS"
                : "FAIL_REQUIRES_TWO_OF_THREE";
        }
        return d;
    }

    // ---------------------------------------------------- §12/§23 ---
    /// <summary>Validate a star-precision-map/v1 record: storage and
    /// execution precision are declared per component and may differ.
    /// Known components only; unvalidated profiles cannot promote.
    /// </summary>
    public static Dictionary<string, object?> ValidatePrecisionMap(
        JsonElement el)
    {
        var components = new[]
        {
            "embedding", "router", "attention", "delta_core",
            "moe_experts", "shared_expert", "kv_cache",
            "delta_state", "vision", "lm_head",
        };
        var storageOk = new HashSet<string>
            { "fp64", "fp32", "bf16", "fp16", "fp8", "int8", "fp4",
              "int4" };
        var execOk = new HashSet<string>
            { "fp64", "fp32", "bf16", "fp16", "fp8" };
        var map = new Dictionary<string, object?>();
        var warnings = new List<string>();
        foreach (var c in components)
        {
            if (!el.TryGetProperty(c, out var ce) ||
                ce.ValueKind != JsonValueKind.Object)
                continue;
            string stor = ce.TryGetProperty("storage", out var s)
                ? s.GetString() ?? "" : "";
            string exec = ce.TryGetProperty("execution", out var ex)
                ? ex.GetString() ?? "" : "";
            if (!storageOk.Contains(stor))
                throw new ExecutorError("PRECISION_MAP_INVALID",
                    $"{c}.storage '{stor}' unknown");
            if (!execOk.Contains(exec))
                throw new ExecutorError("PRECISION_MAP_INVALID",
                    $"{c}.execution '{exec}' unknown — execution " +
                    "requires at least fp8-class");
            if (stor != exec)
                warnings.Add($"{c}: storage={stor} exec={exec} " +
                             "(dequant path required)");
            map[c] = new Dictionary<string, object?>
            {
                ["storage"] = stor, ["execution"] = exec,
            };
        }
        if (map.Count == 0)
            throw new ExecutorError("PRECISION_MAP_INVALID",
                "no recognised component entries");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = PrecisionMapFormat,
            ["components"] = map, ["dequant_paths"] = warnings,
            ["promotion_note"] =
                "precision targets are PROFILES — promotion requires " +
                "parity validation per §12",
        };
    }

    // ---------------------------------------------------- §24/§26 ---
    /// <summary>ScaleHardwareGate: run before ANY scale candidate is
    /// created (§24). Inputs: parameter counts, precision map bytes,
    /// expert topology, state geometry, context target, training mode,
    /// hardware caps. Verdict is the strictest applicable result.</summary>
    public static Dictionary<string, object?> Evaluate(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("SCALE_HARDWARE_INSUFFICIENT",
                "gate input must be an object");
        var caps = HardwareCaps.From(
            el.TryGetProperty("hardware", out var hw) ? hw : el);
        long total = Num(el, "total_params"),
             active = Num(el, "active_params"),
             trainable = Num(el, "trainable_params"),
             perExpert = Num(el, "per_expert_params"),
             pinnedBytes = Num(el, "pinned_bytes");
        double weightBytes = DNum(el, "weight_bytes_per_param", 2.0);
        double actBytes = DNum(el, "activation_bytes_per_token", 0);
        long context = Num(el, "context_target", 4096);
        bool wantsTraining =
            el.TryGetProperty("training_mode", out var tm) &&
            (tm.GetString() ?? "none") != "none";

        long pinned = pinnedBytes > 0
            ? pinnedBytes : (long)(active * weightBytes);
        long totalBytes = (long)(total * weightBytes);
        long residentBudget = caps.VramBytes + caps.RamBytes;
        var verdicts = new List<string>();
        string verdict;

        if (caps.VramBytes <= 0 && caps.RamBytes <= 0)
            verdict = "HARDWARE_INSUFFICIENT";
        else if (pinned > caps.VramBytes &&
                 pinned > residentBudget)
            verdict = "HARDWARE_INSUFFICIENT";
        else
        {
            if (pinned > caps.VramBytes)
                verdicts.Add("QUANTIZATION_REQUIRED");
            if (totalBytes > caps.VramBytes && perExpert > 0)
                verdicts.Add("EXPERT_OFFLOAD_REQUIRED");
            if (totalBytes > residentBudget && perExpert <= 0)
                verdicts.Add("SCALE_REDUCE_REQUIRED");

            // §26 training working set: peak, not full size.
            var ws = WorkingSet(el, weightBytes, actBytes,
                                context, trainable);
            long wsPeak = (long)(double)ws["peak_working_set_bytes"]!;
            ws["hardware_headroom_bytes"] =
                residentBudget > 0 ? residentBudget - wsPeak : -1;
            bool trainFits = caps.VramBytes <= 0 ||
                             wsPeak <= residentBudget;
            if (wantsTraining && !trainFits)
                verdicts.Add("SCALE_REDUCE_REQUIRED");

            verdict =
                verdicts.Contains("HARDWARE_INSUFFICIENT") ? "HARDWARE_INSUFFICIENT" :
                verdicts.Contains("SCALE_REDUCE_REQUIRED") ? "SCALE_REDUCE_REQUIRED" :
                verdicts.Contains("QUANTIZATION_REQUIRED") ? "QUANTIZATION_REQUIRED" :
                verdicts.Contains("EXPERT_OFFLOAD_REQUIRED") ? "EXPERT_OFFLOAD_REQUIRED" :
                wantsTraining && trainFits ? "TRAIN_AND_INFER" :
                "INFER_ONLY";

            return new Dictionary<string, object?>
            {
                ["ok"] = true, ["format"] = GateFormat,
                ["verdict"] = verdict,
                ["verdicts_triggered"] = verdicts,
                // §25: train and infer budgets are separate facts.
                ["max_inference_scale_params"] =
                    residentBudget > 0
                        ? (long)(residentBudget / weightBytes) : 0,
                ["max_trainable_scale_params"] =
                    residentBudget > 0 && wsPeak > 0
                        ? Math.Min(total,
                            (long)(residentBudget /
                                   Math.Max(1.0,
                                       (double)wsPeak /
                                       Math.Max(1, trainable))))
                        : 0,
                ["working_set"] = ws,
            };
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = false, ["format"] = GateFormat,
            ["verdict"] = verdict,
            ["error_code"] = verdict,
        };
    }

    /// <summary>§26 peak training working set: active weights +
    /// gradients + optimizer (adam = 2 extra slots) + activations
    /// scaled by context + MTP + routing buffers + workspace.</summary>
    public static Dictionary<string, object?> WorkingSet(
        JsonElement el, double weightBytes,
        double actBytesPerTok, long context, long trainable)
    {
        long activeParams = Num(el, "active_params", trainable);
        double wBytes = activeParams * weightBytes;
        double grads = trainable * weightBytes;
        double optim = trainable * weightBytes * 2.0;  // adam m+v
        double acts = actBytesPerTok > 0
            ? actBytesPerTok * Math.Max(1, context)
            : activeParams * 0.05;   // conservative fraction
        double workspace = wBytes * 0.10;
        double peak = wBytes + grads + optim + acts + workspace;
        return new Dictionary<string, object?>
        {
            ["format"] = WorksetFormat,
            ["active_weight_bytes"] = (long)wBytes,
            ["gradient_bytes"] = (long)grads,
            ["optimizer_bytes"] = (long)optim,
            ["activation_bytes_est"] = (long)acts,
            ["workspace_bytes_est"] = (long)workspace,
            ["peak_working_set_bytes"] = (long)peak,
            ["gate_basis"] = "PEAK_WORKING_SET_NOT_FULL_MODEL",
        };
    }

    // ---------------------------------------------------- §48 -------
    /// <summary>Emit star-resource-cert/v1 for a production bundle —
    /// every certified bundle carries the seven parameter metrics
    /// plus bytes and latency evidence.</summary>
    public static Dictionary<string, object?> ResourceCert(
        JsonElement el)
    {
        foreach (var m in ParamMetrics)
            if (!el.TryGetProperty(m, out _))
                throw new ExecutorError("RESOURCE_CERT_INVALID",
                    $"cert missing {m}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = CertFormat,
            ["scale_profile"] =
                el.TryGetProperty("scale_profile", out var sp)
                    ? sp.GetString() : null,
            ["generation"] =
                el.TryGetProperty("generation", out var g)
                    ? g.GetString() : null,
            ["total_params"] = Num(el, "total_params"),
            ["unique_params"] = Num(el, "unique_params"),
            ["active_params"] = Num(el, "active_params"),
            ["resident_params"] = Num(el, "gpu_resident_params"),
            ["ram_params"] = Num(el, "ram_resident_params"),
            ["nvme_params"] = Num(el, "nvme_cold_params"),
            ["trainable_params"] = Num(el, "trainable_params"),
            ["weight_bytes"] = Num(el, "weight_bytes"),
            ["state_bytes"] = Num(el, "state_bytes"),
            ["workspace_bytes"] = Num(el, "workspace_bytes"),
            ["ttft_ms"] = DNum(el, "ttft_ms"),
            ["itl_ms"] = DNum(el, "itl_ms"),
            ["tps"] = DNum(el, "tps"),
            ["power_w"] = DNum(el, "power_w"),
        };
    }

    // ---------------------------------------------------- §49 -------
    /// <summary>Scale promotion gate: every one of the seven gates
    /// must pass. A capability gain on unusable hardware never
    /// promotes.</summary>
    public static Dictionary<string, object?> PromotionGate(
        JsonElement el)
    {
        var required = new[]
        {
            "capability_gate", "resource_gate", "latency_gate",
            "state_gate", "expert_residency_gate", "generation_gate",
            "provenance_gate",
        };
        var results = new Dictionary<string, object?>();
        var failed = new List<string>();
        foreach (var gname in required)
        {
            bool pass = el.TryGetProperty(gname, out var gv) &&
                        gv.ValueKind == JsonValueKind.True;
            results[gname] = pass;
            if (!pass) failed.Add(gname);
        }
        bool ok = failed.Count == 0;
        return new Dictionary<string, object?>
        {
            ["ok"] = ok, ["format"] = PromotionFormat,
            ["gates"] = results, ["failed_gates"] = failed,
            ["promotion"] = ok ? "ALLOWED" : "DENIED",
        };
    }
}
