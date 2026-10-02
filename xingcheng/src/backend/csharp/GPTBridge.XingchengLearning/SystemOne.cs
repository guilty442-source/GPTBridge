// SystemOne.cs — Laya-class fast-path decisions, natively reimplemented
// (§2-§17, §40-§45, §49).
//
//   NativeSystemOneHead    contract: prefill hidden state -> typed
//                          decision; never generates free text. The
//                          head lives as a bundle-side artifact
//                          (decision-head.bin) bound to
//                          model_hash+generation+hidden_size — an
//                          incompatible head falls back to System-2,
//                          never breaks the main model (§49).
//   star-typed-decision/v1 BOOLEAN/CHOICE/ORDINAL_SCORE/CONFIDENCE
//                          with raw+calibrated confidence (§5).
//   DecisionCalibration    temperature + per-type + per-option-count
//                          calibration; softmax is never the reported
//                          confidence (§8/§9).
//   Abstention             calibrated_confidence < threshold ->
//                          ABSTAIN -> SYSTEM_2 (§10).
//   NativeCognitionRouter  FAST_DECISION/GENERATIVE/THINKING/
//                          TOOL_AGENT/RAG/CODING — capability/runtime
//                          routing, NOT a second MoE router (§11).
//   DecisionTrace          ledger: probabilities/confidence/latency/
//                          model hash per fast decision (§42).
//
// Hard boundary (§41): System-1 can never grant permissions, rewrite
// safety, or authorize tools — Governance gates still decide.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class SystemOne
{
    public const string DecisionFormat = "star-typed-decision/v1";
    public const string CalibrationFormat = "star-decision-calibration/v1";
    public const string TraceFormat = "star-decision-trace/v1";
    public const string HeadFormat = "star-system1-head/v1";
    public const string ResourceFormat = "star-system1-resource/v1";
    public const string TraceRel =
        "xingcheng/runtime/logs/decision-trace.jsonl";

    public static readonly string[] DecisionTypes =
        { "BOOLEAN", "CHOICE", "ORDINAL_SCORE", "CONFIDENCE" };

    // §6/§51 first wave — only verifiable, high-value domains. General
    // user-facing answers are never a System-1 target.
    public static readonly string[] CertifiedDomains =
        { "RAG_REQUIRED", "TOOL_REQUIRED", "TOOL_CLASS",
          "CONTINUE_STOP", "INPUT_CLASSIFICATION",
          "RISK_CLASSIFICATION", "TASK_PRIORITY", "YES_NO" };

    public static readonly string[] RouteOutcomes =
        { "FAST_DECISION", "GENERATIVE", "THINKING",
          "TOOL_AGENT", "RAG", "CODING" };

    // ------------------------------------------------ typed decision --

    /// <summary>Validate a star-typed-decision/v1 record. Probabilities
    /// must sum to ~1, the selection must exist, and both raw and
    /// calibrated confidence must be present with a calibration
    /// profile — uncalibrated softmax alone fails closed (§5/§8).</summary>
    public static Dictionary<string, object?> ValidateDecision(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                "decision must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != DecisionFormat)
            throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                $"expected format {DecisionFormat}");
        string type = el.TryGetProperty("decision_type", out var dt)
            ? dt.GetString() ?? "" : "";
        if (!DecisionTypes.Contains(type))
            throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                $"bad decision_type {type}");
        foreach (var k in new[]
                 { "raw_confidence", "calibrated_confidence",
                   "calibration_profile", "decision_version" })
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                    $"missing field {k}");

        double calibrated =
            el.GetProperty("calibrated_confidence").GetDouble();
        if (!(calibrated >= 0.0 && calibrated <= 1.0))
            throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                "calibrated_confidence outside [0,1]");

        int n = 0;
        string selected = "";
        if (type == "BOOLEAN")
        {
            foreach (var k in new[] { "p_true", "p_false" })
                if (!el.TryGetProperty(k, out _))
                    throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                        $"boolean missing {k}");
            double s = el.GetProperty("p_true").GetDouble() +
                       el.GetProperty("p_false").GetDouble();
            if (Math.Abs(s - 1.0) > 1e-6)
                throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                    "boolean probabilities do not sum to 1");
            selected = el.GetProperty("p_true").GetDouble() >= 0.5
                ? "true" : "false";
            n = 2;
        }
        else if (type == "CHOICE")
        {
            if (!el.TryGetProperty("choices", out var ch) ||
                ch.ValueKind != JsonValueKind.Array ||
                ch.GetArrayLength() == 0 ||
                !el.TryGetProperty("probabilities", out var pr) ||
                pr.ValueKind != JsonValueKind.Array ||
                pr.GetArrayLength() != ch.GetArrayLength())
                throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                    "choice needs equal-length choices[]/probabilities[]");
            double s = 0; double best = -1; int bi = -1; int i = 0;
            foreach (var p in pr.EnumerateArray())
            {
                double v = p.GetDouble(); s += v;
                if (v > best) { best = v; bi = i; }
                ++i;
            }
            if (Math.Abs(s - 1.0) > 1e-6)
                throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                    "choice probabilities do not sum to 1");
            n = i;
            selected = ch[bi].GetString() ?? "";
            if (el.TryGetProperty("selected", out var sel) &&
                sel.ValueKind == JsonValueKind.String &&
                (sel.GetString() ?? "").Length > 0)
            {
                selected = sel.GetString()!;
                if (!ch.EnumerateArray().Any(
                        c => c.GetString() == selected))
                    throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                        "selected not in choices");
            }
        }
        else if (type == "ORDINAL_SCORE")
        {
            if (!el.TryGetProperty("levels", out var lv) ||
                lv.ValueKind != JsonValueKind.Array ||
                lv.GetArrayLength() == 0 ||
                !el.TryGetProperty("distribution", out var di) ||
                di.ValueKind != JsonValueKind.Array ||
                di.GetArrayLength() != lv.GetArrayLength())
                throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                    "ordinal needs equal-length levels[]/distribution[]");
            double s = 0, expected = 0; int i = 0;
            foreach (var p in di.EnumerateArray())
            {
                double v = p.GetDouble(); s += v;
                expected += v * lv[i].GetDouble(); ++i;
            }
            if (Math.Abs(s - 1.0) > 1e-6)
                throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                    "ordinal distribution does not sum to 1");
            n = i;
            return new Dictionary<string, object?>
            {
                ["ok"] = true, ["format"] = DecisionFormat,
                ["decision_type"] = type, ["option_count"] = n,
                ["expected_score"] = expected,
                ["calibrated_confidence"] = calibrated,
            };
        }
        else // CONFIDENCE — a scalar confidence statement, no options.
        {
            if (!el.TryGetProperty("statement", out var st) ||
                st.ValueKind != JsonValueKind.String ||
                (st.GetString() ?? "").Length == 0)
                throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                    "confidence type needs a non-empty statement");
            n = 1;
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = DecisionFormat,
            ["decision_type"] = type, ["option_count"] = n,
            ["selected"] = selected,
            ["calibrated_confidence"] = calibrated,
        };
    }

    // --------------------------------------------------- calibration --
    // §8: temperature scaling + optional per-type temperature +
    // per-option-count correction. Uncalibrated softmax alone can
    // never be the reported confidence.

    /// <summary>Temperature-scale a probability vector; returns the
    /// calibrated max-probability.</summary>
    public static double[] TemperatureScale(
        IReadOnlyList<double> probs, double temperature)
    {
        if (temperature <= 0)
            throw new ExecutorError("SYSTEM1_UNCALIBRATED",
                "temperature must be > 0");
        int n = probs.Count;
        var logits = new double[n];
        for (int i = 0; i < n; ++i)
            logits[i] = Math.Log(Math.Max(probs[i], 1e-12)) / temperature;
        double mx = logits.Max();
        double s = 0;
        var outp = new double[n];
        for (int i = 0; i < n; ++i)
        { outp[i] = Math.Exp(logits[i] - mx); s += outp[i]; }
        for (int i = 0; i < n; ++i) outp[i] /= s;
        return outp;
    }

    /// <summary>Apply a calibration profile {temperature,
    /// option_count_correction?} to raw probabilities. Returns the
    /// calibrated vector + calibrated_confidence.</summary>
    public static Dictionary<string, object?> Calibrate(
        IReadOnlyList<double> probs, JsonElement profile)
    {
        double temperature = 1.0;
        if (profile.ValueKind == JsonValueKind.Object &&
            profile.TryGetProperty("temperature", out var t) &&
            t.ValueKind == JsonValueKind.Number)
            temperature = t.GetDouble();
        var cal = TemperatureScale(probs, temperature);
        if (profile.ValueKind == JsonValueKind.Object &&
            profile.TryGetProperty("option_count_correction",
                out var oc) && oc.ValueKind == JsonValueKind.Number &&
            oc.GetDouble() > 0)
        {
            // per-option-count calibration: shrink confidence toward
            // uniform as the option count grows (§8).
            double corr = oc.GetDouble();
            int n = cal.Length;
            double uniform = 1.0 / n;
            for (int i = 0; i < n; ++i)
                cal[i] = uniform + (cal[i] - uniform) *
                    Math.Exp(-corr * (n - 2));
            double s = cal.Sum();
            for (int i = 0; i < n; ++i) cal[i] /= s;
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = CalibrationFormat,
            ["probabilities"] = cal.Cast<object?>().ToList(),
            ["calibrated_confidence"] = cal.Max(),
            ["option_count"] = cal.Length,
        };
    }

    /// <summary>§9 metrics over [{p_correct, correct}] records:
    /// ECE (10 bins), Brier, NLL, accuracy, over/under-confidence.</summary>
    public static Dictionary<string, object?> CalibrationMetrics(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object ||
            !el.TryGetProperty("records", out var rs) ||
            rs.ValueKind != JsonValueKind.Array ||
            rs.GetArrayLength() == 0)
            throw new ExecutorError("SYSTEM1_UNCALIBRATED",
                "metrics need records[] {p, correct}");
        var bins = new int[10]; var binP = new double[10];
        var binC = new double[10];
        int n = 0, correct = 0;
        double brier = 0, nll = 0;
        foreach (var r in rs.EnumerateArray())
        {
            double p = r.GetProperty("p").GetDouble();
            bool ok = r.GetProperty("correct").GetBoolean();
            ++n; if (ok) ++correct;
            brier += (p - (ok ? 1.0 : 0.0)) * (p - (ok ? 1.0 : 0.0));
            nll -= Math.Log(Math.Max(ok ? p : 1.0 - p, 1e-12));
            int b = Math.Min(9, (int)(p * 10));
            ++bins[b]; binP[b] += p; if (ok) ++binC[b];
        }
        double ece = 0;
        var hist = new List<object?>();
        for (int b = 0; b < 10; ++b)
        {
            if (bins[b] == 0) continue;
            double conf = binP[b] / bins[b], acc = binC[b] / bins[b];
            ece += bins[b] * Math.Abs(conf - acc) / n;
            hist.Add(new Dictionary<string, object?>
            {
                ["bin"] = b, ["count"] = bins[b],
                ["mean_confidence"] = conf, ["accuracy"] = acc,
            });
        }
        int over = 0, under = 0;
        foreach (var r in rs.EnumerateArray())
        {
            double p = r.GetProperty("p").GetDouble();
            bool ok = r.GetProperty("correct").GetBoolean();
            if (p >= 0.8 && !ok) ++over;
            if (p < 0.5 && ok) ++under;
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = CalibrationFormat,
            ["n"] = n,
            ["accuracy"] = (double)correct / n,
            ["ece"] = ece,
            ["brier"] = brier / n,
            ["nll"] = nll / n,
            ["confidence_histogram"] = hist,
            ["overconfidence_rate"] = (double)over / n,
            ["underconfidence_rate"] = (double)under / n,
        };
    }

    // ------------------------------------------- abstention + router --
    // §10/§40: below threshold -> ABSTAIN -> SYSTEM_2; the domain must
    // be certified for System-1 or the request fails closed.

    public static Dictionary<string, object?> Route(
        JsonElement el)
    {
        string domain =
            el.TryGetProperty("domain", out var d)
                ? d.GetString() ?? "" : "";
        if (domain.Length > 0 && !CertifiedDomains.Contains(domain))
            throw new ExecutorError("SYSTEM1_DOMAIN_UNCERTIFIED",
                $"domain '{domain}' is not System-1 certified");
        bool eligible =
            el.TryGetProperty("system1_eligible", out var se) &&
            se.ValueKind == JsonValueKind.True;
        double conf =
            el.TryGetProperty("calibrated_confidence", out var cc) &&
            cc.ValueKind == JsonValueKind.Number
                ? cc.GetDouble() : 0.0;
        double threshold =
            el.TryGetProperty("confidence_threshold", out var ct) &&
            ct.ValueKind == JsonValueKind.Number
                ? ct.GetDouble() : 0.8;
        string intent =
            el.TryGetProperty("intent", out var i)
                ? i.GetString() ?? "" : "";
        if (eligible && domain.Length > 0)
        {
            if (conf >= threshold)
                return new Dictionary<string, object?>
                {
                    ["ok"] = true, ["route"] = "FAST_DECISION",
                    ["domain"] = domain,
                    ["calibrated_confidence"] = conf,
                    ["decode_tokens"] = 0,   // §7: never decodes
                };
            return new Dictionary<string, object?>
            {
                ["ok"] = true, ["route"] = "GENERATIVE",
                ["domain"] = domain, ["abstain"] = true,
                ["reason"] = "SYSTEM1_CONFIDENCE_LOW",
                ["fallback"] = "SYSTEM_2",
            };
        }
        // Slow-path routing — capability axis, never MoE routing (§11).
        string route = intent switch
        {
            "agent_task" or "multi_step" => "TOOL_AGENT",
            "document_qa" or "research" => "RAG",
            "coding" or "fim" => "CODING",
            "complex_reasoning" => "THINKING",
            _ => "GENERATIVE",
        };
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["route"] = route,
            ["domain"] = domain.Length > 0 ? domain : null,
            ["abstain"] = false,
        };
    }

    // ------------------------------------------------ decision trace --

    /// <summary>§42 ledger — every fast decision is auditable.</summary>
    public static Dictionary<string, object?> RecordTrace(
        string toolRoot, JsonElement el)
    {
        foreach (var k in new[]
                 { "decision_type", "selected", "calibrated_confidence",
                   "latency_ms" })
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                    $"trace missing {k}");
        var rec = new Dictionary<string, object?>
        {
            ["format"] = TraceFormat,
            ["recorded_at"] = XcPaths.IsoNow(),
            ["decision_type"] = el.GetProperty("decision_type")
                .GetString(),
            ["domain"] = el.TryGetProperty("domain", out var dm)
                ? dm.GetString() : null,
            ["probabilities"] =
                el.TryGetProperty("probabilities", out var pr)
                    ? ModelLifecycle.Decode(pr) : null,
            ["selected"] = el.GetProperty("selected").GetString(),
            ["calibrated_confidence"] =
                el.GetProperty("calibrated_confidence").GetDouble(),
            ["calibration_profile"] =
                el.TryGetProperty("calibration_profile", out var cp)
                    ? ModelLifecycle.Decode(cp) : null,
            ["fallback"] =
                el.TryGetProperty("fallback", out var fb) &&
                fb.ValueKind == JsonValueKind.True,
            ["latency_ms"] =
                el.GetProperty("latency_ms").GetDouble(),
            ["model_hash"] =
                el.TryGetProperty("model_hash", out var mh)
                    ? mh.GetString() : null,
            ["generation"] =
                el.TryGetProperty("generation", out var ge)
                    ? ge.GetString() : null,
        };
        string path = Path.Combine(toolRoot,
            TraceRel.Replace('/', Path.DirectorySeparatorChar));
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.AppendAllText(path, CanonicalJson.CanonicalDict(rec) + "\n");
        rec["ok"] = true;
        return rec;
    }

    // ------------------------------------------- head artifact (§49) --

    /// <summary>Validate decision-head.bin envelope binding:
    /// model_hash + generation + hidden_size + head_version +
    /// calibration_profile. An incompatible head returns
    /// fallback=SYSTEM_2 — it can never fail the main model.</summary>
    public static Dictionary<string, object?> ValidateHead(
        JsonElement el, string modelHash, string generation,
        long hiddenSize)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                "head envelope must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != HeadFormat)
            throw new ExecutorError("SYSTEM1_SCHEMA_INVALID",
                $"expected format {HeadFormat}");
        bool bound =
            el.TryGetProperty("model_hash", out var mh) &&
            mh.GetString() == modelHash &&
            el.TryGetProperty("generation", out var ge) &&
            ge.GetString() == generation &&
            el.TryGetProperty("hidden_size", out var hs) &&
            hs.ValueKind == JsonValueKind.Number &&
            hs.GetInt64() == hiddenSize;
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = HeadFormat,
            ["bound"] = bound,
            ["fallback"] = bound ? null : "SYSTEM_2",
            ["head_version"] =
                el.TryGetProperty("head_version", out var hv)
                    ? hv.GetString() : null,
            ["calibration_profile"] =
                el.TryGetProperty("calibration_profile", out var cp)
                    ? ModelLifecycle.Decode(cp) : null,
        };
    }
}
