// Repository.cs — TransformerTrainingRepository port (PostgreSQL lane).
//
// Verbatim port of the retired Python mixins:
//   training_repo_schema.py                — DDL + constants + helpers
//   transformer_training_repository.py     — shell/status/maintain
//   _datasets.py / _jobs.py / _audit.py    — dataset, job, audit surface
//   transformer_adapter_registry.py        — candidate/eval/release lifecycle
//
// Schema target: XINGCHENG_SHARED_PG_SCHEMA (default gptbridge_xingcheng).
// tool_root = <local-model>/xingcheng (same as Python: root/"xingcheng").

using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal sealed class TransformerTrainingRepository
{
    public const int SchemaVersion = 1;
    public const string DatabaseName = "gptbridge_xingcheng";
    public const string BaseModelId = "xingcheng-native-transformer";
    public const string RuntimeModelId = "xingcheng-native-transformer";
    public const string TrainingMethod = "sft-native-full-parameter";

    public static readonly HashSet<string> DatasetStates =
        new(StringComparer.Ordinal) { "prepared", "invalidated", "archived" };

    public static readonly HashSet<string> JobStates =
        new(StringComparer.Ordinal)
        { "queued", "preflight", "training", "validating", "completed", "failed", "cancelled" };

    public static readonly HashSet<string> AdapterStates =
        new(StringComparer.Ordinal)
        { "candidate", "validated", "staged", "active", "rejected", "retired" };

    public static readonly IReadOnlyDictionary<string, HashSet<string>> JobTransitions =
        new Dictionary<string, HashSet<string>>(StringComparer.Ordinal)
        {
            ["queued"] = new() { "preflight", "cancelled", "failed" },
            ["preflight"] = new() { "training", "cancelled", "failed" },
            ["training"] = new() { "validating", "cancelled", "failed" },
            ["validating"] = new() { "completed", "failed" },
            ["completed"] = new(),
            ["failed"] = new(),
            ["cancelled"] = new(),
        };

    private static readonly string[] StatusTables =
    {
        "transformer_training_dataset",
        "transformer_training_dataset_example",
        "transformer_training_job",
        "transformer_adapter_candidate",
        "transformer_adapter_evaluation",
        "transformer_adapter_release",
        "transformer_runtime_model_state",
        "transformer_training_audit_event",
    };

    private const string MigrationScript = """
        CREATE TABLE IF NOT EXISTS transformer_schema_metadata (
            metadata_key TEXT PRIMARY KEY,
            metadata_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transformer_training_dataset (
            dataset_id TEXT PRIMARY KEY,
            content_sha256 TEXT NOT NULL,
            format_version TEXT NOT NULL,
            base_model_id TEXT NOT NULL,
            runtime_model_id TEXT NOT NULL,
            example_count INTEGER NOT NULL CHECK(example_count > 0),
            training_example_count INTEGER NOT NULL
                CHECK(training_example_count > 0),
            validation_example_count INTEGER NOT NULL
                CHECK(validation_example_count > 0),
            minimum_quality_score REAL NOT NULL
                CHECK(minimum_quality_score >= 0.8
                      AND minimum_quality_score <= 1.0),
            source_manifest_json TEXT NOT NULL,
            snapshot_path TEXT NOT NULL,
            snapshot_sha256 TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'prepared'
                CHECK(state IN ('prepared', 'invalidated', 'archived')),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK(example_count =
                  training_example_count + validation_example_count),
            UNIQUE (content_sha256, snapshot_sha256)
        );

        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'transformer_training_dataset_content_sha256_key'
            ) THEN
                ALTER TABLE transformer_training_dataset
                    DROP CONSTRAINT transformer_training_dataset_content_sha256_key;
                ALTER TABLE transformer_training_dataset
                    ADD CONSTRAINT transformer_training_dataset_snapshot_key
                    UNIQUE (content_sha256, snapshot_sha256);
            END IF;
        END $$;

        CREATE TABLE IF NOT EXISTS transformer_training_dataset_example (
            dataset_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
            split TEXT NOT NULL CHECK(split IN ('train', 'validation')),
            owner_model_id TEXT NOT NULL,
            database_scope TEXT NOT NULL
                CHECK(database_scope IN
                      ('main', 'investment', 'mathematical', 'coding')),
            source_example_id TEXT NOT NULL,
            source_revision INTEGER NOT NULL CHECK(source_revision >= 1),
            content_sha256 TEXT NOT NULL,
            source_type TEXT NOT NULL,
            quality_score REAL NOT NULL
                CHECK(quality_score >= 0.8 AND quality_score <= 1.0),
            PRIMARY KEY(dataset_id, ordinal),
            UNIQUE(dataset_id, content_sha256),
            FOREIGN KEY(dataset_id)
                REFERENCES transformer_training_dataset(dataset_id)
                ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_transformer_dataset_example_source
            ON transformer_training_dataset_example(
                owner_model_id, source_revision
            );

        CREATE TABLE IF NOT EXISTS transformer_training_job (
            job_id TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL,
            base_model_id TEXT NOT NULL,
            training_method TEXT NOT NULL,
            configuration_json TEXT NOT NULL,
            configuration_sha256 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK(status IN
                      ('queued', 'preflight', 'training', 'validating',
                       'completed', 'failed', 'cancelled')),
            output_path TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            requested_by TEXT NOT NULL,
            retry_of_job_id TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(dataset_id)
                REFERENCES transformer_training_dataset(dataset_id)
                ON DELETE RESTRICT,
            FOREIGN KEY(retry_of_job_id)
                REFERENCES transformer_training_job(job_id)
                ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_transformer_training_job_status
            ON transformer_training_job(status, created_at DESC);

        CREATE TABLE IF NOT EXISTS transformer_adapter_candidate (
            adapter_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL UNIQUE,
            dataset_id TEXT NOT NULL,
            base_model_id TEXT NOT NULL,
            adapter_format TEXT NOT NULL,
            artifact_path TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL UNIQUE,
            metrics_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate'
                CHECK(status IN
                      ('candidate', 'validated', 'staged', 'active',
                       'rejected', 'retired')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(job_id)
                REFERENCES transformer_training_job(job_id)
                ON DELETE RESTRICT,
            FOREIGN KEY(dataset_id)
                REFERENCES transformer_training_dataset(dataset_id)
                ON DELETE RESTRICT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_transformer_adapter
            ON transformer_adapter_candidate(status)
            WHERE status = 'active';

        CREATE TABLE IF NOT EXISTS transformer_adapter_evaluation (
            evaluation_id TEXT PRIMARY KEY,
            adapter_id TEXT NOT NULL,
            suite_id TEXT NOT NULL,
            suite_sha256 TEXT NOT NULL,
            baseline_metrics_json TEXT NOT NULL,
            adapter_metrics_json TEXT NOT NULL,
            comparison_json TEXT NOT NULL,
            quality_gates_json TEXT NOT NULL,
            passed INTEGER NOT NULL CHECK(passed IN (0, 1)),
            evaluated_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(adapter_id, suite_sha256),
            FOREIGN KEY(adapter_id)
                REFERENCES transformer_adapter_candidate(adapter_id)
                ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS transformer_adapter_release (
            release_id TEXT PRIMARY KEY,
            adapter_id TEXT NOT NULL,
            action TEXT NOT NULL
                CHECK(action IN
                      ('stage', 'activate', 'rollback', 'retire')),
            previous_adapter_id TEXT,
            governed_by TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(adapter_id)
                REFERENCES transformer_adapter_candidate(adapter_id)
                ON DELETE RESTRICT,
            FOREIGN KEY(previous_adapter_id)
                REFERENCES transformer_adapter_candidate(adapter_id)
                ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS transformer_runtime_model_state (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
            base_model_id TEXT NOT NULL,
            runtime_model_id TEXT NOT NULL,
            active_adapter_id TEXT,
            previous_adapter_id TEXT,
            automatic_weight_replacement INTEGER NOT NULL DEFAULT 0
                CHECK(automatic_weight_replacement = 0),
            updated_at TEXT NOT NULL,
            FOREIGN KEY(active_adapter_id)
                REFERENCES transformer_adapter_candidate(adapter_id)
                ON DELETE RESTRICT,
            FOREIGN KEY(previous_adapter_id)
                REFERENCES transformer_adapter_candidate(adapter_id)
                ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS transformer_training_audit_event (
            sequence BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            previous_event_sha256 TEXT NOT NULL,
            event_sha256 TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_transformer_training_audit_entity
            ON transformer_training_audit_event(
                entity_type, entity_id, sequence DESC
            );

        -- sqlite_to_pg migrated rows created ``sequence`` as plain
        -- bigint without identity. Repair idempotently and re-seed
        -- past max(sequence) so the PK never collides.
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'transformer_training_audit_event'
                  AND column_name = 'sequence'
                  AND is_identity = 'NO'
            ) THEN
                ALTER TABLE transformer_training_audit_event
                    ALTER COLUMN sequence
                    ADD GENERATED BY DEFAULT AS IDENTITY;
                PERFORM setval(
                    pg_get_serial_sequence(
                        'transformer_training_audit_event', 'sequence'
                    ),
                    COALESCE(
                        (SELECT max(sequence)
                         FROM transformer_training_audit_event), 0
                    )
                );
            END IF;
        END $$;

        CREATE OR REPLACE FUNCTION transformer_dataset_snapshot_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'TRANSFORMER_DATASET_SNAPSHOT_IMMUTABLE';
        END;
        $$;
        CREATE OR REPLACE FUNCTION transformer_audit_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'TRANSFORMER_TRAINING_AUDIT_IMMUTABLE';
        END;
        $$;
        DROP TRIGGER IF EXISTS protect_transformer_dataset_examples_update
            ON transformer_training_dataset_example;
        CREATE TRIGGER protect_transformer_dataset_examples_update
        BEFORE UPDATE ON transformer_training_dataset_example
        FOR EACH ROW EXECUTE FUNCTION transformer_dataset_snapshot_immutable();
        DROP TRIGGER IF EXISTS protect_transformer_dataset_identity_update
            ON transformer_training_dataset;
        CREATE TRIGGER protect_transformer_dataset_identity_update
        BEFORE UPDATE OF
            dataset_id, content_sha256, format_version, base_model_id,
            runtime_model_id, example_count, training_example_count,
            validation_example_count, minimum_quality_score,
            source_manifest_json, snapshot_path, snapshot_sha256,
            created_by, created_at
        ON transformer_training_dataset
        FOR EACH ROW EXECUTE FUNCTION transformer_dataset_snapshot_immutable();
        DROP TRIGGER IF EXISTS protect_transformer_dataset_examples_delete
            ON transformer_training_dataset_example;
        CREATE TRIGGER protect_transformer_dataset_examples_delete
        BEFORE DELETE ON transformer_training_dataset_example
        FOR EACH ROW EXECUTE FUNCTION transformer_dataset_snapshot_immutable();
        DROP TRIGGER IF EXISTS protect_transformer_audit_update
            ON transformer_training_audit_event;
        CREATE TRIGGER protect_transformer_audit_update
        BEFORE UPDATE ON transformer_training_audit_event
        FOR EACH ROW EXECUTE FUNCTION transformer_audit_immutable();
        DROP TRIGGER IF EXISTS protect_transformer_audit_delete
            ON transformer_training_audit_event;
        CREATE TRIGGER protect_transformer_audit_delete
        BEFORE DELETE ON transformer_training_audit_event
        FOR EACH ROW EXECUTE FUNCTION transformer_audit_immutable();
        """;

    /// <summary>tool_root mirrors Python: root/"xingcheng".</summary>
    public string ToolRoot { get; }
    public string Schema { get; }

    public TransformerTrainingRepository(string toolRoot)
    {
        ToolRoot = Path.GetFullPath(Path.Combine(toolRoot, "xingcheng"));
        Schema = Pg.Schema;
        Migrate();
    }

    // ------------------------------------------------------------ helpers --

    internal static string Now()
        => DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss.ffffff+00:00",
                                    CultureInfo.InvariantCulture);

    internal static string Sha256Text(string value)
        => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value)))
            .ToLowerInvariant();

    internal static string Sha256File(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                                          FileShare.Read, 1024 * 1024);
        return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
    }

    internal static string RequireSha256(string? value, string field)
    {
        string normalized = (value ?? "").Trim().ToLowerInvariant();
        if (normalized.Length != 64 || normalized.Any(c => !Uri.IsHexDigit(c)))
            throw new ArgumentException($"{field} must be a hexadecimal SHA-256 digest");
        return normalized;
    }

    private T InTx<T>(Func<Pg.PgScope, T> body)
    {
        using var db = Pg.Connect(Schema, autocommit: false);
        try
        {
            T result = body(db);
            db.Commit();
            return result;
        }
        catch
        {
            db.Rollback();
            throw;
        }
    }

    private void Migrate()
    {
        InTx(db =>
        {
            db.ExecuteScript(MigrationScript);
            string now = Now();
            db.Execute(
                """
                INSERT INTO transformer_schema_metadata(
                    metadata_key, metadata_value, updated_at
                ) VALUES ('schema_version', $1, $2)
                ON CONFLICT(metadata_key) DO UPDATE SET
                    metadata_value = excluded.metadata_value,
                    updated_at = excluded.updated_at
                """,
                SchemaVersion.ToString(CultureInfo.InvariantCulture), now);
            db.Execute(
                """
                INSERT INTO transformer_runtime_model_state(
                    singleton_id, base_model_id, runtime_model_id,
                    active_adapter_id, previous_adapter_id,
                    automatic_weight_replacement, updated_at
                ) VALUES (1, $1, $2, NULL, NULL, 0, $3)
                ON CONFLICT(singleton_id) DO NOTHING
                """,
                BaseModelId, RuntimeModelId, now);
            db.Execute(
                """
                UPDATE transformer_runtime_model_state
                SET base_model_id = $1, runtime_model_id = $2, updated_at = $3
                WHERE singleton_id = 1 AND active_adapter_id IS NULL
                """,
                BaseModelId, RuntimeModelId, now);
            return 0;
        });
    }

    // ------------------------------------------------------------ datasets --

    private const string DatasetColumns =
        "dataset_id, content_sha256, format_version, base_model_id, " +
        "runtime_model_id, example_count, training_example_count, " +
        "validation_example_count, minimum_quality_score, source_manifest_json, " +
        "snapshot_path, snapshot_sha256, state, created_by, created_at";

    private string ResolveSnapshotFile(string snapshotPath)
    {
        string normalized = (snapshotPath ?? "").Trim();
        if (normalized.Length == 0)
            throw new ArgumentException("snapshot_path is required");
        string candidate = Path.IsPathRooted(normalized)
            ? normalized
            : Path.Combine(ToolRoot, normalized);
        candidate = Path.GetFullPath(candidate);
        if (!candidate.StartsWith(ToolRoot + Path.DirectorySeparatorChar,
                                  StringComparison.Ordinal) &&
            !string.Equals(candidate, ToolRoot, StringComparison.Ordinal))
            throw new UnauthorizedAccessException("TRANSFORMER_TRAINING_SNAPSHOT_SCOPE_DENIED");
        if (!File.Exists(candidate))
            throw new FileNotFoundException("transformer training snapshot does not exist");
        return candidate;
    }

    internal static List<Dictionary<string, object?>> NormalizeDatasetExamples(
        IEnumerable<IReadOnlyDictionary<string, object?>> examples)
    {
        var normalized = new List<Dictionary<string, object?>>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        int ordinal = 0;
        foreach (var example in examples)
        {
            ordinal += 1;
            string split = (Str(example, "split") ?? "").Trim().ToLowerInvariant();
            string scope = (Str(example, "database_scope") ?? "").Trim().ToLowerInvariant();
            string itemHash = RequireSha256(Str(example, "content_sha256"),
                                            "example.content_sha256");
            if (split is not ("train" or "validation"))
                throw new ArgumentException("example.split must be train or validation");
            if (scope is not ("main" or "investment" or "mathematical" or "coding"))
                throw new ArgumentException("example.database_scope is not supported");
            if (!seen.Add(itemHash))
                throw new ArgumentException("duplicate example content hash in dataset");
            double quality = Num(example, "quality_score");
            if (quality < 0.8 || quality > 1.0)
                throw new ArgumentException("example quality must be between 0.8 and 1.0");
            int sourceRevision = Int(example, "source_revision");
            if (sourceRevision < 1)
                throw new ArgumentException("example source_revision must be positive");
            normalized.Add(new Dictionary<string, object?>
            {
                ["ordinal"] = ordinal,
                ["split"] = split,
                ["owner_model_id"] = (Str(example, "owner_model_id") ?? "").Trim(),
                ["database_scope"] = scope,
                ["source_example_id"] = (Str(example, "source_example_id") ?? "").Trim(),
                ["source_revision"] = sourceRevision,
                ["content_sha256"] = itemHash,
                ["source_type"] = (Str(example, "source_type") ?? "").Trim(),
                ["quality_score"] = quality,
            });
        }
        return normalized;
    }

    private static (int train, int validation) DatasetExampleCounts(
        List<Dictionary<string, object?>> normalized)
    {
        int train = normalized.Count(e => (string?)e["split"] == "train");
        int validation = normalized.Count(e => (string?)e["split"] == "validation");
        if (normalized.Count == 0 || train < 1 || validation < 1)
            throw new ArgumentException("dataset requires train and validation examples");
        if (normalized.Any(e =>
                string.IsNullOrEmpty((string?)e["owner_model_id"]) ||
                string.IsNullOrEmpty((string?)e["source_example_id"]) ||
                string.IsNullOrEmpty((string?)e["source_type"])))
            throw new ArgumentException(
                "dataset example ownership and provenance are required");
        return (train, validation);
    }

    public Dictionary<string, object?> CreateDataset(
        string contentSha256,
        string snapshotPath,
        string snapshotSha256,
        IReadOnlyList<IReadOnlyDictionary<string, object?>> examples,
        IReadOnlyDictionary<string, object?> sourceManifest,
        string createdBy = "star-main-native-model",
        string formatVersion = "star-transformer-sft/v1")
    {
        string contentDigest = RequireSha256(contentSha256, "content_sha256");
        string snapshotDigest = RequireSha256(snapshotSha256, "snapshot_sha256");
        string snapshotFile = ResolveSnapshotFile(snapshotPath);
        string actualDigest = Sha256File(snapshotFile);
        if (actualDigest != snapshotDigest)
            throw new ArgumentException("transformer training snapshot SHA-256 mismatch");
        var normalized = NormalizeDatasetExamples(examples);
        var (trainCount, validationCount) = DatasetExampleCounts(normalized);
        // Dataset identity is (content_sha256, snapshot_sha256): identical
        // content re-exported with different bytes (e.g. after a snapshot
        // serialization change) registers a new row instead of colliding
        // with an unrecoverable stale one.
        string datasetId = $"star-transformer-dataset-" +
            Sha256Text(contentDigest + ":" + snapshotDigest)[..24];
        string manifestJson = CanonicalJson.CanonicalDict(sourceManifest);
        string createdAt = Now();

        var (row, inserted) = InTx(db =>
        {
            var existing = db.QueryOne(
                $"SELECT {DatasetColumns} FROM transformer_training_dataset " +
                "WHERE content_sha256 = $1 AND snapshot_sha256 = $2",
                contentDigest, snapshotDigest);
            if (existing != null)
            {
                // Snapshot columns are immutable
                // (TRANSFORMER_DATASET_SNAPSHOT_IMMUTABLE), so a pruned file
                // can only be repaired by restoring identical bytes at the
                // stored path — guaranteed possible since the digest in the
                // row equals the digest of the file just verified.
                string storedPath = (string?)existing["snapshot_path"] ?? "";
                string storedSha = (string?)existing["snapshot_sha256"] ?? "";
                bool usable = storedPath.Length > 0 &&
                              File.Exists(storedPath) &&
                              Sha256File(storedPath) == storedSha;
                if (!usable)
                {
                    string restored = Path.IsPathRooted(storedPath)
                        ? storedPath
                        : Path.Combine(ToolRoot, storedPath);
                    restored = Path.GetFullPath(restored);
                    if (!restored.StartsWith(
                            ToolRoot + Path.DirectorySeparatorChar,
                            StringComparison.Ordinal))
                        throw new UnauthorizedAccessException(
                            "TRANSFORMER_TRAINING_SNAPSHOT_SCOPE_DENIED");
                    string? parentDir = Path.GetDirectoryName(restored);
                    if (parentDir != null && !Directory.Exists(parentDir))
                        Directory.CreateDirectory(parentDir);
                    File.Copy(snapshotFile, restored, overwrite: true);
                    if (Sha256File(restored) != storedSha)
                        throw new InvalidOperationException(
                            "transformer training snapshot restore failed");
                    AppendAudit(db,
                        eventType: "dataset-snapshot-restored",
                        entityType: "training-dataset",
                        entityId: (string)existing["dataset_id"]!,
                        payload: new Dictionary<string, object?>
                        {
                            ["content_sha256"] = contentDigest,
                            ["snapshot_sha256"] = storedSha,
                            ["snapshot_path"] = restored,
                        });
                }
                return (existing, false);
            }
            db.Execute(
                """
                INSERT INTO transformer_training_dataset(
                    dataset_id, content_sha256, format_version, base_model_id,
                    runtime_model_id, example_count, training_example_count,
                    validation_example_count, minimum_quality_score,
                    source_manifest_json, snapshot_path, snapshot_sha256,
                    state, created_by, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                          'prepared', $13, $14)
                """,
                datasetId, contentDigest, formatVersion, BaseModelId,
                RuntimeModelId, normalized.Count, trainCount, validationCount,
                normalized.Min(e => (double)e["quality_score"]!),
                manifestJson, snapshotFile, snapshotDigest,
                string.IsNullOrEmpty(createdBy) ? "star-main-native-model" : createdBy,
                createdAt);
            foreach (var item in normalized)
                db.Execute(
                    """
                    INSERT INTO transformer_training_dataset_example(
                        dataset_id, ordinal, split, owner_model_id,
                        database_scope, source_example_id, source_revision,
                        content_sha256, source_type, quality_score
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    datasetId, item["ordinal"], item["split"],
                    item["owner_model_id"], item["database_scope"],
                    item["source_example_id"], item["source_revision"],
                    item["content_sha256"], item["source_type"],
                    item["quality_score"]);
            AppendAudit(db,
                eventType: "dataset-created",
                entityType: "training-dataset",
                entityId: datasetId,
                payload: new Dictionary<string, object?>
                {
                    ["content_sha256"] = contentDigest,
                    ["snapshot_sha256"] = snapshotDigest,
                    ["example_count"] = normalized.Count,
                    ["training_example_count"] = trainCount,
                    ["validation_example_count"] = validationCount,
                    ["created_by"] = createdBy,
                });
            var row = db.QueryOne(
                $"SELECT {DatasetColumns} FROM transformer_training_dataset " +
                "WHERE dataset_id = $1", datasetId);
            return (row, true);
        });
        if (row == null)
            throw new InvalidOperationException("transformer training dataset was not created");
        row["inserted"] = inserted;
        return row;
    }

    /// <summary>Every registered dataset's snapshot_path — the rows are
    /// immutable, so retention must never prune a referenced file.</summary>
    public List<string> DatasetSnapshotPaths()
    {
        using var db = Pg.Connect(Schema);
        return db.Query(
                "SELECT snapshot_path FROM transformer_training_dataset")
            .Select(r => (string)r["snapshot_path"]!).ToList();
    }

    public Dictionary<string, object?>? DatasetById(string datasetId)
    {
        using var db = Pg.Connect(Schema);
        return db.QueryOne(
            $"SELECT {DatasetColumns} FROM transformer_training_dataset " +
            "WHERE dataset_id = $1", datasetId);
    }

    /// <summary>dataset row + {content_sha256: split} map (executor port).</summary>
    public (Dictionary<string, object?>, Dictionary<string, string>) DatasetAndSplits(
        string datasetId)
    {
        using var db = Pg.Connect(Schema);
        var dataset = db.QueryOne(
            $"SELECT {DatasetColumns} FROM transformer_training_dataset " +
            "WHERE dataset_id = $1", datasetId);
        if (dataset == null)
            return (new Dictionary<string, object?>(), new Dictionary<string, string>());
        var splits = db.Query(
            "SELECT content_sha256, split FROM transformer_training_dataset_example " +
            "WHERE dataset_id = $1", datasetId)
            .ToDictionary(r => (string)r["content_sha256"]!, r => (string)r["split"]!,
                          StringComparer.Ordinal);
        return (dataset, splits);
    }

    // ---------------------------------------------------------------- jobs --

    private const string JobColumns =
        "job_id, dataset_id, base_model_id, training_method, configuration_json, " +
        "configuration_sha256, status, output_path, error_code, error_message, " +
        "requested_by, retry_of_job_id, created_at, started_at, completed_at";

    public Dictionary<string, object?> CreateTrainingJob(
        string datasetId,
        IReadOnlyDictionary<string, object?> configuration,
        string requestedBy = "star-main-native-model",
        string? retryOfJobId = null)
    {
        string normalizedDatasetId = (datasetId ?? "").Trim();
        string configurationJson = CanonicalJson.CanonicalDict(configuration);
        string configurationSha256 = Sha256Text(configurationJson);
        string jobId = $"star-transformer-job-{Guid.NewGuid().ToString("N")[..24]}";
        string createdAt = Now();
        var row = InTx(db =>
        {
            var dataset = db.QueryOne(
                "SELECT state FROM transformer_training_dataset WHERE dataset_id = $1",
                normalizedDatasetId);
            if (dataset == null)
                throw new KeyNotFoundException("transformer training dataset does not exist");
            if ((string?)dataset["state"] != "prepared")
                throw new ArgumentException("transformer training dataset is not prepared");
            db.Execute(
                """
                INSERT INTO transformer_training_job(
                    job_id, dataset_id, base_model_id, training_method,
                    configuration_json, configuration_sha256, status,
                    requested_by, retry_of_job_id, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, 'queued', $7, $8, $9)
                """,
                jobId, normalizedDatasetId, BaseModelId, TrainingMethod,
                configurationJson, configurationSha256,
                string.IsNullOrEmpty(requestedBy) ? "star-main-native-model" : requestedBy,
                string.IsNullOrWhiteSpace(retryOfJobId) ? null : retryOfJobId.Trim(),
                createdAt);
            AppendAudit(db,
                eventType: "training-job-created",
                entityType: "training-job",
                entityId: jobId,
                payload: new Dictionary<string, object?>
                {
                    ["dataset_id"] = normalizedDatasetId,
                    ["configuration_sha256"] = configurationSha256,
                    ["requested_by"] = requestedBy,
                });
            return db.QueryOne(
                $"SELECT {JobColumns} FROM transformer_training_job WHERE job_id = $1",
                jobId);
        });
        if (row == null)
            throw new InvalidOperationException("transformer training job was not created");
        return row;
    }

    public Dictionary<string, object?>? JobRow(string jobId)
    {
        using var db = Pg.Connect(Schema);
        return db.QueryOne(
            $"SELECT {JobColumns} FROM transformer_training_job WHERE job_id = $1",
            jobId);
    }

    public List<Dictionary<string, object?>> QueuedJobs(int limit = 16)
    {
        using var db = Pg.Connect(Schema);
        return db.Query(
            $"SELECT {JobColumns} FROM transformer_training_job " +
            "WHERE status = 'queued' ORDER BY created_at ASC LIMIT $1", limit);
    }

    public Dictionary<string, object?> TransitionTrainingJob(
        string jobId, string status,
        string outputPath = "", string errorCode = "", string errorMessage = "")
    {
        string requested = (status ?? "").Trim().ToLowerInvariant();
        if (!JobStates.Contains(requested))
            throw new ArgumentException("unsupported transformer training job status");
        var updated = InTx(db =>
        {
            var row = db.QueryOne(
                $"SELECT {JobColumns} FROM transformer_training_job WHERE job_id = $1",
                jobId);
            if (row == null)
                throw new KeyNotFoundException("transformer training job does not exist");
            string current = (string)row["status"]!;
            if (!JobTransitions[current].Contains(requested))
                throw new ArgumentException(
                    $"invalid transformer training transition: {current} -> {requested}");
            string now = Now();
            string startedAt = requested == "training"
                ? now : (string)(row["started_at"] ?? "");
            string completedAt = requested is "completed" or "failed" or "cancelled"
                ? now : (string)(row["completed_at"] ?? "");
            string code = errorCode.Length > 96 ? errorCode[..96] : errorCode;
            string message = errorMessage.Length > 1000
                ? errorMessage[..1000] : errorMessage;
            db.Execute(
                """
                UPDATE transformer_training_job
                SET status = $1, output_path = $2, error_code = $3,
                    error_message = $4, started_at = $5, completed_at = $6
                WHERE job_id = $7
                """,
                requested,
                string.IsNullOrEmpty(outputPath) ? row["output_path"] : outputPath,
                code, message, startedAt, completedAt, jobId);
            AppendAudit(db,
                eventType: "training-job-transitioned",
                entityType: "training-job",
                entityId: jobId,
                payload: new Dictionary<string, object?>
                {
                    ["from"] = current,
                    ["to"] = requested,
                    ["error_code"] = code,
                });
            return db.QueryOne(
                $"SELECT {JobColumns} FROM transformer_training_job WHERE job_id = $1",
                jobId);
        });
        if (updated == null)
            throw new InvalidOperationException(
                "transformer training job transition was not stored");
        return updated;
    }

    // ------------------------------------------------------------ adapters --

    private const string CandidateColumns =
        "adapter_id, job_id, dataset_id, base_model_id, adapter_format, " +
        "artifact_path, artifact_sha256, metrics_json, status, created_at, " +
        "updated_at";

    private const string CandidateSelect =
        "SELECT " + CandidateColumns +
        " FROM transformer_adapter_candidate WHERE adapter_id = $1";

    private static readonly HashSet<string> ReleaseActions =
        new(StringComparer.Ordinal) { "stage", "activate", "rollback", "retire" };

    private string ResolveArtifact(string artifactPath)
    {
        string raw = (artifactPath ?? "").Trim();
        string candidate = Path.IsPathRooted(raw)
            ? raw
            : Path.Combine(ToolRoot, raw);
        candidate = Path.GetFullPath(candidate);
        if (!candidate.StartsWith(ToolRoot + Path.DirectorySeparatorChar,
                                  StringComparison.Ordinal) &&
            !string.Equals(candidate, ToolRoot, StringComparison.Ordinal))
            throw new UnauthorizedAccessException("ADAPTER_ARTIFACT_SCOPE_DENIED");
        if (!File.Exists(candidate))
            throw new FileNotFoundException("adapter artifact does not exist");
        return candidate;
    }

    public Dictionary<string, object?> RegisterAdapterCandidate(
        string jobId,
        string artifactPath,
        IReadOnlyDictionary<string, object?> metrics,
        string adapterFormat = "native-checkpoint")
    {
        string artifact = ResolveArtifact(artifactPath);
        string artifactSha256 = Sha256File(artifact);
        string adapterId = $"star-transformer-adapter-{Guid.NewGuid().ToString("N")[..24]}";
        string now = Now();
        var row = InTx(db =>
        {
            var job = db.QueryOne(
                "SELECT job_id, dataset_id, status FROM transformer_training_job " +
                "WHERE job_id = $1", jobId);
            if (job == null)
                throw new KeyNotFoundException("transformer training job does not exist");
            if ((string?)job["status"] != "completed")
                throw new ArgumentException("adapter requires a completed training job");
            db.Execute(
                """
                INSERT INTO transformer_adapter_candidate(
                    adapter_id, job_id, dataset_id, base_model_id,
                    adapter_format, artifact_path, artifact_sha256,
                    metrics_json, status, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'candidate', $9, $10)
                """,
                adapterId, jobId, job["dataset_id"], BaseModelId,
                string.IsNullOrEmpty(adapterFormat) ? "native-checkpoint" : adapterFormat,
                artifact, artifactSha256,
                CanonicalJson.CanonicalDict(metrics), now, now);
            AppendAudit(db,
                eventType: "adapter-candidate-registered",
                entityType: "adapter-candidate",
                entityId: adapterId,
                payload: new Dictionary<string, object?>
                {
                    ["job_id"] = jobId,
                    ["dataset_id"] = job["dataset_id"],
                    ["artifact_sha256"] = artifactSha256,
                    ["adapter_format"] = adapterFormat,
                });
            return db.QueryOne(CandidateSelect, adapterId);
        });
        if (row == null)
            throw new InvalidOperationException("adapter candidate was not registered");
        return row;
    }

    public Dictionary<string, object?> AdapterCandidate(string adapterId)
    {
        using var db = Pg.Connect(Schema);
        var row = db.QueryOne(CandidateSelect, adapterId);
        if (row == null)
            throw new KeyNotFoundException("transformer adapter candidate does not exist");
        return row;
    }

    public Dictionary<string, object?> RecordAdapterEvaluation(
        string adapterId,
        string suiteId,
        IReadOnlyDictionary<string, object?> baselineMetrics,
        IReadOnlyDictionary<string, object?> adapterMetrics,
        IReadOnlyDictionary<string, object?> comparison,
        IReadOnlyDictionary<string, object?> qualityGates,
        bool passed,
        string evaluatedBy = "star-main-native-model",
        string? suiteSha256 = null)
    {
        string suiteDigest = !string.IsNullOrWhiteSpace(suiteSha256)
            ? suiteSha256.Trim()
            : Sha256Text(suiteId);
        string evaluationId = $"star-transformer-eval-{Guid.NewGuid().ToString("N")[..24]}";
        string now = Now();
        var row = InTx(db =>
        {
            var candidate = db.QueryOne(CandidateSelect, adapterId);
            if (candidate == null)
                throw new KeyNotFoundException(
                    "transformer adapter candidate does not exist");
            string status = (string)candidate["status"]!;
            if (status is not ("candidate" or "validated"))
                throw new ArgumentException(
                    $"adapter is not evaluable in status {status}");
            db.Execute(
                """
                INSERT INTO transformer_adapter_evaluation(
                    evaluation_id, adapter_id, suite_id, suite_sha256,
                    baseline_metrics_json, adapter_metrics_json,
                    comparison_json, quality_gates_json, passed,
                    evaluated_by, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                evaluationId, adapterId, suiteId, suiteDigest,
                CanonicalJson.CanonicalDict(baselineMetrics),
                CanonicalJson.CanonicalDict(adapterMetrics),
                CanonicalJson.CanonicalDict(comparison),
                CanonicalJson.CanonicalDict(qualityGates),
                passed ? 1 : 0, evaluatedBy, now);
            string newStatus = passed ? "validated" : status;
            if (newStatus != status)
                db.Execute(
                    "UPDATE transformer_adapter_candidate " +
                    "SET status = $1, updated_at = $2 WHERE adapter_id = $3",
                    newStatus, now, adapterId);
            AppendAudit(db,
                eventType: "adapter-evaluated",
                entityType: "adapter-candidate",
                entityId: adapterId,
                payload: new Dictionary<string, object?>
                {
                    ["evaluation_id"] = evaluationId,
                    ["suite_id"] = suiteId,
                    ["passed"] = passed,
                    ["evaluated_by"] = evaluatedBy,
                    ["status"] = newStatus,
                });
            return db.QueryOne(
                "SELECT evaluation_id, adapter_id, suite_id, suite_sha256, " +
                "baseline_metrics_json, adapter_metrics_json, comparison_json, " +
                "quality_gates_json, passed, evaluated_by, created_at " +
                "FROM transformer_adapter_evaluation WHERE evaluation_id = $1",
                evaluationId);
        });
        return row ?? throw new InvalidOperationException("evaluation was not stored");
    }

    public Dictionary<string, object?> ReleaseAdapter(
        string adapterId, string action,
        string governedBy, string reason)
    {
        string normalized = (action ?? "").Trim().ToLowerInvariant();
        if (!ReleaseActions.Contains(normalized))
            throw new ArgumentException($"unsupported adapter release action: {action}");
        if (string.IsNullOrWhiteSpace(governedBy))
            throw new ArgumentException("governed_by is required");
        if (string.IsNullOrWhiteSpace(reason))
            throw new ArgumentException("release reason is required");
        string releaseId = $"star-transformer-release-{Guid.NewGuid().ToString("N")[..24]}";
        string now = Now();
        var row = InTx(db =>
        {
            var candidate = db.QueryOne(CandidateSelect, adapterId);
            if (candidate == null)
                throw new KeyNotFoundException(
                    "transformer adapter candidate does not exist");
            string status = (string)candidate["status"]!;
            var runtime = db.QueryOne(
                "SELECT active_adapter_id, previous_adapter_id " +
                "FROM transformer_runtime_model_state WHERE singleton_id = 1");
            string active = (string?)runtime?["active_adapter_id"] ?? "";
            string previous = (string?)runtime?["previous_adapter_id"] ?? "";

            string newStatus = status;
            string? newActive = active.Length > 0 ? active : null;
            string? newPrevious = previous.Length > 0 ? previous : null;
            string? previousAdapterId = null;
            switch (normalized)
            {
                case "stage":
                    if (status != "validated")
                        throw new ArgumentException(
                            "only validated adapters can be staged");
                    newStatus = "staged";
                    break;
                case "activate":
                    if (status != "staged")
                        throw new ArgumentException(
                            "only staged adapters can be activated");
                    if (active.Length > 0)
                        db.Execute(
                            "UPDATE transformer_adapter_candidate " +
                            "SET status = 'staged', updated_at = $1 " +
                            "WHERE adapter_id = $2", now, active);
                    previousAdapterId = active.Length > 0 ? active : null;
                    newActive = adapterId;
                    newPrevious = previousAdapterId;
                    newStatus = "active";
                    break;
                case "rollback":
                    if (previous.Length == 0)
                        throw new ArgumentException(
                            "no previous adapter to roll back to");
                    if (active.Length > 0)
                        db.Execute(
                            "UPDATE transformer_adapter_candidate " +
                            "SET status = 'staged', updated_at = $1 " +
                            "WHERE adapter_id = $2", now, active);
                    db.Execute(
                        "UPDATE transformer_adapter_candidate " +
                        "SET status = 'active', updated_at = $1 WHERE adapter_id = $2",
                        now, previous);
                    previousAdapterId = previous;
                    newActive = previous;
                    newPrevious = null;
                    newStatus = "staged";
                    break;
                default: // retire
                    if (status is not ("staged" or "active" or "candidate"))
                        throw new ArgumentException(
                            $"adapter in status {status} cannot be retired");
                    if (status == "active")
                    {
                        if (previous.Length > 0)
                            db.Execute(
                                "UPDATE transformer_adapter_candidate " +
                                "SET status = 'active', updated_at = $1 " +
                                "WHERE adapter_id = $2", now, previous);
                        previousAdapterId = previous.Length > 0 ? previous : null;
                        newActive = previousAdapterId;
                        newPrevious = null;
                    }
                    newStatus = "retired";
                    break;
            }
            db.Execute(
                "UPDATE transformer_adapter_candidate " +
                "SET status = $1, updated_at = $2 WHERE adapter_id = $3",
                newStatus, now, adapterId);
            if (normalized is "activate" or "rollback" or "retire")
                db.Execute(
                    """
                    UPDATE transformer_runtime_model_state
                    SET active_adapter_id = $1, previous_adapter_id = $2,
                        updated_at = $3
                    WHERE singleton_id = 1
                    """,
                    newActive, newPrevious, now);
            db.Execute(
                """
                INSERT INTO transformer_adapter_release(
                    release_id, adapter_id, action, previous_adapter_id,
                    governed_by, reason, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                releaseId, adapterId, normalized, previousAdapterId,
                governedBy, reason, now);
            AppendAudit(db,
                eventType: "adapter-released",
                entityType: "adapter-candidate",
                entityId: adapterId,
                payload: new Dictionary<string, object?>
                {
                    ["release_id"] = releaseId,
                    ["action"] = normalized,
                    ["previous_adapter_id"] = previousAdapterId,
                    ["governed_by"] = governedBy,
                    ["from_status"] = status,
                    ["to_status"] = newStatus,
                });
            return db.QueryOne(CandidateSelect, adapterId);
        });
        if (row == null)
            throw new InvalidOperationException("adapter release was not stored");
        row["runtime_state"] = RuntimeModelState();
        return row;
    }

    public Dictionary<string, object?> RejectAdapter(
        string adapterId, string governedBy, string reason)
    {
        if (string.IsNullOrWhiteSpace(governedBy) || string.IsNullOrWhiteSpace(reason))
            throw new ArgumentException("governed_by and reason are required");
        string now = Now();
        var row = InTx(db =>
        {
            var candidate = db.QueryOne(CandidateSelect, adapterId);
            if (candidate == null)
                throw new KeyNotFoundException(
                    "transformer adapter candidate does not exist");
            string status = (string)candidate["status"]!;
            if (status is not ("candidate" or "validated"))
                throw new ArgumentException(
                    $"adapter in status {status} cannot be rejected");
            db.Execute(
                "UPDATE transformer_adapter_candidate " +
                "SET status = 'rejected', updated_at = $1 WHERE adapter_id = $2",
                now, adapterId);
            AppendAudit(db,
                eventType: "adapter-rejected",
                entityType: "adapter-candidate",
                entityId: adapterId,
                payload: new Dictionary<string, object?>
                {
                    ["governed_by"] = governedBy,
                    ["reason"] = reason,
                });
            return db.QueryOne(CandidateSelect, adapterId);
        });
        if (row == null)
            throw new InvalidOperationException("adapter rejection was not stored");
        return row;
    }

    public Dictionary<string, object?> RuntimeModelState()
    {
        using var db = Pg.Connect(Schema);
        return db.QueryOne(
            "SELECT singleton_id, base_model_id, runtime_model_id, " +
            "active_adapter_id, previous_adapter_id, " +
            "automatic_weight_replacement, updated_at " +
            "FROM transformer_runtime_model_state WHERE singleton_id = 1")
            ?? new Dictionary<string, object?>();
    }

    // ---------------------------------------------------------------- audit --

    private string AuditEventBody(
        string eventId, string eventType, string entityType, string entityId,
        JsonElement payload, string previousSha256, string createdAt)
    {
        var body = new Dictionary<string, object?>
        {
            ["event_id"] = eventId,
            ["event_type"] = eventType,
            ["entity_type"] = entityType,
            ["entity_id"] = entityId,
            ["payload"] = payload,
            ["previous_event_sha256"] = previousSha256,
            ["created_at"] = createdAt,
        };
        return CanonicalJson.CanonicalDict(body);
    }

    internal Dictionary<string, object?> AppendAudit(
        Pg.PgScope db,
        string eventType, string entityType, string entityId,
        IReadOnlyDictionary<string, object?> payload)
    {
        string eventId = $"star-transformer-audit-{Guid.NewGuid().ToString("N")[..24]}";
        string createdAt = Now();
        var previous = db.QueryOne(
            "SELECT event_sha256 FROM transformer_training_audit_event " +
            "ORDER BY sequence DESC LIMIT 1");
        string previousSha256 = (string?)previous?["event_sha256"]
                                ?? new string('0', 64);
        string payloadJson = CanonicalJson.CanonicalDict(payload);
        using var doc = JsonDocument.Parse(payloadJson);
        string eventBody = AuditEventBody(
            eventId, eventType, entityType, entityId,
            doc.RootElement, previousSha256, createdAt);
        string eventSha256 = Sha256Text(eventBody);
        db.Execute(
            """
            INSERT INTO transformer_training_audit_event(
                event_id, event_type, entity_type, entity_id, payload_json,
                previous_event_sha256, event_sha256, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            eventId, eventType, entityType, entityId, payloadJson,
            previousSha256, eventSha256, createdAt);
        return new Dictionary<string, object?>
        {
            ["event_id"] = eventId,
            ["event_sha256"] = eventSha256,
            ["previous_event_sha256"] = previousSha256,
            ["created_at"] = createdAt,
        };
    }

    public Dictionary<string, object?> VerifyAuditChain()
    {
        List<Dictionary<string, object?>> rows;
        using (var db = Pg.Connect(Schema))
            rows = db.Query(
                "SELECT event_id, event_type, entity_type, entity_id, " +
                "payload_json, previous_event_sha256, event_sha256, created_at " +
                "FROM transformer_training_audit_event ORDER BY sequence ASC");
        string expectedPrevious = new string('0', 64);
        for (int index = 0; index < rows.Count; index++)
        {
            var row = rows[index];
            try
            {
                using var doc = JsonDocument.Parse((string)row["payload_json"]!);
                JsonElement payload = doc.RootElement;
                string eventBody = AuditEventBody(
                    (string)row["event_id"]!, (string)row["event_type"]!,
                    (string)row["entity_type"]!, (string)row["entity_id"]!,
                    payload, (string)row["previous_event_sha256"]!,
                    (string)row["created_at"]!);
                if ((string)row["previous_event_sha256"]! != expectedPrevious ||
                    Sha256Text(eventBody) != (string)row["event_sha256"]!)
                {
                    return new Dictionary<string, object?>
                    {
                        ["ok"] = false,
                        ["event_count"] = rows.Count,
                        ["failed_sequence"] = index + 1,
                        ["reason"] = "audit-chain-mismatch",
                    };
                }
            }
            catch (JsonException)
            {
                return new Dictionary<string, object?>
                {
                    ["ok"] = false,
                    ["event_count"] = rows.Count,
                    ["failed_sequence"] = index + 1,
                    ["reason"] = "invalid-payload-json",
                };
            }
            expectedPrevious = (string)row["event_sha256"]!;
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["event_count"] = rows.Count,
            ["head_sha256"] = expectedPrevious,
        };
    }

    // --------------------------------------------------------------- status --

    public Dictionary<string, object?> DatabaseStatus()
    {
        using var db = Pg.Connect(Schema);
        string integrity = "ok";
        var versionRow = db.QueryOne(
            "SELECT metadata_value FROM transformer_schema_metadata " +
            "WHERE metadata_key = 'schema_version'");
        int userVersion = versionRow != null
            ? int.Parse((string)versionRow["metadata_value"]!, CultureInfo.InvariantCulture)
            : 0;
        var sizeRow = db.QueryOne(
            "SELECT COALESCE(SUM(pg_total_relation_size(" +
            "quote_ident(table_schema) || '.' || quote_ident(table_name))), 0) " +
            "FROM information_schema.tables WHERE table_schema = current_schema()");
        long sizeBytes = sizeRow != null ? Convert.ToInt64(sizeRow["coalesce"]) : 0;
        var tables = StatusTables.ToDictionary(
            t => t,
            t => Convert.ToInt64(db.QueryOne($"SELECT COUNT(*) FROM \"{t}\"")!["count"]));
        var state = db.QueryOne(
            "SELECT singleton_id, base_model_id, runtime_model_id, " +
            "active_adapter_id, previous_adapter_id, " +
            "automatic_weight_replacement, updated_at " +
            "FROM transformer_runtime_model_state WHERE singleton_id = 1");
        var audit = VerifyAuditChain();
        return new Dictionary<string, object?>
        {
            ["ok"] = integrity == "ok" && (bool)audit["ok"]!,
            ["engine"] = "postgresql",
            ["canonical_central_engine"] = "postgresql",
            ["canonical"] = false,
            ["authority"] = "module-private-postgresql",
            ["reconciliation_required"] = false,
            ["schema"] = "star-transformer-training-database/v1",
            ["schema_version"] = userVersion,
            ["path"] = $"postgresql:{Schema}",
            ["size_bytes"] = sizeBytes,
            ["engine_integrity"] = integrity,
            ["tables"] = tables,
            ["audit_chain"] = audit,
            ["runtime_model_state"] = state ?? new Dictionary<string, object?>(),
            ["base_weights_immutable"] = true,
            ["automatic_weight_replacement"] = false,
            ["role_database_ownership_preserved"] = true,
        };
    }

    public Dictionary<string, object?> Maintain()
    {
        var before = DatabaseStatus();
        InTx(db =>
        {
            db.Execute("ANALYZE");
            var last = db.QueryOne(
                "SELECT created_at FROM transformer_training_audit_event " +
                "WHERE event_type = 'database-maintained' " +
                "ORDER BY sequence DESC LIMIT 1");
            bool shouldRecord = last == null;
            if (last != null)
            {
                if (DateTimeOffset.TryParse((string)last["created_at"]!, out var at))
                    shouldRecord = (DateTimeOffset.UtcNow - at).TotalSeconds >= 86400;
                else
                    shouldRecord = true;
            }
            if (shouldRecord || (bool)before["ok"]! != true)
                AppendAudit(db,
                    eventType: "database-maintained",
                    entityType: "training-database",
                    entityId: DatabaseName,
                    payload: new Dictionary<string, object?>
                    {
                        ["schema_version"] = SchemaVersion,
                        ["integrity_before"] = before["engine_integrity"],
                        ["audit_chain_before"] =
                            ((Dictionary<string, object?>)before["audit_chain"]!)["ok"],
                    });
            return 0;
        });
        return DatabaseStatus();
    }

    // ------------------------------------------------------ value helpers --

    internal static string? Str(IReadOnlyDictionary<string, object?> map, string key)
        => map.TryGetValue(key, out object? v) ? v as string ?? v?.ToString() : null;

    internal static double Num(IReadOnlyDictionary<string, object?> map, string key)
    {
        if (!map.TryGetValue(key, out object? v) || v == null) return 0.0;
        return v switch
        {
            double d => d,
            float f => f,
            int i => i,
            long l => l,
            decimal m => (double)m,
            _ => double.TryParse(v.ToString(), NumberStyles.Float,
                                 CultureInfo.InvariantCulture, out double d) ? d : 0.0,
        };
    }

    internal static int Int(IReadOnlyDictionary<string, object?> map, string key)
    {
        if (!map.TryGetValue(key, out object? v) || v == null) return 0;
        return v switch
        {
            int i => i,
            long l => (int)l,
            double d => (int)d,
            _ => int.TryParse(v.ToString(), NumberStyles.Integer,
                              CultureInfo.InvariantCulture, out int i) ? i : 0,
        };
    }

    internal static bool Truthy(object? value) => value switch
    {
        bool b => b,
        int i => i != 0,
        long l => l != 0,
        double d => d != 0.0,
        string s => s.Length > 0 &&
                    s != "0" && !s.Equals("false", StringComparison.OrdinalIgnoreCase),
        _ => false,
    };
}
