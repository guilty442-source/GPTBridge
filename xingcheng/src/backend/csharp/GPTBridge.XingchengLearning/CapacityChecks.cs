// CapacityChecks.cs — acceptance battery for CapacityPlane
// (capacity directive §0-§57). Mirrors SiliconChecks conventions:
// every check returns bool, fail-closed ExecutorError paths are
// exercised with ExpectError, native probes are invoked through the
// governed subprocess lane. Report format: star-capacity-checks/v1.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CapacityChecks
{
    public const string ReportFormat = "star-capacity-checks/v1";

    private static JsonElement J(string json)
        => JsonDocument.Parse(json).RootElement.Clone();

    private sealed record CheckResult(string Name, bool Ok,
                                      string Detail);

    private static CheckResult Check(string name, Func<bool> run,
                                     string detail = "")
    {
        try { return new(name, run(), detail); }
        catch (Exception ex)
        {
            return new(name, false,
                $"{ex.GetType().Name}: {ex.Message}");
        }
    }

    private static bool ExpectError(string code, Action act)
    {
        try { act(); return false; }
        catch (ExecutorError ee) { return ee.ErrorCode == code; }
    }

    private static JsonElement? Native(string toolRoot,
                                       string stderrLog,
                                       params string[] args)
    {
        var r = NativeTools.Run(
            NativeTools.ModelToolExe(toolRoot), args,
            toolRoot, stderrLog, timeoutS: 120);
        if (r.ExitCode != 0) return null;
        var tail = r.StdoutTail.TrimEnd();
        int nl = tail.LastIndexOf('\n');
        var last = (nl >= 0 ? tail[(nl + 1)..] : tail).Trim();
        try { return J(last); }
        catch (JsonException) { return null; }
    }

    private static bool OkTrue(JsonElement? d) =>
        d.HasValue && d.Value.TryGetProperty("ok", out var ok) &&
        ok.ValueKind == JsonValueKind.True;

    public static Dictionary<string, object?> Run(string toolRoot)
    {
        string scratch = Path.Combine(
            toolRoot,
            "xingcheng/runtime/state/_capacity-smoke"
                .Replace('/', Path.DirectorySeparatorChar) +
            "-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(scratch);
        string stderrLog = Path.Combine(scratch, "stderr.log");

        string? bundle = null;
        foreach (var cand in new[]
                 { "xingcheng/runtime/devin/bf16-candidate/bundle",
                   "xingcheng/runtime/devin/hybrid-e2e/bundle",
                   "xingcheng/runtime/devin/gen-consolidate/bundle" })
        {
            string p = Path.Combine(toolRoot,
                cand.Replace('/', Path.DirectorySeparatorChar));
            if (File.Exists(Path.Combine(p, "manifest.json")))
            { bundle = p; break; }
        }

        var checks = new List<CheckResult>();
        try
        {
            // ------------ §4/§5 native measured six-param report ----
            checks.Add(Check("capacity-metrics", () =>
            {
                if (bundle == null) return true;
                var d = Native(toolRoot, stderrLog,
                               "capacity-metrics", "--bundle", bundle);
                if (!OkTrue(d)) return false;
                foreach (string k in CapacityPlane.ParamMetrics
                                          .Concat(CapacityPlane
                                                      .StorageMetrics))
                    if (!d!.Value.TryGetProperty(k, out _)) return false;
                // §0: the active ceiling verdict must be honest.
                return d!.Value.TryGetProperty(
                           "active_within_ceiling", out _);
            }));

            // ------------ §51-§53 ceilings --------------------------
            checks.Add(Check("capacity-ceiling", () =>
            {
                var r = CapacityPlane.CeilingReport();
                bool hard = ExpectError(
                    "ACTIVE_PARAMETER_CEILING_EXCEEDED",
                    () => CapacityPlane.RequireWithinActiveCeiling(
                        1_000_000_001L));
                CapacityPlane.RequireWithinActiveCeiling(
                    1_000_000_000L);   // boundary passes
                return hard &&
                    (long)r["current_total_capacity_ceiling"]! ==
                        CapacityPlane.DefaultTotalCeiling &&
                    (long)r["active_params_ceiling"]! ==
                        CapacityPlane.ActiveCeiling;
            }));

            // ------------ codex publishable band 300M..20B ----------
            checks.Add(Check("publishable-scale-band", () =>
            {
                // boundaries inclusive
                CapacityPlane.RequirePublishableScale(
                    CapacityPlane.PublishableMinParams, "b-min");
                CapacityPlane.RequirePublishableScale(
                    CapacityPlane.PublishableMaxParams, "b-max");
                bool low = ExpectError(
                    "MODEL_SCALE_OUT_OF_PUBLISHABLE_BAND",
                    () => CapacityPlane.RequirePublishableScale(
                        CapacityPlane.PublishableMinParams - 1, "low"));
                bool high = ExpectError(
                    "MODEL_SCALE_OUT_OF_PUBLISHABLE_BAND",
                    () => CapacityPlane.RequirePublishableScale(
                        CapacityPlane.PublishableMaxParams + 1, "high"));
                // fail-closed: a bundle without readable param_count
                // cannot be promoted to a formal model.
                string scratch = Path.Combine(
                    Path.GetTempPath(),
                    "xc-band-" + Guid.NewGuid().ToString("N")[..8]);
                Directory.CreateDirectory(scratch);
                bool unverifiable = ExpectError(
                    "MODEL_SCALE_UNVERIFIABLE",
                    () => CapacityPlane.EnforcePublishableOnActivate(
                        scratch));
                try { Directory.Delete(scratch, true); } catch { }
                return low && high && unverifiable;
            }));

            // ------------ §32-§34 common floor ----------------------
            checks.Add(Check("common-floor-gate", () =>
            {
                var ok_ = CapacityPlane.CommonFloorGate(
                    350_000_000L, 60_000_000L);
                var high = CapacityPlane.CommonFloorGate(
                    550_000_000L, 80_000_000L);
                return (string)ok_["verdict"]! == "FLOOR_OK" &&
                       (long)ok_["routed_active_budget"]! ==
                           590_000_000L &&
                       (string)high["verdict"]! ==
                           "COMMON_FLOOR_TOO_HIGH" &&
                       !(bool)high["ok"]!;
            }));

            // ------------ §4 metrics completeness -------------------
            checks.Add(Check("six-param-metric-contract", () =>
            {
                var r = CapacityPlane.ValidateMetrics(J("""
                    {"SOURCE_PARAMS":1300000,"DISTILLED_TOTAL_PARAMS":
                     1300000,"UNIQUE_PARAMS":1200000,
                     "ACTIVE_PARAMS":400000,"TRAINABLE_PARAMS":300000,
                     "RESIDENT_PARAMS":400000,"BF16_WEIGHT_BYTES":
                     2600000,"QUANTIZED_WEIGHT_BYTES":2600000,
                     "GPU_RESIDENT_BYTES":800000,"RAM_RESIDENT_BYTES":0,
                     "NVME_BYTES":2600000}
                    """));
                bool missing = ExpectError("ACTIVE_PARAMS_MISSING",
                    () => CapacityPlane.ValidateMetrics(J(
                        """{"total_params":1300000}""")));
                return (bool)r["metrics_complete"]! && missing;
            }));

            // ------------ §37/§38 effective compute -----------------
            checks.Add(Check("effective-compute", () =>
            {
                var a = CapacityPlane.EffectiveCompute(
                    600_000_000L, 1000, 14, 14, 0, 0);
                var b = CapacityPlane.EffectiveCompute(
                    600_000_000L, 10_000, 14, 14, 0, 0);
                // 10x reasoning tokens -> ~10x effective compute.
                return (double)b["effective_compute"]! /
                       (double)a["effective_compute"]! > 9.0;
            }));

            // ------------ §9-§15 distillation plane -----------------
            checks.Add(Check("distillation-plane-contract", () =>
            {
                var c = CapacityPlane.DistillationContract();
                var lanes = (List<object?>)c["lanes"]!;
                return lanes.Count == 4 &&
                       lanes.Cast<string>().SequenceEqual(
                           CapacityPlane.DistillLanes) &&
                       (bool)c["quantization_aware"]! &&
                       (int)((Dictionary<string, object?>)
                            c["routing"]!)["student_top_k"]! == 2;
            }));

            checks.Add(Check("reasoning-compression-gate", () =>
            {
                var win = CapacityPlane.ReasoningCompression(
                    200, 40, 0.80, 0.79);
                var reg = CapacityPlane.ReasoningCompression(
                    200, 40, 0.80, 0.60);
                return (string)win["verdict"]! == "STUDENT_WINS" &&
                       (string)reg["verdict"]! ==
                           "THINKING_COMPRESSION_REGRESSION";
            }));

            checks.Add(Check("capability-distill-gate", () =>
            {
                var teacher = new Dictionary<string, double>
                    { ["math"] = 0.8, ["coding"] = 0.7 };
                var ok_ = CapacityPlane.CapabilityDistillGate(
                    teacher, new Dictionary<string, double>
                        { ["math"] = 0.79, ["coding"] = 0.70 });
                bool reg = ExpectError("DISTILLATION_REGRESSION",
                    () => CapacityPlane.CapabilityDistillGate(
                        teacher, new Dictionary<string, double>
                            { ["math"] = 0.5, ["coding"] = 0.7 }));
                return (bool)ok_["ok"]! && reg;
            }));

            // ------------ §20-§26 precision table -------------------
            checks.Add(Check("quantization-policy", () =>
            {
                bool routerInt4 = ExpectError(
                    "QUANTIZATION_REGRESSION",
                    () => CapacityPlane.ValidatePrecision(
                        "router", "INT4"));
                bool commonInt4 = ExpectError(
                    "QUANTIZATION_REGRESSION",
                    () => CapacityPlane.ValidatePrecision(
                        "common_core", "INT4"));
                var cold = CapacityPlane.ValidatePrecision(
                    "routed_cold", "INT4");
                var router = CapacityPlane.ValidatePrecision(
                    "router", "FP32");
                var int8c = CapacityPlane.ValidatePrecision(
                    "common_core", "INT8", certified: true);
                return routerInt4 && commonInt4 &&
                       (bool)cold["ok"]! && (bool)router["ok"]! &&
                       (bool)int8c["ok"]!;
            }));

            // ------------ §27-§30 three-level compression -----------
            checks.Add(Check("compression-three-levels", () =>
            {
                var r = CapacityPlane.CompressionReport(
                    100_000_000L, 20_000_000_000L, 500_000_000L);
                return r.ContainsKey("A_parameter_compression") &&
                       r.ContainsKey("B_precision_compression") &&
                       r.ContainsKey("C_working_set_compression");
            }));

            // ------------ §16-§19 expert lifecycle ------------------
            checks.Add(Check("expert-lifecycle-gate", () =>
            {
                var dup = CapacityPlane.ExpertLifecycle(
                    3, weightSimilarity: 0.95, utilization: 0.4,
                    capabilityContribution: 0.02, replaceable: true,
                    capabilityGatePassed: true);
                bool merge = ((List<object?>)dup["candidates"]!)
                    .Contains("MERGE");
                var dead = CapacityPlane.ExpertLifecycle(
                    7, 0.10, utilization: 0.0005,
                    capabilityContribution: 0.001, replaceable: true,
                    capabilityGatePassed: true);
                bool prunable = (bool)dead["prunable"]!;
                bool denied = ExpectError("EXPERT_PRUNING_REGRESSION",
                    () => CapacityPlane.ExpertLifecycle(
                        9, 0.1, 0.0005, 0.001, true, false));
                return merge && prunable && denied;
            }));

            // ------------ §7/§54 pipeline + promotion ---------------
            checks.Add(Check("generation-pipeline", () =>
            {
                var all = CapacityPlane.Pipeline
                    .Concat(new[] { "CAPABILITY", "DISTILLATION",
                                    "QUANTIZATION", "COMPRESSION",
                                    "ACTIVE_COMPUTE", "MEMORY",
                                    "LATENCY", "REGRESSION",
                                    "LINEAGE" })
                    .ToDictionary(s => s, _ => true);
                var pass = CapacityPlane.PromotionGate(all);
                var partial = new Dictionary<string, bool>(all)
                    { ["QUANTIZE"] = false };
                var blocked = CapacityPlane.PromotionGate(partial);
                return (string)pass["verdict"]! ==
                           "PROMOTION_ELIGIBLE" &&
                       (string)blocked["verdict"]! ==
                           "PROMOTION_BLOCKED";
            }));

            // ------------ §56 KPI record ----------------------------
            checks.Add(Check("capacity-kpis", () =>
            {
                var r = CapacityPlane.KpiReport(J("""
                    {"total_params":20000000000,"unique_params":
                     19000000000,"active_params":600000000,
                     "active_ratio":0.03,"trainable_params":300000000,
                     "gpu_resident_params":800000000,
                     "ram_resident_params":4000000000,
                     "nvme_params":20000000000,
                     "compressed_bytes":10000000000,
                     "reasoning_tokens":40,
                     "transfer_bytes_per_token":1200}
                    """));
                return ((JsonElement)r["active_params"]!)
                           .GetInt64() == 600_000_000L &&
                       (bool)r["kpi_set_fixed"]!;
            }));
        }
        finally
        {
            try { Directory.Delete(scratch, true); } catch { }
        }

        int passed = checks.Count(c => c.Ok);
        return new Dictionary<string, object?>
        {
            ["ok"] = passed == checks.Count,
            ["format"] = ReportFormat,
            ["passed"] = passed,
            ["total"] = checks.Count,
            ["weights_mutated"] = false,
            ["checks"] = checks.Select(c => (object?)
                new Dictionary<string, object?>
                {
                    ["name"] = c.Name,
                    ["ok"] = c.Ok,
                    ["detail"] = c.Detail,
                }).ToList(),
        };
    }
}
