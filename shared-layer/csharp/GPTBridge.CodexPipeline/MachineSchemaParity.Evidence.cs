namespace GPTBridge.CodexPipeline;

internal static partial class MachineSchemaParity
{
    private const string EvidenceColumns =
        "p.canonical_semantic_hash, p.status AS evidence_status, "
        + "p.validated_against_version AS evidence_generation, "
        + "p.producer_semantic_hash AS evidence_producer_hash, "
        + "p.validator_semantic_hash AS evidence_validator_hash, "
        + "p.persistence_semantic_hash AS evidence_persistence_hash";

    private static void AssertGeneration(IReadOnlyDictionary<string, object?> before,
        IReadOnlyDictionary<string, object?> after)
    {
        if (before.GetValueOrDefault("source_sha256") is not string hash
            || hash.Length != 64
            || hash.Any(character => character is not (>= '0' and <= '9') and not (>= 'a' and <= 'f')))
            throw new InvalidOperationException("BLOCKED_GENERATION_DRIFT");
        foreach (var field in new[] { "codex_version", "source_sha256" })
            if (before.GetValueOrDefault(field) is not string value
                || string.IsNullOrWhiteSpace(value)
                || !Equals(value, after.GetValueOrDefault(field)))
                throw new InvalidOperationException("BLOCKED_GENERATION_DRIFT");
    }

    private static Dictionary<string, object?> EvaluateEvidence(
        IReadOnlyDictionary<string, object?> row, string generation)
    {
        var result = SemanticHashToolchain.EvaluateRow(row,
            row.GetValueOrDefault("canonical_semantic_hash") as string,
            row.GetValueOrDefault("content_hash") as string);
        var reasons = new List<string>();
        var registryStatus = row.GetValueOrDefault("parity_status") as string;
        if (registryStatus is not ("PASS" or "VERIFIED"))
            reasons.Add("REGISTRY_PARITY_NOT_VERIFIED");
        if (!Equals(row.GetValueOrDefault("evidence_status"), "PASS"))
            reasons.Add("EVIDENCE_NOT_PASS");
        if (string.IsNullOrWhiteSpace(generation)
            || !Equals(row.GetValueOrDefault("evidence_generation"), generation))
            reasons.Add("CURRENT_GENERATION_EVIDENCE_MISSING");
        var producer = (string)result["producer_semantic_hash"]!;
        foreach (var field in new[] { "canonical_semantic_hash", "evidence_producer_hash",
            "evidence_validator_hash", "evidence_persistence_hash" })
            if (!Equals(row.GetValueOrDefault(field), producer))
                reasons.Add("EVIDENCE_HASH_MISMATCH:" + field);
        result["evidence_generation"] = row.GetValueOrDefault("evidence_generation");
        result["current_evidence_status"] = reasons.Count == 0 ? "PASS" : "INCOMPLETE_EVIDENCE";
        result["current_evidence_reasons"] = reasons;
        return result;
    }
}
