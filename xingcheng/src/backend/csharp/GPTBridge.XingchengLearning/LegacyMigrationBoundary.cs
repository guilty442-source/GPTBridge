namespace GPTBridge.XingchengLearning;

// Historical PostgreSQL sources are retained in LegacyMigration and cannot
// execute in the production binary. No connection, bootstrap or client library.
internal static class MetadataMigration
{
    public static Dictionary<string, object?> Backfill(TransformerTrainingRepository repository) =>
        RuntimeStateBackfill.Run(Path.GetDirectoryName(repository.ToolRoot)!);
}
internal static class MetadataParityCheck
{
    public const string ReportFormat = "star-metadata-parity-report/v1";
    public static Dictionary<string, object?> Run(TransformerTrainingRepository repository) =>
        new() { ["format"] = ReportFormat, ["ok"] = false, ["status"] = "LEGACY_MIGRATION_ONLY" };
}
