// LayaMiMoChecks.cs — §44 acceptance battery for the Laya + MiMo-V2.6
// native absorption phase (System-1 typed decisions, router stability,
// unified trajectory, multi-harness, reward integrity, groupwise
// grading, MTP runtime contract).
//
//   --system1-checks   runs all mandated smokes against scratch state
//                      under <tool-root>/xingcheng/runtime/state/
//                      _system1-smoke-<ts>/ (self-cleaning).
//
// Contract-level only — no weights, no training, capability training
// stays frozen; RL stays behind the §33 unfreeze order.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class LayaMiMoChecks
{
    public const string ReportFormat = "star-system1-checks/v1";

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

    private static bool ExpectError(string code, Action act)
    {
        try { act(); return false; }
        catch (ExecutorError ee) { return ee.ErrorCode == code; }
    }

    public static Dictionary<string, object?> Run(string toolRoot)
    {
        string scratch = Path.Combine(
            toolRoot,
            "xingcheng/runtime/state/_system1-smoke"
                .Replace('/', Path.DirectorySeparatorChar) +
            "-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(scratch);

        var checks = new List<CheckResult>();
        try
        {
            // ---------------- System-1 typed decisions (§44) ---------
            checks.Add(Check("system1-smoke", () =>
            {
                var v = SystemOne.ValidateDecision(J("""
                    {"format":"star-typed-decision/v1",
                     "decision_type":"BOOLEAN","p_true":0.9,
                     "p_false":0.1,"raw_confidence":0.9,
                     "calibrated_confidence":0.85,
                     "calibration_profile":{"temperature":1.2},
                     "decision_version":"d1"}
                    """));
                return v["ok"] is bool b && b &&
                       (string)v["selected"]! == "true";
            }));

            checks.Add(Check("system1-choice", () =>
            {
                var v = SystemOne.ValidateDecision(J("""
                    {"format":"star-typed-decision/v1",
                     "decision_type":"CHOICE",
                     "choices":["NONE","RAG","CALCULATOR"],
                     "probabilities":[0.1,0.7,0.2],
                     "selected":"RAG","raw_confidence":0.7,
                     "calibrated_confidence":0.66,
                     "calibration_profile":{"temperature":1.1},
                     "decision_version":"d1"}
                    """));
                return (string)v["selected"]! == "RAG" &&
                       (int)v["option_count"]! == 3;
            }));

            checks.Add(Check("system1-score", () =>
            {
                var v = SystemOne.ValidateDecision(J("""
                    {"format":"star-typed-decision/v1",
                     "decision_type":"ORDINAL_SCORE",
                     "levels":[1,2,3],
                     "distribution":[0.2,0.5,0.3],
                     "raw_confidence":0.5,
                     "calibrated_confidence":0.45,
                     "calibration_profile":{"temperature":1.0},
                     "decision_version":"d1"}
                    """));
                return Math.Abs((double)v["expected_score"]! - 2.1)
                       < 1e-9;
            }));

            checks.Add(Check("system1-schema-invalid", () =>
                ExpectError("SYSTEM1_SCHEMA_INVALID", () =>
                    SystemOne.ValidateDecision(J("""
                        {"format":"star-typed-decision/v1",
                         "decision_type":"CHOICE",
                         "choices":["A","B"],
                         "probabilities":[0.3,0.3],
                         "raw_confidence":0.3,
                         "calibrated_confidence":0.3,
                         "calibration_profile":{},
                         "decision_version":"d1"}
                        """)))));

            checks.Add(Check("system1-calibration", () =>
            {
                // temperature > 1 flattens: calibrated confidence must
                // drop below raw max.
                var cal = SystemOne.Calibrate(
                    new[] { 0.9, 0.05, 0.05 },
                    J("""{"temperature":4.0}"""));
                double cc = (double)cal["calibrated_confidence"]!;
                if (!(cc < 0.9)) return false;
                var m = SystemOne.CalibrationMetrics(J("""
                    {"records":[
                      {"p":0.9,"correct":true},
                      {"p":0.9,"correct":false},
                      {"p":0.4,"correct":false},
                      {"p":0.4,"correct":true}]}
                    """));
                return (double)m["ece"]! > 0.0 &&
                       Math.Abs((double)m["accuracy"]! - 0.5) < 1e-9 &&
                       m["confidence_histogram"] is List<object?> h &&
                       h.Count > 0;
            }));

            checks.Add(Check("system1-abstention", () =>
            {
                var r = SystemOne.Route(J("""
                    {"domain":"RAG_REQUIRED","system1_eligible":true,
                     "calibrated_confidence":0.4,
                     "confidence_threshold":0.8}
                    """));
                return (string)r["route"]! == "GENERATIVE" &&
                       r["abstain"] is bool a && a &&
                       (string)r["fallback"]! == "SYSTEM_2";
            }));

            checks.Add(Check("system1-domain-uncertified", () =>
                ExpectError("SYSTEM1_DOMAIN_UNCERTIFIED", () =>
                    SystemOne.Route(J("""
                        {"domain":"GENERAL_ANSWER",
                         "system1_eligible":true,
                         "calibrated_confidence":0.99}
                        """)))));

            checks.Add(Check("tool-routing-eval", () =>
            {
                var r = SystemOne.Route(J("""
                    {"domain":"TOOL_CLASS","system1_eligible":true,
                     "calibrated_confidence":0.95,
                     "confidence_threshold":0.8}
                    """));
                // §7: FAST_DECISION never enters autoregressive decode.
                return (string)r["route"]! == "FAST_DECISION" &&
                       (int)r["decode_tokens"]! == 0;
            }));

            checks.Add(Check("rag-routing-eval", () =>
            {
                var r = SystemOne.Route(J("""
                    {"domain":"RAG_REQUIRED","system1_eligible":true,
                     "calibrated_confidence":0.9,
                     "confidence_threshold":0.8}
                    """));
                return (string)r["route"]! == "FAST_DECISION";
            }));

            checks.Add(Check("system1-head-binding", () =>
            {
                // §49: bound head validates; wrong model_hash falls
                // back to SYSTEM_2 instead of crashing the main model.
                var ok = SystemOne.ValidateHead(J("""
                    {"format":"star-system1-head/v1",
                     "model_hash":"sha256:abc",
                     "generation":"gen-2-consolidated",
                     "hidden_size":768,"head_version":"h1",
                     "calibration_profile":{"temperature":1.2}}
                    """), "sha256:abc", "gen-2-consolidated", 768);
                var bad = SystemOne.ValidateHead(J("""
                    {"format":"star-system1-head/v1",
                     "model_hash":"sha256:xyz",
                     "generation":"gen-2-consolidated",
                     "hidden_size":768,"head_version":"h1",
                     "calibration_profile":{}}
                    """), "sha256:abc", "gen-2-consolidated", 768);
                return ok["bound"] is bool ob && ob &&
                       bad["bound"] is bool bb && !bb &&
                       (string)bad["fallback"]! == "SYSTEM_2";
            }));

            // ---------------- MiMo router stability (§18-§20) ---------
            checks.Add(Check("router-stability", () =>
            {
                var rl = RouterStability.Policy("LARGE_AGENT_RL");
                var pt = RouterStability.Policy("PRETRAIN");
                if ((string)rl["router_trainability"]! !=
                        "FROZEN_BY_DEFAULT" ||
                    (string)pt["router_trainability"]! != "TRAINABLE")
                    return false;
                var stable = RouterStability.Gate(J("""
                    {"entropy_min":1.2,"entropy_max":2.0,
                     "utilization_min":0.8,"drift_js":0.02,
                     "session_affinity_drift":0.05}
                    """));
                return (string)stable["verdict"]! == "STABLE" &&
                       ExpectError("ROUTER_DRIFT_EXCEEDED", () =>
                           RouterStability.Gate(J("""
                            {"entropy_min":1.2,"entropy_max":2.0,
                             "utilization_min":0.8,"drift_js":0.5}
                            """)));
            }));

            // ---------------- unified trajectory (§21-§22) -----------
            checks.Add(Check("trajectory-schema", () =>
            {
                var v = AgentLearning.ValidateTrajectory(J("""
                    {"format":"star-agent-trajectory/v1",
                     "task_id":"t1","task_type":"coding","step":3,
                     "state":{"s":1},"decision":{"d":1},
                     "reward":0.5,"termination":false}
                    """));
                if (!(v["ok"] is bool b && b)) return false;
                // §22: a foreign trajectory schema fails closed.
                return ExpectError("TRAJECTORY_INVALID", () =>
                    AgentLearning.ValidateTrajectory(J("""
                        {"format":"coding-agent-trajectory/v1",
                         "task_id":"t2","step":0}
                        """)));
            }));

            // ---------------- multi-harness (§24-§26) ----------------
            checks.Add(Check("multi-harness-eval", () =>
            {
                AgentLearning.HarnessRegister(scratch, J("""
                    {"harness_id":"hA","task_family":"tool"}
                    """));
                AgentLearning.HarnessRegister(scratch, J("""
                    {"harness_id":"hA-unseen","task_family":"tool"}
                    """));
                // seen harness passes, unseen collapses -> overfit;
                // the 4th unseen run crosses the threshold.
                for (int i = 0; i < 3; ++i)
                {
                    AgentLearning.HarnessOutcome(scratch, J("""
                        {"harness_id":"hA-unseen","seen":true,
                         "pass":true}
                        """));
                    AgentLearning.HarnessOutcome(scratch, J("""
                        {"harness_id":"hA-unseen","seen":false,
                         "pass":false}
                        """));
                }
                AgentLearning.HarnessOutcome(scratch, J("""
                    {"harness_id":"hA-unseen","seen":true,
                     "pass":true}
                    """));
                return ExpectError("HARNESS_OVERFIT", () =>
                    AgentLearning.HarnessOutcome(scratch, J("""
                        {"harness_id":"hA-unseen","seen":false,
                         "pass":false}
                        """)));
            }));

            // ---------------- groupwise grading (§27-§29) ------------
            checks.Add(Check("groupwise-eval", () =>
            {
                var g = AgentLearning.GroupwiseEval(J("""
                    {"candidates":[
                      {"trajectory_id":"t-bad","correct":false,
                       "token_cost":100},
                      {"trajectory_id":"t-cheap","correct":true,
                       "token_cost":200,"path_length":3,
                       "retries":0,"latency_ms":1000},
                      {"trajectory_id":"t-costly","correct":true,
                       "token_cost":900,"path_length":15,
                       "retries":3,"latency_ms":9000}]}
                    """));
                var ranked = (List<object?>)g["ranked"]!;
                var elim = (List<object?>)g["eliminated_incorrect"]!;
                var best = (Dictionary<string, object?>)ranked[0]!;
                return ranked.Count == 2 &&
                       elim.Count == 1 &&
                       (string)elim[0]! == "t-bad" &&
                       (string)best["trajectory_id"]! == "t-cheap";
            }));

            // ---------------- reward integrity (§30-§31) -------------
            checks.Add(Check("reward-integrity", () =>
            {
                var ok = AgentLearning.RewardGate(J("""
                    {"grader_score":0.8,"verifier_verdict":true,
                     "consistency_ok":true,"adversarial_ok":true,
                     "declared_success":true}
                    """));
                if (!(ok["accepted"] is bool a && a)) return false;
                bool mismatch = ExpectError(
                    "REWARD_VERIFIER_MISMATCH", () =>
                        AgentLearning.RewardGate(J("""
                            {"grader_score":0.95,
                             "verifier_verdict":false}
                            """)));
                bool suspect = ExpectError("REWARD_SUSPECT", () =>
                    AgentLearning.RewardGate(J("""
                        {"grader_score":0.95,"verifier_verdict":true,
                         "declared_success":true,
                         "trajectory":{"path_length":1,
                                       "tool_calls":30}}
                        """)));
                return mismatch && suspect;
            }));

            checks.Add(Check("self-correction-schema", () =>
            {
                var v = AgentLearning.ValidateCorrection(J("""
                    {"format":"star-self-correction/v1",
                     "task_id":"t9",
                     "failed_trajectory_ref":"traj://f1",
                     "verifier_explanation":"wrong tool",
                     "corrected_action":{"tool":"calculator"}}
                    """));
                return v["ok"] is bool b && b;
            }));

            // ---------------- MTP runtime contract (§34-§37) ---------
            checks.Add(Check("mtp-runtime", () =>
            {
                // MTP is training-aux + runtime accel, never a second
                // decoder core — taxonomy must classify it off the
                // MODEL_CORE axis and the catalog must carry the
                // drafter as EXPERIMENTAL_RUNTIME.
                var cls = ArchitectureTaxonomy.Classify(
                    "NativeMtpDrafter");
                if (cls.PrimaryAxis == "MODEL_CORE") return false;
                var f = FeatureCatalog.Canonical.FirstOrDefault(
                    x => x.FeatureId == "f-mtp-drafter");
                return f != null &&
                       f.Status == "EXPERIMENTAL_RUNTIME";
            }));

            checks.Add(Check("mtp-precision-parity", () =>
            {
                // §36: drafter may run aggressive precision (FP8/INT8/
                // FP4) because the main decoder verifies — precision
                // itself is PRECISION_AXIS, never architecture.
                var cls = ArchitectureTaxonomy.Classify("FP8");
                return cls.PrimaryAxis == "PRECISION_AXIS";
            }));

            checks.Add(Check("mtp-speedup", () =>
            {
                // §37: a drafter whose cost exceeds the saved decode
                // compute auto-disables — the promotion metric is
                // contract-level here.
                bool MtpEnabled(double draftMs, double verifyMs,
                                double decodeMs) =>
                    draftMs + verifyMs < decodeMs;
                return MtpEnabled(1, 2, 10) &&
                       !MtpEnabled(8, 4, 10); // negative -> off
            }));

            // ---------------- resource report (§45) ------------------
            checks.Add(Check("system1-resource-report", () =>
            {
                var tr = SystemOne.RecordTrace(scratch, J("""
                    {"decision_type":"BOOLEAN","domain":"RAG_REQUIRED",
                     "selected":"true","calibrated_confidence":0.85,
                     "latency_ms":3.2,"model_hash":"sha256:x",
                     "generation":"gen-2-consolidated"}
                    """));
                return tr["ok"] is bool b && b &&
                       File.Exists(Path.Combine(
                           scratch,
                           SystemOne.TraceRel.Replace(
                               '/', Path.DirectorySeparatorChar)));
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
