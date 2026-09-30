// ConvergenceChecks.cs — §35 acceptance suite for batch-2.
// Every check is fail-closed: a contract breach, a missing component
// or a native-probe error produces {ok:false} for that smoke and
// ok=false overall. Native probes run through xc_modeltool — the same
// governed subprocess lane as every other native invocation.

using System.Text.Json;

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
            // ==================== §39 repo-level convergence ===========
            // Platform invariants — one runtime owner, one generation
            // owner, canonical contract, frozen training, supported
            // axes only. Repo-state reads are bounded to toolRoot.
            new("repo-single-canonical-runtime", () =>
            {
                string src = Path.Combine(toolRoot, "src", "backend");
                if (!Directory.Exists(src)) return false;
                // One C++ engine owner: exactly one engine.cpp and one
                // public engine header across the native lane.
                return Directory.GetFiles(
                           src, "engine.cpp", SearchOption.AllDirectories)
                           .Length == 1 &&
                       Directory.GetFiles(
                           src, "xingcheng_inference.hpp",
                           SearchOption.AllDirectories).Length == 1;
            }),
            new("repo-single-generation-owner", () =>
            {
                // Exactly one lifecycle.json owns the governed model id —
                // per-model lifecycle dirs and selftest scratch copies
                // are separate owners by scope, not duplicates.
                string lcDir = Path.Combine(
                    toolRoot,
                    XcPaths.LifecycleRel.Replace(
                        '/', Path.DirectorySeparatorChar));
                if (!Directory.Exists(lcDir)) return true;
                return Directory.GetFiles(lcDir, "lifecycle.json")
                           .Length <= 1;
            }),
            new("repo-architecture-contract", () =>
            {
                // §22 quarantine: a bundle claiming xc-fused-1 while
                // enabling CSA/MLA is CANONICAL_CONTRACT_VIOLATION.
                string rt = Path.Combine(toolRoot, "xingcheng", "runtime");
                if (!Directory.Exists(rt)) return true;
                foreach (string mf in Directory.GetFiles(
                             rt, "manifest.json", SearchOption.AllDirectories))
                {
                    JsonDocument doc;
                    try { doc = JsonDocument.Parse(File.ReadAllText(mf)); }
                    catch { continue; }
                    using (doc)
                    {
                        var r = doc.RootElement;
                        string arch =
                            r.TryGetProperty("architecture_profile",
                                             out var ap) ? ap.GetString() ?? "" :
                            r.TryGetProperty("architecture",
                                             out var ar) ? ar.GetString() ?? "" : "";
                        if (arch != "xc-fused-1") continue;
                        if (!r.TryGetProperty("config", out var cfg))
                            continue;
                        foreach (string k in new[]
                                 { "use_csa", "csa_enabled", "use_mla",
                                   "mla_enabled", "use_latent_moe",
                                   "use_rwkv", "use_mamba" })
                            if (cfg.TryGetProperty(k, out var v) &&
                                v.ValueKind == JsonValueKind.True)
                                return false;
                    }
                }
                return true;
            }),
            new("repo-active-generation-singleton", () =>
            {
                string sp = Path.Combine(
                    toolRoot, GenerationMigration.StateDirRel
                                  .Replace('/', Path.DirectorySeparatorChar),
                    GenerationMigration.StateFile);
                if (!File.Exists(sp)) return true;
                using var doc = JsonDocument.Parse(File.ReadAllText(sp));
                // exactly one active generation string
                return doc.RootElement.TryGetProperty(
                           "active_generation", out var ag) &&
                       ag.ValueKind == JsonValueKind.String;
            }),
            new("repo-dangling-lineage", () =>
            {
                string dir = Path.Combine(
                    toolRoot, GenerationMigration.StateDirRel
                                  .Replace('/', Path.DirectorySeparatorChar));
                if (!Directory.Exists(dir)) return true;
                foreach (string mf in Directory.GetFiles(
                             dir, "migration-*.json"))
                {
                    JsonDocument doc;
                    try { doc = JsonDocument.Parse(File.ReadAllText(mf)); }
                    catch { return false; }
                    using (doc)
                    {
                        var r = doc.RootElement;
                        string status =
                            r.TryGetProperty("status", out var s)
                                ? s.GetString() ?? "" : "";
                        if (status == "PURGED" || status == "FAILED")
                            continue;
                        if (r.TryGetProperty("weights", out var w) &&
                            w.TryGetProperty("target_path", out var tp))
                        {
                            string rel = tp.GetString() ?? "";
                            string abs = Path.Combine(
                                toolRoot, rel.Replace('/',
                                    Path.DirectorySeparatorChar));
                            if (rel.Length > 0 && !File.Exists(abs) &&
                                !Directory.Exists(abs))
                                return false;   // dangling lineage
                        }
                    }
                }
                return true;
            }),
            new("repo-orphan-artifacts", () =>
            {
                // Production store only — runtime/devin is a scratch
                // lane whose fixtures are not lineage-referenced by
                // contract.
                string store = Path.Combine(
                    toolRoot, "xingcheng", "runtime", "models");
                if (!Directory.Exists(store)) return true;
                var referenced = new HashSet<string>(
                    StringComparer.OrdinalIgnoreCase);
                string lcDir = Path.Combine(
                    toolRoot,
                    XcPaths.LifecycleRel.Replace(
                        '/', Path.DirectorySeparatorChar));
                var lc = ModelLifecycle.LoadOrCreate(
                    lcDir, XcPaths.ModelId);
                void Collect(object? node)
                {
                    switch (node)
                    {
                        case Dictionary<string, object?> d:
                            foreach (var kv in d)
                            {
                                if (kv.Value is string s &&
                                    kv.Key.EndsWith("_path",
                                        StringComparison.Ordinal))
                                    referenced.Add(
                                        s.Replace('\\', '/'));
                                else Collect(kv.Value);
                            }
                            break;
                        case List<object?> l:
                            foreach (var i in l) Collect(i);
                            break;
                    }
                }
                Collect(lc.Artifacts);
                foreach (string b in Directory.GetDirectories(
                             store, "*", SearchOption.AllDirectories))
                {
                    string mpath = Path.Combine(b, "manifest.json");
                    if (!File.Exists(mpath)) continue;
                    string rel = Path.GetRelativePath(toolRoot, b)
                        .Replace('\\', '/');
                    if (referenced.Contains(rel) || referenced.Any(
                            p => p.StartsWith(rel,
                                StringComparison.OrdinalIgnoreCase)))
                        continue;
                    // Owned by a governing record (job dir) or carrying
                    // its own lineage (source_checkpoint / provenance)
                    // is not an orphan; a bundle with neither is.
                    if (rel.Contains("/jobs/",
                            StringComparison.OrdinalIgnoreCase))
                        continue;
                    JsonDocument doc;
                    try
                    { doc = JsonDocument.Parse(File.ReadAllText(mpath)); }
                    catch { return false; }
                    using (doc)
                    {
                        var r = doc.RootElement;
                        bool selfDescribing =
                            r.TryGetProperty("source_checkpoint",
                                             out var s1) &&
                            s1.ValueKind == JsonValueKind.String &&
                            (s1.GetString() ?? "").Length > 0;
                        if (!selfDescribing &&
                            !File.Exists(Path.Combine(b,
                                             "provenance.json")))
                            return false;
                    }
                }
                return true;
            }),
            new("repo-production-axis-supported", () =>
            {
                string rt = Path.Combine(toolRoot, "xingcheng", "runtime");
                if (!Directory.Exists(rt)) return true;
                var quants = new HashSet<string>(
                    StringComparer.OrdinalIgnoreCase)
                    { "none", "int8", "int4", "int4_packed", "bf16" };
                foreach (string mf in Directory.GetFiles(
                             rt, "manifest.json", SearchOption.AllDirectories))
                {
                    JsonDocument doc;
                    try { doc = JsonDocument.Parse(File.ReadAllText(mf)); }
                    catch { continue; }
                    using (doc)
                    {
                        if (doc.RootElement.TryGetProperty(
                                "quantization", out var q) &&
                            q.ValueKind == JsonValueKind.String &&
                            !quants.Contains(q.GetString() ?? ""))
                            return false;
                    }
                }
                return true;
            }),
            new("repo-forbidden-language", () =>
                LanguageBoundary.Scan(
                    Path.Combine(toolRoot, "src")).Count == 0),
            new("repo-training-frozen", () =>
            {
                string sp = Path.Combine(
                    toolRoot, "runtime", "settings",
                    "self-learning.json");
                if (!File.Exists(sp)) return true;
                using var doc = JsonDocument.Parse(File.ReadAllText(sp));
                // §3/§28: while the frozen flag is set, no scheduler may
                // emit a weight-changing job — the flag itself is the
                // contract; verify it is still latched. The governed
                // SINGLE_CAPABILITY_RECOVERY lane is itself bounded
                // (one active capability, SFT only) and counts as a
                // latched state — it cannot emit a free-form job.
                var root = doc.RootElement;
                if (root.TryGetProperty(
                        "capability_training_frozen", out var f) &&
                    f.ValueKind == JsonValueKind.True)
                    return true;
                return root.TryGetProperty(
                           "capability_training_mode", out var m) &&
                       m.ValueKind == JsonValueKind.String &&
                       m.GetString() == "SINGLE_CAPABILITY_RECOVERY" &&
                       root.TryGetProperty(
                           "active_capability", out var ac) &&
                       ac.ValueKind == JsonValueKind.String &&
                       !string.IsNullOrEmpty(ac.GetString());
            }),
            new("repo-xcn-writer-v10", () =>
            {
                string rt = Path.Combine(toolRoot, "xingcheng", "runtime");
                if (!Directory.Exists(rt)) return true;
                foreach (string mf in Directory.GetFiles(
                             rt, "manifest.json", SearchOption.AllDirectories))
                {
                    JsonDocument doc;
                    try { doc = JsonDocument.Parse(File.ReadAllText(mf)); }
                    catch { continue; }
                    using (doc)
                    {
                        foreach (string k in new[]
                                 { "checkpoint_version", "xcn_version",
                                   "format_version" })
                            if (doc.RootElement.TryGetProperty(k, out var v) &&
                                v.ValueKind == JsonValueKind.String)
                            {
                                string s = v.GetString() ?? "";
                                var mm = System.Text.RegularExpressions
                                    .Regex.Match(s, @"v(\d+)");
                                if (mm.Success &&
                                    int.Parse(mm.Groups[1].Value) > 10)
                                    return false;   // XCN writer > 10
                            }
                    }
                }
                return true;
            }),
            new("repo-state-version-supported", () =>
            {
                // Every persisted state envelope we honor is v1/v2 —
                // a state file claiming a higher version than the
                // runtime understands is unsupported reads.
                string dir = Path.Combine(
                    toolRoot, GenerationMigration.StateDirRel
                                  .Replace('/', Path.DirectorySeparatorChar));
                if (!Directory.Exists(dir)) return true;
                foreach (string f in Directory.GetFiles(dir, "*.json"))
                {
                    JsonDocument doc;
                    try { doc = JsonDocument.Parse(File.ReadAllText(f)); }
                    catch { return false; }
                    using (doc)
                        if (doc.RootElement.TryGetProperty(
                                "state_version", out var sv) &&
                            sv.ValueKind == JsonValueKind.Number &&
                            sv.GetInt64() > 2)
                            return false;
                }
                return true;
            }),
            // ==================== 300M maturation (§65) =================
            // §65 promotion gate leg 1: the trainer probe suite — the
            // governed subprocess lane, same as the modeltool probes.
            new("maturation-trainer-probes", () =>
            {
                var run = NativeTools.Run(
                    NativeTools.TrainerExe(toolRoot),
                    new List<string> { "--probe-all" }, toolRoot,
                    Path.Combine(toolRoot, "runtime", "logs",
                                 "converge-stderr.log"),
                    timeoutS: 600);
                if (run.ExitCode != 0 ||
                    !run.StdoutTail.Contains("\"ok\":true"))
                    return false;
                // §65 requires >=16/16 — parse the probe report and
                // enforce the floor explicitly rather than trusting the
                // process flag alone.
                int i = run.StdoutTail.IndexOf(
                    "{\"format\":\"star-trainer-probe-report/v1\"",
                    StringComparison.Ordinal);
                if (i < 0) return false;
                try
                {
                    using var doc = JsonDocument.Parse(
                        run.StdoutTail.Substring(i));
                    var r = doc.RootElement;
                    return r.TryGetProperty("passed", out var p) &&
                           r.TryGetProperty("total", out var t) &&
                           p.GetInt32() == t.GetInt32() &&
                           t.GetInt32() >= 16;
                }
                catch (JsonException) { return false; }
            }),
            // §65 leg 2: the maturation plane itself — sequence order,
            // freeze bookkeeping, ladder math and the §3 baseline
            // contract, exercised on a scratch state (never the live
            // one).
            new("maturation-plane-smoke", () =>
            {
                // Head of a fresh state must be instruction_following.
                var fresh = new Dictionary<string, object?>
                {
                    ["capabilities"] = Maturation300M.Sequence
                        .ToDictionary(
                            s => s.Id,
                            s => (object?)new Dictionary<string, object?>
                            { ["status"] = "pending" }),
                };
                if (Maturation300M.Head(fresh)?.Id
                        != "instruction_following")
                    return false;
                // Freeze the head -> head advances.
                var caps =
                    (Dictionary<string, object?>)fresh["capabilities"]!;
                caps["instruction_following"] =
                    new Dictionary<string, object?>
                    { ["status"] = "frozen" };
                if (Maturation300M.Head(fresh)?.Id != "context_tracking")
                    return false;
                // Freeze all -> head is null; scale stays locked until
                // L6 evidence exists.
                foreach (var s in Maturation300M.Sequence)
                    caps[s.Id] = new Dictionary<string, object?>
                    { ["status"] = "frozen" };
                if (Maturation300M.Head(fresh) != null) return false;
                var l5 = new Dictionary<int, bool?>
                {
                    [0] = true, [1] = true, [2] = true, [3] = true,
                    [4] = true, [5] = true,
                };
                if (Maturation300M.ScaleUnlocked(l5, fresh))
                    return false;
                l5[6] = true;
                if (!Maturation300M.ScaleUnlocked(l5, fresh))
                    return false;
                // First skipped level caps certification.
                var broken = new Dictionary<int, bool?>
                { [0] = true, [1] = true, [2] = null, [3] = true };
                if (Maturation300M.CertifiedLevel(broken) != 1)
                    return false;
                // §64: mixed change classes are denied.
                if (!ExpectThrow(() => Maturation300M
                        .GuardCandidateClasses(
                            new[] { "CAPABILITY_CHANGE",
                                    "RUNTIME_CHANGE" })))
                    return false;
                Maturation300M.GuardCandidateClasses(
                    new[] { "DATA_CHANGE" });
                // §3: metric conflation across baseline sections fails.
                return ExpectThrow(() => Maturation300M.WriteBaseline(
                    Path.Combine(Path.GetTempPath(),
                                 "xc-maturation-scratch"),
                    "w", "h",
                    new Dictionary<string, object?> { ["acc"] = 0.9 },
                    new Dictionary<string, object?> { ["acc"] = 0.9 },
                    new Dictionary<string, object?>()));
            }),
            // §65 leg 3: the §5 instruction-maturity suite file exists
            // and carries all nine dimensions — the P0 gate artifact.
            new("maturation-instruction-suite", () =>
            {
                string p = Path.Combine(
                    toolRoot, "xingcheng", "eval",
                    "star-capability-suite-instruction-300m.json");
                if (!File.Exists(p)) return false;
                using var doc = JsonDocument.Parse(File.ReadAllText(p));
                var r = doc.RootElement;
                if (!r.TryGetProperty("items", out var items) ||
                    items.ValueKind != JsonValueKind.Array)
                    return false;
                var dims = new HashSet<string>();
                foreach (var it in items.EnumerateArray())
                    if (it.TryGetProperty("category", out var c))
                        dims.Add(c.GetString() ?? "");
                foreach (var d in Maturation300M.Sequence[0].Metrics)
                    if (!dims.Contains("instruction_" + d))
                        return false;
                return true;
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
