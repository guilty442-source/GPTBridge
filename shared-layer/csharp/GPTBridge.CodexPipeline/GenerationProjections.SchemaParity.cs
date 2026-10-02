namespace GPTBridge.CodexPipeline;

internal static partial class GenerationProjections
{
    private static int RebuildSchemaParityStatus(StageConnection connection)
    {
        if (!HasTable(connection, "machine_schema_registry")) return 0;
        if (!HasTable(connection, "machine_schema_parity_evidence"))
            return connection.Execute("UPDATE machine_schema_registry SET parity_status='PENDING' WHERE parity_status IN ('PASS','VERIFIED')").RowCount;
        var version = ReadVersion(connection);
        var registryColumns = Columns(connection, "machine_schema_registry");
        var evidenceColumns = Columns(connection, "machine_schema_parity_evidence");
        Dictionary<string, object?> Record(List<string> columns, object?[] values) =>
            columns.Select((column, index) => (column, values[index]))
                .ToDictionary(pair => pair.column, pair => pair.Item2, StringComparer.Ordinal);
        var evidence = Rows(connection, "machine_schema_parity_evidence")
            .Select(values => Record(evidenceColumns, values))
            .ToLookup(row => row.GetValueOrDefault("schema_code") as string, StringComparer.Ordinal);
        var changed = 0;
        foreach (var values in Rows(connection, "machine_schema_registry"))
        {
            var row = Record(registryColumns, values);
            var code = row.GetValueOrDefault("schema_code") as string;
            var eligible = evidence[code].Any(receipt => SchemaReceiptMatches(row, receipt, version));
            var oldStatus = row.GetValueOrDefault("parity_status") as string;
            // Only verification labels are projections. Other lifecycle states
            // remain owner-controlled when no qualifying receipt exists.
            var next = eligible ? "VERIFIED" : oldStatus is "VERIFIED" or "PASS" ? "PENDING" : oldStatus;
            if (next == oldStatus) continue;
            changed += connection.Execute(
                "UPDATE machine_schema_registry SET parity_status=? WHERE schema_code=?",
                new object?[] { next, code }).RowCount;
        }
        return changed;
    }

    private static bool SchemaReceiptMatches(IReadOnlyDictionary<string, object?> row,
        IReadOnlyDictionary<string, object?> receipt, string version)
    {
        if (string.IsNullOrWhiteSpace(version)
            || !Equals(receipt.GetValueOrDefault("validated_against_version"), version)
            || !Equals(receipt.GetValueOrDefault("status"), "PASS")) return false;
        var descriptorHash = SemanticHashToolchain.ComputeProducerHash(row);
        return new[] { "producer_semantic_hash", "validator_semantic_hash",
            "persistence_semantic_hash", "canonical_semantic_hash" }
            .All(field => Equals(receipt.GetValueOrDefault(field), descriptorHash));
    }
}
