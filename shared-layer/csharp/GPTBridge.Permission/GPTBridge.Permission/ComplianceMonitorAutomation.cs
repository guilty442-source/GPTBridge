using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.Permission;

/// <summary>Violation severity ladder — port of the retired
/// ComplianceSeverity enum.</summary>
public enum ComplianceSeverity
{
    Low,
    Medium,
    High,
    Critical,
}

/// <summary>A recorded compliance violation.</summary>
public sealed class ComplianceViolation
{
    public required string ViolationId { get; init; }
    public required ComplianceSeverity Severity { get; init; }
    public required string Actor { get; init; }
    public required string Capability { get; init; }
    public required string Target { get; init; }
    public required string Description { get; init; }
    public required DateTimeOffset DetectedAt { get; init; }
    public required bool AutoResolvable { get; init; }
    public bool Resolved { get; internal set; }
    public DateTimeOffset? ResolvedAt { get; internal set; }
    public string? ResolutionAction { get; internal set; }
}

/// <summary>
/// Native successor of the retired
/// ``permission_automation_compliance.ComplianceMonitor``.
///
/// Each <see cref="RunOnceAsync"/> tick processes newly ingested
/// violation records (deduplicated by canonical fingerprint), runs the
/// directory-integrity checks, checks grant ledger actors against the
/// sealed identity directory (A436 ledger-vs-directory drift), then
/// applies the hourly risk-score decay.
///
/// The host feeds raw violation records through
/// <see cref="IngestViolation(JsonObject)"/> — there is no
/// ``permission_sovereign._compliance_violations`` in the native core,
/// so ingestion is an explicit seam rather than a private field read.
/// Severity keyword tables, scoring weights, decay, the >50
/// high-risk threshold and the ``viol-N`` id scheme are preserved
/// verbatim from the retired implementation.
/// </summary>
public sealed class ComplianceMonitorAutomation
{
    public static readonly TimeSpan DefaultInterval =
        TimeSpan.FromSeconds(60);

    /// <summary>Risk-weight per severity (LOW1/MEDIUM5/HIGH20/
    /// CRITICAL100).</summary>
    public static readonly IReadOnlyDictionary<ComplianceSeverity, double>
        SeverityWeights = new Dictionary<ComplianceSeverity, double>
        {
            [ComplianceSeverity.Low] = 1.0,
            [ComplianceSeverity.Medium] = 5.0,
            [ComplianceSeverity.High] = 20.0,
            [ComplianceSeverity.Critical] = 100.0,
        };

    private readonly string _projectRoot;
    private readonly HashSet<string> _seenViolations = new();
    private readonly Dictionary<string, ComplianceViolation> _violations =
        new();
    private readonly Dictionary<string, double> _riskScores = new();
    private int _violationCounter;

    public ComplianceMonitorAutomation(string projectRoot)
    {
        _projectRoot = projectRoot;
    }

    /// <summary>Issues recorded by the latest tick — warnings the
    /// retired module logged (directory version mismatch, incomplete
    /// identities, integrity failures).</summary>
    public List<string> LatestIssues { get; private set; } = new();

    /// <summary>Queue a raw violation record for the next tick.</summary>
    public void IngestViolation(JsonObject violation)
    {
        _pending.Enqueue(violation);
    }

    private readonly Queue<JsonObject> _pending = new();

    /// <summary>Single compliance check tick.</summary>
    public Task RunOnceAsync(
        PermissionGrantLedger? ledger = null)
    {
        var issues = new List<string>();
        while (_pending.TryDequeue(out var v))
            ProcessViolation(v);
        var snap = RegistrySnapshot.LoadAll(_projectRoot);
        CheckDirectoryIntegrity(snap, issues);
        CheckGrantConsistency(snap, ledger);
        UpdateRiskScores();
        LatestIssues = issues;
        return Task.CompletedTask;
    }

    /// <summary>Process one violation record — dedup by canonical
    /// fingerprint, classify severity, update risk score. Returns the
    /// stored violation or null when already seen.</summary>
    public ComplianceViolation? ProcessViolation(JsonObject violation)
    {
        var fingerprint = violation.ToJsonString(
            new JsonSerializerOptions { WriteIndented = false });
        if (!_seenViolations.Add(fingerprint))
            return null;

        var id = $"viol-{_violationCounter}";
        _violationCounter += 1;
        var severity = AssessSeverity(violation);
        var actor = violation["actor"]?.GetValue<string>() ?? "";
        var cv = new ComplianceViolation
        {
            ViolationId = id,
            Severity = severity,
            Actor = actor,
            Capability =
                violation["capability"]?.GetValue<string>() ?? "",
            Target = violation["target"]?.GetValue<string>() ?? "",
            Description =
                violation["violation"]?.GetValue<string>() ?? "",
            DetectedAt = DateTimeOffset.UtcNow,
            AutoResolvable = severity is ComplianceSeverity.Low
                or ComplianceSeverity.Medium,
        };
        _violations[id] = cv;
        _riskScores.TryGetValue(actor == "" ? "unknown" : actor, out var s);
        _riskScores[actor == "" ? "unknown" : actor] =
            s + SeverityWeights[severity];
        return cv;
    }

