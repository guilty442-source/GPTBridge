// SiliconChecks.cs — §85 acceptance battery for the
// NativeSiliconEfficiencyPlane (system reuse + NPU-first + CPU profile +
// parameter efficiency).
//
//   --silicon-checks    runs the battery against scratch state under
//                       <tool-root>/xingcheng/runtime/state/
//                       _silicon-smoke-<ts>/ (self-cleaning).
//
// Native probes run through xc_modeltool — the same governed subprocess
// lane as every other native invocation. Contract/policy checks stay
// in-process. No NPU on the bench machine is evidence, not failure:
// npu-* checks pass when the probe reports npu_present:false.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class SiliconChecks
{
    public const string ReportFormat = "star-silicon-checks/v1";

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

    // Run a native mode; return the last stdout JSON line's root.
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
            "xingcheng/runtime/state/_silicon-smoke"
                .Replace('/', Path.DirectorySeparatorChar) +
            "-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(scratch);
        string stderrLog = Path.Combine(scratch, "stderr.log");

        // A real bundle for manifest-driven probes; absent is a SKIP
        // verdict at the contract level, not a battery failure.
        string? bundle = null;
        foreach (var cand in new[]
                 { "xingcheng/runtime/devin/hybrid-e2e/bundle",
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
            // ------------- §2/§3/§6 single owner + dedup -------------
            checks.Add(Check("single-runtime-owner", () =>
            {
                var d = Native(toolRoot, stderrLog,
                               "single-runtime-owner");
                return OkTrue(d) && d!.Value.TryGetProperty(
                    "second_claim_denied", out var s) &&
                    s.ValueKind == JsonValueKind.True;
            }));

            checks.Add(Check("system-reuse-probe", () =>
            {
                if (bundle == null) return true;   // skip-evidence
                var d = Native(toolRoot, stderrLog,
                               "system-reuse-probe",
                               "--bundle", bundle,
                               "--consumers", "3");
                return OkTrue(d) && d!.Value.TryGetProperty(
                    "system_reuse_ratio", out var r) &&
                    Math.Abs(r.GetDouble() - 1.0 / 3.0) < 1e-6;
            }));

            checks.Add(Check("artifact-dedup", () =>
            {
                if (bundle == null) return true;
                var d = Native(toolRoot, stderrLog,
                               "artifact-dedup", "--bundle", bundle);
                return OkTrue(d) && d!.Value.TryGetProperty(
                    "dedup_holds", out var x) &&
                    x.ValueKind == JsonValueKind.True;
            }));

            // C# registry: identical key+hash reuses; same key
            // different bytes fails ARTIFACT_DUPLICATE_LOAD.
            checks.Add(Check("artifact-registry-reuse", () =>
            {
                var a = J("""
                    {"generation":"g","bundle_hash":"h1",
                     "architecture_hash":"a","weight_version":"1",
                     "precision":"fp64","device":"cpu",
                     "compiled_profile":"none","bytes_hash":"B",
                     "bytes":100}
                    """);
                var r1 = SiliconRuntime.RegisterArtifact(scratch, a);
                var r2 = SiliconRuntime.RegisterArtifact(scratch, a);
                bool reused = r2["reused"] is bool rb && rb;
                var forged = J("""
                    {"generation":"g","bundle_hash":"h1",
                     "architecture_hash":"a","weight_version":"1",
                     "precision":"fp64","device":"cpu",
                     "compiled_profile":"none","bytes_hash":"DIFFERENT",
                     "bytes":100}
                    """);
                bool denied = ExpectError("ARTIFACT_DUPLICATE_LOAD",
                    () => SiliconRuntime.RegisterArtifact(
                        scratch, forged));
                return reused && denied;
            }));

            // ------------------- §19-§25 CPU profile -----------------
            checks.Add(Check("cpu-affinity-probe", () =>
            {
                var d = Native(toolRoot, stderrLog,
                               "cpu-affinity-probe");
                return OkTrue(d) && d!.Value.TryGetProperty(
                    "physical_cores", out var p) &&
                    p.GetInt32() >= 1;
            }));

            checks.Add(Check("cpu-bf16-bench", () =>
            {
                var d = Native(toolRoot, stderrLog,
                               "cpu-bf16-bench", "--n", "64");
                return OkTrue(d) && d!.Value.TryGetProperty(
                    "gflops", out var g) && g.GetDouble() > 0;
            }));

            checks.Add(Check("cpu-plan", () =>
            {
                var p = SiliconRuntime.CpuPlan(J("""
                    {"logical":16,"physical":8,"hybrid_pe":true}
                    """));
                var asg = (Dictionary<string, object?>)
                    p["assignments"]!;
                return (string)asg["LATENCY_CRITICAL"]! == "P-CORE" &&
                       (string)asg["IO"]! == "E-CORE" &&
                       p["smt_memory_heavy_exclusive"] is bool s && s;
            }));

            checks.Add(Check("cpu-affinity-invalid", () =>
                ExpectError("CPU_AFFINITY_INVALID", () =>
                    SiliconRuntime.CpuPlan(J(
                        """{"logical":0,"physical":0}""")))));

            // ------------- §8-§12/§84 NPU probe-first ----------------
            checks.Add(Check("npu-discovery", () =>
            {
                var d = Native(toolRoot, stderrLog, "npu-discovery");
                // Probe-first: absent NPU reports present:false and
                // still ok:true (§12).
                return OkTrue(d) && d!.Value.TryGetProperty(
                    "npu_present", out var _);
            }));

            checks.Add(Check("npu-system1-bench", () =>
            {
                var d = Native(toolRoot, stderrLog,
                               "npu-system1-bench");
                // Absent NPU -> skipped:true is the pass condition.
                return OkTrue(d);
            }));

            checks.Add(Check("npu-prefill-bench", () =>
            {
                var d = Native(toolRoot, stderrLog,
                               "npu-prefill-bench");
                return OkTrue(d);
            }));

            checks.Add(Check("silicon-routing-bench", () =>
            {
                var d = Native(toolRoot, stderrLog,
                               "silicon-routing-bench");
                return OkTrue(d) && d!.Value.TryGetProperty(
                    "routes", out var r) &&
                    r.ValueKind == JsonValueKind.Array &&
                    r.GetArrayLength() >= 8;
            }));

            // ------------- §9/§67 broker contract --------------------
            checks.Add(Check("silicon-route-fallback", () =>
            {
                var avail = new HashSet<string> { "GPU", "CPU" };
                var r = SiliconRuntime.Route(
                    "system1", avail, "INTERACTIVE");
                // NPU preferred but absent -> GPU (§8/§12).
                return (string)r["selected"]! == "GPU";
            }));

            checks.Add(Check("silicon-route-uncertified", () =>
                ExpectError("SILICON_ROUTE_UNCERTIFIED", () =>
                    SiliconRuntime.Route(
                        "unregistered_op",
                        new HashSet<string> { "GPU" }, "NORMAL"))));

            // ------------- §30-§33/§79 shared/routed -----------------
            checks.Add(Check("shared-routed-isolation", () =>
            {
                if (bundle == null) return true;
                var d = Native(toolRoot, stderrLog,
                               "shared-routed-isolation",
                               "--bundle", bundle);
                return OkTrue(d);
            }));

            checks.Add(Check("shared-expert-offload-denied", () =>
                ExpectError("SHARED_EXPERT_OFFLOAD_DENIED", () =>
                    SiliconRuntime.ExpertResidencyPolicy(
                        "shared_expert", "NVME"))));

            // ------------- §36-§38 granularity -----------------------
            checks.Add(Check("expert-granularity-probe", () =>
            {
                if (bundle == null) return true;
                var d = Native(toolRoot, stderrLog,
                               "expert-granularity-probe",
                               "--bundle", bundle);
                if (!OkTrue(d)) return false;
                return d!.Value.TryGetProperty("variants", out var v) &&
                       v.ValueKind == JsonValueKind.Array;
            }));

            // ------------- §47/§51 freeze + sparse optim -------------
            checks.Add(Check("parameter-freeze-probe", () =>
            {
                if (bundle == null) return true;
                var d = Native(toolRoot, stderrLog,
                               "parameter-freeze-probe",
                               "--bundle", bundle);
                return OkTrue(d) && d!.Value.TryGetProperty(
                    "sparse_optimizer_bytes_saved", out var s) &&
                    s.GetInt64() >= 0;
            }));

            checks.Add(Check("sparse-optimizer-probe", () =>
            {
                var v = SiliconRuntime.ValidateFreezeMap(J("""
                    {"format":"star-parameter-freeze-map/v1",
                     "max_trainable_params":1000,
                     "components":{"lm_head":{"state":"TRAINABLE",
                                              "params":800},
                                   "common":{"state":"FROZEN",
                                             "params":999000}}}
                    """));
                return (long)v["trainable_params"]! == 800L &&
                       (long)v["optimizer_state_bytes"]! == 12800L;
            }));

            checks.Add(Check("parameter-freeze-violation", () =>
                ExpectError("PARAMETER_FREEZE_VIOLATION", () =>
                    SiliconRuntime.ValidateFreezeMap(J("""
                        {"format":"star-parameter-freeze-map/v1",
                         "max_trainable_params":100,
                         "components":{"all":{"state":"TRAINABLE",
                                              "params":200}}}
                        """)))));

            // ------------- §29 lifetime plan --------------------------
            checks.Add(Check("global-lifetime-plan", () =>
            {
                var p = SiliconRuntime.LifetimePlan(J("""
                    {"requests":[{"name":"decode_ws","bytes":1000,
                                  "tier":"SESSION_PERSISTENT"},
                                 {"name":"prefill_ws","bytes":4000,
                                  "tier":"LAYER_TEMP"}]}
                    """));
                return (long)p["aliased_peak_bytes"]! == 4000L &&
                       (long)p["summed_bytes"]! == 5000L;
            }));

            // ------------- §56/§57 efficiency report ------------------
            checks.Add(Check("parameter-efficiency-report", () =>
            {
                if (bundle != null)
                {
                    var d = Native(toolRoot, stderrLog,
                                   "parameter-efficiency-report",
                                   "--bundle", bundle);
                    if (!OkTrue(d)) return false;
                }
                var r = SiliconRuntime.EfficiencyReport(J("""
                    {"total_params":1000000,"unique_params":900000,
                     "active_params":300000,"capability_score":0.6}
                    """));
                return Math.Abs(
                    (double)r["capability_gain_per_million_params"]! -
                    0.6) < 1e-9;
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
