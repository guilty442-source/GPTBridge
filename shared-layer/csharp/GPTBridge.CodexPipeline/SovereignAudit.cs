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
internal static class SovereignAudit
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

    private static List<Dictionary<string, object?>> DivisionPlan(
        string amendmentId) =>
        DivisionAssignments.Select(item =>
            new Dictionary<string, object?>(StringComparer.Ordinal)
            {
                ["amendment_id"] = amendmentId,
                ["work_item"] = item.WorkItem,
                ["owner"] = item.Owner,
                ["released_by"] = "five-sovereign-audit-gate",
            }).ToList();

    /// <summary>Issue the seal-closing certificate from the five audit
    /// receipts — metadata only.</summary>
    public static Dictionary<string, object?> Certificate(
        string amendmentId, IReadOnlyList<Receipt> receipts,
        object? predecessor, string requester = "")
    {
        var payload = new Dictionary<string, object?>(
            StringComparer.Ordinal)
        {
            ["schema"] = CertificateSchema,
            ["amendment_id"] = amendmentId,
            ["verdict"] = "FIVE_SOVEREIGN_AUDIT_PASSED",
            ["requester"] = requester,
            ["predecessor"] = predecessor
                ?? new Dictionary<string, object?>(),
            ["receipts"] = receipts.Select(receipt =>
                (object?)new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["sovereign_id"] = receipt.SovereignId,
                    ["domain"] = receipt.Domain,
                    ["owner_sub_sovereign"] =
                        receipt.OwnerSubSovereign,
                    ["method"] = receipt.Method,
                    ["evidence_hash"] = receipt.EvidenceHash,
                    ["independent_verifier"] =
                        receipt.IndependentVerifier,
                }).ToList(),
        };
        payload["certificate_hash"] = CanonicalHash(payload);
        return payload;
    }

    /// <summary>Xingcheng assistant's deterministic core audit check —
    /// bounded metadata conformance review over the immutable amendment
    /// identity and scope.</summary>
    public static Func<Dictionary<string, object?>>
        BuildXingchengAssistantCoreCheck(
            string amendmentId, IEnumerable<string> scope)
    {
        var normalizedId = (amendmentId ?? "").Trim();
        var normalizedScope = scope
            .Select(item => (item ?? "").Trim())
            .Where(item => item.Length > 0)
            .Distinct(StringComparer.Ordinal)
            .OrderBy(item => item, StringComparer.Ordinal)
            .ToArray();
        return () =>
        {
            var valid = normalizedId.Length > 0
                && normalizedScope.Length > 0;
            return new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["ok"] = valid,
                ["method"] = "xingcheng-assistant-core-audit",
                ["findings"] = new List<object?>
                {
                    valid ? "assistant-core-scope-reviewed"
                        : "assistant-core-scope-missing",
                },
                ["evidence"] = new Dictionary<string, object?>(
                    StringComparer.Ordinal)
                {
                    ["assistant_core_review"] =
                        "deterministic-standard-charter",
                    ["scope_classification"] =
                        normalizedScope.Cast<object?>().ToList(),
                    ["redaction_check"] = "metadata-only",
                },
                ["error"] = valid ? ""
                    : "ASSISTANT_CORE_SCOPE_UNAVAILABLE",
            };
        };
    }

    /// <summary>The five-sovereign audit gate.</summary>
    public sealed class Gate
    {
        private readonly double? _deadline;
        private readonly double? _flowDeadline;
        private readonly string _ledgerPath;

        public Gate(
            double? deadlineSeconds = DefaultAuditDeadlineSeconds,
            double? flowDeadlineSeconds =
                DefaultAuditFlowDeadlineSeconds,
            string? ledgerPath = null)
        {
            _deadline = deadlineSeconds;
            _flowDeadline = flowDeadlineSeconds;
            _ledgerPath = ledgerPath ?? AuditLedgerPath();
        }

        /// <summary>Run the bounded five-sovereign audit for one
        /// amendment.  ``checks`` maps sovereign ids to check
        /// callables returning payload dictionaries.</summary>
        public async Task<AuditResult> AuditAsync(
            string amendmentId,
            IReadOnlyDictionary<string,
                Func<Dictionary<string, object?>>> checks,
            object? predecessor = null,
            string requester = "")
        {
            var started = System.Diagnostics.Stopwatch.StartNew();
            var identifier = (amendmentId ?? "").Trim();
            var rawRequester = (requester ?? "").Trim();
            var requesterId = SovereignAliases.TryGetValue(rawRequester,
                out var alias) ? alias : rawRequester;
            if (requesterId.Length > 0
                && !SovereignIds.Contains(requesterId))
                return Deny(identifier, "UNKNOWN_AMENDMENT_REQUESTER",
                    unknown: new[] { requesterId }, started: started,
                    requester: rawRequester);
            if (identifier.Length == 0)
                return Deny("", "AMENDMENT_ID_REQUIRED",
                    started: started, requester: requesterId);
            var normalized = new Dictionary<string,
                Func<Dictionary<string, object?>>>(StringComparer.Ordinal);
            var unknown = new List<string>();
            foreach (var pair in checks)
            {
                var sovereignId = NormalizeSovereign(pair.Key);
                if (!SovereignIds.Contains(sovereignId)
                    || normalized.ContainsKey(sovereignId))
                {
                    unknown.Add(pair.Key);
                    continue;
                }
                normalized[sovereignId] = pair.Value;
            }
            var missing = SovereignIds
                .Where(id => !normalized.ContainsKey(id)).ToList();
            if (unknown.Count > 0)
                return Deny(identifier, "UNKNOWN_AUDIT_ACTOR",
                    unknown: unknown.ToArray(), started: started,
                    requester: requesterId);
            if (missing.Count > 0)
                return Deny(identifier, "MISSING_SOVEREIGN_AUDIT",
                    missing: missing.ToArray(), started: started,
                    requester: requesterId);

            bool? selfAuditPassed = null;
            List<Receipt> receipts;
            if (requesterId.Length > 0)
            {
                var selfReceipt = await RunOne(requesterId,
                    normalized[requesterId]);
                selfAuditPassed = selfReceipt.Ok;
                if (!selfReceipt.Ok)
                {
                    var denied = new AuditResult
                    {
                        AmendmentId = identifier,
                        Ok = false,
                        Reason = "ORIGINATOR_SELF_AUDIT_FAILED",
                        Receipts = new List<Receipt> { selfReceipt },
                        Failed = new[] { selfReceipt.SovereignId },
                        DurationMs = ElapsedMs(started),
                        BudgetMs = BudgetMs(),
                        Requester = requesterId,
                        RequesterIndependentVerifier =
                            selfReceipt.IndependentVerifier,
                        SelfAuditPassed = false,
                    };
                    denied.AuditRecorded = Record(denied);
                    return denied;
                }
                var remaining = RemainingFlowBudget(started);
                try
                {
                    var tasks = SovereignIds
                        .Where(id => id != requesterId)
                        .Select(id => RunOne(id, normalized[id]))
                        .ToArray();
                    var others = remaining is not null
                        ? await Task.WhenAll(tasks)
                            .WaitAsync(TimeSpan.FromSeconds(
                                remaining.Value))
                        : await Task.WhenAll(tasks);
                    receipts = new List<Receipt> { selfReceipt };
                    receipts.AddRange(others);
                }
                catch (TimeoutException)
                {
                    return DenyFlowDeadline(identifier, started,
                        receipts: new List<Receipt> { selfReceipt },
                        requesterId, selfAuditPassed: true);
                }
            }
            else
            {
                try
                {
                    var tasks = SovereignIds
                        .Select(id => RunOne(id, normalized[id]))
                        .ToArray();
                    var results = _flowDeadline is not null
                        ? await Task.WhenAll(tasks).WaitAsync(
                            TimeSpan.FromSeconds(_flowDeadline.Value))
                        : await Task.WhenAll(tasks);
                    receipts = results.ToList();
                }
                catch (TimeoutException)
                {
                    return DenyFlowDeadline(identifier, started,
                        requester: requesterId);
                }
            }
            if (FlowBudgetExceeded(started))
                return DenyFlowDeadline(identifier, started,
                    receipts: receipts, requesterId,
                    selfAuditPassed);

            var failed = receipts.Where(r => !r.Ok)
                .Select(r => r.SovereignId).ToArray();
            var reason = failed.Length == 0
                ? "ALL_FIVE_SOVEREIGNS_AUDITED" : "SOVEREIGN_AUDIT_FAILED";
            var requesterVerifier = receipts
                .Where(r => r.SovereignId == requesterId)
                .Select(r => r.IndependentVerifier)
                .FirstOrDefault() ?? "";
            var result = new AuditResult
            {
                AmendmentId = identifier,
                Ok = failed.Length == 0,
                Reason = reason,
                Receipts = receipts,
                Failed = failed,
                DivisionPlan = failed.Length == 0
                    ? DivisionPlan(identifier) : null,
                Certificate = failed.Length == 0
                    ? Certificate(identifier, receipts, predecessor,
                        requesterId)
                    : null,
                DurationMs = ElapsedMs(started),
                BudgetMs = BudgetMs(),
                Requester = requesterId,
                RequesterIndependentVerifier = requesterVerifier,
                SelfAuditPassed = selfAuditPassed,
            };
            var recorded = Record(result);
            if (!result.Ok)
                return result;
            if (!recorded)
            {
                result.Ok = false;
                result.Reason = "AUDIT_RECORD_FAILED";
                result.DivisionPlan = null;
                result.AuditRecorded = false;
                return result;
            }
            result.AuditRecorded = true;
            return result;
        }

        private async Task<Receipt> RunOne(string sovereignId,
            Func<Dictionary<string, object?>> check)
        {
            var started = System.Diagnostics.Stopwatch.StartNew();
            Dictionary<string, object?> payload = new();
            var error = "";
            try
            {
                var outcomeTask = Task.Run(check);
                var outcome = _deadline is not null
                    ? await outcomeTask.WaitAsync(
                        TimeSpan.FromSeconds(_deadline.Value))
                    : await outcomeTask;
                payload = outcome;
            }
            catch (TimeoutException)
            {
                error = "TimeoutError";
            }
            catch (Exception exception)
            {
                error = exception.GetType().Name;
            }
            var durationMs = (int)started.ElapsedMilliseconds;

            var spec = SpecById[sovereignId];
            var ok = payload.TryGetValue("ok", out var okv)
                && okv is true && error.Length == 0;
            var method = payload.TryGetValue("method", out var m)
                && m is not null
                    ? m.ToString() ?? "" : $"{sovereignId}-audit";
            var networkSearch = payload.TryGetValue("network_search",
                out var ns) && ns is true;
            var evidenceMap = payload.TryGetValue("evidence",
                    out var ev)
                && ev is IDictionary<string, object?> map
                    ? map
                    : new Dictionary<string, object?>();
            var missingEvidence = spec.RequiredEvidence
                .Where(key => key == "network_search"
                    ? !networkSearch
                    : !Present(evidenceMap.TryGetValue(key, out var v)
                        ? v : null))
                .ToArray();
            if (missingEvidence.Length > 0 && error.Length == 0)
            {
                ok = false;
                error = "AUDIT_EVIDENCE_INCOMPLETE:"
                    + string.Join(",", missingEvidence);
            }
            var findings = (payload.TryGetValue("findings", out var f)
                    && f is IEnumerable<object?> items
                        ? items : Enumerable.Empty<object?>())
                .Take(MaxFindings)
                .Select(item => (item?.ToString() ?? "")[..Math.Min(200,
                    (item?.ToString() ?? "").Length)])
                .ToArray();
            var evidenceHash = (payload.TryGetValue("evidence_hash",
                    out var eh) ? eh?.ToString() ?? "" : "").Trim();
            if (evidenceHash.Length == 0)
                evidenceHash = CanonicalHash(
                    new Dictionary<string, object?>(
                        StringComparer.Ordinal)
                    {
                        ["sovereign_id"] = sovereignId,
                        ["method"] = method,
                        ["findings"] = findings.Cast<object?>().ToList(),
                        ["evidence"] = payload.TryGetValue("evidence",
                            out var rawEv) ? rawEv : null,
                    });
            if (!ok && error.Length == 0)
                error = payload.TryGetValue("error", out var e)
                    && e is not null
                        ? e.ToString() ?? "AUDIT_NOT_PASSED"
                        : "AUDIT_NOT_PASSED";
            var rawVerifier = (payload.TryGetValue(
                    "independent_verifier", out var iv)
                ? iv?.ToString() ?? "" : "").Trim();
            var independentVerifier = SovereignAliases.TryGetValue(
                rawVerifier, out var verAlias)
                ? verAlias : rawVerifier;
            return new Receipt(sovereignId, spec.Domain, ok, method,
                evidenceHash, spec.OwnerSubSovereign, networkSearch,
                durationMs, independentVerifier, findings, error);
        }

        private AuditResult DenyFlowDeadline(string amendmentId,
            System.Diagnostics.Stopwatch started,
            List<Receipt>? receipts = null, string requester = "",
            bool? selfAuditPassed = null)
        {
            receipts ??= new List<Receipt>();
            var result = new AuditResult
            {
                AmendmentId = amendmentId,
                Ok = false,
                Reason = AuditFlowDeadlineReason,
                Receipts = receipts,
                Failed = receipts.Where(r => !r.Ok)
                    .Select(r => r.SovereignId).ToArray(),
                DurationMs = ElapsedMs(started),
                BudgetMs = BudgetMs(),
                Requester = requester,
                SelfAuditPassed = selfAuditPassed,
            };
            result.AuditRecorded = Record(result);
            return result;
        }

        private AuditResult Deny(string amendmentId, string reason,
            string[]? unknown = null, string[]? missing = null,
            System.Diagnostics.Stopwatch? started = null,
            string requester = "")
        {
            var durationMs = started is not null
                ? ElapsedMs(started) : 0;
            var result = new AuditResult
            {
                AmendmentId = amendmentId,
                Ok = false,
                Reason = reason,
                Failed = unknown ?? Array.Empty<string>(),
                Missing = missing ?? Array.Empty<string>(),
                DurationMs = durationMs,
                BudgetMs = BudgetMs(),
                Requester = requester,
            };
            result.AuditRecorded = RecordPayload(
                new Dictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["amendment_id"] = amendmentId,
                    ["ok"] = false,
                    ["reason"] = reason,
                    ["failed"] = (unknown ?? Array.Empty<string>())
                        .Cast<object?>().ToList(),
                    ["missing"] = (missing ?? Array.Empty<string>())
                        .Cast<object?>().ToList(),
                    ["receipts"] = new List<object?>(),
                    ["division_released"] = false,
                    ["duration_ms"] = durationMs,
                    ["budget_ms"] = BudgetMs(),
                    ["requester"] = requester,
                });
            return result;
        }

        private bool Record(AuditResult result) =>
            RecordPayload(result.ToRecord());

        private bool RecordPayload(
            Dictionary<string, object?> payload)
        {
            var entry = new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["timestamp"] = Repo.UtcNow(),
                ["operation"] =
                    "codex-amendment-five-sovereign-audit",
            };
            foreach (var pair in payload)
                entry[pair.Key] = pair.Value;
            try
            {
                Directory.CreateDirectory(
                    Path.GetDirectoryName(_ledgerPath)!);
                // parity: json.dumps default separators (", "/": ")
                File.AppendAllText(_ledgerPath,
                    CanonJson.SerializeSpaced(entry) + "\n",
                    new UTF8Encoding(false));
                return true;
            }
            catch (IOException)
            {
                return false;
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }
        }

        private static int ElapsedMs(
            System.Diagnostics.Stopwatch started) =>
            (int)started.ElapsedMilliseconds;

        private int BudgetMs() =>
            _flowDeadline is null ? 0 : (int)(_flowDeadline * 1000);

        private bool FlowBudgetExceeded(
            System.Diagnostics.Stopwatch started) =>
            _flowDeadline is not null
                && started.Elapsed.TotalSeconds > _flowDeadline;

        private double? RemainingFlowBudget(
            System.Diagnostics.Stopwatch started) =>
            _flowDeadline is null ? null
                : Math.Max(0.0,
                    _flowDeadline.Value - started.Elapsed.TotalSeconds);
    }
}
