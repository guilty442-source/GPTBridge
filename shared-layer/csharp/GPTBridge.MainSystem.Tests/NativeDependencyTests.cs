using GPTBridge.MainSystem;

namespace GPTBridge.MainSystem.Tests;

public class NativeDependencyTests
{
    private static StartupManifest Manifest => new()
    {
        BootstrapPhases = [], Dependencies = [], ProbeConstants = new Dictionary<string, double>(),
        Ports = new Dictionary<string, int>(), Supervisor = new Dictionary<string, System.Text.Json.Nodes.JsonNode?>(),
        StartupPhases = [], CoreReadyConditions = [], CriticalityClasses = [], NoFixedCriticalityServices = [],
    };
    private static DependencyDeclaration Dep(string contract) => new()
    {
        Identity = "native-metadata", Owner = "main-system", RequiredBy = "startup",
        Criticality = "core-critical", ReadinessContract = contract, Deadline = "1s",
        RetryBudget = 0, ShutdownOrder = 1,
    };

    [Fact]
    public async Task Retired_postgres_never_resolves_dsn_or_falls_back_to_tcp()
    {
        var probes = new DependencyProbes(Manifest, () => throw new Exception("must not resolve"));
        var result = await probes.ProbeAsync(Dep("tcp-or-dsn-select-1"), default);
        Assert.False(result.Ready);
        Assert.Equal("POSTGRESQL_RETIRED", result.FaultCode);
    }

    [Fact]
    public async Task Postgres_identity_cannot_bypass_retirement_with_tcp_contract()
    {
        var dep = Dep("loopback-tcp-5432") with { Identity = "postgresql" };
        var result = await new DependencyProbes(Manifest).ProbeAsync(dep, default);
        Assert.False(result.Ready);
        Assert.Equal("POSTGRESQL_RETIRED", result.FaultCode);
    }

    [Fact]
    public async Task Native_store_requires_an_installed_verifier()
    {
        var result = await new DependencyProbes(Manifest).ProbeAsync(Dep("native-store-integrity"), default);
        Assert.False(result.Ready);
        Assert.Equal("NATIVE_STORE_VERIFIER_UNAVAILABLE", result.FaultCode);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task Native_readiness_uses_actual_integrity_verdict(bool verified)
    {
        var probes = new DependencyProbes(Manifest, nativeIntegrity: (_, _) => Task.FromResult(verified));
        var result = await probes.ProbeAsync(Dep("native-store-integrity"), default);
        Assert.Equal(verified, result.Ready);
    }

    [Fact]
    public async Task Verifier_exception_fails_closed_without_leaking_details()
    {
        var probes = new DependencyProbes(Manifest, nativeIntegrity: (_, _) => throw new Exception("secret"));
        var result = await probes.ProbeAsync(Dep("native-store-integrity"), default);
        Assert.False(result.Ready);
        Assert.DoesNotContain("secret", result.Message);
    }

    [Fact]
    public async Task Cancellation_is_not_reported_as_ready()
    {
        using var cts = new CancellationTokenSource();
        cts.Cancel();
        var probes = new DependencyProbes(Manifest, nativeIntegrity: (_, _) => Task.FromResult(true));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => probes.ProbeAsync(Dep("native-store-integrity"), cts.Token));
    }
}
