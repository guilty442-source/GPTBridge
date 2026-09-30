// ConvergenceChecks.cs — §35 acceptance suite for batch-2.
// Every check is fail-closed: a contract breach, a missing component
// or a native-probe error produces {ok:false} for that smoke and
// ok=false overall. Native probes run through xc_modeltool — the same
// governed subprocess lane as every other native invocation.

namespace GPTBridge.XingchengLearning;

internal static class ConvergenceChecks
{
    private sealed record Check(string Name, Func<bool> Run);

    private static bool ExpectThrow(Action f)
    {
        try { f(); return false; }
        catch (ExecutorError) { return true; }
        catch { return false; }
    }

    /// <summary>Run a native probe; ok iff the subprocess exits 0 and
    /// stdout carries "ok":true.</summary>
    private static bool NativeOk(string toolRoot, string mode,
                                 params string[] extra)
    {
        var args = new List<string> { mode };
        args.AddRange(extra);
        var run = NativeTools.Run(
            NativeTools.ModelToolExe(toolRoot), args, toolRoot,
            Path.Combine(toolRoot, "runtime", "logs",
                         "converge-stderr.log"),
            timeoutS: 300);
        return run.ExitCode == 0 && run.StdoutTail.Contains("\"ok\":true");
    }

    public static Dictionary<string, object?> Run(string toolRoot)
    {
        var checks = new List<Check>
        {
            // --- reasoning-runtime-smoke (§2.1/§16/§19/§2.3/§6)
            new("reasoning-runtime-smoke", () =>
            {
                foreach (var m in new[] { "OFF", "LOW", "MEDIUM", "HIGH" })
                    ReasoningRuntime.Resolve(m);
                var auto = ReasoningRuntime.Resolve(
                    "AUTO", new TaskAssessment
                    {
                        ComplexityScore = 0.8, RiskScore = 0.4,
                        ToolNeed = 0.5, EvidenceNeed = 0.8,
                    });
                if (!Equals(
                        ((auto["budgets"] as Dictionary<string, object?>)!
                            .Count), 6))
                    return false;
                if (!ExpectThrow(() =>
                        ReasoningRuntime.Resolve("AUTO")))
                    return false;
                // §2.3: internal channels never reach final_response.
                var fr = ReasoningRuntime.FinalResponse(
                    new List<Dictionary<string, object?>>
                    {
                        new() { ["channel"] = "reasoning",
                                ["content"] = "x" },
                        new() { ["channel"] = "final",
                                ["content"] = "y" },
                    });
                if (((List<object?>)fr["messages"]!).Count != 1)
                    return false;
                // §6 fallback: regression on the fast lane -> BALANCED.
                return ReasoningRuntime.ResolveDeploymentFallback(
                    DeploymentProfile.UltraLowLatency, true)
                    == DeploymentProfile.Balanced;
            }),
            // --- dialogue-envelope-smoke (§2.2)
            new("dialogue-envelope-smoke", () =>
            {
                var msg = new Dictionary<string, object?>
                {
                    ["role"] = "user", ["content_type"] = "text",
                    ["content"] = "hi", ["request_id"] = "r1",
                    ["sequence"] = 1L, ["visibility"] = "PUBLIC",
                    ["provenance"] = new Dictionary<string, object?>
                    {
                        ["source"] = "human",
                        ["generation"] = "gen-2-consolidated",
                        ["produced_by"] = "test",
                    },
                };
                DialogueEnvelope.ValidateMessage(msg);
                if (!ExpectThrow(() => DialogueEnvelope.ValidateMessage(
                        new Dictionary<string, object?>
                        { ["role"] = "user" })))
                    return false;
                if (!ExpectThrow(() => DialogueEnvelope.ValidateMessage(
                        new Dictionary<string, object?>
                        {
                            ["role"] = "supreme", ["content_type"] = "text",
                            ["content"] = "x", ["request_id"] = "r",
                            ["sequence"] = 1L, ["visibility"] = "PUBLIC",
                            ["provenance"] = new Dictionary<string, object?>
                            { ["source"] = "human" },
                        })))
                    return false;
                return true;
            }),
            // --- context-cache-smoke (§5/§25)
            new("context-cache-smoke", () =>
            {
                var cm = new ContextCacheManager();
                var key = new CacheKey
                {
                    ModelHash = "m1", Generation = "gen-2-consolidated",
                    ArchitectureProfile = "xc-fused-1",
                    TokenizerHash = "t1", TextHash = "x1",
                    RuntimeProfile = "BALANCED",
                };
                cm.Put(CacheLevel.L1_SESSION, key,
                       new Dictionary<string, object?>
                       { ["kind"] = "tokenized_input" });
                var v = cm.Get(CacheLevel.L1_SESSION, key,
                               "gen-2-consolidated", out var hit);
                if (v == null || hit == null || hit.Validity != "VALID")
                    return false;
                // §5 last rule: a stale generation never serves.
                var stale = cm.Get(CacheLevel.L1_SESSION, key,
                                   "gen-3", out var staleHit);
                return stale == null &&
                       staleHit?.Validity == "STALE_GEN";
            }),
            // --- long-context-index-smoke (§8/§20)
            new("long-context-index-smoke", () =>
            {
                var rt = new LongContextRuntime();
                rt.Index.Add(0, 512, 0.9, "rk1", "h1", "TEXT", "s1");
                rt.Index.Add(512, 1024, 0.1, "rk2", "h2");
                var sel = rt.Index.Select(
                    ContextSelectionPolicy.SPARSE, 512);
                if (!Equals(sel["context_blocks_selected"], 1))
                    return false;
                // §20: beyond certified context is PROBE_ONLY.
                return LongContextRuntime.ContextStatus(1_000_000, 128_000)
                       == "PROBE_ONLY" &&
                       LongContextRuntime.ProbeLadder
                           .SequenceEqual(new long[]
                           { 2048, 4096, 8192, 16384, 32768, 65536 });
            }),
            // --- sparse-attention-probe (§7.1, native)
            new("sparse-attention-probe",
                () => NativeOk(toolRoot, "sparse-probe",
                               "--tokens", "4096", "--max-blocks", "8")),
            // --- kv-outer-gather-probe (§7.2, native)
            new("kv-outer-gather-probe",
                () => NativeOk(toolRoot, "kv-gather-probe")),
            // --- precision-map-smoke (§3/§11/§26)
            new("precision-map-smoke", () =>
            {
                var p = QuantizationPolicy.Default();
                p.ToDict();
                if (!ExpectThrow(() => p.Set("router", "MXFP4")))
                    return false;
                if (!ExpectThrow(() => p.Set("bogus_component", "BF16")))
                    return false;
                var legacy = QuantizationPolicy.FromLegacyScalar("BF16");
                // §11.1: router + recurrent_state keep their floors.
                if (legacy["router"] != "FP32" ||
                    legacy["recurrent_state"] != "FP64")
                    return false;
                // §26: no FP64->FP4 leap.
                return !PrecisionRoadmap.CanPromote(0, 3) &&
                       PrecisionRoadmap.CanPromote(2, 3);
            }),
            // --- recurrent-state-drift (§12, native)
            new("recurrent-state-drift",
                () => NativeOk(toolRoot, "state-drift",
                               "--tokens", "8192")),
            // --- multimodal-envelope-smoke (§4.1/§4.2/§21)
            new("multimodal-envelope-smoke", () =>
            {
                var rt = new MultimodalRuntime();
                var audio = rt.AdapterFor("AUDIO");
                if (audio.Capability != "CONTRACT_ONLY") return false;
                var mi = new ModalityInput
                {
                    Modality = "AUDIO", MimeType = "audio/wav",
                    ContentHash = "sha256:00", SizeBytes = 10,
                };
                // Contract parses; any real op is UNAVAILABLE.
                return ExpectThrow(() => audio.Encode(mi));
            }),
            // --- agent-workgraph-smoke (§9/§23/§24)
            new("agent-workgraph-smoke", () =>
            {
                var g = new AgentWorkGraph
                { MaxNodes = 4, MaxDepth = 2, MaxParallel = 2 };
                g.Add(new AgentWorkNode { TaskId = "a", Depth = 0 });
                g.Add(new AgentWorkNode
                    { TaskId = "b", Depth = 1,
                      Dependencies = { "a" } });
                var ready = g.Schedule();
                if (ready.Count != 1 || ready[0].TaskId != "a")
                    return false;
                // §9: authority escalation fails closed.
                if (!ExpectThrow(() => ComputerActionContract.Validate(
                        new Dictionary<string, object?>
                        {
                            ["action"] = "PROPOSE_CLICK",
                            ["authority"] = "system_execution",
                        })))
                    return false;
                // §24: conflicting high-confidence evidence is never
                // silently merged.
                var res = ResultIntegrator.Integrate(
                    new List<Dictionary<string, object?>>
                    {
                        new()
                        {
                            ["confidence"] = 0.9,
                            ["evidence"] = new List<object?>
                            {
                                new Dictionary<string, object?>
                                { ["key"] = "k", ["value"] = "v1" },
                            },
                        },
                        new()
                        {
                            ["confidence"] = 0.85,
                            ["evidence"] = new List<object?>
                            {
                                new Dictionary<string, object?>
                                { ["key"] = "k", ["value"] = "v2" },
                            },
                        },
                    });
                return res["status"]!.ToString()
                       == "CONFLICT_REQUIRES_RESOLUTION";
            }),
            // --- task-resume-smoke (resume contract)
            new("task-resume-smoke", () =>
            {
                var s = new TaskResumeState
                {
                    TaskId = "t1", GenerationId = "gen-2-consolidated",
                    BundleId = "b1", RequestHash = "rh",
                    StateHash = "sh", NextStep = "n",
                    State = "waiting_tool",
                    PendingToolCall = new Dictionary<string, object?>
                        { ["id"] = "tc1" },
                    UpdatedAtUnix =
                        DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
                };
                s.ValidateResume("gen-2-consolidated", "b1",
                                 "sh", "rh", "ph", "ph");
                // Generation mismatch fails closed.
                if (!ExpectThrow(() => s.ValidateResume(
                        "gen-3", "b1", "sh", "rh", "ph", "ph")))
                    return false;
                // Stale pending tool call fails closed.
                s.UpdatedAtUnix = 0;
                return ExpectThrow(() => s.ValidateResume(
                    "gen-2-consolidated", "b1", "sh", "rh", "ph", "ph",
                    maxAgeS: 10));
            }),
            // --- quant-cert-smoke (§28)
            new("quant-cert-smoke", () =>
            {
                var pass = QuantCert.Checks.ToDictionary(
                    c => c, _ => new Dictionary<string, object?>
                    { ["passed"] = true,
                      ["critical_regression"] = false });
                if (QuantCert.Evaluate(pass)["status"]!.ToString()
                        != "CERTIFIED")
                    return false;
                var reg = QuantCert.Checks.ToDictionary(
                    c => c, _ => new Dictionary<string, object?>
                    { ["passed"] = true,
                      ["critical_regression"] = false });
                reg["top1_agreement"]["critical_regression"] = true;
                if (QuantCert.Evaluate(reg)["status"]!.ToString()
                        != "BLOCKED_CRITICAL")
                    return false;
                // Missing check = INCOMPLETE, never a silent pass.
                var partial = pass.Where(kv => kv.Key != "vision_parity")
                    .ToDictionary(kv => kv.Key, kv => kv.Value);
                return QuantCert.Evaluate(partial)["status"]!.ToString()
                       == "INCOMPLETE";
            }),
            // --- bundle-manifest-v2-smoke (§33)
            new("bundle-manifest-v2-smoke", () =>
            {
                var m = BundleManifestV2.Build(
                    QuantizationPolicy.Default(), 65536,
                    new Dictionary<string, object?>
                    { ["min_ram_mb"] = 4096 });
                BundleManifestV2.Validate(m);
                if (!Equals(m["weights_checkpoint"], "XCN1 v10"))
                    return false;
                var broken = new Dictionary<string, object?>
                    { ["format"] = BundleManifestV2.Format };
                return ExpectThrow(() => BundleManifestV2.Validate(broken));
            }),
        };

        var results = new List<object?>();
        int passed = 0;
        foreach (var c in checks)
        {
            bool ok;
            string? err = null;
            try { ok = c.Run(); }
            catch (Exception ex) { ok = false; err = ex.Message; }
            if (ok) ++passed;
            results.Add(new Dictionary<string, object?>
            {
                ["smoke"] = c.Name, ["ok"] = ok,
                ["error"] = err,
            });
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = passed == checks.Count,
            ["format"] = "star-convergence-checks/v2",
            ["passed"] = passed,
            ["total"] = checks.Count,
            ["checks"] = results,
        };
    }
}
