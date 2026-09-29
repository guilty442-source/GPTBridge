using System.Text.Json.Nodes;
using GPTBridge.Permission;

namespace GPTBridge.Permission.Tests;

/// <summary>
/// Parity tests for the C# port of permission_grant_ledger.py,
/// permission_automation_lifecycle.py and permission_lifecycle.py
/// (retired under B167/B38).
/// </summary>
public sealed class PermissionLifecycleTests : IDisposable
{
    private readonly string _dir;

    public PermissionLifecycleTests()
    {
        _dir = Path.Combine(
            Path.GetTempPath(),
            "gptbridge-perm-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(_dir);
    }

    public void Dispose()
    {
        try { Directory.Delete(_dir, recursive: true); }
        catch (IOException) { }
    }

    private string LedgerPath => Path.Combine(_dir, "grant.jsonl");
    private string ViolationPath => Path.Combine(_dir, "violation.jsonl");

    private PermissionGrantLedger NewLedger(
        Func<DateTimeOffset>? clock = null) =>
        new(LedgerPath, ViolationPath, clock);

    // ---------- ledger ----------

    [Fact]
    public void record_grant_appends_issue_entry()
    {
        var ledger = NewLedger();
        var seq = ledger.RecordGrant(
            "p-1", "actor-a", "cap", "tgt", "read", "scope",
            "requester-x", reviewFinding: "rf", reviewId: "rv",
            basis: new[] { "A436" });
        Assert.Equal(1, seq);
        var history = ledger.LoadGrantHistory("p-1");
        Assert.Single(history);
        var e = history[0];
        Assert.Equal("issue", e["operation"]!.GetValue<string>());
        Assert.Equal("issued", e["status"]!.GetValue<string>());
        Assert.Equal("actor-a", e["actor"]!.GetValue<string>());
        Assert.True(ledger.WasIssued("p-1"));
        Assert.False(ledger.WasIssued("p-unknown"));
    }

    [Fact]
    public void lifecycle_append_is_append_only()
    {
        var ledger = NewLedger();
        ledger.RecordGrant(
            "p-1", "a", "c", "t", "act", "ds", "req");
        ledger.RecordLifecycle("suspend", "p-1", "req",
            new[] { "A10" });
        ledger.RecordLifecycle("revoke", "p-1", "req",
            new[] { "A10" });
        var history = ledger.LoadGrantHistory("p-1");
        Assert.Equal(3, history.Count);
        Assert.Equal("issue", history[0]["operation"]!.GetValue<string>());
        // Parity with Python `operation + "d"`: "suspendd"/"renewd"
        // are the historical ledger spellings.
        Assert.Equal("suspendd",
            history[1]["status"]!.GetValue<string>());
        Assert.Equal("revoked",
            history[2]["status"]!.GetValue<string>());
        Assert.Equal("revoked",
            ledger.CurrentStatus("p-1")!["status"]!.GetValue<string>());
        // prior entry untouched
        Assert.Equal("issued", history[0]["status"]!.GetValue<string>());
    }

    [Fact]
    public void lifecycle_rejects_unknown_operation()
    {
        var ledger = NewLedger();
        Assert.Throws<ArgumentException>(
            () => ledger.RecordLifecycle("delete", "p-1", "req"));
    }

    [Fact]
    public void violation_goes_to_separate_ledger()
    {
        var ledger = NewLedger();
        ledger.RecordViolation(
            "sovereign-x", new JsonObject { ["kind"] = "overreach" },
            "req", new[] { "A436" });
        var violations = ledger.LoadViolations();
        Assert.Single(violations);
        Assert.Equal("violation",
            violations[0]["operation"]!.GetValue<string>());
        Assert.Empty(ledger.LoadGrantHistory("any"));
    }

    [Fact]
    public void corrupt_lines_are_skipped()
    {
        var ledger = NewLedger();
        File.AppendAllText(LedgerPath, "{broken json\n");
        ledger.RecordGrant("p-1", "a", "c", "t", "act", "ds", "req");
        var history = ledger.LoadGrantHistory("p-1");
        Assert.Single(history);
    }

    // ---------- automation sweep ----------

    private static readonly DateTimeOffset T0 =
        DateTimeOffset.Parse("2026-09-29T00:00:00Z");

    private PermissionLifecycleAutomation NewAutomation(
        Func<DateTimeOffset> clock,
        Func<PermissionGrant, CancellationToken, Task<bool>>?
            adjudicator = null,
        bool autoRenew = true, int maxRenewals = 10) =>
        new(adjudicator,
            expiryWarning: TimeSpan.FromDays(7),
            autoRenewEnabled: autoRenew,
            maxRenewals: maxRenewals,
            clock: clock);

    [Fact]
    public async Task expired_grant_renews_via_delegated_adjudication()
    {
        var now = T0;
        var clock = () => now;
        var calls = 0;
        var auto = NewAutomation(clock,
            adjudicator: (g, ct) =>
            {
                calls++;
                return Task.FromResult(true);
            });
        var grant = auto.RegisterGrant(
            "g-1", "actor", "cap", "act", "tgt",
            ttl: TimeSpan.FromDays(1));
        now = T0 + TimeSpan.FromDays(2); // expired
        await auto.RunOnceAsync();
        Assert.Equal(1, calls);
        Assert.Equal(PermissionGrantState.Active, grant.State);
        Assert.Equal(1, grant.RenewalCount);
        Assert.Equal(now + TimeSpan.FromDays(30), grant.ExpiresAt);
    }

    [Fact]
    public async Task expired_grant_fails_closed_when_renewal_refused()
    {
        var now = T0;
        var auto = NewAutomation(() => now,
            adjudicator: (g, ct) => Task.FromResult(false));
        var grant = auto.RegisterGrant(
            "g-1", "actor", "cap", "act", "tgt",
            ttl: TimeSpan.FromDays(1));
        now = T0 + TimeSpan.FromDays(2);
        await auto.RunOnceAsync();
        Assert.Equal(PermissionGrantState.Expired, grant.State);
    }

    [Fact]
    public async Task expired_grant_fails_closed_when_adjudicator_throws()
    {
        var now = T0;
        var auto = NewAutomation(() => now,
            adjudicator: (g, ct) =>
                throw new InvalidOperationException("boom"));
        var grant = auto.RegisterGrant(
            "g-1", "actor", "cap", "act", "tgt",
            ttl: TimeSpan.FromDays(1));
        now = T0 + TimeSpan.FromDays(2);
        await auto.RunOnceAsync();
        Assert.Equal(PermissionGrantState.Expired, grant.State);
    }

    [Fact]
    public async Task expired_grant_expires_without_adjudicator()
    {
        var now = T0;
        var auto = NewAutomation(() => now, adjudicator: null);
        var grant = auto.RegisterGrant(
            "g-1", "actor", "cap", "act", "tgt",
            ttl: TimeSpan.FromDays(1));
        now = T0 + TimeSpan.FromDays(2);
        await auto.RunOnceAsync();
        Assert.Equal(PermissionGrantState.Expired, grant.State);
    }

    [Fact]
    public async Task expired_grant_expires_when_auto_renew_disabled()
    {
        var now = T0;
        var auto = NewAutomation(() => now,
            adjudicator: (g, ct) => Task.FromResult(true),
            autoRenew: false);
        var grant = auto.RegisterGrant(
            "g-1", "actor", "cap", "act", "tgt",
            ttl: TimeSpan.FromDays(1));
        now = T0 + TimeSpan.FromDays(2);
        await auto.RunOnceAsync();
        Assert.Equal(PermissionGrantState.Expired, grant.State);
    }

    [Fact]
    public async Task renewal_cap_stops_renewing()
    {
        var now = T0;
        var calls = 0;
        var auto = NewAutomation(() => now,
            adjudicator: (g, ct) =>
            {
                calls++;
                return Task.FromResult(true);
            },
            maxRenewals: 1);
        var grant = auto.RegisterGrant(
            "g-1", "actor", "cap", "act", "tgt",
            ttl: TimeSpan.FromDays(1));
        now = T0 + TimeSpan.FromDays(2);
        await auto.RunOnceAsync();
        Assert.Equal(1, calls);
        Assert.Equal(PermissionGrantState.Active, grant.State);
        now = T0 + TimeSpan.FromDays(40); // past renewed expiry
        await auto.RunOnceAsync();
        Assert.Equal(1, calls); // no second adjudication
        Assert.Equal(PermissionGrantState.Expired, grant.State);
    }

    [Fact]
    public async Task expiring_soon_marks_expiring_state()
    {
        var now = T0;
        var auto = NewAutomation(() => now);
        var grant = auto.RegisterGrant(
            "g-1", "actor", "cap", "act", "tgt",
            ttl: TimeSpan.FromDays(5));
        await auto.RunOnceAsync(); // 5d < 7d warning window
        Assert.Equal(PermissionGrantState.Expiring, grant.State);
    }

    [Fact]
    public async Task terminal_grants_cleanup_after_30_days()
    {
        var now = T0;
        var auto = NewAutomation(() => now);
        auto.RegisterGrant(
            "g-rev", "a", "c", "act", "tgt");
        auto.RevokeGrant("g-rev", "done");
        await auto.RunOnceAsync();
        Assert.NotNull(auto.GetGrant("g-rev"));
        now = T0 + TimeSpan.FromDays(31);
        await auto.RunOnceAsync();
        Assert.Null(auto.GetGrant("g-rev"));
    }

    [Fact]
    public async Task expired_grants_age_from_expires_at()
    {
        var now = T0;
        var auto = NewAutomation(() => now, adjudicator: null);
        var grant = auto.RegisterGrant(
            "g-exp", "a", "c", "act", "tgt",
            ttl: TimeSpan.FromDays(1));
        now = T0 + TimeSpan.FromDays(2);
        await auto.RunOnceAsync();
        Assert.Equal(PermissionGrantState.Expired, grant.State);
        Assert.NotNull(auto.GetGrant("g-exp"));
        // expires_at was T0+1d; retention removes after +30d
        now = T0 + TimeSpan.FromDays(32);
        await auto.RunOnceAsync();
        Assert.Null(auto.GetGrant("g-exp"));
    }

    [Fact]
    public void revoke_and_suspend_transitions()
    {
        var auto = NewAutomation(() => T0);
        auto.RegisterGrant("g-1", "a", "c", "act", "tgt");
        Assert.True(auto.SuspendGrant("g-1"));
        Assert.Equal(PermissionGrantState.Suspended,
            auto.GetGrant("g-1")!.State);
        Assert.True(auto.RevokeGrant("g-1", "manual"));
        var g = auto.GetGrant("g-1")!;
        Assert.Equal(PermissionGrantState.Revoked, g.State);
        Assert.Equal("manual", g.RevokedReason);
        Assert.False(auto.RevokeGrant("g-missing"));
        Assert.False(auto.SuspendGrant("g-missing"));
    }

    [Theory]
    [InlineData(PermissionStopTrigger.Expiry,
        LifecycleOperation.Terminate)]
    [InlineData(PermissionStopTrigger.IdentityOrRoleRevocation,
        LifecycleOperation.Revoke)]
    [InlineData(PermissionStopTrigger.ScopeLoss,
        LifecycleOperation.Restrict)]
    [InlineData(PermissionStopTrigger.GenerationRaise,
        LifecycleOperation.Suspend)]
    [InlineData(PermissionStopTrigger.SecurityEvent,
        LifecycleOperation.Revoke)]
    [InlineData(PermissionStopTrigger.ContractInvalidation,
        LifecycleOperation.Suspend)]
    [InlineData(PermissionStopTrigger.ControllingBasisRetirement,
        LifecycleOperation.Terminate)]
    public void stop_trigger_maps_to_lifecycle_operation(
        PermissionStopTrigger trigger, LifecycleOperation expected) =>
        Assert.Equal(
            expected,
            PermissionLifecycleAutomation.MapStopTrigger(trigger));

    // ---------- adjudicator ----------

    private PermissionLifecycleAdjudicator NewAdjudicator(
        PermissionGrantLedger ledger, string? gateResult = null) =>
        new(ledger,
            (cap, target, purpose, requester, ct) =>
                Task.FromResult(gateResult));

    [Fact]
    public async Task adjudication_refuses_missing_permission_id()
    {
        var ledger = NewLedger();
        var adj = NewAdjudicator(ledger);
        var outcome = await adj.TerminateAsync("", "req");
        Assert.False(outcome.Accepted);
        Assert.Equal("MISSING_PERMISSION_ID", outcome.RefusalCode);
    }

    [Fact]
    public async Task adjudication_refuses_unissued_permission()
    {
        var ledger = NewLedger();
        var adj = NewAdjudicator(ledger);
        var outcome = await adj.RevokeAsync("p-x", "req");
        Assert.False(outcome.Accepted);
        Assert.Equal("PERMISSION_NOT_ISSUED", outcome.RefusalCode);
    }

    [Fact]
    public async Task terminate_refuses_when_already_terminated()
    {
        var ledger = NewLedger();
        ledger.RecordGrant("p-1", "a", "c", "t", "act", "ds", "req");
        var adj = NewAdjudicator(ledger);
        var first = await adj.TerminateAsync("p-1", "req");
        Assert.True(first.Accepted);
        var second = await adj.TerminateAsync("p-1", "req");
        Assert.False(second.Accepted);
        Assert.Equal("PERMISSION_ALREADY_TERMINATED",
            second.RefusalCode);
    }

    [Fact]
    public async Task review_gate_refusal_blocks_ledger_append()
    {
        var ledger = NewLedger();
        ledger.RecordGrant("p-1", "a", "c", "t", "act", "ds", "req");
        var adj = NewAdjudicator(ledger,
            gateResult: "REVIEW_STALE");
        var outcome = await adj.SuspendAsync("p-1", "req");
        Assert.False(outcome.Accepted);
        Assert.Equal("REVIEW_STALE", outcome.RefusalCode);
        Assert.Single(ledger.LoadGrantHistory("p-1")); // only issue
    }

    [Fact]
    public async Task accepted_adjudication_appends_and_delegates()
    {
        var ledger = NewLedger();
        ledger.RecordGrant("p-1", "a", "c", "t", "act", "ds", "req");
        var adj = NewAdjudicator(ledger);
        var outcome = await adj.RestrictAsync(
            "p-1", "req", new JsonObject { ["scope"] = "narrow" });
        Assert.True(outcome.Accepted);
        Assert.Equal("permission.restrict",
            outcome.Payload!["action"]!.GetValue<string>());
        Assert.Equal("delegated-to-governed-executor",
            outcome.Payload!["execution"]!.GetValue<string>());
        var history = ledger.LoadGrantHistory("p-1");
        Assert.Equal(2, history.Count);
        Assert.Equal("restrict",
            history[1]["operation"]!.GetValue<string>());
        Assert.Equal("narrow",
            history[1]["detail"]!["restrictions"]!["scope"]!
                .GetValue<string>());
    }
}
