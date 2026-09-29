using System.Text.Json.Nodes;
using GPTBridge.Permission;

namespace GPTBridge.Permission.Tests;

/// <summary>
/// Parity tests for the C# ports of the remaining retired permission
/// automation duties (B167/B38): DirectorySyncManager,
/// ComplianceMonitor, SelfHealingManager, AuditScheduler and
/// IdentityGroupManager.
/// </summary>
public sealed class PermissionResidualTests : IDisposable
{
    private readonly string _dir;

    public PermissionResidualTests()
    {
        _dir = Path.Combine(
            Path.GetTempPath(),
            "gptbridge-permres-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(_dir);
        WriteRegistries(_dir);
    }

    public void Dispose()
    {
        try { Directory.Delete(_dir, recursive: true); }
        catch (IOException) { }
    }

    // ---------- managed-registry fixtures (PyLit $kw shape) ----------

    private static void WriteRegistries(string root)
    {
        var gov = Path.Combine(root, "governance_rule");
        var perm = Path.Combine(gov, "permission_directory");
        var regs = Path.Combine(perm, "registries", "permissions");
        Directory.CreateDirectory(regs);
        File.WriteAllText(
            Path.Combine(gov, "code_rule_directory.json"),
            """{"CODE_RULE_DIRECTORY": {"$call": "CodeRuleDirectory", "$kw": {"initial_code_version": "1.00000"}}}""");
        File.WriteAllText(
            Path.Combine(perm, "directory_authority.json"),
            """{"AUTHORITY_VERSION_POLICY": {"$call": "AuthorityVersionPolicy", "$kw": {"current_version": 1, "initial_version": 1}}, "CODE_VERSION_POLICY": {"$call": "CodeVersionPolicy", "$kw": {"initial_version": "1.00000"}}}""");
        File.WriteAllText(
            Path.Combine(regs, "identity_groups.json"),
            """{"IDENTITY_GROUP_MAIN_SYSTEM": {"actor": "main-system", "identity_code": "IDENTITY_GROUP_MAIN_SYSTEM"}, "IDENTITY_GROUP_GOVERNANCE_RULE": {"actor": "governance-rule", "identity_code": "IDENTITY_GROUP_GOVERNANCE_RULE"}}""");
        File.WriteAllText(
            Path.Combine(regs, "identity_permissions.json"),
            """{"IDENTITY_GROUP_MAIN_SYSTEM": {"actor": "main-system"}}""");
        File.WriteAllText(
            Path.Combine(regs, "capability_boundaries.json"), "{}");
        File.WriteAllText(
            Path.Combine(gov, "governance_policy.json"), "{}");
    }

    private string AuthPath => Path.Combine(
        _dir, "governance_rule", "permission_directory",
        "directory_authority.json");

    // ---------- DirectorySyncAutomation ----------

    [Fact]
    public async Task sync_first_tick_primes_hash_without_event()
    {
        var sync = new DirectorySyncAutomation(_dir);
        Assert.False(await sync.RunOnceAsync());
        Assert.False(await sync.RunOnceAsync());
        var status = sync.GetSyncStatus();
        Assert.NotNull(status["last_sync_hash"]);
        Assert.Equal(300.0, status["sync_interval"]);
    }

    [Fact]
    public async Task sync_detects_authority_version_drift()
    {
        var sync = new DirectorySyncAutomation(_dir);
        await sync.RunOnceAsync();
        File.WriteAllText(AuthPath,
            """{"AUTHORITY_VERSION_POLICY": {"$call": "AuthorityVersionPolicy", "$kw": {"current_version": 2, "initial_version": 1}}, "CODE_VERSION_POLICY": {"$call": "CodeVersionPolicy", "$kw": {"initial_version": "1.00000"}}}""");
        Assert.True(await sync.RunOnceAsync());
        Assert.False(await sync.RunOnceAsync());
    }

    [Fact]
    public async Task sync_detects_identity_count_change()
    {
        var sync = new DirectorySyncAutomation(_dir);
        await sync.RunOnceAsync();
        var regs = Path.Combine(_dir, "governance_rule",
            "permission_directory", "registries", "permissions",
            "identity_groups.json");
        File.WriteAllText(regs,
            """{"IDENTITY_GROUP_MAIN_SYSTEM": {"actor": "main-system"}, "IDENTITY_GROUP_NEW": {"actor": "new"}, "IDENTITY_GROUP_EXTRA": {"actor": "extra"}}""");
        Assert.True(await sync.RunOnceAsync());
    }

    // ---------- ComplianceMonitorAutomation ----------

    [Fact]
    public void violation_dedup_and_id_sequence()
    {
        var mon = new ComplianceMonitorAutomation(_dir);
        var v = new JsonObject
        {
            ["violation"] = "unauthorized_access",
            ["actor"] = "rogue", ["capability"] = "read",
            ["target"] = "ledger",
        };
        var first = mon.ProcessViolation(v);
        Assert.NotNull(first);
        Assert.Equal("viol-0", first!.ViolationId);
        Assert.Null(mon.ProcessViolation(v));
        var second = mon.ProcessViolation(new JsonObject
        {
            ["violation"] = "mismatch", ["actor"] = "a",
        });
        Assert.Equal("viol-1", second!.ViolationId);
    }

    [Fact]
    public void severity_keyword_table_matches_retired_weights()
    {
        Assert.Equal(ComplianceSeverity.Critical,
            ComplianceMonitorAutomation.AssessSeverity(new JsonObject
            { ["violation"] = "bypass detected" }));
        Assert.Equal(ComplianceSeverity.Critical,
            ComplianceMonitorAutomation.AssessSeverity(new JsonObject
            { ["violation"] = "unauthorized escalation" }));
        Assert.Equal(ComplianceSeverity.High,
            ComplianceMonitorAutomation.AssessSeverity(new JsonObject
            { ["violation"] = "expired grant used" }));
        Assert.Equal(ComplianceSeverity.Medium,
            ComplianceMonitorAutomation.AssessSeverity(new JsonObject
            { ["violation"] = "mismatch found" }));
        Assert.Equal(ComplianceSeverity.Low,
            ComplianceMonitorAutomation.AssessSeverity(new JsonObject
            { ["violation"] = "note" }));
    }

    [Fact]
    public async Task risk_scores_decay_and_high_risk_report()
    {
        var mon = new ComplianceMonitorAutomation(_dir);
        mon.ProcessViolation(new JsonObject
        {
            ["violation"] = "injection attempt", ["actor"] = "bad",
        });
        var report = mon.GetRiskReport();
        Assert.Equal(1, (int)report["total_violations"]!);
        Assert.Equal(1, (int)report["unresolved_violations"]!);
        var high = (List<Dictionary<string, object>>)
            report["high_risk_actors"]!;
        Assert.Single(high);
        Assert.Equal("bad", high[0]["actor"]);
        Assert.Equal(100.0, high[0]["score"]);
        await mon.RunOnceAsync();
        var decayed = mon.GetRiskReport();
        var high2 = (List<Dictionary<string, object>>)
            decayed["high_risk_actors"]!;
        Assert.Single(high2);
        Assert.Equal(90.0, (double)high2[0]["score"]);
    }

    [Fact]
    public void auto_resolvable_only_low_and_medium()
    {
        var mon = new ComplianceMonitorAutomation(_dir);
        Assert.True(mon.ProcessViolation(new JsonObject
        { ["violation"] = "drift" })!.AutoResolvable);
        Assert.True(mon.ProcessViolation(new JsonObject
        { ["violation"] = "other" })!.AutoResolvable);
        Assert.False(mon.ProcessViolation(new JsonObject
        { ["violation"] = "revoked use" })!.AutoResolvable);
        Assert.False(mon.ProcessViolation(new JsonObject
        { ["violation"] = "escalation" })!.AutoResolvable);
    }

    [Fact]
    public async Task directory_integrity_flags_version_mismatch()
    {
        File.WriteAllText(AuthPath,
            """{"AUTHORITY_VERSION_POLICY": {"$call": "AuthorityVersionPolicy", "$kw": {"current_version": 1, "initial_version": 1}}, "CODE_VERSION_POLICY": {"$call": "CodeVersionPolicy", "$kw": {"initial_version": "9.99999"}}}""");
        var mon = new ComplianceMonitorAutomation(_dir);
        await mon.RunOnceAsync();
        Assert.Contains(mon.LatestIssues,
            i => i.StartsWith("code-rule-version-mismatch:"));
    }

    [Fact]
    public async Task grant_consistency_flags_unknown_actor()
    {
        var stateDir = Path.Combine(_dir, "runtime", "state");
        Directory.CreateDirectory(stateDir);
        File.WriteAllText(Path.Combine(
            stateDir, "permission-grant-ledger.jsonl"),
            """{"actor": "phantom", "capability": "x"}""" + "\n" +
            """{"actor": "main-system", "capability": "x"}""" + "\n");
        var mon = new ComplianceMonitorAutomation(_dir);
        await mon.RunOnceAsync(
            new PermissionGrantLedger(
                Path.Combine(stateDir, "permission-grant-ledger.jsonl"),
                Path.Combine(stateDir, "violations.jsonl")));
        var report = mon.GetRiskReport();
        Assert.Equal(1, (int)report["total_violations"]!);
    }

    [Fact]
    public void resolve_violation_marks_record()
    {
        var mon = new ComplianceMonitorAutomation(_dir);
        var v = mon.ProcessViolation(new JsonObject
        { ["violation"] = "note", ["actor"] = "a" })!;
        Assert.True(mon.ResolveViolation(v.ViolationId, "fixed"));
        Assert.False(mon.ResolveViolation("viol-99", "x"));
        var report = mon.GetRiskReport();
        Assert.Equal(0, (int)report["unresolved_violations"]!);
    }

    // ---------- SelfHealingAutomation ----------

    [Fact]
    public async Task healthy_system_reports_no_issues()
    {
        var heal = new SelfHealingAutomation(
            _dir,
            governanceConnected: () => true,
            sovereignHealthy: () => true);
        var issues = await heal.RunOnceAsync();
        Assert.Empty(issues);
        Assert.False(heal.IsDegraded());
    }

    [Fact]
    public async Task failed_checks_report_and_repair_delegates_fire()
    {
        var repaired = new List<HealingIssue>();
        var heal = new SelfHealingAutomation(
            _dir,
            governanceConnected: () => false,
            sovereignHealthy: () => false);
        heal.RegisterRepair(HealingIssue.GovernanceConnection,
            ct => { repaired.Add(HealingIssue.GovernanceConnection);
                    return Task.CompletedTask; });
        heal.RegisterRepair(HealingIssue.SovereignState,
            ct => { repaired.Add(HealingIssue.SovereignState);
                    return Task.CompletedTask; });
        var issues = await heal.RunOnceAsync();
        Assert.Contains(HealingIssue.GovernanceConnection, issues);
        Assert.Contains(HealingIssue.SovereignState, issues);
        Assert.Equal(2, repaired.Count);
        Assert.True(heal.IsDegraded());
    }

    [Fact]
    public async Task missing_registries_flag_directory_checks()
    {
        var empty = Path.Combine(_dir, "empty-root");
        Directory.CreateDirectory(empty);
        var heal = new SelfHealingAutomation(
            empty, () => true, () => true);
        var issues = await heal.RunOnceAsync();
        Assert.Contains(HealingIssue.DirectoryAccess, issues);
        Assert.Contains(HealingIssue.DirectoryPermissions, issues);
        Assert.True(heal.IsDegraded());
    }

    // ---------- AuditSchedulerAutomation ----------

    [Fact]
    public async Task missing_engine_is_fail_closed_record()
    {
        var sched = new AuditSchedulerAutomation(
            _dir, enginePath: Path.Combine(_dir, "no-such.exe"));
        var rec = await sched.RunOnceAsync();
        Assert.False(rec.Passed);
        Assert.StartsWith("audit-engine-not-found:", rec.Error);
        Assert.Single(sched.GetAuditHistory());
    }

    [Fact]
    public async Task engine_exit_code_and_pass_marker_decide_passed()
    {
        var ps = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.System),
            "WindowsPowerShell", "v1.0", "powershell.exe");
        Assert.True(File.Exists(ps));
        var ok = new AuditSchedulerAutomation(
            _dir, enginePath: ps,
            argumentsOverride: () =>
                "-NoProfile -Command \"Write-Output '[PASS] x'\"");
        var rec = await ok.RunOnceAsync();
        Assert.True(rec.Passed);
        Assert.Equal(0, rec.ReturnCode);
        Assert.Contains("[PASS]", rec.Output);

