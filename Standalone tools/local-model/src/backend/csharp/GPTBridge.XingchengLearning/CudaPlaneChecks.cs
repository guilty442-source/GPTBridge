// CudaPlaneChecks.cs — acceptance battery for NativeMemoryCudaPlane
// (memory/CUDA directive §71 priorities + contract sections). Native
// evidence comes from `xc_modeltool memplane-probe`; contract/policy
// checks run in C#. Weight-model untouched — this is the runtime
// optimization axis, never architecture.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CudaPlaneChecks
{
    public const string ReportFormat = "star-cuda-plane-checks/v1";

    private static JsonElement J(string json)
        => JsonDocument.Parse(json).RootElement.Clone();

    private sealed record CheckResult(
        string Name, bool Ok, string Detail);

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

    public static Dictionary<string, object?> Run(string toolRoot)
    {
        string scratch = Path.Combine(
            toolRoot,
            "xingcheng/runtime/state/_cuda-plane-smoke"
                .Replace('/', Path.DirectorySeparatorChar) +
            "-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(scratch);
        string stderrLog = Path.Combine(scratch, "stderr.log");

        var checks = new List<CheckResult>();
        try
        {
            // ---------- native probe: real device or contract sim ----
            checks.Add(Check("memplane-probe-native", () =>
            {
                var r = NativeTools.Run(
                    NativeTools.ModelToolExe(toolRoot),
                    new[] { "memplane-probe",
                            "--budget", "2147483648",
                            "--pinned", "33554432" },
                    toolRoot, stderrLog, timeoutS: 60);
                if (r.ExitCode != 0) return false;
                // last JSON line is the verdict
                var last = r.StdoutTail.TrimEnd()
                    .Split('\n').Last().Trim();
                var doc = J(last);
                return doc.TryGetProperty("ok", out var ok) &&
                       ok.ValueKind == JsonValueKind.True;
            }));

            checks.Add(Check("memplane-telemetry-schema", () =>
            {
                var r = NativeTools.Run(
                    NativeTools.ModelToolExe(toolRoot),
                    new[] { "memplane-telemetry" },
                    toolRoot, stderrLog, timeoutS: 60);
                if (r.ExitCode != 0) return false;
                var doc = J(r.StdoutTail.TrimEnd()
                    .Split('\n').Last().Trim());
                return (bool)MemoryCudaPlane
                    .ValidateTelemetry(doc)["ok"]!;
            }));

            // ---------------------- precision policy (§1/§2/§61) ------
            checks.Add(Check("fp64-oracle-only", () =>
                MemoryCudaPlane.PrecisionPolicy["fp64_role"] ==
                    "ORACLE_ONLY" &&
                MemoryCudaPlane.PrecisionPolicy["common_gemm"] == "BF16"));

            checks.Add(Check("fp8-fp4-disabled", () =>
                MemoryCudaPlane.PrecisionPolicy["fp8"] ==
                    "DISABLED_BY_HARDWARE" &&
                MemoryCudaPlane.PrecisionPolicy["fp4"] ==
                    "DISABLED_BY_HARDWARE"));

            checks.Add(Check("precision-not-architecture", () =>
            {
                // §10 taxonomy: precision is a runtime/storage axis —
                // classifying BF16/INT8 must never hit MODEL_CORE.
                var bf = ArchitectureTaxonomy.Classify("BF16");
                var i8 = ArchitectureTaxonomy.Classify("INT8");
                return bf.PrimaryAxis == "PRECISION_AXIS" &&
                       i8.PrimaryAxis == "PRECISION_AXIS";
            }));

            // ---------------------- pressure ladder (§11/§32) --------
            checks.Add(Check("pressure-ladder-order", () =>
            {
                var p = MemoryCudaPlane.PlanPressure(
                    1000, new Dictionary<string, long>
                    {
                        ["evict_cold_prefix"] = 300,
                        ["reduce_warm_prefix"] = 400,
                        ["evict_routed_expert"] = 500,
                    });
                var steps = (List<object?>)p["steps"]!;
                var first = (Dictionary<string, object?>)steps[0]!;
                return (string)first["step"]! == "evict_cold_prefix" &&
                       p["covered"] is bool c && c;
            }));

            checks.Add(Check("pressure-ladder-rejects-last", () =>
            {
                var p = MemoryCudaPlane.PlanPressure(
                    100000, new Dictionary<string, long>
                    {
                        ["evict_cold_prefix"] = 100,
                        ["reduce_warm_prefix"] = 100,
                        ["evict_routed_expert"] = 100,
                        ["shrink_expert_hotset"] = 100,
                        ["reduce_batch"] = 100,
                        ["reduce_prefill_chunk"] = 100,
                        ["spill_eligible_state"] = 100,
                    });
                var steps = (List<object?>)p["steps"]!;
                var last = (Dictionary<string, object?>)steps[^1]!;
                return p["must_reject"] is bool m && m &&
                       (string)last["step"]! == "reject_request";
            }));

            checks.Add(Check("prefix-evicts-before-active", () =>
                MemoryCudaPlane.LadderIndex("evict_cold_prefix") <
                MemoryCudaPlane.LadderIndex("evict_routed_expert") &&
                MemoryCudaPlane.LadderIndex("evict_routed_expert") <
                MemoryCudaPlane.LadderIndex("reduce_batch")));

            // ------------------- prefill chunking (§42/§43) ----------
            checks.Add(Check("prefill-chunk-plan", () =>
            {
                var small = MemoryCudaPlane.PrefillChunkPlan(
                    150 * 1024, 1024, 10);
                var big = MemoryCudaPlane.PrefillChunkPlan(
                    8L << 20, 1024, 10);
                return (long)small["adaptive_prefill_chunk"]! == 128 &&
                       (long)big["adaptive_prefill_chunk"]! == 1024;
            }));

            // ------------------- tensor-core alignment (§23) ---------
            checks.Add(Check("alignment-score", () =>
                MemoryCudaPlane.AlignmentScore(768) == 1.0 &&
                MemoryCudaPlane.AlignmentScore(100) == 0.5 &&
                MemoryCudaPlane.AlignmentScore(97) == 0));

            // ------------------ memory plane taxonomy ----------------
            checks.Add(Check("memplane-runtime-axis", () =>
            {
                // The whole plane is RUNTIME_OPTIMIZATION — never
                // architecture, never a new generation.
                var cls = ArchitectureTaxonomy.Classify(
                    "UnifiedCudaMemoryManager");
                var cls2 = ArchitectureTaxonomy.Classify(
                    "CudaDevicePool");
                return cls.PrimaryAxis == "RUNTIME_OPTIMIZATION_AXIS" &&
                       cls2.PrimaryAxis == "RUNTIME_OPTIMIZATION_AXIS";
            }));
        }
        finally
        {
            try { Directory.Delete(scratch, true); }
            catch { /* scratch cleanup best-effort */ }
        }

        int passed = checks.Count(c => c.Ok);
        return new Dictionary<string, object?>
        {
            ["ok"] = passed == checks.Count,
            ["format"] = ReportFormat,
            ["passed"] = passed,
            ["total"] = checks.Count,
            ["checks"] = checks.Select(c =>
                (object?)new Dictionary<string, object?>
                {
                    ["name"] = c.Name,
                    ["ok"] = c.Ok,
                    ["detail"] = c.Detail,
                }).ToList(),
            ["weights_mutated"] = false,
            ["capability_training_frozen"] = true,
        };
    }
}