    /// <summary>Severity keyword table — CRITICAL: unauthorized/bypass/
    /// escalation/injection; HIGH: expired/revoked/suspended/
    /// unauthorized_access; MEDIUM: mismatch/inconsistency/drift;
    /// else LOW.</summary>
    public static ComplianceSeverity AssessSeverity(JsonObject v)
    {
        var kind = (v["violation"]?.GetValue<string>() ?? "")
            .ToLowerInvariant();
        if (kind.Contains("unauthorized") || kind.Contains("bypass")
            || kind.Contains("escalation") || kind.Contains("injection"))
            return ComplianceSeverity.Critical;
        if (kind.Contains("expired") || kind.Contains("revoked")
            || kind.Contains("suspended")
            || kind.Contains("unauthorized_access"))
            return ComplianceSeverity.High;
        if (kind.Contains("mismatch") || kind.Contains("inconsistency")
            || kind.Contains("drift"))
            return ComplianceSeverity.Medium;
        return ComplianceSeverity.Low;
    }

    private void CheckDirectoryIntegrity(
        RegistrySnapshot snap, List<string> issues)
    {
        if (snap.CodeRuleDirectory is null
            || snap.DirectoryAuthority is null
            || snap.IdentityGroups is null)
        {
            issues.Add("directory-integrity: registries-unavailable");
            return;
        }
        if (snap.InitialCodeVersion != snap.CodePolicyInitialVersion)
            issues.Add(
                $"code-rule-version-mismatch: {snap.InitialCodeVersion}" +
                $" != {snap.CodePolicyInitialVersion}");
        if (snap.AuthorityCurrentVersion != snap.AuthorityInitialVersion)
            issues.Add("authority-version-drifted-from-initial");
        foreach (var kv in snap.IdentityGroups)
        {
            if (!kv.Key.StartsWith(
                    "IDENTITY_GROUP_", StringComparison.Ordinal))
                continue;
            var actor = kv.Value?["actor"]?.GetValue<string>();
            var code = kv.Value?["identity_code"]?.GetValue<string>();
            if (string.IsNullOrEmpty(actor) || string.IsNullOrEmpty(code))
                issues.Add($"incomplete-identity-record: {kv.Key}");
        }
    }

    /// <summary>Ledger-vs-directory drift — a grant whose actor is not
    /// in the sealed identity-permission directory is a MEDIUM
    /// compliance violation (A436).</summary>
    private void CheckGrantConsistency(
        RegistrySnapshot snap, PermissionGrantLedger? ledger)
    {
        if (ledger is null || snap.IdentityPermissions is null)
            return;
        var known = new HashSet<string>(
            snap.IdentityPermissions
                .Select(kv =>
                    kv.Value?["actor"]?.GetValue<string>()
                    ?? kv.Value?["identity_code"]?.GetValue<string>()
                    ?? kv.Key)
                .Where(s => !string.IsNullOrEmpty(s))!);
        var snapPath = Path.Combine(_projectRoot, "runtime", "state",
            "permission-grant-ledger.jsonl");
        var actors = File.Exists(snapPath)
            ? ExtractLedgerActors(snapPath) : new HashSet<string>();
        foreach (var actor in actors.Where(a => !known.Contains(a)))
        {
            var v = new JsonObject
            {
                ["violation"] = "grant-actor-not-in-directory",
                ["actor"] = actor,
                ["capability"] = "",
                ["target"] = "permission-grant-ledger",
            };
            ProcessViolation(v);
        }
    }

    private static HashSet<string> ExtractLedgerActors(string path)
    {
        var actors = new HashSet<string>();
        foreach (var line in File.ReadLines(path))
        {
            if (line.Length == 0) continue;
            try
            {
                if (JsonNode.Parse(line)?["actor"]
                    is JsonValue a)
                    actors.Add(a.GetValue<string>());
            }
            catch (JsonException) { /* malformed line — skip */ }
        }
        return actors;
    }

    /// <summary>Per-tick decay — every actor score ×0.9, dropped below
    /// 0.1 (hourly-10% contract of the retired implementation).</summary>
    private void UpdateRiskScores()
    {
        foreach (var actor in _riskScores.Keys.ToList())
        {
            _riskScores[actor] *= 0.9;
            if (_riskScores[actor] < 0.1)
                _riskScores.Remove(actor);
        }
    }

    /// <summary>Risk report: actors scoring >50, total and unresolved
    /// violation counts.</summary>
    public IReadOnlyDictionary<string, object?> GetRiskReport() =>
        new Dictionary<string, object?>
        {
            ["high_risk_actors"] = _riskScores
                .Where(kv => kv.Value > 50)
                .OrderByDescending(kv => kv.Value)
                .Select(kv => new Dictionary<string, object>
                {
                    ["actor"] = kv.Key, ["score"] = kv.Value,
                })
                .ToList(),
            ["total_violations"] = _violations.Count,
            ["unresolved_violations"] =
                _violations.Values.Count(v => !v.Resolved),
        };

    /// <summary>Mark a violation resolved with an action note.</summary>
    public bool ResolveViolation(string violationId, string action)
    {
        if (!_violations.TryGetValue(violationId, out var v))
            return false;
        v.Resolved = true;
        v.ResolvedAt = DateTimeOffset.UtcNow;
        v.ResolutionAction = action;
        return true;
    }
}