        var bad = new AuditSchedulerAutomation(
            _dir, enginePath: ps,
            argumentsOverride: () => "-NoProfile -Command \"exit 1\"");
        var fail = await bad.RunOnceAsync();
        Assert.False(fail.Passed);
        Assert.Equal(1, fail.ReturnCode);
    }

    [Fact]
    public async Task bounded_timeout_kills_hung_engine()
    {
        var ps = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.System),
            "WindowsPowerShell", "v1.0", "powershell.exe");
        var sched = new AuditSchedulerAutomation(
            _dir, enginePath: ps,
            argumentsOverride: () =>
                "-NoProfile -Command \"Start-Sleep -Seconds 30\"",
            auditTimeout: TimeSpan.FromSeconds(2));
        var rec = await sched.RunOnceAsync();
        Assert.False(rec.Passed);
        Assert.Equal("timeout", rec.Error);
    }

    [Fact]
    public async Task audit_history_is_bounded_and_ordered()
    {
        var sched = new AuditSchedulerAutomation(
            _dir, enginePath: Path.Combine(_dir, "no-such.exe"));
        for (var i = 0; i < 3; i++)
            await sched.RunOnceAsync();
        var hist = sched.GetAuditHistory(2);
        Assert.Equal(2, hist.Count);
        Assert.Single(sched.GetAuditHistory(50).Skip(2));
    }

    // ---------- IdentityGroupAutomation ----------

    [Fact]
    public void register_and_unregister_lifecycle()
    {
        var mgr = new IdentityGroupAutomation(_dir);
        Assert.True(mgr.RegisterGroup(
            "IDENTITY_GROUP_MAIN_SYSTEM", "main-system",
            new[] { "read" }, "main-system"));
        Assert.False(mgr.RegisterGroup(
            "IDENTITY_GROUP_MAIN_SYSTEM", "dup",
            Array.Empty<string>(), "x"));
        Assert.False(mgr.RegisterGroup(
            "", "a", Array.Empty<string>(), "x"));
        Assert.False(mgr.RegisterGroup(
            "g", "", Array.Empty<string>(), "x"));
        var rec = mgr.GetGroupStatus("IDENTITY_GROUP_MAIN_SYSTEM")!;
        Assert.True(rec.DirectoryRegistered);
        Assert.True(mgr.UnregisterGroup("IDENTITY_GROUP_MAIN_SYSTEM"));
        Assert.False(rec.Active);
        Assert.False(mgr.UnregisterGroup("missing"));
    }

    [Fact]
    public void reconcile_reports_both_drift_directions()
    {
        var mgr = new IdentityGroupAutomation(_dir);
        mgr.RegisterGroup("IDENTITY_GROUP_MAIN_SYSTEM", "main-system",
            Array.Empty<string>(), "main-system");
        mgr.RegisterGroup("local-only-group", "lone",
            Array.Empty<string>(), "x");
        var report = mgr.ReconcileWithDirectory();
        Assert.True((bool)report["ok"]!);
        Assert.Equal(new[] { "IDENTITY_GROUP_GOVERNANCE_RULE" },
            (List<string>)report["missing_locally"]!);
        Assert.Equal(new[] { "local-only-group" },
            (List<string>)report["unregistered_in_directory"]!);
        Assert.Equal(2, (int)report["local_active"]!);
    }

    [Fact]
    public void reconcile_fail_closed_without_directory()
    {
        var empty = Path.Combine(_dir, "no-registry");
        Directory.CreateDirectory(empty);
        var mgr = new IdentityGroupAutomation(empty);
        var report = mgr.ReconcileWithDirectory();
        Assert.False((bool)report["ok"]!);
        Assert.Equal("directory-unavailable", report["error"]);
    }

    [Fact]
    public async Task conflicts_detected_and_newest_kept()
    {
        var mgr = new IdentityGroupAutomation(_dir);
        mgr.RegisterGroup("g1", "actor-x",
            Array.Empty<string>(), "t");
        await Task.Delay(15);
        mgr.RegisterGroup("g2", "actor-x",
            Array.Empty<string>(), "t");
        var conflicts = mgr.DetectConflicts();
        Assert.Single(conflicts);
        Assert.Equal("duplicate_actor", conflicts[0]["type"]);
        var resolved = mgr.ResolveConflicts();
        Assert.Single(resolved);
        Assert.Equal("g1", resolved[0]["group_id"]);
        var active = mgr.ListActiveGroups();
        Assert.Single(active);
        Assert.Equal("g2", active[0].GroupId);
    }
}
