// AgentLearning.cs — MiMo-V2.6-class agent learning contracts,
// natively reimplemented (§18-§33). Schema/registry/evaluator only —
// bounded GRPO-style RL stays frozen behind the capability-training
// freeze.
//
//   star-agent-trajectory/v1    ONE trajectory schema for every agent
//                               kind — differences live in
//                               task_type/environment_type/metadata
//                               (§21/§22).
//   HarnessRegistry             multiple harnesses per task family;
//                               seen/unseen split detection marks
//                               HARNESS_OVERFIT (§24/§25).
//   GroupwiseTrajectoryEvaluator N candidates per task — correctness
//                               first, then quality/cost/path (§27-29).
//   RewardIntegrityGate         grader -> verifier -> consistency ->
//                               adversarial -> acceptance; anomalies
//                               mark REWARD_SUSPECT (§30/§31).
//   star-self-correction/v1     failure -> verifier explanation ->
//                               corrected action (§32).

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class AgentLearning
{
    public const string TrajectoryFormat = "star-agent-trajectory/v1";
    public const string CorrectionFormat = "star-self-correction/v1";
    public const string HarnessRel =
        "xingcheng/runtime/state/harness-registry.json";

    public static readonly string[] TaskTypes =
        { "coding", "rag", "tool", "computer", "document", "research",
          "general_agent" };

    public static readonly string[] RewardComponents =
        { "task_success", "correctness", "verification",
          "path_efficiency", "token_efficiency", "tool_efficiency",
          "latency", "resource_cost" };

    private static readonly string[] TrajectoryRequired =
        { "task_id", "step", "state", "decision", "reward",
          "termination" };

    // ------------------------------------------------ trajectory ----

    /// <summary>Validate one step of star-agent-trajectory/v1 — the
    /// ONLY schema every agent kind may emit (§21/§22).</summary>
    public static Dictionary<string, object?> ValidateTrajectory(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("TRAJECTORY_INVALID",
                "trajectory step must be object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != TrajectoryFormat)
            throw new ExecutorError("TRAJECTORY_INVALID",
                $"expected format {TrajectoryFormat}");
        foreach (var k in TrajectoryRequired)
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError("TRAJECTORY_INVALID",
                    $"missing field {k}");
        string tt = el.TryGetProperty("task_type", out var t)
            ? t.GetString() ?? "" : "";
        if (tt.Length > 0 && !TaskTypes.Contains(tt))
            throw new ExecutorError("TRAJECTORY_INVALID",
                $"bad task_type {tt}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = TrajectoryFormat,
            ["task_id"] = el.GetProperty("task_id").GetString(),
            ["task_type"] = tt.Length > 0 ? tt : "general_agent",
            ["step"] = el.GetProperty("step").GetInt64(),
        };
    }

    // -------------------------------------------------- harnesses ----

    /// <summary>Register a harness variant for a task family — the same
    /// task under a different prompt/schema/tool-order/state
    /// representation (§24).</summary>
    public static Dictionary<string, object?> HarnessRegister(
        string toolRoot, JsonElement el)
    {
        string id = el.TryGetProperty("harness_id", out var h)
            ? h.GetString() ?? "" : "";
        string family = el.TryGetProperty("task_family", out var tf)
            ? tf.GetString() ?? "" : "";
        if (id.Length == 0 || family.Length == 0)
            throw new ExecutorError("TRAJECTORY_INVALID",
                "harness needs harness_id + task_family");
        var reg = LoadRegistry(toolRoot);
        var harnesses = (Dictionary<string, object?>)reg["harnesses"]!;
        harnesses[id] = new Dictionary<string, object?>
        {
            ["task_family"] = family,
            ["variant"] = el.TryGetProperty("variant", out var v)
                ? ModelLifecycle.Decode(v) : null,
            ["seen_runs"] = 0, ["seen_pass"] = 0,
            ["unseen_runs"] = 0, ["unseen_pass"] = 0,
            ["registered_at"] = XcPaths.IsoNow(),
        };
        SaveRegistry(toolRoot, reg);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["harness_id"] = id,
            ["task_family"] = family,
        };
    }

    /// <summary>Record one outcome; the registry tracks seen vs unseen
    /// pass rates so overfit is measured, not assumed (§25).</summary>
    public static Dictionary<string, object?> HarnessOutcome(
        string toolRoot, JsonElement el)
    {
        string id = el.TryGetProperty("harness_id", out var h)
            ? h.GetString() ?? "" : "";
        bool seen = el.TryGetProperty("seen", out var s) &&
                    s.ValueKind == JsonValueKind.True;
        bool pass = el.TryGetProperty("pass", out var p) &&
                    p.ValueKind == JsonValueKind.True;
        var reg = LoadRegistry(toolRoot);
        var harnesses = (Dictionary<string, object?>)reg["harnesses"]!;
        if (!harnesses.TryGetValue(id, out var hv) ||
            hv is not Dictionary<string, object?> hd)
            throw new ExecutorError("TRAJECTORY_INVALID",
                $"unknown harness {id}");
        if (seen)
        {
            hd["seen_runs"] = Convert.ToInt32(hd["seen_runs"]) + 1;
            if (pass) hd["seen_pass"] = Convert.ToInt32(hd["seen_pass"]) + 1;
        }
        else
        {
            hd["unseen_runs"] = Convert.ToInt32(hd["unseen_runs"]) + 1;
            if (pass)
                hd["unseen_pass"] = Convert.ToInt32(hd["unseen_pass"]) + 1;
        }
        SaveRegistry(toolRoot, reg);
        int sr = Convert.ToInt32(hd["seen_runs"]);
        int ur = Convert.ToInt32(hd["unseen_runs"]);
        double seenRate = sr > 0
            ? (double)Convert.ToInt32(hd["seen_pass"]) / sr : 0;
        double unseenRate = ur > 0
            ? (double)Convert.ToInt32(hd["unseen_pass"]) / ur : 0;
        bool overfit = ur >= 4 && seenRate - unseenRate > 0.4;
        if (overfit)
            throw new ExecutorError("HARNESS_OVERFIT",
                $"{id}: seen {seenRate:F2} vs unseen {unseenRate:F2}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["harness_id"] = id,
            ["seen_pass_rate"] = seenRate,
            ["unseen_pass_rate"] = unseenRate,
        };
    }

    private static Dictionary<string, object?> LoadRegistry(
        string toolRoot)
    {
        string p = Path.Combine(toolRoot,
            HarnessRel.Replace('/', Path.DirectorySeparatorChar));
        if (File.Exists(p))
            try
            {
                return (Dictionary<string, object?>)ModelLifecycle.Decode(
                    JsonDocument.Parse(File.ReadAllText(p))
                        .RootElement)!;
            }
            catch { /* fall through */ }
        return new Dictionary<string, object?>
        {
            ["format"] = "star-harness-registry/v1",
            ["harnesses"] = new Dictionary<string, object?>(),
        };
    }

    private static void SaveRegistry(
        string toolRoot, Dictionary<string, object?> reg)
    {
        string p = Path.Combine(toolRoot,
            HarnessRel.Replace('/', Path.DirectorySeparatorChar));
        Directory.CreateDirectory(Path.GetDirectoryName(p)!);
        ModelLifecycle.AtomicWrite(
            p, CanonicalJson.PrettyDict(reg) + "\n");
    }

    // ------------------------------------------------ groupwise eval --

    /// <summary>§27-29: N candidates for one task — incorrect
    /// trajectories are excluded before ranking on quality, cost and
    /// path length. Reward is a component vector, never "shortest
    /// wins" (§28).</summary>
    public static Dictionary<string, object?> GroupwiseEval(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object ||
            !el.TryGetProperty("candidates", out var cs) ||
            cs.ValueKind != JsonValueKind.Array ||
            cs.GetArrayLength() < 2)
            throw new ExecutorError("TRAJECTORY_INVALID",
                "groupwise eval needs >=2 candidates");
        var ranked = new List<(int idx, double score,
            Dictionary<string, object?> rec)>();
        var eliminated = new List<object?>();
        int i = 0;
        foreach (var c in cs.EnumerateArray())
        {
            string tid = c.TryGetProperty("trajectory_id", out var t)
                ? t.GetString() ?? $"c{i}" : $"c{i}";
            bool correct =
                c.TryGetProperty("correct", out var ok) &&
                ok.ValueKind == JsonValueKind.True;
            if (!correct)
            {
                eliminated.Add(tid); ++i; continue;
            }
            double tokens =
                c.TryGetProperty("token_cost", out var tc)
                    ? tc.GetDouble() : 0;
            double latency =
                c.TryGetProperty("latency_ms", out var lm)
                    ? lm.GetDouble() : 0;
            double steps =
                c.TryGetProperty("path_length", out var pl)
                    ? pl.GetDouble() : 0;
            double retries =
                c.TryGetProperty("retries", out var rt)
                    ? rt.GetDouble() : 0;
            // correctness first — only correct candidates rank; then
            // efficiency: fewer tokens/steps/retries score higher.
            double score = 1.0 / (1.0 + tokens / 1000.0 +
                                  steps / 20.0 + retries +
                                  latency / 60000.0);
            ranked.Add((i, score, new Dictionary<string, object?>
            {
                ["trajectory_id"] = tid,
                ["reward_components"] = new Dictionary<string, object?>
                {
                    ["task_success"] = 1.0,
                    ["correctness"] = 1.0,
                    ["token_efficiency"] = 1.0 / (1.0 + tokens / 1000.0),
                    ["path_efficiency"] = 1.0 / (1.0 + steps / 20.0),
                    ["latency"] = 1.0 / (1.0 + latency / 60000.0),
                    ["resource_cost"] = 1.0 / (1.0 + retries),
                },
                ["rank_score"] = score,
            }));
            ++i;
        }
        ranked.Sort((a, b) => b.score.CompareTo(a.score));
        var order = ranked.Select((r, rank) =>
        {
            r.rec["rank"] = rank;
            return (object?)r.rec;
        }).ToList();
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = "star-groupwise-eval/v1",
            ["ranked"] = order,
            ["eliminated_incorrect"] = eliminated,
            ["best"] = order.Count > 0 ? order[0] : null,
        };
    }

    // -------------------------------------------- reward integrity ---

    /// <summary>§30/§31: a single grader can never set the reward.
    /// Input: {grader_score, verifier_verdict(bool|score),
    /// consistency_ok, adversarial_ok, trajectory:{token_cost,
    /// path_length, tool_calls}, declared_success}. Disagreement or a
    /// flagged anomaly -> REWARD_SUSPECT / REWARD_VERIFIER_MISMATCH.</summary>
    public static Dictionary<string, object?> RewardGate(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object ||
            !el.TryGetProperty("grader_score", out var gs) ||
            gs.ValueKind != JsonValueKind.Number)
            throw new ExecutorError("REWARD_SUSPECT",
                "reward gate needs grader_score");
        double grader = gs.GetDouble();
        bool verifierOk =
            el.TryGetProperty("verifier_verdict", out var vv) &&
            vv.ValueKind == JsonValueKind.True;
        bool consistency =
            !el.TryGetProperty("consistency_ok", out var co) ||
            co.ValueKind == JsonValueKind.True;
        bool adversarial =
            !el.TryGetProperty("adversarial_ok", out var ao) ||
            ao.ValueKind == JsonValueKind.True;
        bool declaredSuccess =
            el.TryGetProperty("declared_success", out var ds) &&
            ds.ValueKind == JsonValueKind.True;

        var anomalies = new List<object?>();
        // grader/verifier disagreement is a hard mismatch.
        if (grader >= 0.9 && !verifierOk)
            throw new ExecutorError("REWARD_VERIFIER_MISMATCH",
                "grader high but verifier rejected");
        if (grader >= 0.95 && !declaredSuccess)
            anomalies.Add("high_reward_low_success");
        if (!consistency) anomalies.Add("consistency_failed");
        if (!adversarial) anomalies.Add("adversarial_flagged");
        if (el.TryGetProperty("trajectory", out var tr) &&
            tr.ValueKind == JsonValueKind.Object)
        {
            double steps = tr.TryGetProperty("path_length", out var pl)
                ? pl.GetDouble() : 0;
            double tools = tr.TryGetProperty("tool_calls", out var tc2)
                ? tc2.GetDouble() : 0;
            if (grader >= 0.9 && steps <= 1)
                anomalies.Add("abnormally_short_trajectory");
            if (tools > 20)
                anomalies.Add("tool_abuse");
        }
        if (anomalies.Count > 0)
            throw new ExecutorError("REWARD_SUSPECT",
                string.Join(",", anomalies));
        double reward = grader;
        if (!verifierOk) reward = Math.Min(reward, 0.5);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = "star-reward-integrity/v1",
            ["reward"] = reward,
            ["grader_score"] = grader,
            ["verifier_verdict"] = verifierOk,
            ["anomalies"] = anomalies,
            ["accepted"] = true,
        };
    }

    // ------------------------------------------- self-correction -----

    /// <summary>§32 star-self-correction/v1: failure -> verifier
    /// explanation -> corrected action. Feeds SFT/DPO before any RL.</summary>
    public static Dictionary<string, object?> ValidateCorrection(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("TRAJECTORY_INVALID",
                "correction record must be object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != CorrectionFormat)
            throw new ExecutorError("TRAJECTORY_INVALID",
                $"expected format {CorrectionFormat}");
        foreach (var k in new[]
                 { "task_id", "failed_trajectory_ref",
                   "verifier_explanation", "corrected_action" })
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError("TRAJECTORY_INVALID",
                    $"missing field {k}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = CorrectionFormat,
            ["task_id"] = el.GetProperty("task_id").GetString(),
            ["ready_for"] = new object?[] { "sft", "dpo" }.ToList(),
        };
    }
}
