// MetadataMigration.cs — Phase A backfill: replay the PostgreSQL
// authoritative state into the xstore metadata plane (§87 export/
// import for migration audit — the xstore side is still SHADOW, not
// authority, until the governed flip of §45).
//
// Semantics:
//   - datasets/examples go through the real `create_dataset` domain op
//     (identity is (content_sha256,snapshot_sha256) — re-emitting is
//     idempotent) with skip_file_check because historical snapshot
//     files may have been pruned by retention; the identity hash pair
//     is what matters, not the file's current presence.
//   - jobs/candidates/evaluations/releases/runtime state backfill via
//     put_many as their FINAL committed state — PG stores rows, not
//     transition history, so the legal-state-machine path is
//     unrecoverable history; the replayed record IS the current state.
//   - audit events replay via audit_many in sequence order so the
//     canonical xstore audit carries the full historical entry set
//     (chain hashes differ structurally — parity is semantic).
//   - every failure is collected and reported, never hidden (§36).
//
// Emitted as part of `--migrate --xstore-backfill` (§102: extending an
// existing verb, no new feature verb).

using System.Globalization;

namespace GPTBridge.XingchengLearning;

internal static class MetadataMigration
{
    private const int ChunkSize = 400;

    public sealed class Result
    {
        public int Emitted;
        public int Skipped;
        public List<Dictionary<string, object?>> Failures = new();
    }

