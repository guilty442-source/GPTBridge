using GPTBridge.MainSystem;

namespace GPTBridge.MainSystem.Tests;

public class GovernedStartupTests
{
    private static StartupManifest Manifest(
        IReadOnlyList<DependencyDeclaration> deps) => new()
    {
        BootstrapPhases = ["environment-check", "governance-audit"],
        Dependencies = deps,
        ProbeConstants = new Dictionary<string, double>
        {
            ["startup_gate_deadline_seconds"] = 90.0,
        },
        Ports = new Dictionary<string, int> { ["postgresql"] = 5432 },
        Supervisor = new Dictionary<string, System.Text.Json.Nodes.JsonNode?>(),
        StartupPhases =
        [
            "phase-0-local-preflight",
            "phase-1-minimal-information-bootstrap",
            "phase-2-read-official-codex",
        ],
        CoreReadyConditions =
        [
            "official-codex-valid",
            "all-core-critical-dependencies-ready",
        ],
        CriticalityClasses =
            ["core-critical", "capability-critical", "optional"],
        NoFixedCriticalityServices = ["postgresql", "vectord", "ollama"],
    };

    private static DependencyDeclaration Dep(
        string identity,
        string criticality = "capability-critical",
        string requiredBy = "consumer",
        int shutdownOrder = 10,
        string activation = "eager") => new()
    {
        Identity = identity,
        Owner = "test-owner",
        RequiredBy = requiredBy,
        Criticality = criticality,
        ReadinessContract = "loopback-tcp-1",
        Deadline = "1s",
        RetryBudget = 0,
        ShutdownOrder = shutdownOrder,
        Activation = activation,
    };

    [Fact]
    public void Dag_acyclic_when_no_cycles()
    {
        var dag = new DependencyDag([
            Dep("a", requiredBy: "b"), Dep("b", requiredBy: "c"),
        ]);
        Assert.True(dag.IsAcyclic);
    }

    [Fact]
    public void Dag_detects_cycle()
    {
        var dag = new DependencyDag([
            Dep("a", requiredBy: "b"), Dep("b", requiredBy: "a"),
        ]);
        Assert.False(dag.IsAcyclic);
    }

    [Fact]
    public void StartOrder_deepest_chain_first()
    {
        var dag = new DependencyDag([
            Dep("leaf", requiredBy: "mid"),
            Dep("mid", requiredBy: "root"),
            Dep("root", requiredBy: "external"),
        ]);
        var order = dag.StartOrder().Select(d => d.Identity).ToList();
        // "mid" is required_by "root"; root required_by external (not a
        // declaration) — so root depth=1? Depth walks required_by chain:
        // root.required_by="external" not in manifest -> depth 0;
        // mid.required_by="root" -> depth 1; leaf.required_by="mid" -> 2.
        Assert.Equal("leaf", order[0]);
        Assert.Equal("mid", order[1]);
        Assert.Equal("root", order[2]);
    }

    [Fact]
    public void Classification_flags_invalid_criticality()
    {
        var dep = Dep("svc") with { Criticality = "bogus" };
        var dag = new DependencyDag([dep]);
        var result = GovernedStartup.VerifyDependencyClassification(
            dag, Manifest([dep]));
        Assert.False(result["ok"]!.GetValue<bool>());
        Assert.Contains(
            "invalid-criticality:svc:bogus",
            result["violations"]!.AsArray()
                .Select(v => v!.GetValue<string>()));
    }

    [Fact]
    public void Classification_flags_name_based_core_critical()
    {
        // postgres is in no_fixed_criticality_services: core-critical
        // without a required_by contract is a name-based violation.
        var dep = Dep("postgresql", criticality: "core-critical",
            requiredBy: "");
        var dag = new DependencyDag([dep]);
        var result = GovernedStartup.VerifyDependencyClassification(
            dag, Manifest([dep]));
        Assert.False(result["ok"]!.GetValue<bool>());
        Assert.Contains(
            "name-based-criticality:postgresql:core-critical-without-required-by",
            result["violations"]!.AsArray()
                .Select(v => v!.GetValue<string>()));
    }

    [Fact]
    public void PhaseOrder_flags_missing_and_out_of_order()
    {
        var required = new[] { "p0", "p1", "p2" };
        var ok = GovernedStartup.VerifyPhaseOrder(
            required, ["p0", "p1", "p2"]);
        Assert.True(ok["ok"]!.GetValue<bool>());

        var missing = GovernedStartup.VerifyPhaseOrder(
            required, ["p0", "p2"]);
        Assert.False(missing["ok"]!.GetValue<bool>());
        Assert.Contains("missing-phase:p1",
            missing["violations"]!.AsArray()
                .Select(v => v!.GetValue<string>()));
    }

    [Fact]
    public void CoreReady_requires_all_conditions()
    {
        var conditions = new[] { "a", "b" };
        var ok = GovernedStartup.VerifyCoreReady(
            conditions, new Dictionary<string, bool>
            { ["a"] = true, ["b"] = true });
        Assert.True(ok["core_ready"]!.GetValue<bool>());

        var notOk = GovernedStartup.VerifyCoreReady(
            conditions, new Dictionary<string, bool> { ["a"] = true });
        Assert.False(notOk["core_ready"]!.GetValue<bool>());
        Assert.Contains("b", notOk["missing"]!.AsArray()
            .Select(v => v!.GetValue<string>()));
    }

    [Fact]
    public void FailureSignal_core_vs_capability()
    {
        var core = GovernedStartup.StartupFailureSignal(
            "core-critical", failedDependency: "postgresql");
        Assert.Equal("failed-generation+owned-reverse-DAG-cleanup+" +
            "typed-user-visible-cause", core["action"]!.GetValue<string>());

        var cap = GovernedStartup.StartupFailureSignal(
            "capability-critical", failedDependency: "vectord");
        Assert.Equal("affected-capability-degraded-only+retry-budget",
            cap["action"]!.GetValue<string>());
        Assert.False(cap["manual_approval_required"]!.GetValue<bool>());
    }
}
