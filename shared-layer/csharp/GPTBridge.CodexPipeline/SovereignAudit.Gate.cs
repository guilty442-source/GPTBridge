using System.Security.Cryptography;
using System.Text;

namespace GPTBridge.CodexPipeline;

internal static partial class SovereignAudit
{
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
