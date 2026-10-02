// LEGACY_MIGRATION_ONLY: historical comparison source; excluded from production build.
// MetadataParityCheck.cs — Phase A parity scanner (§38-§41).
//
// Compares PostgreSQL authoritative state against the xstore metadata
// plane's reconstructed state and emits a `star-metadata-parity-report/v1`.
// Comparison is semantic, not byte-level: identity, revision-bearing
// fields, payload content, status, ordering-relevant uniqueness keys —
// §40 "not just row count". Hash chains differ structurally (PG's
// event_sha256 chain vs xstore's raw-line chain) and are verified on
// their own lanes (VerifyAuditChain / metadata-verify), not here.
//
// A mismatch is never merged or patched (§44): it lands in the report's
// mismatches[] and flips ok:false — the authority-flip gate (§42)
// requires a clean report before the governed transition may run.

using System.Globalization;
using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class MetadataParityCheck
{
    public const string ReportFormat = "star-metadata-parity-report/v1";

    /// <summary>Full-state parity scan: PG (authority) vs xstore
    /// reconstructed records. `repo` supplies schema/roots and the
    /// shadow-lane health that the report echoes.</summary>
    public static Dictionary<string, object?> Run(
        TransformerTrainingRepository repo)
    {
        var mismatches = new List<Dictionary<string, object?>>();
        var compared = new Dictionary<string, object?>();
        var report = new Dictionary<string, object?>
        {
            ["format"] = ReportFormat,
            ["checked_at"] = TransformerTrainingRepository.Now(),
            ["authority"] = new Dictionary<string, object?>
            {
                ["current"] = "postgresql",
                ["shadow"] = "xstore",
            },
            ["shadow"] = repo.ShadowStatus(),
        };

        NativeMetadataClient meta;
        try
        {
            meta = new NativeMetadataClient(
                Path.GetDirectoryName(repo.ToolRoot) ?? repo.ToolRoot,
                actor: "xingcheng-metadata-parity");
        }
        catch (MetadataError e)
        {
            report["ok"] = false;
            report["status"] = "xstore-unavailable";
            report["error"] = e.Message;
            return report;
        }

        using var db = Pg.Connect(repo.Schema);

        // ---- datasets ---------------------------------------------------
        Compare("transformer_training_dataset",
            db.Query(
                "SELECT dataset_id, content_sha256, snapshot_sha256, state, " +
                "example_count, format_version, created_by " +
                "FROM transformer_training_dataset"),
            r => (string)r["dataset_id"]!,
            meta.Query(NativeMetadataClient.Types.Dataset, null, 500000),
            r => (string)r["dataset_id"]!,
            new[] { "content_sha256", "snapshot_sha256", "state",
                    "example_count", "format_version", "created_by" },
            mismatches, compared);
        compared["transformer_training_dataset"] = true;

        // ---- dataset examples -------------------------------------------
        Compare("transformer_training_dataset_example",
            db.Query(
                "SELECT dataset_id, ordinal, split, content_sha256, " +
                "source_example_id, source_revision, quality_score " +
                "FROM transformer_training_dataset_example"),
            r => $"{r["dataset_id"]}/{r["ordinal"]}",
            meta.Query(NativeMetadataClient.Types.DatasetExample, null, 500000),
            r => $"{r["dataset_id"]}/{r["ordinal"]}",
            new[] { "split", "content_sha256", "source_example_id",
                    "source_revision", "quality_score" },
            mismatches, compared);
        compared["transformer_training_dataset_example"] = true;

        // ---- training jobs ------------------------------------------------
        Compare("transformer_training_job",
            db.Query(
                "SELECT job_id, dataset_id, status, configuration_sha256, " +
                "requested_by, output_path, error_code " +
                "FROM transformer_training_job"),
            r => (string)r["job_id"]!,
            meta.Query(NativeMetadataClient.Types.TrainingJob, null, 500000),
            r => (string)r["job_id"]!,
            new[] { "dataset_id", "status", "configuration_sha256",
                    "requested_by", "output_path", "error_code" },
            mismatches, compared);
        compared["transformer_training_job"] = true;

        // ---- adapter candidates -------------------------------------------
        Compare("transformer_adapter_candidate",
            db.Query(
                "SELECT adapter_id, job_id, dataset_id, artifact_sha256, " +
                "status, adapter_format FROM transformer_adapter_candidate"),
            r => (string)r["adapter_id"]!,
            meta.Query(NativeMetadataClient.Types.Candidate, null, 500000),
            r => (string)r["adapter_id"]!,
            new[] { "job_id", "dataset_id", "artifact_sha256", "status",
                    "adapter_format" },
            mismatches, compared);
        compared["transformer_adapter_candidate"] = true;

        // ---- evaluations ----------------------------------------------------
        Compare("transformer_adapter_evaluation",
            db.Query(
                "SELECT evaluation_id, adapter_id, suite_id, suite_sha256, " +
                "passed, evaluated_by FROM transformer_adapter_evaluation"),
            r => (string)r["evaluation_id"]!,
            meta.Query(NativeMetadataClient.Types.Evaluation, null, 500000),
            r => (string)r["evaluation_id"]!,
            new[] { "adapter_id", "suite_id", "suite_sha256", "passed",
                    "evaluated_by" },
            mismatches, compared);
        compared["transformer_adapter_evaluation"] = true;

        // ---- releases -------------------------------------------------------
        Compare("transformer_adapter_release",
            db.Query(
                "SELECT release_id, adapter_id, action, previous_adapter_id, " +
                "governed_by, reason FROM transformer_adapter_release"),
            r => (string)r["release_id"]!,
            meta.Query(NativeMetadataClient.Types.Release, null, 500000),
            r => (string)r["release_id"]!,
            new[] { "adapter_id", "action", "previous_adapter_id",
                    "governed_by", "reason" },
            mismatches, compared);
        compared["transformer_adapter_release"] = true;

        // ---- runtime singleton ----------------------------------------------
        var pgRuntime = db.QueryOne(
            "SELECT base_model_id, runtime_model_id, active_adapter_id, " +
            "previous_adapter_id, automatic_weight_replacement " +
            "FROM transformer_runtime_model_state WHERE singleton_id = 1");
        var xsRuntime = meta.GetRuntimeState();
        if (pgRuntime == null && xsRuntime == null)
        {
            // neither side initialized — nothing to compare
        }
        else if (pgRuntime == null || xsRuntime == null)
        {
            mismatches.Add(Mismatch("transformer_runtime_model_state", "1",
                "presence", pgRuntime == null ? "absent" : "present",
                xsRuntime == null ? "absent" : "present"));
        }
        else
        {
            foreach (string f in new[]
                     {
                         "base_model_id", "runtime_model_id",
                         "active_adapter_id", "previous_adapter_id",
                         "automatic_weight_replacement",
                     })
            {
                string pg = Norm(pgRuntime.TryGetValue(f, out object? pv) ? pv : null);
                string xs = Norm(xsRuntime.TryGetValue(f, out object? xv) ? xv : null);
                if (pg != xs)
                    mismatches.Add(Mismatch(
                        "transformer_runtime_model_state", "1", f, pg, xs));
            }
        }
        compared["transformer_runtime_model_state"] = true;

        // ---- audit events (multiset of semantic entries) ---------------------
        // Shadow-failure markers exist only on the PG side by design —
        // they flag the break, they are not domain state.
        var pgAudit = db.Query(
            "SELECT event_type, entity_type, entity_id, payload_json " +
            "FROM transformer_training_audit_event " +
            "WHERE event_type <> 'xstore-metadata-shadow-failed'")
            .Select(r =>
            {
                // Compare payload VALUES, not text: canonical float bytes
                // differ across the Rust boundary (serde shortest-
                // roundtrip vs C# R-format) for the identical f64.
                string payloadNorm = "null";
                try
                {
                    using var doc = JsonDocument.Parse(
                        (string)r["payload_json"]!);
                    if (NativeMetadataClient.ToValue(doc.RootElement) is
                        IReadOnlyDictionary<string, object?> pd)
                        payloadNorm = CanonicalJson.CanonicalDict(pd);
                }
                catch (JsonException) { /* malformed payload stays raw */ }
                return $"{r["event_type"]}|{r["entity_type"]}|" +
                       $"{r["entity_id"]}|{payloadNorm}";
            })
            .ToList();
        var xsAudit = meta.Query(NativeMetadataClient.Types.Audit,
                                 null, 100_000)
            .Select(r =>
            {
                string payloadJson = r.TryGetValue("payload", out object? p) &&
                    p is IReadOnlyDictionary<string, object?> pd
                        ? CanonicalJson.CanonicalDict(pd)
                        : "null";
                return $"{r["event_type"]}|{r["entity_type"]}|" +
                       $"{r["entity_id"]}|{payloadJson}";
            })
            .ToList();
        CompareMultiset("transformer_training_audit_event",
                        pgAudit, xsAudit, mismatches);
        compared["transformer_training_audit_event"] = true;

        report["compared"] = compared;
        report["mismatches"] = mismatches.Count > 200
            ? mismatches.Take(200).ToList()
            : mismatches;
        report["mismatch_count"] = mismatches.Count;
        report["ok"] = mismatches.Count == 0;
        report["status"] = mismatches.Count == 0 ? "parity" : "drift";
        return report;
    }

    // ------------------------------------------------------------- helpers --

    /// <summary>Normalize cross-lane values for comparison: DBNull/null
    /// and "" are equivalent absent markers (PG NULL vs xstore null);
    /// PG stores booleans as 0/1 where xstore stores true/false;
    /// nested objects canonicalize to sorted-key JSON.</summary>
    private static string Norm(object? v) => v switch
    {
        null or DBNull => "",
        // PG stores boolean-ish flags as 0/1 ints where xstore stores
        // true/false — normalize both onto one token.
        bool b => b ? "true-or-1" : "false-or-0",
        int i when i is 0 or 1 => i == 1 ? "true-or-1" : "false-or-0",
        long l when l is 0 or 1 => l == 1 ? "true-or-1" : "false-or-0",
        int i => i.ToString(CultureInfo.InvariantCulture),
        long l => l.ToString(CultureInfo.InvariantCulture),
        double d => d.ToString("R", CultureInfo.InvariantCulture),
        IReadOnlyDictionary<string, object?> m
            => CanonicalJson.CanonicalDict(m),
        JsonElement je => CanonicalJson.Canonical(je),
        _ => v.ToString() ?? "",
    };

    private static Dictionary<string, object?> Mismatch(
        string table, string id, string field, string pg, string xs)
        => new()
        {
            ["table"] = table,
            ["record_id"] = id,
            ["field"] = field,
            ["postgresql"] = pg,
            ["xstore"] = xs,
        };

    /// <summary>Set-keyed field comparison. Missing records and field
    /// divergence both land as mismatches; nothing is merged.</summary>
    private static void Compare(
        string table,
        List<Dictionary<string, object?>> pgRows,
        Func<Dictionary<string, object?>, string> pgKey,
        List<Dictionary<string, object?>> xsRows,
        Func<Dictionary<string, object?>, string> xsKey,
        string[] fields,
        List<Dictionary<string, object?>> mismatches,
        Dictionary<string, object?> compared)
    {
        var pg = new Dictionary<string, Dictionary<string, object?>>(
            StringComparer.Ordinal);
        foreach (var r in pgRows)
            pg[pgKey(r)] = r;
        var xs = new Dictionary<string, Dictionary<string, object?>>(
            StringComparer.Ordinal);
        foreach (var r in xsRows)
            xs[xsKey(r)] = r;
        compared[$"{table}/pg_rows"] = pgRows.Count;
        compared[$"{table}/xs_rows"] = xsRows.Count;
        compared[$"{table}/xs_keys"] = xs.Count;
        foreach (var (id, row) in pg)
        {
            if (!xs.TryGetValue(id, out var xr))
            {
                mismatches.Add(Mismatch(table, id, "presence",
                                        "present", "absent"));
                continue;
            }
            foreach (string f in fields)
            {
                string a = Norm(row.TryGetValue(f, out object? av) ? av : null);
                string b = Norm(xr.TryGetValue(f, out object? bv) ? bv : null);
                if (a != b)
                    mismatches.Add(Mismatch(table, id, f, a, b));
            }
        }
        foreach (var id in xs.Keys)
            if (!pg.ContainsKey(id))
                mismatches.Add(Mismatch(table, id, "presence",
                                        "absent", "present"));
    }

    /// <summary>Multiset comparison for append-only lanes (audit):
    /// entries are opaque normalized strings; multiplicity matters.</summary>
    private static void CompareMultiset(
        string table,
        List<string> pgEntries,
        List<string> xsEntries,
        List<Dictionary<string, object?>> mismatches)
    {
        var pgCount = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var e in pgEntries)
            pgCount[e] = pgCount.TryGetValue(e, out int c) ? c + 1 : 1;
        var xsCount = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var e in xsEntries)
            xsCount[e] = xsCount.TryGetValue(e, out int c) ? c + 1 : 1;
        foreach (var (e, c) in pgCount)
        {
            int xc = xsCount.TryGetValue(e, out int n) ? n : 0;
            if (xc != c)
                mismatches.Add(Mismatch(table, e.Length > 4000 ? e[..4000] : e,
                    "multiplicity", c.ToString(CultureInfo.InvariantCulture),
                    xc.ToString(CultureInfo.InvariantCulture)));
        }
        foreach (var (e, c) in xsCount)
            if (!pgCount.ContainsKey(e))
                mismatches.Add(Mismatch(table,
                    e.Length > 4000 ? e[..4000] : e, "multiplicity",
                    "0", c.ToString(CultureInfo.InvariantCulture)));
    }
}