    /// <summary>Replay all PG authoritative tables into xstore.
    /// Returns a `star-metadata-backfill-report/v1` payload.</summary>
    public static Dictionary<string, object?> Backfill(
        TransformerTrainingRepository repo)
    {
        var perTable = new Dictionary<string, object?>();
        var failures = new List<Dictionary<string, object?>>();
        int emitted = 0;

        NativeMetadataClient meta;
        try
        {
            meta = new NativeMetadataClient(
                Path.GetDirectoryName(repo.ToolRoot) ?? repo.ToolRoot,
                actor: "xingcheng-metadata-backfill");
        }
        catch (MetadataError e)
        {
            return new Dictionary<string, object?>
            {
                ["format"] = "star-metadata-backfill-report/v1",
                ["ok"] = false,
                ["status"] = "xstore-unavailable",
                ["error"] = e.Message,
            };
        }

        using var db = Pg.Connect(repo.Schema);

        // ---- runtime singleton first (FK-ish: everything references it)
        var runtime = db.QueryOne(
            "SELECT singleton_id, base_model_id, runtime_model_id, " +
            "active_adapter_id, previous_adapter_id, updated_at " +
            "FROM transformer_runtime_model_state WHERE singleton_id = 1");
        if (runtime != null)
        {
            var rec = new Dictionary<string, object?>
            {
                ["record_id"] = "1",
                ["singleton_id"] = 1,
                ["base_model_id"] = runtime["base_model_id"],
                ["runtime_model_id"] = runtime["runtime_model_id"],
                ["active_adapter_id"] = runtime["active_adapter_id"],
                ["previous_adapter_id"] = runtime["previous_adapter_id"],
                ["automatic_weight_replacement"] = false, // §16 invariant
                ["updated_at"] = runtime["updated_at"],
            };
            try
            {
                meta.PutMany(NativeMetadataClient.Types.RuntimeState,
                             new[] { rec });
                emitted++;
            }
            catch (Exception e)
            {
                failures.Add(Failure("transformer_runtime_model_state", "1", e));
            }
        }
        perTable["transformer_runtime_model_state"] = runtime != null ? 1 : 0;

        // ---- datasets + examples (domain op preserves identity) --------
        var datasets = db.Query(
            "SELECT dataset_id, content_sha256, snapshot_path, " +
            "snapshot_sha256, source_manifest_json, state, created_by, " +
            "format_version, created_at " +
            "FROM transformer_training_dataset ORDER BY created_at ASC");
        int dsOk = 0;
        foreach (var ds in datasets)
        {
            string datasetId = (string)ds["dataset_id"]!;
            var examples = db.Query(
                "SELECT ordinal, split, owner_model_id, database_scope, " +
                "source_example_id, source_revision, content_sha256, " +
                "source_type, quality_score " +
                "FROM transformer_training_dataset_example " +
                "WHERE dataset_id = $1 ORDER BY ordinal ASC", datasetId)
                .Select(e => (IReadOnlyDictionary<string, object?>)
                    new Dictionary<string, object?>
                    {
                        ["split"] = e["split"],
                        ["owner_model_id"] = e["owner_model_id"],
                        ["database_scope"] = e["database_scope"],
                        ["source_example_id"] = e["source_example_id"],
                        ["source_revision"] = e["source_revision"],
                        ["content_sha256"] = e["content_sha256"],
                        ["source_type"] = e["source_type"],
                        ["quality_score"] = e["quality_score"],
                    })
                .ToList();
            try
            {
                meta.ShadowOp("create_dataset", new Dictionary<string, object?>
                {
                    ["dataset_id"] = datasetId, // preserve PG identity verbatim
                    ["content_sha256"] = ds["content_sha256"],
                    ["snapshot_path"] = ds["snapshot_path"],
                    ["snapshot_sha256"] = ds["snapshot_sha256"],
                    ["source_manifest_json"] = ds["source_manifest_json"],
                    ["created_by"] = ds["created_by"],
                    ["format_version"] = ds["format_version"],
                    ["skip_file_check"] = true, // historical snapshots may be pruned
                    ["suppress_audit"] = true,  // audit history replays verbatim below
                    ["examples"] = examples.Select(e => (object)e).ToList(),
                });
                emitted += 1 + examples.Count;
                string state = (string)(ds["state"] ?? "prepared");
                if (state != "prepared")
                    meta.ShadowOp("transition", new Dictionary<string, object?>
                    {
                        ["record_type"] = "training_dataset",
                        ["record_id"] = datasetId,
                        ["to"] = state,
                        ["suppress_audit"] = true,
                    });
                dsOk++;
            }
            catch (Exception e)
            {
                failures.Add(Failure("transformer_training_dataset",
                                     datasetId, e));
            }
        }
        perTable["transformer_training_dataset"] = dsOk;
        perTable["transformer_training_dataset_example"] =
            datasets.Count == dsOk ? "all" : $"partial:{dsOk}/{datasets.Count}";

        // ---- jobs (final state) -------------------------------------------
        emitted += PutManyRows(meta, "transformer_training_job",
            NativeMetadataClient.Types.TrainingJob,
            db.Query(
                "SELECT job_id, dataset_id, base_model_id, training_method, " +
                "configuration_json, configuration_sha256, status, " +
                "output_path, error_code, error_message, requested_by, " +
                "retry_of_job_id, created_at, started_at, completed_at " +
                "FROM transformer_training_job ORDER BY created_at ASC"),
            "job_id", perTable, failures);

        // ---- candidates ------------------------------------------------------
        emitted += PutManyRows(meta, "transformer_adapter_candidate",
            NativeMetadataClient.Types.Candidate,
            db.Query(
                "SELECT adapter_id, job_id, dataset_id, base_model_id, " +
                "adapter_format, artifact_path, artifact_sha256, " +
                "metrics_json, status, created_at, updated_at " +
                "FROM transformer_adapter_candidate ORDER BY created_at ASC"),
            "adapter_id", perTable, failures);

        // ---- evaluations -----------------------------------------------------
        var evalRows = db.Query(
            "SELECT evaluation_id, adapter_id, suite_id, suite_sha256, " +
            "baseline_metrics_json, adapter_metrics_json, comparison_json, " +
            "quality_gates_json, passed, evaluated_by, created_at " +
            "FROM transformer_adapter_evaluation ORDER BY created_at ASC");
        foreach (var r in evalRows)
            r["passed"] = TransformerTrainingRepository.Truthy(r["passed"]);
        emitted += PutManyRows(meta, "transformer_adapter_evaluation",
            NativeMetadataClient.Types.Evaluation, evalRows,
            "evaluation_id", perTable, failures);

        // ---- releases ---------------------------------------------------------
        emitted += PutManyRows(meta, "transformer_adapter_release",
            NativeMetadataClient.Types.Release,
            db.Query(
                "SELECT release_id, adapter_id, action, previous_adapter_id, " +
                "governed_by, reason, created_at " +
                "FROM transformer_adapter_release ORDER BY created_at ASC"),
            "release_id", perTable, failures);

        // ---- audit events (sequence order) -------------------------------------
        var auditRows = db.Query(
            "SELECT event_id, event_type, entity_type, entity_id, " +
            "payload_json FROM transformer_training_audit_event " +
            "ORDER BY sequence ASC");
        int auditEmitted = 0;
        var auditEvents = auditRows
            .Where(r => (string)r["event_type"]! !=
                        "xstore-metadata-shadow-failed")
            .Select(r => (IReadOnlyDictionary<string, object?>)
                new Dictionary<string, object?>
                {
                    ["event_type"] = r["event_type"],
                    ["entity_type"] = r["entity_type"],
                    ["entity_id"] = r["entity_id"],
                    ["payload"] = System.Text.Json.JsonSerializer
                        .Deserialize<Dictionary<string, object?>>(
                            (string)r["payload_json"]!),
                })
            .ToList();
        foreach (var chunk in auditEvents.Chunk(ChunkSize))
        {
            try
            {
                meta.AuditMany(chunk);
                auditEmitted += chunk.Length;
                emitted += chunk.Length;
            }
            catch (Exception e)
            {
                failures.Add(Failure("transformer_training_audit_event",
                    $"chunk@{auditEmitted}", e));
            }
        }
        perTable["transformer_training_audit_event"] = auditEmitted;

        // ---- post-backfill parity ---------------------------------------------
        Dictionary<string, object?>? parity = null;
        try { parity = MetadataParityCheck.Run(repo); }
        catch (Exception e)
        {
            parity = new Dictionary<string, object?>
            {
                ["format"] = MetadataParityCheck.ReportFormat,
                ["ok"] = false, ["status"] = "scan-failed",
                ["error"] = e.Message,
            };
        }

        return new Dictionary<string, object?>
        {
            ["format"] = "star-metadata-backfill-report/v1",
            ["ok"] = failures.Count == 0,
            ["authority"] = "postgresql",     // Phase A: shadow replay only
            ["emitted"] = emitted,
            ["per_table"] = perTable,
            ["failure_count"] = failures.Count,
            ["failures"] = failures.Take(50).ToList(),
            ["parity"] = parity,
            ["checked_at"] = TransformerTrainingRepository.Now(),
        };
    }

    private static int PutManyRows(
        NativeMetadataClient meta,
        string table, string recordType,
        List<Dictionary<string, object?>> rows,
        string idField,
        Dictionary<string, object?> perTable,
        List<Dictionary<string, object?>> failures)
    {
        int emitted = 0;
        var records = rows
            .Select(r =>
            {
                var rec = new Dictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["record_id"] = r[idField]?.ToString() ?? "",
                };
                foreach (var kv in r)
                    rec[kv.Key] = kv.Value;
                return (IReadOnlyDictionary<string, object?>)rec;
            })
            .ToList();
        foreach (var chunk in records.Chunk(ChunkSize))
        {
            try
            {
                meta.PutMany(recordType, chunk);
                emitted += chunk.Length;
            }
            catch (Exception e)
            {
                failures.Add(Failure(table, $"chunk@{emitted}", e));
            }
        }
        perTable[table] = emitted;
        return emitted;
    }

    private static Dictionary<string, object?> Failure(
        string table, string id, Exception e)
        => new()
        {
            ["table"] = table,
            ["record_id"] = id,
            ["error"] = e is MetadataError me
                ? $"{me.Code}: {me.Message}" : e.Message,
        };
}
