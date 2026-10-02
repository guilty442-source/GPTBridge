// NativeMetadataClient.cs — typed contract surface over xstore's
// native metadata plane (Native Metadata Authority Migration §29-§34).
//
// C# never reads, parses or hashes xstore files directly (§28-§31): all
// state transitions go through `xstore.exe metadata-*` verbs, which own
// canonical serialization, the event hash chain, the writer lease and
// every domain invariant (the PostgreSQL repository semantics ported
// into meta_domain.rs / meta_release.rs).
//
// Phase A role (§35-§37): shadow emitter — every mutation here mirrors
// a committed PostgreSQL transaction; a shadow failure raises
// XSTORE_METADATA_SHADOW_FAILED and never feeds reads back. Phase C
// role (§45-§48): after the governed authority flip this client IS
// TransformerTrainingRepository's storage backend and PG is retired.
//
// Error mapping: xstore prints `XSTORE_FAILED: <CODE>: <detail>` on
// stderr and exits 2; the innermost ALL-CAPS code is surfaced as
// MetadataError.Code so governance callers can switch on it
// (XSTORE_REVISION_CONFLICT, XSTORE_INVALID_TRANSITION, META_UNIQUE,
// XSTORE_STALE_WRITER, XSTORE_RECOVERY_REQUIRED, ...).

using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal sealed class MetadataError : Exception
{
    public string Code { get; }

    public MetadataError(string code, string message) : base(message)
        => Code = code;

    /// <summary>Extract the innermost UPPER_SNAKE code from an xstore
    /// stderr line of the form `XSTORE_FAILED: CODE: detail`.</summary>
    public static string CodeOf(string stderr)
    {
        string text = stderr.Trim();
        if (text.StartsWith("XSTORE_FAILED:", StringComparison.Ordinal))
            text = text["XSTORE_FAILED:".Length..].TrimStart();
        int colon = text.IndexOf(':');
        string head = (colon >= 0 ? text[..colon] : text).Trim();
        if (head.Length > 0 &&
            head.All(c => char.IsUpper(c) || char.IsDigit(c) || c == '_'))
            return head;
        return "XSTORE_FAILED";
    }
}

internal sealed class NativeMetadataClient
{
    /// <summary>Registered record types — kept in lockstep with
    /// meta_types.rs RECORD_TYPES; unknown types fail closed in Rust
    /// regardless, this list is the C#-side contract vocabulary.</summary>
    public static class Types
    {
        public const string Dataset = "training_dataset";
        public const string DatasetExample = "dataset_example";
        public const string TrainingJob = "training_job";
        public const string Candidate = "adapter_candidate";
        public const string Evaluation = "adapter_evaluation";
        public const string Release = "adapter_release";
        public const string RuntimeState = "runtime_model_state";
        public const string Audit = "audit_event";
        public const string CapabilityEvidence = "capability_evidence";
        public const string CapabilityFloor = "capability_floor";
        public const string CapabilityDelta = "capability_delta";
        public const string CapabilityBinding = "capability_binding";
        public const string CapabilityMaturity = "capability_maturity";
        public const string Generation = "generation_record";
        public const string Lifecycle = "lifecycle_state";
        public const string SelfLearning = "self_learning_state";
        public const string Maturation = "maturation_state";
        public const string TeacherEvidence = "teacher_evidence";
        public const string ResourceGrantRef = "resource_grant_ref";
        public const string ResourceUsageReceipt = "resource_usage_receipt";
        public const string RoleExample = "role_training_example";
        public const string RolePair = "role_preference_pair";
        public const string SchemaMetadata = "schema_metadata";
        public const string MigrationMarker = "migration_marker";
    }

    private readonly string _exe;
    private readonly string _actor;

    public string StoreDir { get; }

    public NativeMetadataClient(string toolRoot, string actor = "xingcheng-learning")
    {
        _exe = NativeTools.RustExe(toolRoot, "xstore");
        if (_exe.Length == 0)
            throw new MetadataError(
                "XSTORE_UNAVAILABLE",
                $"xstore.exe not deployed under {toolRoot}/src/backend/rust/xstore/target/release");
        StoreDir = NativeTools.ArtifactStoreDir(toolRoot);
        _actor = actor;
    }

