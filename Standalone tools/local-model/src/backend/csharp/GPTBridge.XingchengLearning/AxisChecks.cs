// AxisChecks.cs — §42 acceptance battery for the single-core-axis
// consolidation (star-architecture-taxonomy/v1, star-model-core/v1).
//
//   --axis-checks    contract-only verification; no weights, no model
//                    execution. Proves: exactly one model core, every
//                    feature has exactly one valid primary axis,
//                    runtime/capability/experimental features never
//                    classify into MODEL_CORE, the contract hash is
//                    deterministic and drifting hashes are refused.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class AxisChecks
{
    public const string ReportFormat = "star-axis-checks/v1";

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

    private static bool ExpectError(string code, Action act)
    {
        try { act(); return false; }
        catch (ExecutorError ee) { return ee.ErrorCode == code; }
    }

    private static string Str(object? v) => v as string ?? "";
    private static bool On(object? v) => v is bool b && b;

    public static Dictionary<string, object?> Run(string toolRoot)
    {
        var checks = new List<CheckResult>();

        // ---- core-contract-smoke (§0/§1/§21/§45): ONE core, causal,
        //      decoder-only, correct layer schedule; unknown arch fails.
        checks.Add(Check("core-contract-smoke", () =>
        {
            var c = ArchitectureTaxonomy.CoreContract("xc-fused-1",
                layers: 8, hidden: 64, heads: 4, kvHeads: 2);
            var sched = (List<object?>)c["layer_schedule"]!;
            bool unknown = ExpectError("ARCHITECTURE_UNKNOWN", () =>
                ArchitectureTaxonomy.CoreContract("xc-fused-99"));
            return Str(c["core_type"]) == "hybrid_causal_decoder" &&
                   Str(c["core_name"]) == "HybridCausalDecoder" &&
                   On(c["causal"]) && On(c["autoregressive"]) &&
                   !On(c["encoder"]) && !On(c["cross_attention"]) &&
                   sched.Count == 8 &&
                   Str(sched[3]) == "FULL_ATTENTION" &&
                   Str(sched[7]) == "FULL_ATTENTION" &&
                   Str(sched[0]) == "DELTA_RECURRENT" &&
                   unknown;
        }, "ONE HybridCausalDecoder"));

        // ---- taxonomy-smoke (§13): only the 12 legal axes.
        checks.Add(Check("taxonomy-smoke", () =>
        {
            var t = ArchitectureTaxonomy.Emit();
            var axes = (List<object?>)t["axes"]!;
            return On(t["ok"]) && axes.Count == 12 &&
                   Convert.ToInt32(t["model_core_count"]) == 1 &&
                   Str(t["model_core"]) == "HybridCausalDecoder";
        }, "12 axes, 1 core"));

        // ---- architecture-hash-smoke (§25): canonical determinism —
        //      same generation resolves to the same contract hash;
        //      field tampering changes it.
        checks.Add(Check("architecture-hash-smoke", () =>
        {
            var a = ArchitectureTaxonomy.CoreContract("xc-fused-1");
            var b = ArchitectureTaxonomy.CoreContract("xc-fused-1");
            string ha = Str(a["architecture_contract_hash"]);
            string hb = Str(b["architecture_contract_hash"]);
            var tampered = new Dictionary<string, object?>(b);
            tampered["causal"] = false;
            string ht = ArchitectureTaxonomy.ContractHash(tampered);
            return ha.Length == 64 && ha == hb && ht != ha;
        }, "deterministic + sensitive"));

        // ---- layer-schedule-smoke (§22): single derivation, no
        //      %interval re-derivation scattered around.
        checks.Add(Check("layer-schedule-smoke", () =>
        {
            var s = ArchitectureTaxonomy.LayerSchedule(8, 4);
            var allAttn = ArchitectureTaxonomy.LayerSchedule(4, 1);
            var noAttn = ArchitectureTaxonomy.LayerSchedule(5, 0);
            bool ok = string.Join(",", s) ==
                "DELTA_RECURRENT,DELTA_RECURRENT,DELTA_RECURRENT," +
                "FULL_ATTENTION,DELTA_RECURRENT,DELTA_RECURRENT," +
                "DELTA_RECURRENT,FULL_ATTENTION";
            return ok &&
                   allAttn.All(x => x == "FULL_ATTENTION") &&
                   noAttn.All(x => x == "DELTA_RECURRENT");
        }, "D D D A repeat"));

        // ---- runtime-axis-isolation (§9/§29-§32/§38): cache, offload,
        //      P/D, precision never classify into the model core.
        checks.Add(Check("runtime-axis-isolation", () =>
        {
            string[] comps =
                { "PrefixCache", "ExpertOffloading", "ExpertResidency",
                  "PrefillDecodeDisaggregation", "BatchScheduling",
                  "CUDA lane", "SpeculativeDecoding", "MemoryPlanning" };
            foreach (var cp in comps)
            {
                var cl = ArchitectureTaxonomy.Classify(cp);
                if (cl.PrimaryAxis != "STATE_AXIS" &&
                    cl.PrimaryAxis != "RUNTIME_OPTIMIZATION_AXIS")
                    throw new ExecutorError("CHECK_FAIL",
                        $"{cp} -> {cl.PrimaryAxis}");
                if (cl.ArchitectureAffecting || !cl.RuntimeOnly)
                    throw new ExecutorError("CHECK_FAIL",
                        $"{cp} flags arch={cl.ArchitectureAffecting} " +
                        $"rt={cl.RuntimeOnly}");
            }
            var kv = ArchitectureTaxonomy.Classify("KV-INT8");
            var bf = ArchitectureTaxonomy.Classify("BF16");
            return kv.PrimaryAxis == "STATE_AXIS" &&
                   bf.PrimaryAxis == "PRECISION_AXIS";
        }, "runtime features stay runtime"));

        // ---- capability-axis-isolation (§11/§33): RAG/thinking/
        //      persona/agent are capabilities, never architecture.
        checks.Add(Check("capability-axis-isolation", () =>
        {
            string[] comps =
                { "RAG", "NativeThinking", "Persona", "Roleplay",
                  "ToolCall", "Coding", "Agent", "Instruction" };
            foreach (var cp in comps)
            {
                var cl = ArchitectureTaxonomy.Classify(cp);
                if (cl.PrimaryAxis != "CAPABILITY_AXIS" ||
                    cl.ArchitectureAffecting)
                    return false;
            }
            return true;
        }, "capabilities are not architecture"));

        // ---- experimental-axis-isolation (§19): research families are
        //      never canonical core.
        checks.Add(Check("experimental-axis-isolation", () =>
        {
            string[] comps =
                { "MLA", "CSA", "Gemma4", "KDA", "Mamba", "RWKV",
                  "AttnRes", "LatentMoE", "MSA" };
            foreach (var cp in comps)
                if (ArchitectureTaxonomy.Classify(cp).PrimaryAxis !=
                    "EXPERIMENTAL_ARCHITECTURE")
                    return false;
            return true;
        }, "research stays experimental"));

        // ---- version-dimension-smoke (§34-§36): seven separate
        //      fields; no single rolled-up version.
        checks.Add(Check("version-dimension-smoke", () =>
        {
            var v = ArchitectureTaxonomy.VersionDimensions(toolRoot);
            foreach (var f in ArchitectureTaxonomy.VersionFields)
                if (!v.ContainsKey(f)) return false;
            return Str(v["architecture_generation"]).Length > 0 &&
                   Str(v["runtime_version"]).Length > 0 &&
                   !v.ContainsKey("version");
        }, "7 version dimensions"));

        // ---- drift-gate-smoke (§26): identical hashes pass; any
        //      mismatch is ARCHITECTURE_CONTRACT_DRIFT.
        checks.Add(Check("drift-gate-smoke", () =>
        {
            var c = ArchitectureTaxonomy.CoreContract("xc-fused-1");
            string h = Str(c["architecture_contract_hash"]);
            var ok = ArchitectureTaxonomy.DriftGate(h, h, h, h);
            bool drift = ExpectError("ARCHITECTURE_CONTRACT_DRIFT",
                () => ArchitectureTaxonomy.DriftGate(
                    h, h, "deadbeef", h));
            return On(ok["ok"]) && On(ok["consistent"]) && drift;
        }, "hash mismatch refused"));

        // ---- catalog-axis-smoke (§14/§38): every catalog feature has
        //      exactly one legal primary axis; NO feature classifies
        //      as MODEL_CORE (the core is not a feature).
        checks.Add(Check("catalog-axis-smoke", () =>
        {
            int coreCount = 0;
            foreach (var f in FeatureCatalog.Canonical)
            {
                var fd = FeatureCatalog.FeatureDict(f);
                string ax = Str(fd["primary_axis"]);
                if (!ArchitectureTaxonomy.Axes.Contains(ax))
                    return false;
                if (ax == "MODEL_CORE") ++coreCount;
            }
            return coreCount == 0;
        }, "features classified, core is not a feature"));

        int passed = checks.Count(c => c.Ok);
        return new Dictionary<string, object?>
        {
            ["ok"] = passed == checks.Count,
            ["format"] = ReportFormat,
            ["passed"] = passed,
            ["total"] = checks.Count,
            ["checks"] = checks.Select(c => new Dictionary<string, object?>
            {
                ["name"] = c.Name,
                ["ok"] = c.Ok,
                ["detail"] = c.Detail,
            }).Cast<object?>().ToList(),
            ["model_core"] = ArchitectureTaxonomy.ModelCore,
            ["model_core_count"] = 1,
            ["weights_mutated"] = false,
        };
    }
}
