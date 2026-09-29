using System.Security.Cryptography;
using System.Text;

namespace GPTBridge.CodexPipeline;

internal static partial class SovereignAudit
{
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
}