    private NativeMetadataClient(string exe, string storeDir, string actor)
    {
        _exe = exe;
        StoreDir = storeDir;
        _actor = actor;
    }

    /// <summary>Same executable, different store root — used ONLY by
    /// governed pre-flip gate probes (crash matrix / concurrent-writer
    /// CAS) which must never corrupt the production store.</summary>
    internal NativeMetadataClient ForStore(string storeDir)
        => new(_exe, storeDir, _actor);

    // --------------------------------------------------------- transport --

    /// <summary>Run one xstore verb; returns the parsed stdout JSON.
    /// Params ship via a temp file (--params @file) so large example
    /// sets never hit command-line limits (§31 — the file is contract
    /// input, not store access).</summary>
    private JsonElement Invoke(IReadOnlyList<string> args, JsonElement? prms = null)
    {
        string? paramsFile = null;
        var full = new List<string>(args) { "--store", StoreDir, "--actor", _actor };
        try
        {
            if (prms.HasValue)
            {
                paramsFile = Path.Combine(
                    Path.GetTempPath(), $"xmeta-{Guid.NewGuid():N}.json");
                File.WriteAllText(paramsFile,
                    CanonicalJson.Canonical(prms.Value), new UTF8Encoding(false));
                full.Add("--params");
                full.Add("@" + paramsFile);
            }
            var psi = new ProcessStartInfo
            {
                FileName = _exe,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = new UTF8Encoding(false),
                StandardErrorEncoding = new UTF8Encoding(false),
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            foreach (string a in full)
                psi.ArgumentList.Add(a);
            using var proc = Process.Start(psi)
                ?? throw new MetadataError("XSTORE_SPAWN_FAILED", "xstore.exe failed to start");
            var stdoutTask = proc.StandardOutput.ReadToEndAsync();
            var stderrTask = proc.StandardError.ReadToEndAsync();
            if (!proc.WaitForExit(60_000))
            {
                try { proc.Kill(entireProcessTree: true); proc.WaitForExit(5000); } catch { }
                throw new MetadataError("XSTORE_TIMEOUT", "xstore.exe metadata call timed out");
            }
            string stdout = stdoutTask.GetAwaiter().GetResult();
            string stderr = stderrTask.GetAwaiter().GetResult();
            if (proc.ExitCode != 0)
                throw new MetadataError(MetadataError.CodeOf(stderr), stderr.Trim());
            using var doc = JsonDocument.Parse(stdout.Trim());
            return doc.RootElement.Clone();
        }
        catch (JsonException e)
        {
            throw new MetadataError("XSTORE_OUTPUT_INVALID", e.Message);
        }
        finally
        {
            if (paramsFile != null)
                try { File.Delete(paramsFile); } catch { }
        }
    }

    private JsonElement Mutate(string op, Dictionary<string, object?> prms)
    {
        using var doc = JsonDocument.Parse(
            CanonicalJson.CanonicalDict(prms));
        return Invoke(new[] { "metadata-put", "--op", op }, doc.RootElement);
    }

    private static Dictionary<string, object?> ToDict(JsonElement el)
    {
        var map = new Dictionary<string, object?>(StringComparer.Ordinal);
        if (el.ValueKind != JsonValueKind.Object)
            return map;
        foreach (var p in el.EnumerateObject())
            map[p.Name] = ToValue(p.Value);
        return map;
    }

    /// <summary>JsonElement → CLR value. Also used by the parity lane to
    /// normalize PG-side canonical text through the same numeric pipeline
    /// as xstore-returned records (f64 boundary: shortest-roundtrip text
    /// differs across languages, values are equal).</summary>
    internal static object? ToValue(JsonElement el) => el.ValueKind switch
    {
        JsonValueKind.String => el.GetString(),
        // NOTE: the ternary must not promote long→double or Norm-based
        // parity comparison loses the integer/boolean distinction.
        JsonValueKind.Number => el.TryGetInt64(out long l)
            ? (object)l : el.GetDouble(),
        JsonValueKind.True => true,
        JsonValueKind.False => false,
        JsonValueKind.Null => null,
        JsonValueKind.Object => ToDict(el),
        JsonValueKind.Array => el.EnumerateArray().Select(ToValue).ToList(),
        _ => null,
    };

    /// <summary>Flatten a metadata record into the row shape the PG
    /// repository returned (payload fields at top level + revision and
    /// event_hash identity columns).</summary>
    private static Dictionary<string, object?> Row(JsonElement record)
    {
        var row = new Dictionary<string, object?>(StringComparer.Ordinal);
        if (record.TryGetProperty("payload", out JsonElement payload))
            foreach (var p in payload.EnumerateObject())
                row[p.Name] = ToValue(p.Value);
        if (record.TryGetProperty("revision", out JsonElement rev))
            row["revision"] = ToValue(rev);
        if (record.TryGetProperty("event_hash", out JsonElement eh))
            row["event_hash"] = ToValue(eh);
        return row;
    }

    // ------------------------------------------------------------- reads --

    public Dictionary<string, object?>? Get(string recordType, string recordId)
    {
        try
        {
            var r = Invoke(new[]
            {
                "metadata-get", "--type", recordType, "--id", recordId,
            });
            if (r.TryGetProperty("record", out JsonElement rec))
                return Row(rec);
            return null;
        }
        catch (MetadataError e) when (e.Code == "META_RECORD_MISSING")
        {
            return null;
        }
    }

    public List<Dictionary<string, object?>> Query(
        string recordType,
        IReadOnlyDictionary<string, object?>? where = null,
        int limit = 1024)
    {
        var args = new List<string>
        {
            "metadata-query", "--type", recordType,
            "--limit", limit.ToString(System.Globalization.CultureInfo.InvariantCulture),
        };
        if (where != null)
        {
            args.Add("--where");
            args.Add(CanonicalJson.CanonicalDict(where));
        }
        var r = Invoke(args);
        var rows = new List<Dictionary<string, object?>>();
        if (r.TryGetProperty("records", out JsonElement recs))
            foreach (var rec in recs.EnumerateArray())
                rows.Add(Row(rec));
        return rows;
    }

    // ---------------------------------------------------- typed queries --

    public Dictionary<string, object?>? GetTrainingJob(string jobId)
        => Get(Types.TrainingJob, jobId);

    public List<Dictionary<string, object?>> ListQueuedJobs(int limit = 16)
        => Query(Types.TrainingJob,
                 new Dictionary<string, object?> { ["status"] = "queued" },
                 limit);

    public List<Dictionary<string, object?>> ListActiveJobs(int limit = 64)
        => Query(Types.TrainingJob, null, limit)
            .Where(r => (r["status"] as string) is "preflight" or "training" or "validating")
            .ToList();

    public Dictionary<string, object?>? GetActiveCandidate()
        => Query(Types.Candidate,
                 new Dictionary<string, object?> { ["status"] = "active" },
                 2).FirstOrDefault();

    public Dictionary<string, object?>? FindEvaluation(string adapterId, string suiteSha256)
        => Query(Types.Evaluation,
                 new Dictionary<string, object?>
                 {
                     ["adapter_id"] = adapterId,
                     ["suite_sha256"] = suiteSha256,
                 }, 2).FirstOrDefault();

    public Dictionary<string, object?>? GetRuntimeState()
        => Get(Types.RuntimeState, "1");

    public List<Dictionary<string, object?>> ListCapabilityEvidence(int limit = 1024)
        => Query(Types.CapabilityEvidence, null, limit);

    // -------------------------------------------------------- mutations --

    /// <summary>Domain ops replicate the PG repository transaction
    /// surface; xstore validates + commits atomically under the writer
    /// lease and returns the mutation report (receipt included).</summary>
    public JsonElement CreateDataset(
        string contentSha256,
        string snapshotPath,
        string snapshotSha256,
        IReadOnlyList<IReadOnlyDictionary<string, object?>> examples,
        string sourceManifestJson,
        string createdBy = "star-main-native-model",
        string formatVersion = "star-transformer-sft/v1",
        string? datasetId = null,
        string? operationId = null)
    {
        var prms = new Dictionary<string, object?>
        {
            ["content_sha256"] = contentSha256,
            ["snapshot_path"] = snapshotPath,
            ["snapshot_sha256"] = snapshotSha256,
            ["source_manifest_json"] = sourceManifestJson,
            ["created_by"] = createdBy,
            ["format_version"] = formatVersion,
            ["examples"] = examples.Select(e => (object)e).ToList(),
        };
        if (datasetId != null) prms["dataset_id"] = datasetId;
        if (operationId != null) prms["operation_id"] = operationId;
        return Mutate("create_dataset", prms);
    }

    public JsonElement CreateTrainingJob(
        string datasetId,
        string configurationJson,
        string requestedBy = "star-main-native-model",
        string? retryOfJobId = null,
        string? jobId = null,
        string? operationId = null)
    {
        var prms = new Dictionary<string, object?>
        {
            ["dataset_id"] = datasetId,
            ["configuration_json"] = configurationJson,
            ["requested_by"] = requestedBy,
        };
        if (retryOfJobId != null) prms["retry_of_job_id"] = retryOfJobId;
        if (jobId != null) prms["job_id"] = jobId;
        if (operationId != null) prms["operation_id"] = operationId;
        return Mutate("create_job", prms);
    }

    public JsonElement TransitionTrainingJob(
        string jobId, string to,
        string? outputPath = null,
        string errorCode = "", string errorMessage = "",
        long? expectedRevision = null,
        string? operationId = null)
    {
        var prms = new Dictionary<string, object?>
        {
            ["job_id"] = jobId,
            ["to"] = to,
            ["error_code"] = errorCode,
            ["error_message"] = errorMessage,
        };
        if (outputPath != null) prms["output_path"] = outputPath;
        if (expectedRevision.HasValue) prms["expected_revision"] = expectedRevision.Value;
        if (operationId != null) prms["operation_id"] = operationId;
        return Mutate("transition_job", prms);
    }

    /// <summary>Serial-lane atomic claim (advisory-lock port): returns
    /// claimed | busy | not_queued | missing in result.result.</summary>
    public JsonElement ClaimTrainingJob(string jobId, string? operationId = null)
    {
        var prms = new Dictionary<string, object?> { ["job_id"] = jobId };
        if (operationId != null) prms["operation_id"] = operationId;
        return Mutate("claim_job", prms);
    }

    public JsonElement RegisterAdapterCandidate(
        string jobId, string artifactPath,
        string metricsJson = "{}",
        string adapterFormat = "native-checkpoint",
        string? adapterId = null,
        string? operationId = null)
    {
        var prms = new Dictionary<string, object?>
        {
            ["job_id"] = jobId,
            ["artifact_path"] = artifactPath,
            ["metrics_json"] = metricsJson,
            ["adapter_format"] = adapterFormat,
        };
        if (adapterId != null) prms["adapter_id"] = adapterId;
        if (operationId != null) prms["operation_id"] = operationId;
        return Mutate("register_candidate", prms);
    }

    public JsonElement RecordAdapterEvaluation(
        string adapterId, string suiteId,
        string baselineMetricsJson, string adapterMetricsJson,
        string comparisonJson, string qualityGatesJson,
        bool passed,
        string suiteSha256 = "",
        string evaluatedBy = "star-main-native-model",
        string? evaluationId = null,
        string? operationId = null)
    {
        var prms = new Dictionary<string, object?>
        {
            ["adapter_id"] = adapterId,
            ["suite_id"] = suiteId,
            ["suite_sha256"] = suiteSha256,
            ["baseline_metrics_json"] = baselineMetricsJson,
            ["adapter_metrics_json"] = adapterMetricsJson,
            ["comparison_json"] = comparisonJson,
            ["quality_gates_json"] = qualityGatesJson,
            ["passed"] = passed,
            ["evaluated_by"] = evaluatedBy,
        };
        if (evaluationId != null) prms["evaluation_id"] = evaluationId;
        if (operationId != null) prms["operation_id"] = operationId;
        return Mutate("record_evaluation", prms);
    }

    /// <summary>action ∈ stage|activate|rollback|retire — anything else
    /// fails closed in Rust.</summary>
    public JsonElement ReleaseAdapter(
        string adapterId, string action,
        string governedBy, string reason,
        string? operationId = null)
    {
        var prms = new Dictionary<string, object?>
        {
            ["adapter_id"] = adapterId,
            ["action"] = action,
            ["governed_by"] = governedBy,
            ["reason"] = reason,
        };
        if (operationId != null) prms["operation_id"] = operationId;
        return Mutate("release", prms);
    }

    public JsonElement RejectAdapter(
        string adapterId, string governedBy, string reason,
        string? operationId = null)
    {
        var prms = new Dictionary<string, object?>
        {
            ["adapter_id"] = adapterId,
            ["governed_by"] = governedBy,
            ["reason"] = reason,
        };
        if (operationId != null) prms["operation_id"] = operationId;
        return Mutate("reject_candidate", prms);
    }

    /// <summary>Runtime singleton init — idempotent (PG Migrate()
    /// parity).</summary>
    public JsonElement InitRuntimeState(string? operationId = null)
    {
        var prms = new Dictionary<string, object?>();
        if (operationId != null) prms["operation_id"] = operationId;
        return Mutate("init_runtime", prms);
    }

    /// <summary>Standalone append-only audit event (canonical audit
    /// entry, §17).</summary>
    public JsonElement AuditEvent(
        string eventType, string entityType, string entityId,
        IReadOnlyDictionary<string, object?> payload,
        string? operationId = null)
    {
        var prms = new Dictionary<string, object?>
        {
            ["event_type"] = eventType,
            ["entity_type"] = entityType,
            ["entity_id"] = entityId,
            ["payload"] = payload,
        };
        if (operationId != null) prms["operation_id"] = operationId;
        return Mutate("audit", prms);
    }

    /// <summary>Generic typed record write for the non-job domains
    /// (capability evidence/floor/delta/binding/maturity, generation,
    /// lifecycle, self-learning, maturation, teacher evidence, resource
    /// grant refs/usage receipts, schema metadata, migration markers).
    /// Append-only types reject a second write; stateful types upsert
    /// with revision++ and honor expected_revision CAS.</summary>
    public JsonElement PutRecord(
        string recordType,
        IReadOnlyDictionary<string, object?> record,
        long? expectedRevision = null,
        string? operationId = null)
    {
        var prms = new Dictionary<string, object?>
        {
            ["record_type"] = recordType,
            ["record"] = record,
        };
        if (expectedRevision.HasValue) prms["expected_revision"] = expectedRevision.Value;
        if (operationId != null) prms["operation_id"] = operationId;
        return Mutate("put", prms);
    }

    /// <summary>Generic governed mutation entry — the shadow emitter
    /// (Repository.Phase A) and migration tooling use this; product code
    /// should prefer the typed methods above.</summary>
    public JsonElement ShadowOp(string op, Dictionary<string, object?> prms)
        => Mutate(op, prms);

    /// <summary>Bulk typed-record write for migration backfill (§87):
    /// the whole batch commits as ONE transaction/receipt. Each record
    /// must carry record_id; per-record rules match PutRecord.</summary>
    public JsonElement PutMany(
        string recordType,
        IReadOnlyList<IReadOnlyDictionary<string, object?>> records,
        string? operationId = null)
    {
        var prms = new Dictionary<string, object?>
        {
            ["record_type"] = recordType,
            ["records"] = records.Select(r => (object)r).ToList(),
        };
        if (operationId != null) prms["operation_id"] = operationId;
        return Mutate("put_many", prms);
    }

    /// <summary>Bulk canonical-audit backfill: one transaction, one
    /// receipt covering every event.</summary>
    public JsonElement AuditMany(
        IReadOnlyList<IReadOnlyDictionary<string, object?>> events,
        string? operationId = null)
    {
        var prms = new Dictionary<string, object?>
        {
            ["events"] = events.Select(e => (object)e).ToList(),
        };
        if (operationId != null) prms["operation_id"] = operationId;
        return Mutate("audit_many", prms);
    }

    // -------------------------------------------------------- operation --

    /// <summary>metadata-verify: canonical scan + materialize + receipts
    /// chain + index freshness + invariant report.</summary>
    public JsonElement Verify()
        => Invoke(new[] { "metadata-verify" });

    /// <summary>Write a restart-acceleration snapshot manifest.</summary>
    public JsonElement Snapshot()
        => Invoke(new[] { "metadata-snapshot" });

    /// <summary>Rebuild the derived index from the canonical log.</summary>
    public JsonElement RebuildIndex()
        => Invoke(new[] { "metadata-rebuild-index" });
}
