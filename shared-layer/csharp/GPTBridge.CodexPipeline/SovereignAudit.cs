using System.Security.Cryptography;
using System.Text;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Automated Codex update — five-sovereign audit gate (A537; active) —
/// direct port of codex_amendment_audit_gate.py.
///
/// The gate is fail-closed and bounded: unknown actors are rejected,
/// every sovereign domain must submit exactly one receipt, required
/// evidence must be present (A446), the whole flow carries the governor's
/// hard 30-second deadline, and a result that cannot be recorded is never
/// a pass.  The gate never writes the Codex, never signs and never
/// publishes.
/// </summary>
internal static partial class SovereignAudit
{
    public const int MaxFindings = 20;
    public const double DefaultAuditDeadlineSeconds = 30.0;
    public const double DefaultAuditFlowDeadlineSeconds = 30.0;
    public const double AmendmentTestSuiteBudgetSeconds = 20.0;
    public const string AuditFlowDeadlineReason =
        "AUDIT_FLOW_DEADLINE_EXCEEDED";
    public const string CertificateSchema =
        "gptbridge.codex-amendment-certificate/v1";

    public static string AuditLedgerPath() =>
        Path.Combine(Repo.Root(), "governance_rule", "execution",
            "audit", "codex_amendment_audit.jsonl");

    public sealed record Spec(
        string SovereignId,
        string Domain,
        string OwnerSubSovereign,
        string[] Duties,
        string[] RequiredEvidence,
        string[] Forbidden,
        string[] PostAuditDuties)
    {
        public Dictionary<string, object?> ToRecord() =>
            new(StringComparer.Ordinal)
            {
                ["sovereign_id"] = SovereignId,
                ["domain"] = Domain,
                ["owner_sub_sovereign"] = OwnerSubSovereign,
                ["duties"] = Duties.Cast<object?>().ToList(),
                ["required_evidence"] =
                    RequiredEvidence.Cast<object?>().ToList(),
                ["forbidden"] = Forbidden.Cast<object?>().ToList(),
                ["post_audit_duties"] =
                    PostAuditDuties.Cast<object?>().ToList(),
            };
    }

    public static readonly Spec[] SovereignSpecs =
    {
        new("decision-sovereign",
            "policy-precedence-and-decision-basis",
            "decision-sovereign",
            new[]
            {
                "verify the amendment change class and required review level",
                "verify no conflicting active successor in the same scope",
                "verify the decision basis references exist and are current",
            },
            new[]
            {
                "amendment_class", "basis_references",
                "successor_scope_unique",
            },
            new[]
            {
                "implicit precedence",
                "selecting among ambiguous successors",
            },
            new[]
            {
                "monitor precedence and successor uniqueness until publication",
                "re-derive the decision basis for the successor generation",
            }),
        new("permission-sovereign",
            "directory-identity-and-access-lifecycle",
            "permission-sovereign",
            new[]
            {
                "verify directory rows, identity format and ownership",
                "verify lifecycle/effective parity and retirement markers",
                "verify test-flow and module registry references",
            },
            new[]
            {
                "directory_rows", "identity_lifecycle_parity",
                "testflow_references",
            },
            new[]
            {
                "self-registering domain truth",
                "inventing domain truth",
            },
            new[]
            {
                "apply the directory/identity/test-flow updates",
                "re-certify the permission sovereign after publication",
            }),
        new("system-runtime-sovereign",
            "runtime-continuity-and-reader-generation",
            "system-runtime-sovereign",
            new[]
            {
                "verify the amendment causes no restart, stop or disconnect",
                "verify old readers drain on the prior generation and new readers use the published generation",
                "verify channel continuity and the health window",
            },
            new[]
            {
                "reader_generation_plan", "channel_continuity",
                "health_window",
            },
            new[]
            {
                "forced reload of incompatible consumers",
                "mixed-generation reads",
            },
            new[]
            {
                "enforce the generation fence and reader drain during publication",
                "re-anchor authority and verify runtime continuity afterwards",
            }),
        new("automation-sovereign",
            "staging-seal-mirror-and-version-mechanics",
            "automation-sovereign",
            new[]
            {
                "verify staged generation isolation and non-authoritative status",
                "verify seal roots recomputation per SEAL_CANONICAL_V1",
                "verify mirror part chain and assembled payload hash",
                "verify version identity increment and the rollback pointer",
            },
            new[]
            {
                "staging_isolation", "seal_roots", "mirror_chain",
                "version_identity", "rollback_pointer",
            },
            new[]
            {
                "publishing partial generations",
                "computing roots over mixed generations",
            },
            new[]
            {
                "execute the automated staging/normalize/validate pipeline",
                "close the seal and issue the five-sovereign audit certificate",
            }),
        new("xingcheng",
            "xingcheng-assistant-core-audit",
            "xingcheng-assistant",
            new[]
            {
                "review the staged amendment under the Xingcheng assistant core charter",
                "classify the amendment scope and confirm metadata-only redaction",
                "verify the assistant audit remains inside the governed audit boundary",
            },
            new[]
            {
                "assistant_core_review", "scope_classification",
                "redaction_check",
            },
            new[]
            {
                "direct sockets or HTTP clients",
                "raw confidential content in audit evidence",
                "external evidence asserted without a governed source",
            },
            new[]
            {
                "re-review assistant-domain scope when the candidate changes before publication",
                "re-verify metadata-only evidence after publication",
            }),
    };

