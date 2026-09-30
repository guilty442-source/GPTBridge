// DepthScale.cs — DepthScalePlanner + star-depth-inheritance/v1
// (efficiency/scale directive §7-§13, §53, §62).
//
// Solar-style depth-up scaling: when a generation's capability is
// mature, prefer more DEPTH over more WIDTH (§7, §13 — expert capacity
// first, depth second, width last). New layers are never born from
// scratch at production: they inherit or are seeded from existing
// blocks (§8), recorded in a star-depth-inheritance/v1 map (§9), and
// must prove themselves in an A/B probe against from-scratch before
// adoption (§11). The canonical layer schedule is inviolable (§12 —
// delta:full = 3:1 period, full_attention_interval unchanged).
//
//   Plan                 target layer count + insertion map; refuses
//                        any schedule-breaking depth (§12).
//   ValidateInheritance  star-depth-inheritance/v1 record check (§9-10).
//   ProbeVerdict         §11 A/B: adoption only if inheritance beats
//                        from-scratch on cost without regression.
//   Efficiency           §53 depth-efficiency metric record.
//
// CAPABILITY_TRAINING_FROZEN: planning/validation only — the probe
// itself runs through the governed trainer lane, never here.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class DepthScale
{
    public const string InheritanceFormat = "star-depth-inheritance/v1";
    public const string PlanFormat = "star-depth-plan/v1";
    public const string ProbeFormat = "star-depth-scale-probe/v1";
    public const string EfficiencyFormat = "star-depth-efficiency/v1";

    // §10 the only permitted init candidates — every method must be
    // probed; nothing duplicates straight into production.
    public static readonly string[] InheritMethods =
    {
        "COPY", "AVERAGE_NEIGHBORS", "IDENTITY_NEAR",
        "SMALL_PERTURBATION",
    };

    private static long Num(JsonElement r, string k, long d = 0) =>
        r.TryGetProperty(k, out var v) && v.ValueKind ==
            JsonValueKind.Number && v.TryGetInt64(out long n) ? n : d;
    private static double DNum(JsonElement r, string k, double d = 0) =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.Number ? v.GetDouble() : d;
    private static string Str(JsonElement r, string k, string d = "") =>
        r.TryGetProperty(k, out var v) &&
        v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? d : d;

    // -------------------------------------------------------- plan --

    /// <summary>§8/§12 depth-expansion plan. The canonical xc-fused-1
    /// schedule is Delta×3 : FullAttn×1 — a 4-layer period. Any target
    /// depth that is not a multiple of the period, or any request that
    /// changes full_attention_interval, is DEPTH_INHERITANCE_INVALID.
    /// New layers inherit per layer-map entries; absent explicit maps
    /// default to AVERAGE_NEIGHBORS (nearest same-kind source).</summary>
    public static Dictionary<string, object?> Plan(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("DEPTH_INHERITANCE_INVALID",
                "plan input must be an object");
        long parentLayers = Num(el, "parent_layers", 0);
        long targetLayers = Num(el, "target_layers", 0);
        long interval = Num(el, "full_attention_interval", 4);
        if (parentLayers <= 0 || targetLayers <= parentLayers)
            throw new ExecutorError("DEPTH_INHERITANCE_INVALID",
                "target_layers must exceed parent_layers");
        if (parentLayers % interval != 0 || targetLayers % interval != 0)
            throw new ExecutorError("DEPTH_INHERITANCE_INVALID",
                $"depth must keep the {interval}-layer canonical " +
                $"period (parent {parentLayers}, target {targetLayers})");
        if (el.TryGetProperty("full_attention_interval_new", out var ni)
            && ni.ValueKind == JsonValueKind.Number &&
            ni.GetInt64() != interval)
            throw new ExecutorError("DEPTH_INHERITANCE_INVALID",
                "full_attention_interval is a canonical invariant " +
                "(§12) — depth-up may not move it");

        // §8 layer map: new layers are inserted at period boundaries so
        // every interleave position keeps its layer kind.
        var map = new List<object?>();
        var explicitMap = new Dictionary<long, JsonElement>();
        if (el.TryGetProperty("layer_map", out var lm) &&
            lm.ValueKind == JsonValueKind.Array)
            foreach (var e in lm.EnumerateArray())
                if (e.ValueKind == JsonValueKind.Object)
                    explicitMap[Num(e, "target_layer")] = e;

        for (long t = 0; t < targetLayers; t++)
        {
            string kind = t % interval == interval - 1
                ? "full_attention" : "deltanet";
            if (t < parentLayers)
            {
                map.Add(new Dictionary<string, object?>
                {
                    ["target_layer"] = t, ["kind"] = kind,
                    ["source_layer"] = t,
                    ["inherit_method"] = "IDENTITY_NEAR",
                    ["trainability"] = "trainable",
                    ["inserted"] = false,
                });
                continue;
            }
            // new block — clone the same-kind neighbour directly below.
            long src = t - 1;
            while (src >= 0 &&
                   src % interval != (t % interval)) src--;
            if (src < 0) src = t - 1;
            string method = "AVERAGE_NEIGHBORS";
            string? whash = null;
            string train = "trainable";
            if (explicitMap.TryGetValue(t, out var ov))
            {
                method = Str(ov, "inherit_method", method);
                whash = Str(ov, "weight_hash", "");
                train = Str(ov, "trainability", train);
                src = Num(ov, "source_layer", src);
            }
            if (!InheritMethods.Contains(method))
                throw new ExecutorError("DEPTH_INHERITANCE_INVALID",
                    $"inherit_method '{method}' not in " +
                    "COPY|AVERAGE_NEIGHBORS|IDENTITY_NEAR|" +
                    "SMALL_PERTURBATION (§10)");
            map.Add(new Dictionary<string, object?>
            {
                ["target_layer"] = t, ["kind"] = kind,
                ["source_layer"] = src,
                ["inherit_method"] = method,
                ["weight_hash"] = whash,
                ["trainability"] = train,
                ["adaptation_status"] = "pending_probe",
                ["inserted"] = true,
            });
        }

        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = PlanFormat,
            ["architecture"] = "xc-fused-1",
            ["parent_layers"] = parentLayers,
            ["target_layers"] = targetLayers,
            ["full_attention_interval"] = interval,
            ["schedule_preserved"] = true,
            ["inserted_layers"] = targetLayers - parentLayers,
            ["layer_map"] = map,
            ["next_gate"] = "depth-scale-probe (§11 A/B vs " +
                            "from-scratch — adoption only on measured " +
                            "efficiency, never direct production)",
        };
    }

    // ------------------------------------------------- inheritance --

    /// <summary>§9 star-depth-inheritance/v1 record validator —
    /// every layer entry must carry source_layer, target_layer,
    /// inherit_method (whitelist §10), weight_hash for copy-derived
    /// weights, trainability and adaptation_status.</summary>
    public static Dictionary<string, object?> ValidateInheritance(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object ||
            !el.TryGetProperty("layers", out var layers) ||
            layers.ValueKind != JsonValueKind.Array)
            throw new ExecutorError("DEPTH_INHERITANCE_INVALID",
                "record must carry a layers[] array");
        var problems = new List<object?>();
        int i = 0;
        foreach (var e in layers.EnumerateArray())
        {
            if (e.ValueKind != JsonValueKind.Object)
            { problems.Add($"layers[{i}] not an object"); i++; continue; }
            string method = Str(e, "inherit_method");
            if (!InheritMethods.Contains(method))
                problems.Add(
                    $"layers[{i}] inherit_method '{method}' invalid");
            if (Num(e, "target_layer", -1) < 0)
                problems.Add($"layers[{i}] missing target_layer");
            if (Num(e, "source_layer", -1) < 0 &&
                method != "IDENTITY_NEAR")
                problems.Add(
                    $"layers[{i}] missing source_layer for {method}");
            if (method == "COPY" &&
                Str(e, "weight_hash").Length == 0)
                problems.Add(
                    $"layers[{i}] COPY requires weight_hash (§9)");
            i++;
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = problems.Count == 0,
            ["format"] = InheritanceFormat,
            ["layers_checked"] = i,
            ["violations"] = problems,
            ["verdict"] = problems.Count == 0
                ? "INHERITANCE_VALID" : "DEPTH_INHERITANCE_INVALID",
        };
    }

    // ------------------------------------------------------- probe --

    /// <summary>§11 A/B probe verdict. Input carries `scratch` and
    /// `inheritance` metric objects: steps_to_target, tokens_to_target,
    /// gpu_hours, vram_bytes, final_capability, regression. Adoption
    /// requires inheritance strictly better on >=2 cost dims and not
    /// worse on capability/regression; a capability drop is
    /// DEPTH_SCALE_REGRESSION.</summary>
    public static Dictionary<string, object?> ProbeVerdict(
        JsonElement el)
    {
        if (!el.TryGetProperty("scratch", out var a) ||
            !el.TryGetProperty("inheritance", out var b) ||
            a.ValueKind != JsonValueKind.Object ||
            b.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("DEPTH_SCALE_REGRESSION",
                "probe requires scratch{} and inheritance{} results");
        var costDims = new[] { "steps_to_target", "tokens_to_target",
                               "gpu_hours", "vram_bytes" };
        int bBetter = 0;
        var detail = new Dictionary<string, object?>();
        foreach (var d in costDims)
        {
            double va = DNum(a, d, double.NaN),
                   vb = DNum(b, d, double.NaN);
            bool better = !double.IsNaN(va) && !double.IsNaN(vb) &&
                          vb < va;
            if (better) bBetter++;
            detail[d] = new Dictionary<string, object?>
            {
                ["scratch"] = double.IsNaN(va) ? null : va,
                ["inheritance"] = double.IsNaN(vb) ? null : vb,
                ["inheritance_better"] = better,
            };
        }
        double capA = DNum(a, "final_capability", double.NaN),
               capB = DNum(b, "final_capability", double.NaN);
        bool capRegress = !double.IsNaN(capA) && !double.IsNaN(capB) &&
                          capB < capA;
        bool regFlag =
            b.TryGetProperty("regression", out var rg) &&
            rg.ValueKind == JsonValueKind.True;
        bool adopt = bBetter >= 2 && !capRegress && !regFlag;
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = ProbeFormat,
            ["verdict"] = regFlag || capRegress
                ? "DEPTH_SCALE_REGRESSION"
                : adopt ? "ADOPT_INHERITANCE" : "REJECT_INHERITANCE",
            ["cost_dims_inheritance_better"] = bBetter,
            ["capability"] = new Dictionary<string, object?>
            {
                ["scratch"] = double.IsNaN(capA) ? null : capA,
                ["inheritance"] = double.IsNaN(capB) ? null : capB,
                ["regression"] = capRegress || regFlag,
            },
            ["cost_dims"] = detail,
            ["rule"] = "adopt only when inheritance is better on " +
                       ">=2 cost dims with no capability regression",
        };
    }

    // --------------------------------------------------- metrics ----

    /// <summary>§53 depth-efficiency record.</summary>
    public static Dictionary<string, object?> Efficiency(JsonElement el)
    {
        long inherited = Num(el, "inherited_params", 0);
        long newp = Num(el, "new_params", 0);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = EfficiencyFormat,
            ["inherited_param_ratio"] =
                inherited + newp > 0
                    ? Math.Round((double)inherited /
                                 (inherited + newp), 4) : 0,
            ["new_param_ratio"] =
                inherited + newp > 0
                    ? Math.Round((double)newp /
                                 (inherited + newp), 4) : 0,
            ["steps_to_recover"] = Num(el, "steps_to_recover"),
            ["steps_to_exceed_parent"] =
                Num(el, "steps_to_exceed_parent"),
            ["training_tokens_saved"] =
                Num(el, "training_tokens_saved"),
        };
    }
}
