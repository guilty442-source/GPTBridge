// Repository.cs — TransformerTrainingRepository (xstore native lane).
//
// Verbatim port of the retired Python mixins:
//   training_repo_schema.py                — DDL + constants + helpers
//   transformer_training_repository.py     — shell/status/maintain
//   _datasets.py / _jobs.py / _audit.py    — dataset, job, audit surface
//   transformer_adapter_registry.py        — candidate/eval/release lifecycle
//
// Metadata authority (§45-§51): Rust xstore is the sole persistent
// authority for structured Xingcheng metadata. PostgreSQL is retired —
// no DSN, no client, no fallback path exists in this process. Every
// operation routes through NativeMetadataClient over the xstore typed
// contract; C# never reads store files directly (§29-§31).
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

    /// <summary>tool_root mirrors Python: root/"xingcheng".</summary>
    public string ToolRoot { get; }

    public TransformerTrainingRepository(string toolRoot)
    {
        ToolRoot = Path.GetFullPath(Path.Combine(toolRoot, "xingcheng"));
        _repoRoot = toolRoot;
        if (MetadataAuthority() != "xstore")
            throw new InvalidOperationException("XSTORE_AUTHORITY_REQUIRED");
        var verification = Meta().Verify();
        if (!JTruth(verification, "ok") || !JTruth(verification, "schema_identity_ok")
            || !JTruth(verification, "invariants_ok"))
            throw new InvalidOperationException("XSTORE_RECOVERY_REQUIRED");
        // xstore is the only metadata authority — the constructor never
        // touches an external database. The runtime singleton is
        // idempotently ensured on the native plane.
        Meta().InitRuntimeState();
    }

    // ------------------------------------------------- authority plane --

    private readonly string _repoRoot;
    private NativeMetadataClient? _metaPrimary;

    /// <summary>The native metadata plane client — the storage backend
    /// for every operation in this repository.</summary>
    internal NativeMetadataClient Meta()
        => _metaPrimary ??= new NativeMetadataClient(
            _repoRoot, actor: "xingcheng-metadata");

    /// <summary>Metadata authority is xstore by construction — the
    /// committed authority-transition marker is the receipt of the
    /// governed migration, not a runtime routing switch.</summary>
    public string MetadataAuthority() =>
        GPTBridge.XingchengLearning.MetadataAuthority.LatestTransition(Meta())
            ?.GetValueOrDefault("new_authority")?.ToString() ?? "postgresql";

    /// <summary>JsonElement truthiness for xstore verb reports.</summary>
    private static bool JTruth(JsonElement el, string key)
        => el.TryGetProperty(key, out var v) &&
           v.ValueKind == JsonValueKind.True;

    /// <summary>Map xstore domain errors onto the exception vocabulary
    /// PG-era callers already handle.</summary>
    private static Exception TranslateMetadataError(MetadataError e)
        => e.Code switch
        {
            "META_RECORD_MISSING" => new KeyNotFoundException(e.Message),
            // Domain rejections map to the ArgumentException contract
            // PG-era callers handle; infrastructure failures
            // (XSTORE_UNAVAILABLE/TIMEOUT/SPAWN/OUTPUT_INVALID) stay
            // MetadataError so ops can distinguish plane-down from
            // request-invalid.
            _ when e.Code.StartsWith("META_", StringComparison.Ordinal) ||
                   e.Code.StartsWith("XSTORE_IMMUTABLE") ||
                   e.Code.StartsWith("XSTORE_INVALID_") ||
                   e.Code.StartsWith("XSTORE_REVISION_") ||
                   e.Code.StartsWith("XSTORE_OP_DENIED") ||
                   e.Code.StartsWith("XSTORE_TYPE_") ||
                   e.Code.StartsWith("XSTORE_TX_")
                => new ArgumentException(e.Message),
            _ => e,
        };

    /// <summary>Migration-lane health for gates and DatabaseStatus:
    /// the shadow/dual-write era is over — xstore IS the authority.</summary>
    public Dictionary<string, object?> ShadowStatus()
        => new()
        {
            ["format"] = "star-metadata-shadow-status/v1",
            ["phase"] = "D-native",
            ["authority"] = "xstore",
            ["shadow_mode"] = false,
            ["dual_write"] = false,
            ["read_source"] = "xstore",
            ["healthy"] = true,
            ["status"] = "authority",
        };

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

    // ------------------------------------------------------------ datasets --

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
        DatasetExampleCounts(normalized);
        // Dataset identity is (content_sha256, snapshot_sha256): identical
        // content re-exported with different bytes (e.g. after a snapshot
        // serialization change) registers a new row instead of colliding
        // with an unrecoverable stale one.
        string datasetId = $"star-transformer-dataset-" +
            Sha256Text(contentDigest + ":" + snapshotDigest)[..24];
        string manifestJson = CanonicalJson.CanonicalDict(sourceManifest);

        // Identical identity and validation, committed on the native
        // plane. The snapshot-restore contract is preserved — a
        // re-registered dataset whose stored snapshot file was pruned
        // still restores identical bytes.
        try
        {
            var existing = Meta().Get(NativeMetadataClient.Types.Dataset,
                                      datasetId);
            if (existing != null)
            {
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
                    bool repointed = false;
                    if (!restored.StartsWith(
                            ToolRoot + Path.DirectorySeparatorChar,
                            StringComparison.Ordinal))
                    {
                        // The recorded path belongs to a foreign root
                        // (the row was written by a sibling worktree or a
                        // retired layout). Dataset identity is
                        // content-bound, so repoint the canonical record
                        // at this run's verified in-scope snapshot rather
                        // than writing outside the tool root.
                        restored = snapshotFile;
                        repointed = true;
                    }
                    if (!string.Equals(
                            restored, snapshotFile,
                            StringComparison.Ordinal))
                    {
                        string? parentDir = Path.GetDirectoryName(restored);
                        if (parentDir != null && !Directory.Exists(parentDir))
                            Directory.CreateDirectory(parentDir);
                        File.Copy(snapshotFile, restored, overwrite: true);
                        if (Sha256File(restored) != storedSha)
                            throw new InvalidOperationException(
                                "transformer training snapshot restore failed");
                    }
                    existing["snapshot_path"] = restored;
                    if (repointed)
                    {
                        var repoint = new Dictionary<string, object?>(
                            existing, StringComparer.Ordinal);
                        repoint.Remove("revision");
                        repoint.Remove("event_hash");
                        repoint["record_id"] = datasetId;
                        long rev = existing["revision"] is long rl
                            ? rl : Convert.ToInt64(existing["revision"]);
                        Meta().PutRecord(
                            NativeMetadataClient.Types.Dataset, repoint,
                            expectedRevision: rev);
                    }
                    Meta().AuditEvent(
                        "dataset-snapshot-restored", "training-dataset",
                        datasetId, new Dictionary<string, object?>
                        {
                            ["content_sha256"] = contentDigest,
                            ["snapshot_sha256"] = storedSha,
                            ["snapshot_path"] = restored,
                            ["repointed_from"] =
                                repointed ? storedPath : "",
                        });
                }
                existing["inserted"] = false;
                return existing;
            }
            Meta().CreateDataset(
                contentDigest, snapshotFile, snapshotDigest, normalized,
                manifestJson,
                string.IsNullOrEmpty(createdBy)
                    ? "star-main-native-model" : createdBy,
                formatVersion, datasetId: datasetId);
            var row = Meta().Get(NativeMetadataClient.Types.Dataset, datasetId)
                ?? throw new InvalidOperationException(
                    "transformer training dataset was not created");
            row["inserted"] = true;
            return row;
        }
        catch (MetadataError e)
        {
            throw TranslateMetadataError(e);
        }
    }

    /// <summary>Every registered dataset's snapshot_path — the rows are
    /// immutable, so retention must never prune a referenced file.</summary>
    public List<string> DatasetSnapshotPaths()
        => Meta().Query(NativeMetadataClient.Types.Dataset, null, 4096)
            .Select(r => (string)r["snapshot_path"]!).ToList();

    public Dictionary<string, object?>? DatasetById(string datasetId)
        => Meta().Get(NativeMetadataClient.Types.Dataset, datasetId);

    /// <summary>dataset row + {content_sha256: split} map (executor port).</summary>
    public (Dictionary<string, object?>, Dictionary<string, string>) DatasetAndSplits(
        string datasetId)
    {
        var ds = Meta().Get(NativeMetadataClient.Types.Dataset, datasetId);
        if (ds == null)
            return (new Dictionary<string, object?>(),
                    new Dictionary<string, string>());
        var splitMap = Meta().Query(
            NativeMetadataClient.Types.DatasetExample,
            new Dictionary<string, object?> { ["dataset_id"] = datasetId },
            500000)
            .ToDictionary(r => (string)r["content_sha256"]!,
                          r => (string)r["split"]!,
                          StringComparer.Ordinal);
        return (ds, splitMap);
    }

    // ---------------------------------------------------------------- jobs --

    public Dictionary<string, object?> CreateTrainingJob(
        string datasetId,
        IReadOnlyDictionary<string, object?> configuration,
        string requestedBy = "star-main-native-model",
        string? retryOfJobId = null)
    {
        string normalizedDatasetId = (datasetId ?? "").Trim();
        string configurationJson = CanonicalJson.CanonicalDict(configuration);
        string jobId = $"star-transformer-job-{Guid.NewGuid().ToString("N")[..24]}";
        try
        {
            Meta().CreateTrainingJob(normalizedDatasetId,
                configurationJson,
                string.IsNullOrEmpty(requestedBy)
                    ? "star-main-native-model" : requestedBy,
                string.IsNullOrWhiteSpace(retryOfJobId)
                    ? null : retryOfJobId.Trim(),
                jobId: jobId);
            return Meta().GetTrainingJob(jobId)
                ?? throw new InvalidOperationException(
                    "transformer training job was not created");
        }
        catch (MetadataError e) { throw TranslateMetadataError(e); }
    }

    public Dictionary<string, object?>? JobRow(string jobId)
        => Meta().GetTrainingJob(jobId);

    public List<Dictionary<string, object?>> QueuedJobs(int limit = 16)
        => Meta().ListQueuedJobs(limit);

    /// <summary>Jobs holding a live-state status
    /// (preflight/training/validating). A crashed run never leaves these
    /// states by itself — the reaper
    /// (TrainingJobExecutor.ReapStaleJobs) is the only way out besides
    /// forward progress.</summary>
    public List<Dictionary<string, object?>> ActiveJobs(int limit = 64)
        => Meta().ListActiveJobs(limit);

    public Dictionary<string, object?> TransitionTrainingJob(
        string jobId, string status,
        string outputPath = "", string errorCode = "", string errorMessage = "")
    {
        string requested = (status ?? "").Trim().ToLowerInvariant();
        if (!JobStates.Contains(requested))
            throw new ArgumentException("unsupported transformer training job status");
        try
        {
            Meta().TransitionTrainingJob(jobId, requested,
                outputPath: string.IsNullOrEmpty(outputPath) ? null : outputPath,
                errorCode: errorCode.Length > 96 ? errorCode[..96] : errorCode,
                errorMessage: errorMessage.Length > 1000
                    ? errorMessage[..1000] : errorMessage);
            return Meta().GetTrainingJob(jobId)
                ?? throw new InvalidOperationException(
                    "transformer training job transition was not stored");
        }
        catch (MetadataError e) { throw TranslateMetadataError(e); }
    }

    public enum JobClaimResult { Claimed, Missing, NotQueued, Busy }

    /// <summary>Atomic serial-training claim (同時訓練上限 = 1): the
    /// Rust claim op IS the serial-lane atomic commit (§75) — the writer
    /// lease + FSM give the same single-claimant guarantee the PG
    /// advisory lock provided, without SELECT FOR UPDATE.</summary>
    public (JobClaimResult Result, Dictionary<string, object?>? Row)
        TryClaimTrainingJob(string jobId)
    {
        try
        {
            var r = Meta().ClaimTrainingJob(jobId);
            string verdict = "";
            if (r.TryGetProperty("result", out JsonElement res) &&
                res.ValueKind == JsonValueKind.Object &&
                res.TryGetProperty("result", out JsonElement inner))
                verdict = inner.GetString() ?? "";
            else if (res.ValueKind == JsonValueKind.String)
                verdict = res.GetString() ?? "";
            var result = verdict switch
            {
                "claimed" => JobClaimResult.Claimed,
                "busy" => JobClaimResult.Busy,
                "not_queued" => JobClaimResult.NotQueued,
                _ => JobClaimResult.Missing,
            };
            return (result, Meta().GetTrainingJob(jobId));
        }
        catch (MetadataError e) { throw TranslateMetadataError(e); }
    }

    // ------------------------------------------------------------ adapters --

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
        string adapterId = $"star-transformer-adapter-{Guid.NewGuid().ToString("N")[..24]}";
        try
        {
            Meta().RegisterAdapterCandidate(jobId, artifact,
                CanonicalJson.CanonicalDict(metrics),
                string.IsNullOrEmpty(adapterFormat)
                    ? "native-checkpoint" : adapterFormat,
                adapterId: adapterId);
            return Meta().Get(NativeMetadataClient.Types.Candidate, adapterId)
                ?? throw new InvalidOperationException(
                    "adapter candidate was not registered");
        }
        catch (MetadataError e) { throw TranslateMetadataError(e); }
    }

    public Dictionary<string, object?> AdapterCandidate(string adapterId)
        => Meta().Get(NativeMetadataClient.Types.Candidate, adapterId)
            ?? throw new KeyNotFoundException(
                "transformer adapter candidate does not exist");

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
        try
        {
            Meta().RecordAdapterEvaluation(adapterId, suiteId,
                CanonicalJson.CanonicalDict(baselineMetrics),
                CanonicalJson.CanonicalDict(adapterMetrics),
                CanonicalJson.CanonicalDict(comparison),
                CanonicalJson.CanonicalDict(qualityGates),
                passed, suiteSha256: suiteDigest,
                evaluatedBy: string.IsNullOrEmpty(evaluatedBy)
                    ? "star-main-native-model" : evaluatedBy,
                evaluationId: evaluationId);
            return Meta().Get(NativeMetadataClient.Types.Evaluation,
                              evaluationId)
                ?? throw new InvalidOperationException(
                    "evaluation was not stored");
        }
        catch (MetadataError e) { throw TranslateMetadataError(e); }
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
        try
        {
            Meta().ReleaseAdapter(adapterId, normalized, governedBy,
                reason);
            var released = Meta().Get(
                NativeMetadataClient.Types.Candidate, adapterId)
                ?? throw new InvalidOperationException(
                    "adapter release was not stored");
            released["runtime_state"] = RuntimeModelState();
            return released;
        }
        catch (MetadataError e) { throw TranslateMetadataError(e); }
    }

    public Dictionary<string, object?> RejectAdapter(
        string adapterId, string governedBy, string reason)
    {
        if (string.IsNullOrWhiteSpace(governedBy) || string.IsNullOrWhiteSpace(reason))
            throw new ArgumentException("governed_by and reason are required");
        try
        {
            Meta().RejectAdapter(adapterId, governedBy, reason);
            return Meta().Get(NativeMetadataClient.Types.Candidate,
                              adapterId)
                ?? throw new InvalidOperationException(
                    "adapter rejection was not stored");
        }
        catch (MetadataError e) { throw TranslateMetadataError(e); }
    }

    public Dictionary<string, object?> RuntimeModelState()
        => Meta().GetRuntimeState()
            ?? new Dictionary<string, object?>();

    // ---------------------------------------------------------------- audit --

    /// <summary>Standalone audit event — the xstore audit_event record
    /// IS the canonical entry (event-hash chained); no second audit
    /// authority exists (§17-§18).</summary>
    public void AuditEvent(string eventType, string entityType,
                           string entityId,
                           IReadOnlyDictionary<string, object?> payload)
        => Meta().AuditEvent(eventType, entityType, entityId, payload);

    public Dictionary<string, object?> VerifyAuditChain()
    {
        // The canonical audit is the xstore audit_event hash chain +
        // the mutation receipts chain — verified by Rust, reported in
        // the shape callers already consume.
        var v = Meta().Verify();
        bool receiptsOk = v.TryGetProperty("receipts", out var rc) &&
                          rc.TryGetProperty("ok", out var ro) &&
                          ro.GetBoolean();
        long auditEvents = Meta().Query(
            NativeMetadataClient.Types.Audit, null, 1000000).Count;
        return new Dictionary<string, object?>
        {
            ["ok"] = JTruth(v, "ok") && receiptsOk,
            ["engine"] = "xstore",
            ["event_count"] = auditEvents,
            ["head_sha256"] = v.TryGetProperty("receipts", out var r2) &&
                              r2.TryGetProperty("head", out var h)
                ? (string?)h.GetString() ?? "" : "",
            ["metadata_head_hash"] =
                v.TryGetProperty("head_hash", out var mh)
                    ? (string?)mh.GetString() ?? "" : "",
        };
    }

    // --------------------------------------------------------------- status --

    /// <summary>DatabaseStatus: the native metadata plane reports
    /// engine/integrity/tables; PostgreSQL is no longer a runtime
    /// dependency (§50-§51).</summary>
    public Dictionary<string, object?> DatabaseStatus()
    {
        var v = Meta().Verify();
        var tables = new Dictionary<string, object?>
        {
            ["transformer_training_dataset"] = Meta().Query(
                NativeMetadataClient.Types.Dataset, null, 500000).Count,
            ["transformer_training_dataset_example"] = Meta().Query(
                NativeMetadataClient.Types.DatasetExample, null, 500000).Count,
            ["transformer_training_job"] = Meta().Query(
                NativeMetadataClient.Types.TrainingJob, null, 500000).Count,
            ["transformer_adapter_candidate"] = Meta().Query(
                NativeMetadataClient.Types.Candidate, null, 500000).Count,
            ["transformer_adapter_evaluation"] = Meta().Query(
                NativeMetadataClient.Types.Evaluation, null, 500000).Count,
            ["transformer_adapter_release"] = Meta().Query(
                NativeMetadataClient.Types.Release, null, 500000).Count,
            ["transformer_training_audit_event"] = Meta().Query(
                NativeMetadataClient.Types.Audit, null, 500000).Count,
        };
        var audit = VerifyAuditChain();
        bool receiptsOk = v.TryGetProperty("receipts", out var rc) &&
                          rc.TryGetProperty("ok", out var ro) && ro.GetBoolean();
        return new Dictionary<string, object?>
        {
            ["ok"] = JTruth(v, "ok") && receiptsOk &&
                     (bool)audit["ok"]!,
            ["engine"] = "xstore",
            ["canonical_central_engine"] = "xstore",
            ["canonical"] = true,
            ["authority"] = "xstore",
            ["postgres_required"] = false,
            ["reconciliation_required"] = false,
            ["schema"] = "xingcheng-metadata/v1",
            ["schema_version"] = SchemaVersion,
            ["path"] = $"xstore:{Meta().StoreDir}",
            ["engine_integrity"] =
                JTruth(v, "schema_identity_ok") &&
                JTruth(v, "invariants_ok") ? "ok" : "fail",
            ["tables"] = tables,
            ["audit_chain"] = audit,
            ["metadata_verify"] = JsonSerializer
                .Deserialize<Dictionary<string, object?>>(
                    v.GetRawText()) ?? new Dictionary<string, object?>(),
            ["runtime_model_state"] = RuntimeModelState(),
            ["base_weights_immutable"] = true,
            ["automatic_weight_replacement"] = false,
            ["role_database_ownership_preserved"] = true,
            ["metadata_shadow"] = ShadowStatus(),
            ["metadata_parity"] = ParityReport(),
        };
    }

    /// <summary>Parity report for --db-status: the PG comparison lane
    /// is retired (§49-§50) — PostgreSQL is not a runtime dependency.</summary>
    private static Dictionary<string, object?> ParityReport()
        => new()
        {
            ["format"] = "star-metadata-parity-report/v1",
            ["ok"] = true,
            ["status"] = "retired",
            ["reason"] = "authority=xstore; postgresql comparison lane retired",
        };

    public Dictionary<string, object?> Maintain()
    {
        // Maintenance is index rebuild + verify — the native plane has
        // no ANALYZE; audit the maintenance tick on the canonical chain.
        Meta().RebuildIndex();
        AuditEvent("database-maintained", "training-database",
                   DatabaseName,
                   new Dictionary<string, object?>
                   {
                       ["schema_version"] = SchemaVersion,
                       ["engine"] = "xstore",
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

    internal static long Int64(IReadOnlyDictionary<string, object?> map, string key)
    {
        if (!map.TryGetValue(key, out object? v) || v == null) return 0;
        return v switch
        {
            long l => l,
            int i => i,
            double d => (long)d,
            _ => long.TryParse(v.ToString(), NumberStyles.Integer,
                               CultureInfo.InvariantCulture, out long l) ? l : 0,
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