    public static readonly string[] SovereignIds =
        SovereignSpecs.Select(s => s.SovereignId).ToArray();

    private static readonly Dictionary<string, Spec> SpecById =
        SovereignSpecs.ToDictionary(s => s.SovereignId,
            StringComparer.Ordinal);

    public static readonly Dictionary<string, string> SovereignAliases =
        new(StringComparer.Ordinal)
        {
            ["星澄"] = "xingcheng",
            ["runtime-sovereign"] = "system-runtime-sovereign",
            ["synchronization-sovereign"] = "automation-sovereign",
        };

    public static readonly (string WorkItem, string Owner)[]
        DivisionAssignments =
        {
            ("staging-normalize-validate",
                "release-update-sync-sub-sovereign"),
            ("seal-mirror-and-successor-manifest",
                "release-update-sync-sub-sovereign"),
            ("directory-identity-and-testflow-update",
                "directory-sub-sovereign"),
            ("permission-recertification", "permission-sovereign"),
            ("runtime-reader-generation-and-reanchor",
                "runtime-state-sync-sub-sovereign"),
            ("xingcheng-assistant-core-review", "xingcheng-assistant"),
            ("seal-closure-and-certificate-issuance",
                "automation-sovereign"),
            ("audit-publication",
                "automatic-log-sync-sub-sovereign"),
        };

    private static string NormalizeSovereign(object? sovereignId)
    {
        var value = (sovereignId?.ToString() ?? "").Trim();
        return SovereignAliases.TryGetValue(value, out var alias)
            ? alias : value;
    }

    /// <summary>The gate's ``_canonical_hash`` — spaced separators, NOT
    /// the compact canonical_json convention.</summary>
    internal static string CanonicalHash(object? payload) =>
        Convert.ToHexString(SHA256.HashData(
            Encoding.UTF8.GetBytes(
                CanonJson.SerializeSpaced(payload))))
            .ToLowerInvariant();

    private static bool Present(object? value) => value switch
    {
        null => false,
        bool b => b,
        string s => s.Length > 0,
        byte[] bytes => bytes.Length > 0,
        System.Collections.ICollection c => c.Count > 0,
        _ => true,
    };

    public sealed record Receipt(
        string SovereignId,
        string Domain,
        bool Ok,
        string Method,
        string EvidenceHash,
        string OwnerSubSovereign = "",
        bool NetworkSearch = false,
        int DurationMs = 0,
        string IndependentVerifier = "",
        string[]? Findings = null,
        string Error = "")
    {
        public Dictionary<string, object?> ToRecord()
        {
            var record = new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["sovereign_id"] = SovereignId,
                ["domain"] = Domain,
                ["owner_sub_sovereign"] = OwnerSubSovereign,
                ["ok"] = Ok,
                ["method"] = Method,
                ["evidence_hash"] = EvidenceHash,
                ["network_search"] = NetworkSearch,
                ["duration_ms"] = DurationMs,
            };
            if (IndependentVerifier.Length > 0)
                record["independent_verifier"] = IndependentVerifier;
            if (Findings is { Length: > 0 })
                record["findings"] = Findings.Cast<object?>().ToList();
            if (Error.Length > 0)
                record["error"] = Error;
            return record;
        }
    }

    public sealed class AuditResult
    {
        public string AmendmentId = "";
        public bool Ok;
        public string Reason = "";
        public List<Receipt> Receipts = new();
        public string[] Failed = Array.Empty<string>();
        public string[] Missing = Array.Empty<string>();
        public List<Dictionary<string, object?>>? DivisionPlan;
        public Dictionary<string, object?>? Certificate;
        public bool AuditRecorded;
        public int DurationMs;
        public int BudgetMs;
        public string Requester = "";
        public string RequesterIndependentVerifier = "";
        public bool? SelfAuditPassed;

        public Dictionary<string, object?> ToRecord()
        {
            var record = new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["amendment_id"] = AmendmentId,
                ["ok"] = Ok,
                ["reason"] = Reason,
                ["failed"] = Failed.Cast<object?>().ToList(),
                ["missing"] = Missing.Cast<object?>().ToList(),
                ["audit_recorded"] = AuditRecorded,
                ["duration_ms"] = DurationMs,
                ["budget_ms"] = BudgetMs,
                ["requester"] = Requester,
                ["division_released"] = DivisionPlan is not null,
                ["certificate_issued"] = Certificate is not null,
                ["charters"] = SovereignSpecs
                    .Select(s => (object?)s.ToRecord()).ToList(),
                ["receipts"] = Receipts
                    .Select(r => (object?)r.ToRecord()).ToList(),
            };
            if (RequesterIndependentVerifier.Length > 0)
                record["requester_independent_verifier"] =
                    RequesterIndependentVerifier;
            if (SelfAuditPassed is not null)
                record["self_audit_passed"] = SelfAuditPassed;
            if (Certificate is not null)
                record["certificate"] = Certificate;
            if (DivisionPlan is not null)
                record["division_plan"] =
                    DivisionPlan.Cast<object?>().ToList();
            return record;
        }
    }

}
