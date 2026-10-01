// Program.cs ??governed entry points for the xingcheng learning lane.
//
// CLI surface preserves the retired Python module's contract:
//   --status              policy + state + training-window snapshot
//   --run-once [--force]  one governed self-learning cycle
//   --enable / --disable  policy kill switch (fail-closed when off)
//   --retention           dry-run sweep (default) | --apply | --status
//   --run-jobs [N]        drain queued governed training jobs
//   --job <id>            run one specific job
//   --verify-audit        repository audit-chain verification
//   --db-status           repository/schema status
//   --migrate             ensure the training repository schema
//
// All output is JSON on stdout (same contract as the Python lane); exit
// code is 0 unless the top-level result carries ok=false.

using System.Globalization;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class Program
{
    private static int Main(string[] args)
    {
        var opts = new Dictionary<string, string>(StringComparer.Ordinal);
        var flags = new HashSet<string>(StringComparer.Ordinal);
        for (int i = 0; i < args.Length; i++)
        {
            string arg = args[i];
            if (!arg.StartsWith("--", StringComparison.Ordinal))
                return Usage();
            string key = arg[2..];
            if (i + 1 < args.Length && !args[i + 1].StartsWith("--"))
                opts[key] = args[++i];
            else
                flags.Add(key);
        }

        string toolRoot = opts.TryGetValue("tool-root", out string? tr) &&
                          tr.Length > 0
            ? Path.GetFullPath(tr)
            : InferToolRoot();

        try
        {
            if (flags.Contains("status") && !flags.Contains("retention"))
                return Emit(Status(toolRoot));
            if (flags.Contains("run-once"))
                return Emit(SelfLearning.RunCycle(
                    toolRoot, force: flags.Contains("force")));
            if (flags.Contains("enable") || flags.Contains("disable"))
                return Emit(SetEnabled(toolRoot, flags.Contains("enable")));
            if (flags.Contains("retention"))
                return Emit(flags.Contains("status")
                    ? RetentionStatus(toolRoot)
                    : Retention.ApplyRetention(
                        toolRoot, dryRun: !flags.Contains("apply")));
            if (flags.Contains("run-jobs"))
                return Emit(RunJobs(toolRoot,
                    opts.TryGetValue("run-jobs", out string? n) &&
                    int.TryParse(n, out int limit) ? limit : 16));
            if (opts.TryGetValue("job", out string? jobId))
                return Emit(RunJob(toolRoot, jobId));
            if (flags.Contains("self-test"))
                return Emit(SelfTest(toolRoot));
            if (flags.Contains("converge-check"))
                return Emit(ConvergenceChecks.Run(toolRoot));
            if (flags.Contains("maturation-status"))
                return Emit(MaturationStatus(toolRoot));
            if (flags.Contains("maturation-freeze"))
                return Emit(Maturation300M.Freeze(
                    toolRoot,
                    opts.TryGetValue("capability", out string? mc)
                        ? mc : "",
                    opts.TryGetValue("evidence", out string? me)
                        ? me : ""));
            if (flags.Contains("maturation-reopen"))
                return Emit(Maturation300M.Reopen(
                    toolRoot,
                    opts.TryGetValue("capability", out string? mrc)
                        ? mrc : "",
                    opts.TryGetValue("reason", out string? mrr)
                        ? mrr : ""));
            if (flags.Contains("maturation-unsupported"))
                return Emit(Maturation300M.MarkUnsupported(
                    toolRoot,
                    opts.TryGetValue("capability", out string? muc)
                        ? muc : "",
                    opts.TryGetValue("evidence", out string? mue)
                        ? mue : "",
                    opts.TryGetValue("reason", out string? mur)
                        ? mur : ""));
            if (flags.Contains("maturation-baseline"))
                return Emit(MaturationBaseline(toolRoot, opts));
            // §15/§16: thinking OFF/ON comparison over identical-suite
            // star-capability-eval/v1 reports; cost JSON optional.
            if (flags.Contains("thinking-compare"))
                return Emit(Maturation300M.ThinkingCompare(
                    ReadJsonElement(
                        opts.TryGetValue("baseline-off", out string? tbo)
                            ? tbo : ""),
                    ReadJsonElement(
                        opts.TryGetValue("candidate-off", out string? tco)
                            ? tco : ""),
                    ReadJsonElement(
                        opts.TryGetValue("candidate-on", out string? tcn)
                            ? tcn : ""),
                    opts.TryGetValue("cost", out string? tcj)
                        ? ReadJsonElement(tcj)
                        : default));
            if (flags.Contains("release-gate"))
                return Emit(ConvergenceGate.Run(
                    toolRoot,
                    opts.TryGetValue("bundle", out string? gb)
                        ? gb : null,
                    !flags.Contains("no-builds"),
                    opts.TryGetValue("suite", out string? gs)
                        ? gs : null));
            if (flags.Contains("verify-audit"))
                return Emit(new TransformerTrainingRepository(toolRoot)
                    .VerifyAuditChain());
            if (flags.Contains("db-status"))
                return Emit(new TransformerTrainingRepository(toolRoot)
                    .DatabaseStatus());
            if (flags.Contains("migrate"))
                return Emit(new Dictionary<string, object?>
                {
                    ["ok"] = true,
                    ["migrated"] =
                        new TransformerTrainingRepository(toolRoot).Maintain(),
                });
            if (flags.Contains("teacher-collect"))
                return Emit(TeacherCollect.Collect(
                    toolRoot, dryRun: flags.Contains("dry-run")));
            if (flags.Contains("evaluate"))
                return Emit(Evaluate(
                    toolRoot,
                    opts.TryGetValue("job-id", out string? ej) ? ej : "",
                    opts.TryGetValue("bundle", out string? eb) ? eb : "",
                    opts.TryGetValue("suite", out string? es) ? es : "",
                    opts.TryGetValue("baseline", out string? bl) ? bl : null,
                    flags.Contains("chat")));
            if (flags.Contains("gen-begin"))
                return Emit(GenerationMigration.Begin(
                    toolRoot,
                    opts.TryGetValue("target", out string? gt) ? gt : "",
                    opts.TryGetValue("weights", out string? gw) ? gw : "",
                    opts.TryGetValue("weight-method", out string? wm)
                        ? wm : "",
                    source: opts.TryGetValue("source", out string? gs)
                        ? gs : "",
                    tokenizer: opts.TryGetValue("tokenizer", out string? tk)
                        ? tk : "",
                    schemaFrom: opts.TryGetValue("schema-from",
                        out string? sf) ? sf : "",
                    schemaTo: opts.TryGetValue("schema-to", out string? st)
                        ? st : "",
                    expertLineage: opts.TryGetValue("expert-lineage",
                        out string? el) ? el : "",
                    notes: opts.TryGetValue("notes", out string? nt)
                        ? nt : ""));
            if (flags.Contains("gen-record"))
                return Emit(GenerationMigration.Record(
                    toolRoot,
                    opts.TryGetValue("manifest", out string? rm) ? rm : "",
                    opts.TryGetValue("domain", out string? rd) ? rd : "",
                    opts.TryGetValue("status", out string? rs) ? rs : "",
                    opts.TryGetValue("migrated", out string? mig) &&
                        long.TryParse(mig, out long mv) ? mv : 0,
                    opts.TryGetValue("transformed", out string? tr2) &&
                        long.TryParse(tr2, out long tv) ? tv : 0,
                    opts.TryGetValue("rejected", out string? rej) &&
                        long.TryParse(rej, out long rv) ? rv : 0,
                    opts.TryGetValue("note", out string? rn) ? rn : ""));
            if (flags.Contains("gen-certify"))
                return Emit(GenerationMigration.Certify(
                    toolRoot,
                    opts.TryGetValue("manifest", out string? cm) ? cm : "",
                    suitePath: opts.TryGetValue("suite", out string? cs)
                        ? cs : ""));
            if (flags.Contains("gen-promote"))
                return Emit(GenerationMigration.Promote(
                    toolRoot,
                    opts.TryGetValue("manifest", out string? pm) ? pm : ""));
            if (flags.Contains("gen-purge"))
                return Emit(GenerationMigration.Purge(
                    toolRoot,
                    opts.TryGetValue("manifest", out string? pu) ? pu : "",
                    apply: flags.Contains("apply")));
            if (flags.Contains("gen-status"))
                return Emit(GenerationMigration.Status(
                    toolRoot,
                    opts.TryGetValue("manifest", out string? sm) ? sm : ""));
            if (flags.Contains("trace-record"))
                return Emit(CapabilityTrace.RecordTrace(
                    toolRoot,
                    opts.TryGetValue("trace", out string? tf) &&
                        tf.Length > 0 ? tf
                    : opts.TryGetValue("file", out string? tf2)
                        ? tf2 : ""));
            if (flags.Contains("cap-record"))
                return Emit(CapabilityTrace.RecordResult(
                    toolRoot,
                    opts.TryGetValue("result", out string? rf) &&
                        rf.Length > 0 ? rf
                    : opts.TryGetValue("file", out string? rf2)
                        ? rf2 : ""));
            if (flags.Contains("trace-status"))
                return Emit(CapabilityTrace.Status(toolRoot));
            // ---- long-horizon tasks (§5 checkpoint/compact/resume)
            if (flags.Contains("task-create"))
                return Emit(LongHorizonTasks.Create(
                    toolRoot,
                    opts.TryGetValue("goal", out string? tg) ? tg : "",
                    opts.TryGetValue("constraints", out string? tc)
                        ? tc : ""));
            if (flags.Contains("task-plan"))
                return Emit(LongHorizonTasks.SetPlan(
                    toolRoot,
                    opts.TryGetValue("task", out string? tp1) ? tp1 : "",
                    (opts.TryGetValue("steps", out string? tps)
                        ? tps : "").Split(';',
                        StringSplitOptions.RemoveEmptyEntries)));
            if (flags.Contains("task-step"))
                return Emit(LongHorizonTasks.RecordStep(
                    toolRoot,
                    opts.TryGetValue("task", out string? ts1) ? ts1 : "",
                    opts.TryGetValue("step", out string? ts2) ? ts2 : "",
                    opts.TryGetValue("tool-result", out string? ttr)
                        ? ttr : "",
                    opts.TryGetValue("evidence", out string? tev)
                        ? tev : ""));
            if (flags.Contains("task-checkpoint"))
                return Emit(LongHorizonTasks.Checkpoint(
                    toolRoot,
                    opts.TryGetValue("task", out string? tc1)
                        ? tc1 : ""));
            if (flags.Contains("task-compact"))
                return Emit(LongHorizonTasks.Compact(
                    toolRoot,
                    opts.TryGetValue("task", out string? tc2)
                        ? tc2 : ""));
            if (flags.Contains("task-resume"))
                return Emit(LongHorizonTasks.Resume(
                    toolRoot,
                    opts.TryGetValue("checkpoint", out string? trc)
                        ? trc : ""));
            if (flags.Contains("task-revalidate"))
                return Emit(LongHorizonTasks.Revalidate(
                    toolRoot,
                    opts.TryGetValue("task", out string? trv)
                        ? trv : ""));
            if (flags.Contains("task-transition"))
                return Emit(LongHorizonTasks.Transition(
                    toolRoot,
                    opts.TryGetValue("task", out string? tt1)
                        ? tt1 : "",
                    opts.TryGetValue("to", out string? tt2)
                        ? tt2 : "",
                    opts.TryGetValue("reason", out string? ttr2)
                        ? ttr2 : ""));
            if (flags.Contains("task-status"))
                return Emit(LongHorizonTasks.Status(toolRoot));
            // ---- coding lane
            if (flags.Contains("code-task-validate"))
                return Emit(CodeAgent.ValidateTask(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? ct)
                            ? ct : "",
                        "CODE_TASK_INVALID")));
            if (flags.Contains("fim-validate"))
                return Emit(CodeAgent.ValidateFim(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? ff)
                            ? ff : "",
                        "FIM_CONTRACT_INVALID")));
            if (flags.Contains("harness-validate"))
                return Emit(CodeAgent.ValidateHarness(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? hvf)
                            ? hvf : "",
                        "REPO_TASK_SCOPE_INVALID")));
            // ---- modality + teacher lineage
            if (flags.Contains("modality-validate"))
                return Emit(Modality.ValidateProvenance(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? mf)
                            ? mf : "", "MODALITY_RECORD_INVALID")));
            if (flags.Contains("teacher-validate"))
                return Emit(Modality.ValidateTeacher(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? tlf)
                            ? tlf : "", "TEACHER_LINEAGE_INVALID")));
            // ---- dataset quality
            if (flags.Contains("dataset-quality"))
                return Emit(DataQuality.Evaluate(
                    toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("record", out string? dq)
                            ? dq : "", "DQ_RECORD_INVALID")));
            // ---- evaluation plane + arch gate + provenance
            if (flags.Contains("eval-result"))
                return Emit(EvalCoordinator.ValidateResult(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? erf)
                            ? erf : "", "EVAL_RESULT_INVALID")));
            if (flags.Contains("eval-status"))
                return Emit(EvalCoordinator.SuitesStatus());
            // ---- two-level routing trace
            if (flags.Contains("routing-record"))
                return Emit(RoutingAnalysis.Record(
                    toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? rrf)
                            ? rrf : "", "ROUTING_TRACE_INVALID")));
            if (flags.Contains("routing-aggregate"))
                return Emit(RoutingAnalysis.Aggregate(toolRoot));
            if (flags.Contains("arch-gate"))
                return Emit(ArchitectureGate.Evaluate(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? agf)
                            ? agf : "",
                        "ARCHITECTURE_CHANGE_NOT_JUSTIFIED")));
            if (flags.Contains("provenance-check"))
                return Emit(BundleProvenance.Check(
                    toolRoot,
                    opts.TryGetValue("bundle", out string? pb)
                        ? pb : ""));
            if (flags.Contains("catalog-emit"))
                return Emit(FeatureCatalog.Emit(toolRoot));
            if (flags.Contains("catalog-validate"))
                return Emit(FeatureCatalog.Validate(
                    opts.TryGetValue("file", out string? fv) ? fv : ""));
            // ---- repo-level convergence battery: platform invariants
            // (single runtime owner, canonical contract, frozen
            // training, supported axes). star-convergence-checks/v1.
            if (flags.Contains("converge-check"))
                return Emit(ConvergenceChecks.Run(toolRoot));
            // ---- single-capability recovery lane
            //      (star-single-capability-recovery/v1): armed by policy
            //      capability_training_mode=SINGLE_CAPABILITY_RECOVERY +
            //      active_capability; every stage is fail-closed.
            if (flags.Contains("recovery-dataset-build"))
            {
                if (opts.TryGetValue("capability", out string? rcp) &&
                    rcp.Length > 0)
                    InstructionRecovery.Capability = rcp;
                return Emit(InstructionRecovery.BuildDataset(
                    opts.TryGetValue("out", out string? rdo)
                        ? rdo : "",
                    opts.TryGetValue("count", out string? rc) &&
                        int.TryParse(rc, out int rcv) ? rcv : 2800,
                    opts.TryGetValue("seed", out string? rsd) &&
                        int.TryParse(rsd, out int rsv) ? rsv : 42));
            }
            if (flags.Contains("recovery-eval"))
            {
                if (opts.TryGetValue("capability", out string? rec) &&
                    rec.Length > 0)
                    InstructionRecovery.Capability = rec;
                return Emit(InstructionRecovery.EvalBundle(
                    toolRoot,
                    opts.TryGetValue("bundle", out string? reb)
                        ? reb : "",
                    opts.TryGetValue("suite", out string? res)
                        ? res : "",
                    opts.TryGetValue("out", out string? reo)
                        ? reo : null));
            }
            if (flags.Contains("recovery-run"))
                return Emit(InstructionRecovery.Run(
                    toolRoot,
                    opts.TryGetValue("plan", out string? rpp)
                        ? rpp : ""));
            // ---- XingchengConvergenceGate: the single release gate.
            // Ordered steps; any critical FAIL -> PROMOTION_BLOCKED.
            if (flags.Contains("release-gate"))
                return Emit(ConvergenceGate.Run(toolRoot,
                    opts.TryGetValue("bundle", out string? gb) &&
                    gb.Length > 0 ? gb : null,
                    runBuilds: !flags.Contains("no-builds"),
                    suite: opts.TryGetValue("suite", out string? gs) &&
                    gs.Length > 0 ? gs : null));
            // ---- runtime capability plane (star-runtime-capabilities/v1)
            if (flags.Contains("caps-status"))
                return Emit(RuntimeCapabilities.Status(toolRoot));
            if (flags.Contains("caps-validate"))
                return Emit(RuntimeCapabilities.Validate(
                    opts.TryGetValue("file", out string? cvf)
                        ? cvf : ""));
            // ---- grounded RAG plane (Command-R lessons)
            if (flags.Contains("grounded-v2-validate"))
                return Emit(GroundedRag.ValidateClaimV2(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? g2)
                            ? g2 : "", "GROUNDING_UNSUPPORTED_CLAIM")));
            if (flags.Contains("graph-add-node"))
                return Emit(GroundedRag.GraphAddNode(
                    toolRoot,
                    opts.TryGetValue("graph", out string? gn)
                        ? gn : "default",
                    ToolContracts.ReadJson(
                        opts.TryGetValue("node", out string? gnn)
                            ? gnn : "", "EVIDENCE_GRAPH_INVALID")));
            if (flags.Contains("graph-add-edge"))
                return Emit(GroundedRag.GraphAddEdge(
                    toolRoot,
                    opts.TryGetValue("graph", out string? ge)
                        ? ge : "default",
                    ToolContracts.ReadJson(
                        opts.TryGetValue("edge", out string? gee)
                            ? gee : "", "EVIDENCE_GRAPH_INVALID")));
            if (flags.Contains("graph-query"))
                return Emit(GroundedRag.GraphQuery(
                    toolRoot,
                    opts.TryGetValue("graph", out string? gq)
                        ? gq : "default",
                    opts.TryGetValue("subject", out string? gs)
                        ? gs : ""));
            if (flags.Contains("grounding-gate"))
                return Emit(GroundedRag.Gate(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? gg)
                            ? gg : "", "GROUNDING_UNAVAILABLE")));
            if (flags.Contains("rag-decide"))
                return Emit(GroundedRag.Decide(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? rd)
                            ? rd : "", "RETRIEVAL_DECISION_INVALID")));
            if (flags.Contains("citation-metrics"))
                return Emit(GroundedRag.CitationMetrics(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? cm)
                            ? cm : "", "CITATION_METRICS_INVALID")));
            // ---- inference efficiency plane
            if (flags.Contains("eff-tier-plan"))
                return Emit(EfficiencyRuntime.TierPlan(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? etp)
                            ? etp : "", "RUNTIME_CAPS_INVALID")));
            if (flags.Contains("rag-prefix-manifest"))
                return Emit(EfficiencyRuntime.RagPrefixManifest(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? rpm)
                            ? rpm : "", "PREFIX_STATE_INCOMPATIBLE")));
            if (flags.Contains("evidence-cache"))
                return Emit(EfficiencyRuntime.EvidenceCacheOp(
                    toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? eoc)
                            ? eoc : "", "PREFIX_STATE_INCOMPATIBLE")));
            if (flags.Contains("eff-policy"))
                return Emit(EfficiencyRuntime.ResolvePolicy(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? ep)
                            ? ep : "", "RUNTIME_CAPS_INVALID")));
            // ---- NativeScaleEfficiencyPlane (scale directive)
            if (flags.Contains("scale-profile-validate"))
                return Emit(ScaleHardwareGate.ValidateProfile(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? spv)
                            ? spv : "", "SCALE_PROFILE_INVALID")));
            if (flags.Contains("scale-precision-map"))
                return Emit(ScaleHardwareGate.ValidatePrecisionMap(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? spm)
                            ? spm : "", "PRECISION_MAP_INVALID")));
            if (flags.Contains("scale-hardware-gate"))
                return Emit(ScaleHardwareGate.Evaluate(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? shg)
                            ? shg : "", "SCALE_HARDWARE_INSUFFICIENT")));
            if (flags.Contains("scale-resource-cert"))
                return Emit(ScaleHardwareGate.ResourceCert(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? src)
                            ? src : "", "RESOURCE_CERT_INVALID")));
            if (flags.Contains("scale-promotion-gate"))
                return Emit(ScaleHardwareGate.PromotionGate(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? spg)
                            ? spg : "", "SCALE_PROMOTION_INVALID")));
            // ---- model-efficiency directive contract layer
            //      (Liquid/Solar/OLMo/Arctic/Arctic-Embed absorption)
            if (flags.Contains("hardware-scale-search"))
                return Emit(ScaleSearch.Search(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? hss)
                            ? hss : "", "SCALE_SHAPE_INEFFICIENT")));
            if (flags.Contains("scale-scorecard"))
                return Emit(ScaleSearch.ScoreCandidate(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? ssc)
                            ? ssc : "", "SCALE_SHAPE_INEFFICIENT")));
            if (flags.Contains("depth-plan"))
                return Emit(DepthScale.Plan(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? dp)
                            ? dp : "", "DEPTH_INHERITANCE_INVALID")));
            if (flags.Contains("depth-inheritance-validate"))
                return Emit(DepthScale.ValidateInheritance(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? div)
                            ? div : "", "DEPTH_INHERITANCE_INVALID")));
            if (flags.Contains("depth-scale-probe"))
                return Emit(DepthScale.ProbeVerdict(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? dsp)
                            ? dsp : "", "DEPTH_SCALE_REGRESSION")));
            if (flags.Contains("depth-efficiency"))
                return Emit(DepthScale.Efficiency(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? de)
                            ? de : "", "DEPTH_INHERITANCE_INVALID")));
            if (flags.Contains("curriculum-stage-policy"))
                return Emit(Curriculum.StagePolicy(
                    opts.TryGetValue("stage", out string? cst)
                        ? cst : ""));
            if (flags.Contains("curriculum-stage-check"))
                return Emit(Curriculum.ValidateStage(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? csc)
                            ? csc : "", "TRAINING_STAGE_INVALID")));
            if (flags.Contains("training-mixture"))
                return Emit(Curriculum.Mixture(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? tm)
                            ? tm : "", "TRAINING_STAGE_INVALID")));
            if (flags.Contains("training-repro"))
                return Emit(Curriculum.ReproRecord(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? trp)
                            ? trp : "", "TRAINING_STAGE_INVALID")));
            if (flags.Contains("data-order-probe"))
                return Emit(Curriculum.DataOrderProbe(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? dop)
                            ? dop : "", "TRAINING_STAGE_INVALID")));
            if (flags.Contains("expert-scale-validate"))
                return Emit(ExpertPolicy.ValidateScale(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? esv)
                            ? esv : "", "EXPERT_GRANULARITY_INEFFICIENT")));
            if (flags.Contains("expert-granularity-compare"))
                return Emit(ExpertPolicy.CompareGranularity(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? egc)
                            ? egc : "", "EXPERT_GRANULARITY_INEFFICIENT")));
            if (flags.Contains("expert-specialization"))
                return Emit(ExpertPolicy.Specialization(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? esp)
                            ? esp : "", "EXPERT_SPECIALIZATION_COLLAPSE")));
            if (flags.Contains("expert-residency-plan"))
                return Emit(ExpertPolicy.ResidencyPlan(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? erp)
                            ? erp : "", "EXPERT_GRANULARITY_INEFFICIENT")));
            if (flags.Contains("adaptive-embedding"))
                return Emit(AdaptiveRetrieval.Embedding(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? ae)
                            ? ae : "", "EMBEDDING_COMPRESSION_REGRESSION")));
            if (flags.Contains("vector-tier-policy"))
                return Emit(AdaptiveRetrieval.TierPolicy());
            if (flags.Contains("two-stage-retrieval"))
                return Emit(AdaptiveRetrieval.TwoStage(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? tsr)
                            ? tsr : "", "RETRIEVAL_RECALL_REGRESSION")));
            if (flags.Contains("retrieval-compression-gate"))
                return Emit(AdaptiveRetrieval.Gate(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? rcg)
                            ? rcg : "", "RETRIEVAL_RECALL_REGRESSION")));
            if (flags.Contains("retrieval-efficiency"))
                return Emit(AdaptiveRetrieval.Metrics(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? rem)
                            ? rem : "", "RETRIEVAL_RECALL_REGRESSION")));
            // ---- product scale tiers (1B STANDARD / 20B EXTREME) ----
            if (flags.Contains("scale-tiers"))
                return Emit(ProductScale.Tiers());
            if (flags.Contains("scale-profile-seed"))
                return Emit(ProductScale.SeedProfiles(toolRoot));
            if (flags.Contains("model-identity"))
                return Emit(ProductScale.Identity(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? mi)
                            ? mi : "", "SCALE_PROFILE_INVALID")));
            if (flags.Contains("active-compute-gate"))
                return Emit(ProductScale.ActiveGate(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? acg)
                            ? acg : "", "SCALE_TIER_INVALID")));
            if (flags.Contains("scale-tier-validate"))
                return Emit(ProductScale.ValidateTier(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? stv)
                            ? stv : "", "SCALE_TIER_INVALID")));
            if (flags.Contains("residency-plan"))
                return Emit(ProductScale.ResidencyPlan(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? rp)
                            ? rp : "", "SCALE_TIER_INVALID")));
            if (flags.Contains("trainable-budget"))
                return Emit(ProductScale.TrainableBudget(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? tbg)
                            ? tbg : "", "SCALE_TIER_INVALID")));
            if (flags.Contains("thinking-levels"))
                return Emit(ProductScale.ThinkingContract());
            // ---- capacity & active-parameter formal spec
            //      (capacity directive §0-§57)
            if (flags.Contains("capacity-checks"))
                return Emit(CapacityChecks.Run(toolRoot));
            if (flags.Contains("capacity-ceiling"))
                return Emit(CapacityPlane.CeilingReport(
                    opts.TryGetValue("total-ceiling", out string? tc)
                        ? long.Parse(tc, CultureInfo.InvariantCulture)
                        : CapacityPlane.DefaultTotalCeiling));
            if (flags.Contains("common-floor-gate"))
                return Emit(CapacityPlane.CommonFloorGate(
                    long.Parse(opts.TryGetValue("common", out string? cc)
                                   ? cc : "0",
                               CultureInfo.InvariantCulture),
                    long.Parse(opts.TryGetValue("shared", out string? csh)
                                   ? csh : "0",
                               CultureInfo.InvariantCulture)));
            if (flags.Contains("capacity-validate"))
                return Emit(CapacityPlane.ValidateMetrics(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? cv)
                            ? cv : "", "ACTIVE_PARAMS_MISSING")));
            if (flags.Contains("effective-compute"))
                return Emit(CapacityPlane.EffectiveCompute(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? ec)
                            ? ec : "", "ACTIVE_PARAMS_MISSING")));
            if (flags.Contains("distillation-contract"))
                return Emit(CapacityPlane.DistillationContract());
            if (flags.Contains("reasoning-compression"))
                return Emit(CapacityPlane.ReasoningCompression(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? rcf)
                            ? rcf : "",
                        "THINKING_COMPRESSION_REGRESSION")));
            if (flags.Contains("quantization-validate"))
                return Emit(CapacityPlane.ValidatePrecision(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? qvf)
                            ? qvf : "", "QUANTIZATION_REGRESSION")));
            if (flags.Contains("expert-lifecycle-gate"))
                return Emit(CapacityPlane.ExpertLifecycle(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? elf)
                            ? elf : "", "EXPERT_PRUNING_REGRESSION")));
            if (flags.Contains("promotion-gate"))
                return Emit(CapacityPlane.PromotionGate(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? pgf)
                            ? pgf : "", "PROMOTION_BLOCKED")));
            if (flags.Contains("capacity-kpis"))
                return Emit(CapacityPlane.KpiReport(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? ck)
                            ? ck : "", "ACTIVE_PARAMS_MISSING")));
            // ---- NativeTrainingAccelerationPlane (§0-§74) — the KPI
            //      is TIME_TO_QUALIFIED_MODEL, not step/s.
            if (flags.Contains("training-telemetry-validate"))
                return Emit(TrainingAcceleration.ValidateTelemetry(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? ttv)
                            ? ttv : "", "TELEMETRY_INCOMPLETE")));
            if (flags.Contains("bottleneck-classify"))
                return Emit(TrainingAcceleration.BottleneckClassify(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? bc)
                            ? bc : "", "TELEMETRY_INCOMPLETE")));
            if (flags.Contains("eval-tier-policy"))
                return Emit(TrainingAcceleration.EvalTiers(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? etp)
                            ? etp : "", "EVAL_TIERS_INVALID")));
            if (flags.Contains("training-pilot"))
                return Emit(TrainingAcceleration.PilotLadder(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? tp)
                            ? tp : "", "PILOT_INVALID")));
            if (flags.Contains("training-batch-plan"))
                return Emit(TrainingAcceleration.BatchPlan(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? tbp)
                            ? tbp : "", "TRAINING_STAGE_INVALID")));
            if (flags.Contains("sequence-buckets"))
                return Emit(new Dictionary<string, object?>
                {
                    ["ok"] = true,
                    ["format"] = "star-sequence-buckets/v1",
                    ["buckets"] = TrainingAcceleration.SeqBuckets
                        .Cast<object?>().ToList(),
                    ["rule"] = "same bucket = same tensor shape = " +
                               "CUDA-graphable (§7/§31-§32)",
                });
            if (flags.Contains("training-precision-policy"))
                return Emit(TrainingAcceleration.PrecisionPolicy());
            if (flags.Contains("distill-artifact-validate"))
                return Emit(TrainingAcceleration
                    .ValidateDistillArtifact(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? dav)
                            ? dav : "", "DISTILL_ARTIFACT_INVALID")));
            if (flags.Contains("time-to-quality"))
                return Emit(TrainingAcceleration.TimeToQuality(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? t2q)
                            ? t2q : "", "TRAINING_STAGE_INVALID")));
            if (flags.Contains("speed-gate"))
                return Emit(TrainingAcceleration.SpeedGate(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? sg)
                            ? sg : "", "TRAINING_STAGE_INVALID")));
            // ---- CUDA language policy: CUDA written in our own
            //      C/C++ (embedded NVRTC source), no external compute
            //      libraries.
            if (flags.Contains("cuda-language-policy"))
                return Emit(CudaLanguage.Policy());
            if (flags.Contains("cuda-language-check"))
                return Emit(CudaLanguage.Check(toolRoot));
            // ---- NativeCudaTrainingPlane (§0-§72): device-side
            //      contracts — precision map, stream lanes, arenas,
            //      graph-cache key, fused-AdamW status.
            if (flags.Contains("cuda-training-plane"))
                return Emit(CudaTrainingPlane.Contract());
            if (flags.Contains("training-precision-map"))
                return Emit(CudaTrainingPlane.PrecisionMap(
                    opts.TryGetValue("file", out string? tpm)
                        ? ToolContracts.ReadJson(tpm,
                            "TRAIN_PRECISION_INVALID")
                        : default));
            if (flags.Contains("graph-key-validate"))
                return Emit(CudaTrainingPlane.GraphKey(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? gkv)
                            ? gkv : "", "GRAPH_KEY_INVALID")));
            if (flags.Contains("fused-adamw-status"))
                return Emit(CudaTrainingPlane.FusedAdamWStatus());
            // ---- Autonomous Training Mainline Convergence (§0-§86):
            //      effective policy, mutation lease, capacity proof,
            //      sequence consistency, binary provenance.
            if (flags.Contains("effective-policy"))
                return Emit(MainlineConvergence.EffectivePolicy(toolRoot));
            if (flags.Contains("mutation-lease-status"))
                return Emit(MainlineConvergence.LeaseStatus(toolRoot));
            if (flags.Contains("mutation-lease-acquire"))
                return Emit(MainlineConvergence.Acquire(
                    toolRoot,
                    opts.TryGetValue("generation", out string? lg)
                        ? lg : "",
                    opts.TryGetValue("candidate", out string? lc)
                        ? lc : "",
                    opts.TryGetValue("model-version", out string? lmv)
                        ? lmv : "",
                    opts.TryGetValue("source-ckpt-sha256",
                        out string? lss) ? lss : "",
                    opts.TryGetValue("arch-hash", out string? lah)
                        ? lah : "",
                    opts.TryGetValue("mutation", out string? lm)
                        ? lm : "SFT"));
            if (flags.Contains("mutation-lease-commit"))
                return Emit(MainlineConvergence.Settle(
                    toolRoot,
                    opts.TryGetValue("candidate", out string? sc)
                        ? sc : "", "COMMIT_MUTATION",
                    opts.TryGetValue("evidence", out string? se)
                        ? se : null));
            if (flags.Contains("mutation-lease-abort"))
                return Emit(MainlineConvergence.Settle(
                    toolRoot,
                    opts.TryGetValue("candidate", out string? ac)
                        ? ac : "", "ABORT_MUTATION",
                    opts.TryGetValue("evidence", out string? ae)
                        ? ae : null));
            if (flags.Contains("capacity-proof"))
                return Emit(MainlineConvergence.CapacityProof(
                    opts.TryGetValue("bundle", out string? cpb)
                        ? cpb : toolRoot));
            if (flags.Contains("sequence-check"))
                return Emit(MainlineConvergence.SequenceCheck(toolRoot));
            if (flags.Contains("binary-provenance"))
                return Emit(MainlineConvergence.BinaryProvenance(
                    toolRoot));
            // ---- AutonomousCapabilityRecoveryLoop (§0-§85):
            //      contract emission, mode/stage admission, failure
            //      classification, dataset purity, training trigger,
            //      circuit breaker, typed receipts.
            if (flags.Contains("self-training-contract"))
                return Emit(SelfTrainingLoop.Contract(toolRoot));
            if (flags.Contains("self-training-admit"))
                return Emit(SelfTrainingLoop.Admit(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? sta)
                            ? sta : "", "SELF_TRAINING_DISABLED"),
                    toolRoot));
            if (flags.Contains("failure-classify-check"))
                return Emit(SelfTrainingLoop.ClassifyCheck(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? fcc)
                            ? fcc : "",
                        "CAPABILITY_CLASSIFICATION_UNCERTAIN")));
            if (flags.Contains("dataset-purity-check"))
                return Emit(SelfTrainingLoop.PurityCheck(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? dpc)
                            ? dpc : "", "CAPABILITY_IMPURE_DATASET")));
            if (flags.Contains("self-train-trigger"))
                return Emit(SelfTrainingLoop.TriggerCheck(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? stt)
                            ? stt : "", "SELF_TRAINING_DISABLED")));
            if (flags.Contains("self-training-circuit"))
                return Emit(SelfTrainingLoop.CircuitCheck(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? stc)
                            ? stc : "", "SELF_TRAINING_CIRCUIT_OPEN")));
            if (flags.Contains("self-training-receipt"))
                return Emit(SelfTrainingLoop.ReceiptCheck(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? str2)
                            ? str2 : "", "SELF_TRAINING_DISABLED")));
            // ---- §10/§11 per-capability failure pools (COLLECT_ONLY
            //      substrate): status + optional per-class row dump.
            if (flags.Contains("failure-record"))
            {
                var fr = ToolContracts.ReadJson(
                    opts.TryGetValue("file", out string? frf)
                        ? frf : "", "SELF_TRAINING_DISABLED");
                string fcap = fr.TryGetProperty("capability",
                    out var fcp) ? fcp.GetString() ?? "" : "";
                string fin = fr.TryGetProperty("input",
                    out var finp) ? finp.GetString() ?? "" : "";
                if (fin.Length == 0)
                    throw new ExecutorError(
                        "CAPABILITY_CLASSIFICATION_UNCERTAIN",
                        "failure-record requires input");
                var rec = FailurePool.Record(
                    toolRoot, fin,
                    fr.TryGetProperty("generation", out var fg)
                        ? fg.GetString() ?? "" : "",
                    FailurePool.ClassForCapability(fcap),
                    fr.TryGetProperty("expected", out var fe)
                        ? fe.GetString() ?? "" : "",
                    fr.TryGetProperty("actual", out var fa)
                        ? fa.GetString() ?? "" : "",
                    fr.TryGetProperty("evidence", out var fev)
                        ? fev.GetString() ?? "" : "",
                    fr.TryGetProperty("severity", out var fs)
                        ? fs.GetString() ?? "medium" : "medium",
                    true,
                    fr.TryGetProperty("reason", out var frs)
                        ? frs.GetString() : null,
                    fr.TryGetProperty("model_version", out var fmv)
                        ? fmv.GetString() : null,
                    fr.TryGetProperty("runtime_version", out var frv)
                        ? frv.GetString() : null,
                    fr.TryGetProperty("provenance", out var fpv)
                        ? fpv.GetString() : null);
                return Emit(new Dictionary<string, object?>
                {
                    ["ok"] = rec != null,
                    ["format"] = "star-self-training-failure/v1",
                    ["capability"] = fcap,
                    ["failure_class"] =
                        FailurePool.ClassForCapability(fcap),
                    ["recorded"] = rec != null,
                    ["dedup"] = rec != null &&
                        rec.TryGetValue("dedup", out var dd)
                            ? dd : "new",
                });
            }
            if (flags.Contains("failure-pool-status"))
            {
                var st = FailurePool.Status(toolRoot);
                if (opts.TryGetValue("capability", out string? fps) &&
                    fps.Length > 0)
                    st["pool_rows"] = FailurePool.ReadPool(
                        toolRoot,
                        FailurePool.ClassForCapability(fps));
                return Emit(st);
            }
            // ---- XC-1B Mature Standard (maturity directive §1-§40)
            if (flags.Contains("maturity-checks"))
                return Emit(MaturityChecks.Run(toolRoot));
            if (flags.Contains("maturity-baseline"))
                return Emit(MaturityStandard.SeedBaseline(toolRoot));
            if (flags.Contains("maturity-registry"))
                return Emit(MaturityStandard.BaselineRegistry(toolRoot));
            if (flags.Contains("capability-floor-gate"))
            {
                var fg = ToolContracts.ReadJson(
                    opts.TryGetValue("file", out string? fgf)
                        ? fgf : "",
                    "MATURITY_CAPABILITY_FLOOR_FAILED");
                var scores = new Dictionary<string, double>();
                var floors = new Dictionary<string, double>();
                foreach (var p in fg.GetProperty("scores")
                                      .EnumerateObject())
                    scores[p.Name] = p.Value.GetDouble();
                foreach (var p in fg.GetProperty("floors")
                                      .EnumerateObject())
                    floors[p.Name] = p.Value.GetDouble();
                return Emit(MaturityStandard.FloorGate(
                    scores, floors,
                    fg.TryGetProperty("layer", out var ly)
                        ? ly.GetString() ?? "BASE_MODEL"
                        : "BASE_MODEL"));
            }
            if (flags.Contains("retention-gate"))
            {
                var rt = ToolContracts.ReadJson(
                    opts.TryGetValue("file", out string? rtf)
                        ? rtf : "", "POST_COMPRESSION_REGRESSION");
                Dictionary<string, double> Map(string k)
                {
                    var m = new Dictionary<string, double>();
                    if (rt.TryGetProperty(k, out var o))
                        foreach (var p in o.EnumerateObject())
                            m[p.Name] = p.Value.GetDouble();
                    return m;
                }
                return Emit(MaturityStandard.RetentionGate(
                    rt.TryGetProperty("phase", out var ph)
                        ? ph.GetString() ?? "compress" : "compress",
                    Map("pre"), Map("post"), Map("min_retention")));
            }
            if (flags.Contains("golden-gate"))
                return Emit(MaturityStandard.GoldenGate(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? gg)
                            ? gg : "", "GOLDEN_USABILITY_FAILED")));
            if (flags.Contains("certification-gate"))
            {
                var cg = ToolContracts.ReadJson(
                    opts.TryGetValue("file", out string? cgf)
                        ? cgf : "", "MATURITY_CAPABILITY_FLOOR_FAILED");
                var certs = new Dictionary<string, bool>();
                foreach (var p in cg.EnumerateObject())
                    if (p.Value.ValueKind is JsonValueKind.True or
                            JsonValueKind.False)
                        certs[p.Name] = p.Value.ValueKind ==
                            JsonValueKind.True;
                return Emit(MaturityStandard.Certification(certs));
            }
            if (flags.Contains("maturity-promotion"))
            {
                var mp = ToolContracts.ReadJson(
                    opts.TryGetValue("file", out string? mpf)
                        ? mpf : "", "MATURITY_CAPABILITY_FLOOR_FAILED");
                var conds = new Dictionary<string, bool>();
                foreach (var p in mp.EnumerateObject())
                    if (p.Value.ValueKind is JsonValueKind.True or
                            JsonValueKind.False)
                        conds[p.Name] = p.Value.ValueKind ==
                            JsonValueKind.True;
                return Emit(MaturityStandard.PromotionDecision(conds));
            }
            // ---- persona / style / steerability (Hermes lessons)
            if (flags.Contains("persona-validate"))
                return Emit(PersonaRuntime.ValidatePersona(
                    toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? pv)
                            ? pv : "", "PERSONA_INVALID")));
            if (flags.Contains("persona-get"))
                return Emit(PersonaRuntime.GetPersona(
                    toolRoot,
                    opts.TryGetValue("id", out string? pgid)
                        ? pgid : ""));
            if (flags.Contains("style-profile"))
                return Emit(PersonaRuntime.StyleProfile(
                    opts.TryGetValue("style", out string? spn)
                        ? spn : "neutral"));
            if (flags.Contains("steer"))
                return Emit(PersonaRuntime.Steer(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? stf)
                            ? stf : "", "STEER_INVALID")));
            if (flags.Contains("conflict-resolve"))
                return Emit(PersonaRuntime.ResolveConflict(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? cf)
                            ? cf : "", "INSTRUCTION_CONFLICT_INVALID")));
            if (flags.Contains("injection-guard"))
                return Emit(PersonaRuntime.GuardInjection(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? ig)
                            ? ig : "", "PERSONA_INJECTION_GUARD")));
            // ---- creative runtime
            if (flags.Contains("creative-profile"))
                return Emit(CreativeRuntime.CreativeProfile(
                    opts.TryGetValue("profile", out string? cp)
                        ? cp : "BALANCED"));
            if (flags.Contains("factuality"))
                return Emit(CreativeRuntime.FactualityResolve(
                    opts.TryGetValue("mode", out string? fq)
                        ? fq : "GENERAL"));
            if (flags.Contains("memory-write"))
                return Emit(CreativeRuntime.MemoryWrite(
                    toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("record", out string? mw)
                            ? mw : "", "NARRATIVE_MEMORY_INVALID")));
            if (flags.Contains("memory-read"))
                return Emit(CreativeRuntime.MemoryRead(
                    toolRoot,
                    opts.TryGetValue("namespace", out string? mns)
                        ? mns : "NARRATIVE_MEMORY",
                    opts.TryGetValue("key", out string? mk)
                        ? mk : ""));
            if (flags.Contains("memory-isolation"))
                return Emit(CreativeRuntime.MemoryIsolationCheck(
                    toolRoot));
            if (flags.Contains("roleplay-create"))
                return Emit(CreativeRuntime.SessionCreate(
                    toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? rc)
                            ? rc : "", "ROLEPLAY_SESSION_INVALID")));
            if (flags.Contains("roleplay-event"))
                return Emit(CreativeRuntime.SessionEvent(
                    toolRoot,
                    opts.TryGetValue("session", out string? re)
                        ? re : "",
                    ToolContracts.ReadJson(
                        opts.TryGetValue("event", out string? rev)
                            ? rev : "", "ROLEPLAY_SESSION_INVALID")));
            if (flags.Contains("roleplay-compact"))
                return Emit(CreativeRuntime.SessionCompact(
                    toolRoot,
                    opts.TryGetValue("session", out string? rpc)
                        ? rpc : ""));
            if (flags.Contains("refusal-decide"))
                return Emit(CreativeRuntime.RefusalDecide(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? rfd)
                            ? rfd : "", "REFUSAL_DECISION_INVALID")));
            if (flags.Contains("refusal-eval"))
                return Emit(CreativeRuntime.RefusalEval(
                    toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("record", out string? rfe)
                            ? rfe : "", "REFUSAL_EVAL_INVALID")));
            if (flags.Contains("roleplay-eval"))
                return Emit(CreativeRuntime.RoleplayEval(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? rpe)
                            ? rpe : "", "ROLEPLAY_EVAL_INVALID")));
            if (flags.Contains("route-mode"))
                return Emit(CreativeRuntime.Route(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? rm)
                            ? rm : "", "INTERACTION_ROUTE_INVALID")));
            // ---- training-future metadata
            if (flags.Contains("module-sensitivity"))
                return Emit(ModuleSensitivity.Record(
                    toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("record", out string? ms)
                            ? ms : "", "MODULE_SENSITIVITY_INVALID")));
            if (flags.Contains("model-merge"))
                return Emit(ModuleSensitivity.MergeRequest(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? mm)
                            ? mm : "", "MODEL_MERGE_DISABLED")));
            // ---- tool decision gate + contracts (§16/§17/§18)
            if (flags.Contains("tool-validate"))
            {
                string kind = opts.TryGetValue("kind", out string? tvk)
                    ? tvk : "request";
                var el = ToolContracts.ReadJson(
                    opts.TryGetValue("file", out string? tvf)
                        ? tvf : "",
                    "TOOL_SCHEMA_INVALID");
                return Emit(kind == "result"
                    ? ToolContracts.ValidateResult(el)
                    : ToolContracts.ValidateCall(el));
            }
            if (flags.Contains("tool-gate"))
            {
                var decided = ToolContracts.Decide(
                    toolRoot,
                    opts.TryGetValue("tool", out string? tgt) ? tgt : "",
                    opts.TryGetValue("requirement", out string? tgr)
                        ? tgr : "optional",
                    opts.TryGetValue("reason", out string? tre)
                        ? tre : "");
                if (opts.TryGetValue("outcome-status", out string? tos))
                    decided["outcome"] = ToolContracts.RecordOutcome(
                        toolRoot,
                        (string)decided["decision"]!,
                        schemaValid: !flags.Contains("schema-invalid"),
                        status: tos);
                return Emit(decided);
            }
            if (flags.Contains("tool-metrics"))
                return Emit(ToolContracts.MetricsPayload(toolRoot));
            if (flags.Contains("grounded-validate"))
                return Emit(ToolContracts.ValidateGrounded(
                    toolRoot,
                    opts.TryGetValue("file", out string? gvf)
                        ? gvf : ""));
            if (flags.Contains("structured-validate"))
            {
                string output =
                    opts.TryGetValue("output", out string? svo)
                        ? svo : "";
                var schema = ToolContracts.ReadJson(
                    opts.TryGetValue("schema", out string? svs)
                        ? svs : "",
                    "STRUCTURED_SCHEMA_FAILED");
                if (!File.Exists(output))
                    throw new ExecutorError(
                        "STRUCTURED_PARSE_FAILED",
                        "output file missing");
                return Emit(StructuredOutput.Validate(
                    File.ReadAllText(output), schema,
                    repairOnce: flags.Contains("repair")));
            }
            if (flags.Contains("langcheck"))
                return Emit(LangCheck.Scan(toolRoot));
            // §16/§17 tool contracts
            if (flags.Contains("tool-call-validate"))
                return Emit(ToolContracts.ValidateCall(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? tc)
                            ? tc : "", "TOOL_SCHEMA_INVALID")));
            if (flags.Contains("tool-result-validate"))
                return Emit(ToolContracts.ValidateResult(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? trr)
                            ? trr : "", "TOOL_SCHEMA_INVALID")));
            if (flags.Contains("tool-gate"))
                return Emit(ToolContracts.Gate(
                    toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("call", out string? gc)
                            ? gc : "", "TOOL_SCHEMA_INVALID"),
                    (opts.TryGetValue("tools", out string? ta)
                        ? ta : "").Split(',', StringSplitOptions
                            .RemoveEmptyEntries |
                            StringSplitOptions.TrimEntries),
                    opts.TryGetValue("budget", out string? gb) &&
                        int.TryParse(gb, out int gbv) ? gbv : -1,
                    flags.Contains("confirmed"),
                    flags.Contains("needed")));
            if (flags.Contains("tool-metrics"))
                return Emit(ToolContracts.Metrics(toolRoot));
            if (flags.Contains("grounded-validate"))
                return Emit(ToolContracts.ValidateGrounded(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? gv)
                            ? gv : "", "GROUNDING_UNSUPPORTED_CLAIM")));
            // §18 structured output
            if (flags.Contains("structured-validate"))
            {
                string outFile =
                    opts.TryGetValue("file", out string? of) ? of : "";
                if (!File.Exists(outFile))
                    throw new ExecutorError(
                        "STRUCTURED_PARSE_FAILED", "output file missing");
                var schema = ToolContracts.ReadJson(
                    opts.TryGetValue("schema", out string? sf2)
                        ? sf2 : "", "STRUCTURED_SCHEMA_FAILED");
                return Emit(StructuredOutput.Validate(
                    File.ReadAllText(outFile), schema,
                    !flags.Contains("no-repair")));
            }
            // §5 long-horizon tasks
            if (flags.Contains("task-create"))
                return Emit(LongHorizonTasks.Create(
                    toolRoot,
                    opts.TryGetValue("goal", out string? g2) ? g2 : "",
                    opts.TryGetValue("constraints", out string? c2)
                        ? c2 : ""));
            if (flags.Contains("task-plan"))
                return Emit(LongHorizonTasks.SetPlan(
                    toolRoot,
                    opts.TryGetValue("task", out string? tp) ? tp : "",
                    (opts.TryGetValue("steps", out string? st2)
                        ? st2 : "").Split(',', StringSplitOptions
                            .RemoveEmptyEntries |
                            StringSplitOptions.TrimEntries)));
            if (flags.Contains("task-step"))
                return Emit(LongHorizonTasks.RecordStep(
                    toolRoot,
                    opts.TryGetValue("task", out string? ts) ? ts : "",
                    opts.TryGetValue("step", out string? ss) ? ss : "",
                    opts.TryGetValue("result", out string? sr) ? sr : "",
                    opts.TryGetValue("evidence", out string? se)
                        ? se : ""));
            if (flags.Contains("task-checkpoint"))
                return Emit(LongHorizonTasks.Checkpoint(
                    toolRoot,
                    opts.TryGetValue("task", out string? ck) ? ck : ""));
            if (flags.Contains("task-compact"))
                return Emit(LongHorizonTasks.Compact(
                    toolRoot,
                    opts.TryGetValue("task", out string? cp) ? cp : ""));
            if (flags.Contains("task-resume"))
                return Emit(LongHorizonTasks.Resume(
                    toolRoot,
                    opts.TryGetValue("checkpoint", out string? rc)
                        ? rc : ""));
            if (flags.Contains("task-revalidate"))
                return Emit(LongHorizonTasks.Revalidate(
                    toolRoot,
                    opts.TryGetValue("task", out string? rv) ? rv : ""));
            if (flags.Contains("task-transition"))
                return Emit(LongHorizonTasks.Transition(
                    toolRoot,
                    opts.TryGetValue("task", out string? tt) ? tt : "",
                    opts.TryGetValue("to", out string? to2) ? to2 : "",
                    opts.TryGetValue("reason", out string? re)
                        ? re : ""));
            if (flags.Contains("task-status"))
                return Emit(LongHorizonTasks.Status(toolRoot));
            // §10/§11/§22 coding + FIM contracts
            if (flags.Contains("code-task-validate"))
                return Emit(CodeAgent.ValidateTask(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? cd)
                            ? cd : "", "REPO_TASK_SCOPE_INVALID")));
            if (flags.Contains("fim-validate"))
                return Emit(CodeAgent.ValidateFim(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? fm)
                            ? fm : "", "FIM_CONTRACT_INVALID")));
            if (flags.Contains("harness-validate"))
                return Emit(CodeAgent.ValidateHarness(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? hv)
                            ? hv : "", "REPO_TASK_SCOPE_INVALID")));
            // §12 modality + teacher lineage schemas
            if (flags.Contains("modality-validate"))
                return Emit(Modality.ValidateProvenance(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? mv)
                            ? mv : "", "VISION_FALLBACK_FAILED")));
            if (flags.Contains("teacher-validate"))
                return Emit(Modality.ValidateTeacher(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? tv)
                            ? tv : "", "BUNDLE_PROVENANCE_INVALID")));
            // §15 architecture change gate
            if (flags.Contains("arch-gate"))
                return Emit(ArchitectureGate.Evaluate(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? ag)
                            ? ag : "", "ARCHITECTURE_CHANGE_NOT_JUSTIFIED")));
            // §14/§19 dataset quality
            if (flags.Contains("data-quality"))
                return Emit(DataQuality.Evaluate(
                    toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? dq)
                            ? dq : "", "DATA_QUALITY_INVALID"),
                    opts.TryGetValue("min-quality", out string? mq) &&
                        double.TryParse(mq, out double mqv)
                            ? mqv : 0.0));
            // §29 routing trace
            if (flags.Contains("routing-record"))
                return Emit(RoutingAnalysis.Record(
                    toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? rr)
                            ? rr : "", "MOE_TRACE_INVALID")));
            if (flags.Contains("routing-status"))
                return Emit(RoutingAnalysis.Aggregate(toolRoot));
            // §30 eval plane
            if (flags.Contains("eval-result-validate"))
                return Emit(EvalCoordinator.ValidateResult(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? ev)
                            ? ev : "", "EVAL_RESULT_INVALID")));
            if (flags.Contains("eval-suites"))
                return Emit(EvalCoordinator.SuitesStatus());
            if (flags.Contains("queue-job"))
                return Emit(QueueJob(
                    toolRoot,
                    opts.TryGetValue("rows", out string? rows) ? rows : "",
                    opts.TryGetValue("config", out string? cfg) ? cfg : "",
                    opts.TryGetValue("val-permille", out string? vp) &&
                    int.TryParse(vp, out int vpv) ? vpv : 50,
                    flags.Contains("include-collected")));
            // ── convergence contract (P0-P9) entry points ────────────
            if (flags.Contains("feature-catalog"))
                return Emit(FeatureCatalog.Install(toolRoot));
            if (flags.Contains("capabilities-resolve"))
                return Emit(CapabilitiesResolve(
                    toolRoot,
                    opts.TryGetValue("request", out string? rq)
                        ? rq : "",
                    opts.TryGetValue("bundle", out string? rb)
                        ? rb : ""));
            if (flags.Contains("language-scan"))
                return Emit(LanguageBoundary.Report(
                    opts.TryGetValue("root", out string? lr) &&
                    lr.Length > 0
                        ? Path.GetFullPath(lr) : toolRoot));
            if (flags.Contains("arch-gate"))
                return Emit(ArchGate(
                    opts.TryGetValue("evidence", out string? ev)
                        ? ev : ""));
            if (flags.Contains("tool-call-validate"))
                return Emit(ToolCallV2.ValidateRequest(
                    ParseJsonFile(
                        opts.TryGetValue("call", out string? tc)
                            ? tc : "")));
            if (flags.Contains("tool-result-validate"))
                return Emit(ToolCallV2.ValidateResult(
                    ParseJsonFile(
                        opts.TryGetValue("result", out string? trr)
                            ? trr : "")));
            if (flags.Contains("structured-validate"))
                return Emit(StructuredValidate(
                    opts.TryGetValue("text", out string? sv) ? sv : "",
                    opts.TryGetValue("schema", out string? ss)
                        ? ss : ""));
            if (flags.Contains("fim-validate"))
                return Emit(FimContract.Validate(
                    ParseJsonFile(
                        opts.TryGetValue("fim", out string? fj)
                            ? fj : "")));
            if (flags.Contains("code-task-validate"))
                return Emit(StarCodeAgentRuntime.ValidateTask(
                    ParseJsonFile(
                        opts.TryGetValue("task", out string? ct)
                            ? ct : "")));
            if (flags.Contains("task-create"))
                return Emit(LongHorizonTaskCoordinator.Create(
                    toolRoot,
                    opts.TryGetValue("goal", out string? tg) ? tg : "",
                    opts.TryGetValue("constraints", out string? tcs) &&
                    tcs.Length > 0
                        ? tcs.Split(';').ToList()
                        : new List<string>(),
                    opts.TryGetValue("generation", out string? tgen)
                        ? tgen : "gen-2-consolidated"));
            if (flags.Contains("task-status"))
                return Emit(LongHorizonTaskCoordinator.Load(
                    toolRoot,
                    opts.TryGetValue("task", out string? tid)
                        ? tid : "").ToDict());
            if (flags.Contains("task-transition"))
                return Emit(LongHorizonTaskCoordinator.Transition(
                    toolRoot,
                    opts.TryGetValue("task", out string? tt) ? tt : "",
                    Enum.Parse<LongHorizonState>(
                        opts.TryGetValue("state", out string? ts)
                            ? ts : "", ignoreCase: true),
                    opts.TryGetValue("note", out string? tn)
                        ? tn : ""));
            if (flags.Contains("task-checkpoint"))
                return Emit(LongHorizonTaskCoordinator.Checkpoint(
                    toolRoot,
                    opts.TryGetValue("task", out string? tc2) ? tc2 : "",
                    opts.TryGetValue("summary", out string? sm)
                        ? sm : ""));
            if (flags.Contains("task-resume"))
                return Emit(LongHorizonTaskCoordinator.Resume(
                    toolRoot,
                    opts.TryGetValue("task", out string? trm)
                        ? trm : "",
                    opts.TryGetValue("generation", out string? rg)
                        ? rg : ""));
            if (flags.Contains("task-step"))
                return Emit(LongHorizonTaskCoordinator.RecordStep(
                    toolRoot,
                    opts.TryGetValue("task", out string? st2) ? st2 : "",
                    opts.TryGetValue("step", out string? sp) ? sp : "",
                    flags.Contains("done")));
            if (flags.Contains("provenance-verify"))
                return Emit(BundleProvenance.Verify(
                    opts.TryGetValue("bundle", out string? pb)
                        ? pb : "",
                    ParseJsonFile(
                        opts.TryGetValue("provenance", out string? pp)
                            ? pp : ""),
                    opts.TryGetValue("generation", out string? pg)
                        ? pg : "",
                    opts.TryGetValue("architecture", out string? pa)
                        ? pa : "xc-fused-1"));
            if (flags.Contains("provenance-compute"))
                return Emit(BundleProvenance.Compute(
                    opts.TryGetValue("bundle", out string? cb)
                        ? cb : "",
                    opts.TryGetValue("generation", out string? cg)
                        ? cg : "gen-2-consolidated",
                    opts.TryGetValue("architecture", out string? ca)
                        ? ca : "xc-fused-1",
                    opts.TryGetValue("xcn", out string? cx)
                        ? cx : "XCN1 v10",
                    opts.TryGetValue("build", out string? cbd)
                        ? cbd : "",
                    opts.TryGetValue("runtime", out string? crt)
                        ? crt : "xc-native-cpp23",
                    opts.TryGetValue("lineage", out string? cl)
                        ? cl : ""));
            // §36 community-fine-tune acceptance battery
            if (flags.Contains("community-checks"))
                return Emit(CommunityChecks.Run(toolRoot));
            // single-core-axis contracts (taxonomy / core / drift)
            if (flags.Contains("taxonomy"))
                return Emit(ArchitectureTaxonomy.Emit());
            if (flags.Contains("core-contract"))
                return Emit(ArchitectureTaxonomy.CoreContract(
                    opts.TryGetValue("architecture", out string? acn)
                        ? acn : ArchitectureTaxonomy.CanonicalArchitecture,
                    opts.TryGetValue("layers", out string? lc) &&
                        long.TryParse(lc, out long lcv) ? lcv : 12,
                    opts.TryGetValue("hidden", out string? hd) &&
                        long.TryParse(hd, out long hdv) ? hdv : 768,
                    opts.TryGetValue("heads", out string? qh) &&
                        long.TryParse(qh, out long qhv) ? qhv : 12,
                    opts.TryGetValue("kv-heads", out string? kvh) &&
                        long.TryParse(kvh, out long kvhv) ? kvhv : 4));
            if (flags.Contains("drift-gate"))
                return Emit(ArchitectureTaxonomy.DriftGate(
                    opts.TryGetValue("job-hash", out string? jh)
                        ? jh : "",
                    opts.TryGetValue("checkpoint-hash", out string? ch)
                        ? ch : "",
                    opts.TryGetValue("bundle-hash", out string? bh)
                        ? bh : "",
                    opts.TryGetValue("runtime-hash", out string? rh)
                        ? rh : ""));
            if (flags.Contains("version-dimensions"))
                return Emit(ArchitectureTaxonomy.VersionDimensions(
                    toolRoot));
            // §42 acceptance battery
            if (flags.Contains("axis-checks"))
                return Emit(AxisChecks.Run(toolRoot));
            // ---- Laya + MiMo-V2.6 capability plane (contract level)
            if (flags.Contains("typed-decision-validate"))
                return Emit(SystemOne.ValidateDecision(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? td)
                            ? td : "", "SYSTEM1_SCHEMA_INVALID")));
            if (flags.Contains("decision-calibrate"))
                return Emit(SystemOne.Calibrate(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("probs", out string? pb)
                            ? pb : "", "SYSTEM1_UNCALIBRATED")
                        .EnumerateArray()
                        .Select(p => p.GetDouble()).ToArray(),
                    ToolContracts.ReadJson(
                        opts.TryGetValue("profile", out string? pf)
                            ? pf : "", "SYSTEM1_UNCALIBRATED")));
            if (flags.Contains("decision-metrics"))
                return Emit(SystemOne.CalibrationMetrics(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? dm)
                            ? dm : "", "SYSTEM1_UNCALIBRATED")));
            if (flags.Contains("cognition-route"))
                return Emit(SystemOne.Route(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? cr)
                            ? cr : "", "SYSTEM1_SCHEMA_INVALID")));
            if (flags.Contains("decision-trace"))
                return Emit(SystemOne.RecordTrace(toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? dtf)
                            ? dtf : "", "SYSTEM1_SCHEMA_INVALID")));
            if (flags.Contains("router-stability"))
            {
                var sp = ToolContracts.ReadJson(
                    opts.TryGetValue("file", out string? rs)
                        ? rs : "", "ROUTER_STABILITY_INVALID");
                if (sp.TryGetProperty("stage", out var st) &&
                    st.ValueKind == JsonValueKind.String)
                    return Emit(RouterStability.Policy(
                        st.GetString()!));
                return Emit(RouterStability.Gate(sp));
            }
            if (flags.Contains("trajectory-validate"))
                return Emit(AgentLearning.ValidateTrajectory(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? tv)
                            ? tv : "", "TRAJECTORY_INVALID")));
            if (flags.Contains("harness-register"))
                return Emit(AgentLearning.HarnessRegister(toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? hr)
                            ? hr : "", "TRAJECTORY_INVALID")));
            if (flags.Contains("harness-outcome"))
                return Emit(AgentLearning.HarnessOutcome(toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? ho)
                            ? ho : "", "TRAJECTORY_INVALID")));
            if (flags.Contains("groupwise-eval"))
                return Emit(AgentLearning.GroupwiseEval(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? gw)
                            ? gw : "", "TRAJECTORY_INVALID")));
            if (flags.Contains("reward-gate"))
                return Emit(AgentLearning.RewardGate(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? rg)
                            ? rg : "", "REWARD_SUSPECT")));
            if (flags.Contains("correction-validate"))
                return Emit(AgentLearning.ValidateCorrection(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? cv)
                            ? cv : "", "TRAJECTORY_INVALID")));
            // §44 acceptance battery
            if (flags.Contains("system1-checks"))
                return Emit(LayaMiMoChecks.Run(toolRoot));
            // ---- NativeMemoryCudaPlane (memory/CUDA directive)
            if (flags.Contains("precision-policy"))
                return Emit(MemoryCudaPlane.PrecisionReport());
            if (flags.Contains("prefill-chunk"))
            {
                var pc = ToolContracts.ReadJson(
                    opts.TryGetValue("file", out string? pcf)
                        ? pcf : "", "MEMPLANE_BUDGET_EXCEEDED");
                return Emit(MemoryCudaPlane.PrefillChunkPlan(
                    pc.GetProperty("free_vram_bytes").GetInt64(),
                    pc.GetProperty("bytes_per_token").GetInt64(),
                    pc.TryGetProperty("active_decode_kbs", out var adk)
                        ? adk.GetInt64() : 0));
            }
            if (flags.Contains("memplane-telemetry-validate"))
                return Emit(MemoryCudaPlane.ValidateTelemetry(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? mtv)
                            ? mtv : "", "MEMPLANE_TELEMETRY_INVALID")));
            // memory/CUDA acceptance battery
            if (flags.Contains("cuda-plane-checks"))
                return Emit(CudaPlaneChecks.Run(toolRoot));
            // ---- NativeSiliconEfficiencyPlane (silicon directive)
            if (flags.Contains("runtime-host-acquire"))
                return Emit(SiliconRuntime.AcquireHost(
                    toolRoot,
                    opts.TryGetValue("owner", out string? ow)
                        ? ow : "xc-learning"));
            if (flags.Contains("artifact-register"))
                return Emit(SiliconRuntime.RegisterArtifact(toolRoot,
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? arf)
                            ? arf : "", "ARTIFACT_DUPLICATE_LOAD")));
            if (flags.Contains("silicon-route"))
            {
                var sr = ToolContracts.ReadJson(
                    opts.TryGetValue("file", out string? srf)
                        ? srf : "", "SILICON_ROUTE_UNCERTIFIED");
                var avail = new HashSet<string>(
                    sr.TryGetProperty("available", out var av) &&
                    av.ValueKind == JsonValueKind.Array
                        ? av.EnumerateArray()
                            .Select(x => x.GetString() ?? "")
                        : Array.Empty<string>(),
                    StringComparer.OrdinalIgnoreCase);
                return Emit(SiliconRuntime.Route(
                    sr.TryGetProperty("op", out var op)
                        ? op.GetString() ?? "" : "",
                    avail,
                    sr.TryGetProperty("qos", out var q)
                        ? q.GetString() ?? "NORMAL" : "NORMAL"));
            }
            if (flags.Contains("cpu-plan"))
                return Emit(SiliconRuntime.CpuPlan(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? cpf)
                            ? cpf : "", "CPU_AFFINITY_INVALID")));
            if (flags.Contains("freeze-map-validate"))
                return Emit(SiliconRuntime.ValidateFreezeMap(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? fmf)
                            ? fmf : "", "PARAMETER_FREEZE_VIOLATION")));
            if (flags.Contains("param-efficiency"))
                return Emit(SiliconRuntime.EfficiencyReport(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? pef)
                            ? pef : "", "EFFICIENCY_INPUT_INVALID")));
            if (flags.Contains("lifetime-plan"))
                return Emit(SiliconRuntime.LifetimePlan(
                    ToolContracts.ReadJson(
                        opts.TryGetValue("file", out string? lpf)
                            ? lpf : "", "LIFETIME_PLAN_INVALID")));
            // silicon acceptance battery
            if (flags.Contains("silicon-checks"))
                return Emit(SiliconChecks.Run(toolRoot));
            return Usage();
        }
        catch (Exception exc)
        {
            return Emit(new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["error"] = exc.Message.Length > 500
                    ? exc.Message[..500] : exc.Message,
                ["error_type"] = exc.GetType().Name,
                // §36 fail-closed taxonomy — the code is contract
                // surface, never swallowed into free text.
                ["error_code"] = exc is ExecutorError ee
                    ? ee.ErrorCode : "INTERNAL",
            });
        }
    }

    private static int Usage()
    {
        Console.Error.WriteLine(
            "GPTBridge.XingchengLearning [--tool-root <dir>] " +
            "(--status | --run-once [--force] | --enable | --disable | " +
            "--retention [--apply|--status] | --run-jobs [n] | " +
            "--job <id> | --self-test | --converge-check | " +
            "--maturation-status | --maturation-freeze --capability " +
            "<id> --evidence <ref> | --maturation-reopen --capability " +
            "<id> --reason <text> | --maturation-unsupported " +
            "--capability <id> --evidence <ref> --reason <text> | " +
            "--maturation-baseline --weights " +
            "<ref> --weights-sha256 <sha> --model <f> --runtime <f> " +
            "--service <f> | " +
            "--release-gate [--bundle <dir>] [--suite <file>] " +
            "[--no-builds] | " +
            "--verify-audit | --db-status | " +
            "--migrate | --teacher-collect [--dry-run] | " +
            "--queue-job --config <cfg.json> [--rows <rows.jsonl>] " +
            "[--include-collected] [--val-permille N] | " +
            "--evaluate --job-id <id> --bundle <dir> --suite <suite.json> " +
            "[--baseline <dir>] [--chat] | " +
            "--gen-begin --target <gen> --weights <path> " +
            "--weight-method <direct|partial|distill> [--source <gen>] " +
            "[--tokenizer <path>] [--schema-from <s>] [--schema-to <s>] " +
            "[--expert-lineage <file.json>] [--notes <text>] | " +
            "--gen-record --manifest <id> --domain <name> " +
            "--status <s> [--migrated N] [--transformed N] " +
            "[--rejected N] [--note <text>] | " +
            "--gen-certify --manifest <id> [--suite <suite.json>] | " +
            "--gen-promote --manifest <id> | " +
            "--gen-purge --manifest <id> [--apply] | " +
            "--gen-status [--manifest <id>] | " +
            "--feature-catalog | " +
            "--capabilities-resolve --request <json> [--bundle <dir>] | " +
            "--language-scan [--root <dir>] | " +
            "--arch-gate --evidence <file.json> | " +
            "--tool-call-validate --call <file.json> | " +
            "--tool-result-validate --result <file.json> | " +
            "--structured-validate --text <file> [--schema <file>] | " +
            "--fim-validate --fim <file.json> | " +
            "--code-task-validate --task <file.json> | " +
            "--task-create --goal <text> [--constraints a;b] " +
            "[--generation <gen>] | " +
            "--task-status|--task-transition|--task-checkpoint|" +
            "--task-resume|--task-step --task <id> [...] | " +
            "--provenance-verify --bundle <dir> --provenance <file.json> " +
            "[--generation <gen>] [--architecture <arch>] | " +
            "--provenance-compute --bundle <dir> [...] | " +
            "--trace-record --trace <file.json> | " +
            "--cap-record --result <file.json> | --trace-status | " +
            "--caps-status | --caps-validate --file <f.json> | " +
            "--catalog-emit | --catalog-validate --file <f.json> | " +
            "--tool-validate --file <f.json> --kind <request|result> | " +
            "--tool-gate [--tool <name>] [--requirement <req>] " +
            "[--reason <code>] [--outcome-status <s>] [--schema-invalid] | " +
            "--tool-metrics | --grounded-validate --file <f.json> | " +
            "--structured-validate --output <f.json> --schema <f.json> | " +
            "--langcheck | --community-checks | " +
            "--taxonomy | --core-contract [--architecture <a>] " +
            "[--layers N] [--hidden N] [--heads N] [--kv-heads N] | " +
            "--drift-gate --job-hash <h> --checkpoint-hash <h> " +
            "--bundle-hash <h> --runtime-hash <h> | " +
            "--version-dimensions | --axis-checks | " +
            "--typed-decision-validate --file <f.json> | " +
            "--decision-calibrate --probs <a.json> --profile <p.json> | " +
            "--decision-metrics --file <f.json> | " +
            "--cognition-route --file <f.json> | " +
            "--decision-trace --file <f.json> | " +
            "--router-stability --file <f.json> | " +
            "--trajectory-validate --file <f.json> | " +
            "--harness-register --file <f.json> | " +
            "--harness-outcome --file <f.json> | " +
            "--groupwise-eval --file <f.json> | " +
            "--reward-gate --file <f.json> | " +
            "--correction-validate --file <f.json> | " +
            "--system1-checks | --precision-policy | " +
            "--prefill-chunk --file <f.json> | " +
            "--memplane-telemetry-validate --file <f.json> | " +
            "--cuda-plane-checks | " +
            "--runtime-host-acquire [--owner <name>] | " +
            "--artifact-register --file <f.json> | " +
            "--silicon-route --file <f.json> | " +
            "--cpu-plan --file <f.json> | " +
            "--freeze-map-validate --file <f.json> | " +
            "--param-efficiency --file <f.json> | " +
            "--lifetime-plan --file <f.json> | --silicon-checks | " +
            "--capacity-checks | " +
            "--recovery-dataset-build --out <dir> [--count N] " +
            "[--seed N] [--capability <id>] | " +
            "--recovery-eval --bundle <dir> --suite <file> " +
            "[--out <file>] | --recovery-run --plan <plan.json>)");
        return 2;
    }

    // ── convergence helpers ───────────────────────────────────────────

    private static JsonElement ReadJsonElement(string path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
            throw new ExecutorError("THINKING_COMPARE_INVALID",
                $"json file missing or unreadable: {path}");
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        return doc.RootElement.Clone();
    }

    private static Dictionary<string, object?> ParseJsonFile(string path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
            throw new ArgumentException("json file missing or unreadable");
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        return (Dictionary<string, object?>)ModelLifecycle.Decode(
            doc.RootElement)!;
    }

    private static Dictionary<string, object?> CapabilitiesResolve(
        string toolRoot, string requestPath, string bundle)
    {
        long trainedContext = 0;
        if (bundle.Length > 0)
        {
            // Read the bundle manifest's trained context so the context
            // budget resolves against the real ceiling.
            string mp = Path.Combine(bundle, "manifest.json");
            if (File.Exists(mp))
            {
                using var doc = JsonDocument.Parse(
                    File.ReadAllText(mp));
                if (doc.RootElement.TryGetProperty("config",
                        out var cfg) &&
                    cfg.TryGetProperty("max_position_embeddings",
                        out var mpe) &&
                    mpe.ValueKind == JsonValueKind.Number)
                    trainedContext = mpe.GetInt64();
            }
        }
        var req = string.IsNullOrWhiteSpace(requestPath)
            ? new Dictionary<string, object?>()
            : ParseJsonFile(requestPath);
        return RuntimeCapabilityLayer.Resolve(req, trainedContext);
    }

    private static Dictionary<string, object?> ArchGate(string evidencePath)
    {
        var e = string.IsNullOrWhiteSpace(evidencePath)
            ? new ArchitectureChangeGate.Evidence()
            : ParseEvidence(evidencePath);
        return ArchitectureChangeGate.Evaluate(e);
    }

    private static ArchitectureChangeGate.Evidence ParseEvidence(
        string path)
    {
        var d = ParseJsonFile(path);
        bool B(string k)
            => d.TryGetValue(k, out var v) && v is bool b && b;
        return new ArchitectureChangeGate.Evidence
        {
            ExistingArchCannotSolve = B("existing_arch_cannot_solve"),
            RuntimeOptimizationIneffective =
                B("runtime_optimization_ineffective"),
            DataImprovementIneffective = B("data_improvement_ineffective"),
            PostTrainingIneffective = B("post_training_ineffective"),
            IndependentBenchmark = B("independent_benchmark"),
            Ablation = B("ablation"),
            MemoryImpact = B("memory_impact"),
            LatencyImpact = B("latency_impact"),
        };
    }

    private static Dictionary<string, object?> StructuredValidate(
        string textPath, string schemaPath)
    {
        if (string.IsNullOrWhiteSpace(textPath) ||
            !File.Exists(textPath))
            throw new ArgumentException("structured text file missing");
        string text = File.ReadAllText(textPath);
        string? schema =
            string.IsNullOrWhiteSpace(schemaPath) ||
            !File.Exists(schemaPath)
                ? null : File.ReadAllText(schemaPath);
        var outcome = StructuredOutputValidator.Validate(text, schema);
        return new Dictionary<string, object?>
        {
            ["ok"] = outcome.Ok,
            ["error"] = outcome.Error.Length > 0 ? outcome.Error : null,
            ["repaired"] = outcome.Repaired,
            ["format"] = StructuredOutputValidator.Format,
        };
    }

    private static string InferToolRoot()
    {
        // Walk ancestors looking for the local-model marker
        // (runtime/settings/self-learning.json or xingcheng/).
        var dir = new DirectoryInfo(Directory.GetCurrentDirectory());
        while (dir != null)
        {
            if (Directory.Exists(Path.Combine(dir.FullName, "xingcheng")) &&
                Directory.Exists(Path.Combine(dir.FullName, "runtime")))
                return dir.FullName;
            dir = dir.Parent;
        }
        return Directory.GetCurrentDirectory();
    }

    private static int Emit(Dictionary<string, object?> payload)
    {
        Console.WriteLine(CanonicalJson.PrettyDict(payload));
        return payload.TryGetValue("ok", out object? ok) &&
               ok is bool b && !b ? 1 : 0;
    }

    private static Dictionary<string, object?> Status(string toolRoot)
    {
        var policy = SelfLearningPolicy.Load(toolRoot);
        var state = SelfLearningState.Load(toolRoot);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["policy"] = policy.ToDict(),
            ["state"] = state,
            ["training_window"] = policy.TrainingWindowStatus(),
            ["runtime_checkpoint"] = EngineSettings.PinnedCheckpoint(toolRoot),
            ["retention_policy"] = RetentionPolicy.Load(toolRoot).ToDict(),
            ["checked_at"] = XcPaths.IsoNow(),
        };
    }

    private static Dictionary<string, object?> SetEnabled(string toolRoot, bool on)
    {
        var policy = SelfLearningPolicy.Load(toolRoot);
        policy.Enabled = on;
        policy.Save(toolRoot);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["action"] = on ? "enabled" : "disabled",
            ["policy"] = policy.ToDict(),
        };
    }

    private static Dictionary<string, object?> RetentionStatus(string toolRoot)
        => new()
        {
            ["ok"] = true,
            ["policy"] = RetentionPolicy.Load(toolRoot).ToDict(),
            ["checked_at"] = XcPaths.IsoNow(),
        };

    private static Dictionary<string, object?> RunJobs(string toolRoot, int limit)
    {
        var repo = new TransformerTrainingRepository(toolRoot);
        var executor = new TrainingJobExecutor(repo, toolRoot);
        var results = new List<object?>();
        foreach (var row in repo.QueuedJobs(limit))
        {
            results.Add(executor.RunJob((string)row["job_id"]!));
            // sequential: the retired executor drained the queue one job at
            // a time to keep resource supervision deterministic.
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["processed"] = results.Count,
            ["results"] = results,
        };
    }

    private static Dictionary<string, object?> RunJob(string toolRoot, string jobId)
    {
        var repo = new TransformerTrainingRepository(toolRoot);
        return new TrainingJobExecutor(repo, toolRoot).RunJob(jobId);
    }

    /// <summary>Registers a completed job's exported bundle as an adapter
    /// candidate and runs the governed native evaluation (capability or
    /// eval suite) against an optional baseline bundle. Records the full
    /// result row in the repository ??the same gate self-learning uses.
    /// --chat measures the deployed chat surface.</summary>
    private static Dictionary<string, object?> Evaluate(
        string toolRoot, string jobId, string bundle, string suitePath,
        string? baseline, bool chat)
    {
        if (string.IsNullOrWhiteSpace(jobId) ||
            string.IsNullOrWhiteSpace(bundle) ||
            string.IsNullOrWhiteSpace(suitePath))
            throw new ArgumentException(
                "evaluate requires --job-id <id> --bundle <dir> " +
                "--suite <suite.json>");
        var repo = new TransformerTrainingRepository(toolRoot);
        var adapter = repo.RegisterAdapterCandidate(
            jobId, bundle,
            new Dictionary<string, object?>
            {
                ["origin"] = "queue-job-evaluate",
                ["prompt_mode"] = chat ? "chat" : "verbatim",
            });
        var eval = Evaluation.RunEvaluation(
            repo, (string)adapter["adapter_id"]!, bundle, suitePath,
            baselineArtifact: baseline,
            evaluatedBy: "queue-job-evaluate", chat: chat);
        return new Dictionary<string, object?>
        {
            ["ok"] = TransformerTrainingRepository.Truthy(
                eval.GetValueOrDefault("ok")),
            ["adapter_id"] = adapter["adapter_id"],
            ["evaluation"] = eval,
        };
    }

    /// <summary>Governed queue entry for externally prepared SFT rows:
    /// rows.jsonl (input_text/target_text/intent/source_type/
    /// quality_score/example_id/revision[/scope]) -> snapshot -> dataset
    /// -> queued job; execution stays behind the same audited job lane as
    /// self-learning. The configuration file carries the training
    /// configuration dictionary verbatim (model arch, init_checkpoint,
    /// weight_quant, budgets).</summary>
    private static Dictionary<string, object?> QueueJob(
        string toolRoot, string rowsPath, string configPath,
        int valPermille, bool includeCollected)
    {
        if (string.IsNullOrWhiteSpace(configPath) ||
            (string.IsNullOrWhiteSpace(rowsPath) && !includeCollected))
            throw new ArgumentException(
                "queue-job requires --config <json> and --rows <jsonl> " +
                "or --include-collected");
        var byScope =
            new Dictionary<string, List<Dictionary<string, object?>>>(
                StringComparer.OrdinalIgnoreCase);
        int lineno = 0;
        if (!string.IsNullOrWhiteSpace(rowsPath))
            foreach (var line in File.ReadLines(rowsPath))
            {
                ++lineno;
                if (string.IsNullOrWhiteSpace(line)) continue;
                Dictionary<string, object?> row;
                try
                {
                    row = (Dictionary<string, object?>)ModelLifecycle.Decode(
                        JsonDocument.Parse(line).RootElement)!;
                }
                catch (Exception parseEx)
                    when (parseEx is JsonException or InvalidCastException)
                {
                    throw new ArgumentException(
                        $"rows.jsonl line {lineno}: invalid JSON");
                }
                string scope =
                    (TransformerTrainingRepository.Str(row, "scope") ?? "main")
                    .Trim();
                if (!byScope.TryGetValue(scope, out var list))
                    byScope[scope] = list =
                        new List<Dictionary<string, object?>>();
                list.Add(row);
            }
        int collected = 0;
        if (includeCollected)
            foreach (var (scope, rows) in
                     Collectors.CollectVerifiedExamples(toolRoot))
            {
                if (!byScope.TryGetValue(scope, out var list))
                    byScope[scope] = list =
                        new List<Dictionary<string, object?>>();
                list.AddRange(rows);
                collected += rows.Count;
            }
        if (byScope.Count == 0)
            throw new ArgumentException("queue-job produced no examples");

        Dictionary<string, object?> configuration;
        try
        {
            configuration = (Dictionary<string, object?>)
                ModelLifecycle.Decode(
                    JsonDocument.Parse(
                        File.ReadAllText(configPath)).RootElement)!;
        }
        catch (Exception cfgEx) when (cfgEx is JsonException or IOException
                                          or InvalidCastException)
        {
            throw new ArgumentException("config file unreadable or invalid");
        }

        string snapshotPath = Path.Combine(
            toolRoot, XcPaths.SelfLearningSnapshotRel,
            $"queue-job-{DateTime.UtcNow:yyyyMMdd-HHmmss}.jsonl");
        bool isPretrain =
            configuration.TryGetValue("training_kind", out object? tk) &&
            tk is string tkStr &&
            string.Equals(tkStr, "pretrain", StringComparison.OrdinalIgnoreCase);
        var snapshot = isPretrain
            ? SftDataset.BuildPretrainDataset(snapshotPath, byScope, valPermille)
            : SftDataset.BuildSftDataset(snapshotPath, byScope, valPermille);
        var repo = new TransformerTrainingRepository(toolRoot);
        var dataset = repo.CreateDataset(
            contentSha256: (string)snapshot["content_sha256"]!,
            snapshotPath: (string)snapshot["snapshot_path"]!,
            snapshotSha256: (string)snapshot["snapshot_sha256"]!,
            examples: (List<Dictionary<string, object?>>)snapshot["examples"]!,
            sourceManifest: new Dictionary<string, object?>
            {
                ["format"] = isPretrain ? SftDataset.PretrainFormatVersion
                                        : SftDataset.SftFormatVersion,
                ["origin"] = "queue-job",
                ["rows_source"] = string.IsNullOrWhiteSpace(rowsPath)
                    ? null : Path.GetFileName(rowsPath),
                ["include_collected"] = includeCollected,
                ["collected_examples"] = collected,
            },
            createdBy: "queue-job",
            formatVersion: isPretrain ? SftDataset.PretrainFormatVersion
                                      : SftDataset.SftFormatVersion);
        var job = repo.CreateTrainingJob(
            datasetId: (string)dataset["dataset_id"]!,
            configuration: configuration,
            requestedBy: "queue-job");
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["dataset_id"] = dataset["dataset_id"],
            ["dataset_inserted"] = dataset.GetValueOrDefault("inserted"),
            ["job_id"] = job["job_id"],
            ["snapshot_path"] = snapshot["snapshot_path"],
            ["examples"] = snapshot["examples"] is
                List<Dictionary<string, object?>> exList ? exList.Count : 0,
        };
    }

    /// <summary>Governed end-to-end smoke of the native lane: synthetic
    /// snapshot -> dataset -> queued job -> tokenize -> trainer subprocess
    /// -> export-bundle -> completed. Uses a tiny scratch model so the
    /// 2.4 GB production bundle is never touched; every mutation goes
    /// through the repository's audited paths.</summary>
    private static Dictionary<string, object?> MaturationStatus(
        string toolRoot)
    {
        var state = Maturation300M.LoadState(toolRoot);
        var caps = (Dictionary<string, object?>)state["capabilities"]!;
        var head = Maturation300M.Head(state);
        var rows = new List<object?>();
        foreach (var spec in Maturation300M.Sequence)
        {
            var raw = caps.TryGetValue(spec.Id, out object? c)
                ? c as Dictionary<string, object?> : null;
            rows.Add(new Dictionary<string, object?>
            {
                ["capability"] = spec.Id,
                ["status"] = raw?.GetValueOrDefault("status") ?? "pending",
                ["weight_version"] =
                    raw?.GetValueOrDefault("weight_version"),
                ["metrics"] = spec.Metrics.Cast<object?>().ToList(),
            });
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["phase"] = Maturation300M.PhaseId,
            ["model_scale"] = Maturation300M.ModelScale,
            ["architecture_generation"] =
                Maturation300M.ArchitectureGeneration,
            ["active_capability"] = head?.Id,
            ["capabilities"] = rows,
            ["ladder"] = Maturation300M.Ladder.Select(
                g => (object?)new Dictionary<string, object?>
                {
                    ["level"] = g.Level,
                    ["code"] = g.Code,
                    ["requirement"] = g.Requirement,
                }).ToList(),
        };
    }

    private static Dictionary<string, object?> MaturationBaseline(
        string toolRoot, Dictionary<string, string> opts)
    {
        string weights = opts.TryGetValue("weights", out string? w)
            ? w : "";
        string sha = opts.TryGetValue("weights-sha256", out string? s)
            ? s : "";
        string Get(string k) =>
            opts.TryGetValue(k, out string? v) ? v : "";
        var model = ParseJsonFile(Get("model"));
        var runtime = ParseJsonFile(Get("runtime"));
        var service = ParseJsonFile(Get("service"));
        var artifact = Maturation300M.WriteBaseline(
            toolRoot, weights, sha, model, runtime, service);
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["baseline"] = Maturation300M.BaselineRel,
            ["sections"] =
                Maturation300M.BaselineSections.Cast<object?>().ToList(),
            ["weights_ref"] = artifact["weights_ref"],
        };
    }

    private static Dictionary<string, object?> SelfTest(string toolRoot)
    {
        var steps = new List<object?>();
        var repo = new TransformerTrainingRepository(toolRoot);

        // Synthetic examples with a guaranteed train+validation split:
        // iterate suffixes until the deterministic hash split yields both.
        var examples = new List<Dictionary<string, object?>>();
        bool hasVal = false;
        for (int i = 0; i < 400 && (examples.Count < 6 || !hasVal); i++)
        {
            string prompt = $"xc-selftest prompt {i}";
            string completion = $"xc-selftest completion {i}";
            string hash = TransformerTrainingRepository.Sha256Text(
                $"{prompt}\n\n{completion}");
            bool val = Convert.ToInt32(hash[..8], 16) % 1000 < 500;
            if (!val || !hasVal)
            {
                examples.Add(new Dictionary<string, object?>
                {
                    ["revision"] = i + 1,
                    ["example_id"] = $"selftest-{i}",
                    ["intent"] = "selftest",
                    ["input_text"] = prompt,
                    ["target_text"] = completion,
                    ["source_type"] = "selftest",
                    ["quality_score"] = 0.9,
                });
                if (val) hasVal = true;
            }
        }
        string snapshotPath = Path.Combine(
            toolRoot, XcPaths.SelfLearningSnapshotRel,
            $"selftest-{DateTime.UtcNow:yyyyMMdd-HHmmss}.jsonl");
        var snapshot = SftDataset.BuildSftDataset(
            snapshotPath,
            new Dictionary<string, List<Dictionary<string, object?>>>
            {
                ["main"] = examples,
            },
            valPermille: 500);
        steps.Add(new Dictionary<string, object?>
        {
            ["step"] = "snapshot",
            ["examples"] = snapshot["manifest"] is Dictionary<string, object?> m
                ? m.GetValueOrDefault("example_count") : null,
        });

        var dataset = repo.CreateDataset(
            contentSha256: (string)snapshot["content_sha256"]!,
            snapshotPath: (string)snapshot["snapshot_path"]!,
            snapshotSha256: (string)snapshot["snapshot_sha256"]!,
            examples: (List<Dictionary<string, object?>>)snapshot["examples"]!,
            sourceManifest: new Dictionary<string, object?>
            {
                ["format"] = SftDataset.SftFormatVersion,
                ["origin"] = "xc-learning-selftest",
            },
            createdBy: "xc-learning-selftest");
        steps.Add(new Dictionary<string, object?>
        {
            ["step"] = "dataset",
            ["dataset_id"] = dataset["dataset_id"],
            ["inserted"] = dataset.GetValueOrDefault("inserted"),
        });

        // Dedup reuse: a previously registered dataset keeps its
        // original snapshot_path, which retention may have cleaned.
        // Content is deterministic, so restore the file to the
        // recorded path — the registered sha256 still verifies.
        if (dataset["inserted"] is bool ins && !ins &&
            dataset["snapshot_path"] is string recPath &&
            recPath.Length > 0 && !File.Exists(recPath))
        {
            Directory.CreateDirectory(Path.GetDirectoryName(recPath)!);
            File.Copy(snapshotPath, recPath, overwrite: true);
        }

        // Tiny scratch model — the point is the governed chain, not
        // capacity. No init_checkpoint => trainer inits from ``model``.
        var job = repo.CreateTrainingJob(
            datasetId: (string)dataset["dataset_id"]!,
            configuration: new Dictionary<string, object?>
            {
                ["training_kind"] = "sft",
                // §0 recovery lane: under SINGLE_CAPABILITY_RECOVERY a
                // governed-chain job must declare the active capability
                // — an unlabeled sft job is denied by GuardJob.
                ["capability"] = SelfLearningPolicy.Load(toolRoot)
                    .ActiveCapability,
                ["tokenizer_dir"] =
                    "runtime/tokenizers/xingcheng-bpe-8k-20260919-120054",
                ["model_id"] = "xingcheng-selftest",
                ["model"] = new Dictionary<string, object?>
                {
                    ["vocab_size"] = 16384,
                    ["hidden_size"] = 64,
                    ["intermediate_size"] = 128,
                    ["num_hidden_layers"] = 2,
                    ["num_attention_heads"] = 4,
                    ["num_key_value_heads"] = 4,
                    ["max_position_embeddings"] = 256,
                    ["moe_num_experts"] = 0,
                },
                ["max_length"] = 128,
                ["max_steps"] = 4,
                ["warmup_steps"] = 1,
                ["lr"] = 1e-3,
                ["checkpoint_every"] = 0,
                ["eval_every"] = 0,
                ["log_every"] = 1,
                ["device"] = "cpu",
                ["max_train_seconds"] = 600,
                ["seed"] = 7,
            },
            requestedBy: "xc-learning-selftest");
        steps.Add(new Dictionary<string, object?>
        {
            ["step"] = "job-queued", ["job_id"] = job["job_id"],
        });

        // Under FROZEN (no recovery lane) the governed chain must seal at
        // execution: the denial is the expected outcome, not a failure.
        // Downstream smoke that needs a produced artifact falls back to
        // existing files (lifecycle registration only hashes the path).
        var freezePol = SelfLearningPolicy.Load(toolRoot);
        bool frozenSealExpected = freezePol.CapabilityTrainingFrozen &&
            !CapabilityFreeze.RecoveryLaneOpen(freezePol);
        Dictionary<string, object?>? report = null;
        bool executeDeniedFrozen = false;
        try
        {
            report = new TrainingJobExecutor(repo, toolRoot)
                .RunJob((string)job["job_id"]!);
        }
        catch (ExecutorError ee) when (
            ee.ErrorCode == "EXECUTOR_TRAINING_FROZEN" && frozenSealExpected)
        {
            executeDeniedFrozen = true;
        }
        // A completed maturation sequence is also a sealed lane: the
        // sequence head has no remaining capability, so SFT denial via
        // MATURATION_SEQUENCE_COMPLETE is the correct fail-closed
        // outcome and exercises the same downstream paths.
        if (report != null &&
            !TransformerTrainingRepository.Truthy(report["ok"]) &&
            report["job"] is Dictionary<string, object?> jr &&
            (string?)jr.GetValueOrDefault("error_code") ==
                "MATURATION_SEQUENCE_COMPLETE")
            executeDeniedFrozen = true;
        steps.Add(new Dictionary<string, object?>
        {
            ["step"] = "execute",
            ["ok"] = executeDeniedFrozen || (report != null &&
                     TransformerTrainingRepository.Truthy(report["ok"])),
            ["frozen_denial"] = executeDeniedFrozen,
            ["error_code"] = report?.GetValueOrDefault("error_code"),
            ["output_path"] = report?.GetValueOrDefault("output_path"),
        });
        if (!executeDeniedFrozen &&
            (report == null ||
             !TransformerTrainingRepository.Truthy(report["ok"])))
            return new Dictionary<string, object?>
            {
                ["ok"] = false, ["steps"] = steps,
                ["job"] = report?.GetValueOrDefault("job"),
            };

        // Lifecycle smoke against a scratch directory ??production
        // lifecycle roots are never touched. Under a sealed queue the
        // artifact paths point at the deterministic snapshot instead —
        // registration hashes file content, mechanics are identical.
        string outputPath = executeDeniedFrozen
            ? snapshotPath
            : report!["output_path"]!.ToString()!;
        string bundleDir = Path.GetDirectoryName(outputPath)!;
        string jobDir = Directory.GetParent(bundleDir)!.FullName;
        string lcDir = Path.Combine(
            toolRoot, XcPaths.SelfLearningSnapshotRel,
            $"selftest-lifecycle-{DateTime.UtcNow:yyyyMMdd-HHmmss}");
        Directory.CreateDirectory(lcDir);
        // Adapter registration + native eval gate: candidate bundle vs
        // itself as baseline (regression delta = 0). The gate outcome is
        // reported; the step asserts the eval ran and was recorded.
        // Under a sealed queue no candidate artifact exists — the eval
        // lane is exercised on the next recovery lane; mark skipped.
        bool evalOk = true;
        if (!executeDeniedFrozen)
        {
            var adapter = repo.RegisterAdapterCandidate(
                (string)job["job_id"]!,
                report!["output_path"]!.ToString()!,
                new Dictionary<string, object?>
                {
                    ["origin"] = "xc-learning-selftest",
                });
            string suitePath = Path.Combine(
                toolRoot, "xingcheng", "eval",
                "star-native-eval-dialogue-20260921-125054.json");
            var eval = Evaluation.RunEvaluation(
                repo, (string)adapter["adapter_id"]!,
                report!["output_path"]!.ToString()!, suitePath,
                baselineArtifact: report!["output_path"]!.ToString()!,
                evaluatedBy: "xc-learning-selftest");
            evalOk = TransformerTrainingRepository.Truthy(
                eval.GetValueOrDefault("ok"));
            steps.Add(new Dictionary<string, object?>
            {
                ["step"] = "evaluate",
                ["adapter_id"] = adapter["adapter_id"],
                ["suite"] = eval.GetValueOrDefault("suite"),
                ["passed"] = eval.GetValueOrDefault("passed"),
                ["eval_ok"] = eval.GetValueOrDefault("ok"),
            });
        }
        else
        {
            steps.Add(new Dictionary<string, object?>
            {
                ["step"] = "evaluate",
                ["skipped"] = "sealed_by_freeze",
            });
        }

        // DPO preference-pair bridge: build a governed snapshot and
        // register it (dataset row + audit), without queueing training.
        string pairsPath = Path.Combine(
            toolRoot, XcPaths.SelfLearningSnapshotRel,
            $"selftest-pairs-{DateTime.UtcNow:yyyyMMdd-HHmmss}.jsonl");
        var pairsManifest = SftDataset.BuildPairsSnapshot(
            Enumerable.Range(0, 4).Select(i =>
                new Dictionary<string, object?>
                {
                    ["pair_id"] = $"selftest-pair-{i}",
                    ["prompt_text"] = $"xc-selftest dpo prompt {i}",
                    ["chosen_text"] = $"chosen answer {i}",
                    ["rejected_text"] = $"rejected answer {i}",
                }).ToList(),
            pairsPath);
        var pairsDataset = SftDataset.RegisterPairsSnapshot(
            repo, pairsManifest, createdBy: "xc-learning-selftest");
        steps.Add(new Dictionary<string, object?>
        {
            ["step"] = "preference-pairs",
            ["pairs"] = pairsManifest["pairs"],
            ["dataset_id"] = pairsDataset["dataset_id"],
        });

        var lc = ModelLifecycle.LoadOrCreate(lcDir, "xingcheng-selftest");
        lc.Transition("INITIALIZED");
        lc.Transition("SFT_TRAINING");
        lc.Transition("INSTRUCT_READY");
        var meta = new Dictionary<string, object?>
        {
            ["config_sha256"] = "selftest-fp",
            ["maturity_level"] = 7,
        };
        lc.RegisterArtifact("weights",
            executeDeniedFrozen ? snapshotPath
                                : Path.Combine(jobDir, "final.xcn"),
            meta, activate: true);
        lc.RegisterArtifact("weights",
            executeDeniedFrozen ? pairsPath
                                : Path.Combine(bundleDir, "weights.bin"),
            meta, activate: true);
        lc.Save(lcDir);
        var reloaded = ModelLifecycle.Load(lcDir);
        if (reloaded.ActiveWeightsVersion != 2)
            throw new InvalidOperationException("lifecycle reload mismatch");
        // 世代繼任契�?：v2 ?�用?�自?��???v1 完整記�? ?��??�除?�代後其
        // 資�?仍�??�在?�代 metadata.succeeded_from ?��?
        bool successionRecorded = false;
        if (reloaded.Artifacts.TryGetValue("weights", out var wg) &&
            wg.TryGetValue("versions", out object? wv) &&
            wv is List<object?> wlist)
            foreach (object? item in wlist)
                if (item is Dictionary<string, object?> e &&
                    Convert.ToInt32(e["version"]) == 2 &&
                    e["metadata"] is Dictionary<string, object?> em &&
                    em["succeeded_from"] is Dictionary<string, object?> sf &&
                    Convert.ToInt32(sf["version"]) == 1 &&
                    sf["sha256"] is string sfs && sfs.Length > 0)
                    successionRecorded = true;
        reloaded.GovernedRollbackWeights(
            1, "selftest-fp", excludeVersions: new[] { 2 });
        if (reloaded.ActiveWeightsVersion != 1)
            throw new InvalidOperationException("governed rollback mismatch");
        bool denied = false;
        try
        {
            reloaded.GovernedRollbackWeights(2, "wrong-fp");
        }
        catch (ArgumentException) { denied = true; }
        // Fail-closed: the active generation is never retired by the
        // supersession path even when a successor nominates it.
        bool retireActiveDenied = reloaded.RetireWeightVersion(1, 2) == null;
        var retired = reloaded.RetireWeights(keepLatest: 1);
        reloaded.Save(lcDir);
        steps.Add(new Dictionary<string, object?>
        {
            ["step"] = "lifecycle",
            ["active_version"] = reloaded.ActiveWeightsVersion,
            ["rollback_denied_on_bad_fingerprint"] = denied,
            ["succession_recorded"] = successionRecorded,
            ["retire_active_denied"] = retireActiveDenied,
            ["retired_versions"] = retired
                .Select(e => (object?)e["version"]).ToList(),
        });
        return new Dictionary<string, object?>
        {
            ["ok"] = denied && successionRecorded && retireActiveDenied &&
                     reloaded.ActiveWeightsVersion == 1 && evalOk,
            ["frozen_sealed"] = executeDeniedFrozen,
            ["steps"] = steps,
            ["job"] = report?.GetValueOrDefault("job"),
        };
    }
}
