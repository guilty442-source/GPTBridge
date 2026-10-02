// MetadataAuthority.cs — authority state + NATIVE_METADATA_AUTHORITY_GATE
// (§45-§49, §99-§100).
//
// Authority state lives in the xstore metadata plane itself as
// append-only `migration_marker` records carrying
// `star-metadata-authority-transition/v1` payloads (§46). The
// PostgreSQL → xstore flip committed on 2026-10-02; Phase D retired the
// PG client entirely, so xstore is now the only possible authority —
// the marker remains as the governed receipt of that transition, and a
// fresh native store can commit a genesis marker through Ensure().
//
// Resolution goes through NativeMetadataClient only — C# never reads
// store files (§31).

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class MetadataAuthority
{
    public const string TransitionFormat = "star-metadata-authority-transition/v1";
    public const string GateId = "NATIVE_METADATA_AUTHORITY_GATE";
    public const string GateReportFormat = "star-native-metadata-authority-gate/v1";

    private const string MarkerPrefix = "authority-transition-";

    /// <summary>JsonElement truthiness — the verify/mutation reports
    /// arrive as JsonElement (a struct; `?? false` cannot apply).</summary>
    private static bool Truth(JsonElement el, string key)
        => el.TryGetProperty(key, out var v) &&
           v.ValueKind == JsonValueKind.True;

    // ------------------------------------------------------- resolution --

    /// <summary>Newest authority-transition marker, or null when the
    /// store carries no transition receipt yet.</summary>
    public static Dictionary<string, object?>? LatestTransition(
        NativeMetadataClient meta)
    {
        var markers = meta.Query(NativeMetadataClient.Types.MigrationMarker,
                                 null, 4096);
        Dictionary<string, object?>? latest = null;
        string latestStamp = "";
        foreach (var m in markers)
        {
            if ((m["format"] as string) != TransitionFormat)
                continue;
            string stamp = (m["timestamp"] as string) ?? "";
            if (latest == null ||
                string.CompareOrdinal(stamp, latestStamp) > 0)
            {
                latest = m;
                latestStamp = stamp;
            }
        }
        return latest;
    }

    /// <summary>Current metadata authority. PostgreSQL is retired — a
    /// store without a transition marker is native by construction, so
    /// the answer is always "xstore".</summary>
    public static string Current(NativeMetadataClient meta) => "xstore";

    // ------------------------------------------------------------ gates --

    /// <summary>Certification probes run on a THROWAWAY store — the
    /// production store is never deliberately corrupted
    /// (§42, §94-§97 matrix). Also used as pre-commit evidence when a
    /// store without a marker needs its genesis transition.</summary>
    public static Dictionary<string, object?> CertificationProbes(
        NativeMetadataClient meta)
    {
        var gates = new Dictionary<string, object?>();

        var verify = meta.Verify();
        bool auditOk = Truth(verify, "ok") &&
                       verify.TryGetProperty("receipts", out var rc) &&
                       Truth(rc, "ok");
        bool integrityOk = Truth(verify, "ok") &&
                           Truth(verify, "schema_identity_ok") &&
                           Truth(verify, "invariants_ok");
        gates["audit_verify"] = Gate("audit_verify", auditOk,
            $"receipts.ok={auditOk}");
        gates["metadata_integrity"] = Gate("metadata_integrity", integrityOk,
            $"schema_identity_ok={Truth(verify, "schema_identity_ok")}");

        // Replay + restart (§83): snapshot → rebuild index → rescan.
        bool replay = false;
        string replayDetail = "";
        try
        {
            meta.Snapshot();
            meta.RebuildIndex();
            var again = meta.Verify();
            replay = Truth(again, "ok") && Truth(again, "index_fresh");
            replayDetail = "snapshot+rebuild+rescan";
        }
        catch (Exception e) { replayDetail = e.Message; }
        gates["replay_restart"] = Gate("replay_restart", replay, replayDetail);

        gates["crash_recovery"] = CrashProbe(meta);
        gates["concurrent_writer"] = ConcurrentProbe(meta);

        bool all = gates.Values.All(g =>
            (bool)((Dictionary<string, object?>)g)["ok"]!);
        return new Dictionary<string, object?>
        {
            ["format"] = "star-metadata-preflip-gates/v1",
            ["ok"] = all,
            ["gates"] = gates,
        };
    }

    /// <summary>§100 gate — emitted standalone so release gates can
    /// re-verify without re-running probes.</summary>
    public static Dictionary<string, object?> Gate(NativeMetadataClient meta)
    {
        var report = new Dictionary<string, object?>
        {
            ["format"] = GateReportFormat,
            ["gate"] = GateId,
        };
        try
        {
            var marker = LatestTransition(meta);
            var verify = meta.Verify();
            bool auditRoot = verify.TryGetProperty("receipts", out var rc) &&
                             Truth(rc, "ok");
            bool integrity = Truth(verify, "ok") &&
                             Truth(verify, "schema_identity_ok") &&
                             Truth(verify, "invariants_ok");
            report["metadata_authority"] = "xstore";
            report["postgres_required"] = false;
            report["shadow_mode"] = false;
            report["dual_write"] = false;
            report["transition_marker"] = marker != null;
            report["audit_root_valid"] = auditRoot;
            report["metadata_integrity"] = integrity ? "PASS" : "FAIL";
            report["ok"] = auditRoot && integrity;
            report["status"] = (bool)report["ok"]! ? "PASS" : "FAIL";
        }
        catch (Exception e)
        {
            report["ok"] = false;
            report["status"] = "ERROR";
            report["error"] = e.Message;
        }
        return report;
    }

    // ------------------------------------------------------ ensure/genesis --

    /// <summary>Ensure the store carries a governed authority receipt
    /// (§45-§46). Idempotent:
    ///   - xstore marker with complete production-gate evidence →
    ///     "already-flipped";
    ///   - xstore marker missing production-gate fields → a
    ///     reaffirmation marker is committed, carrying the original
    ///     parity hash forward and re-running the live probes;
    ///   - no marker → a genesis transition commits only after the
    ///     certification probes pass — fail-closed otherwise.</summary>
    public static Dictionary<string, object?> Ensure(
        NativeMetadataClient meta)
    {
        string now = TransformerTrainingRepository.Now();
        var existing = LatestTransition(meta);
        if (existing != null &&
            (existing["new_authority"] as string) == "xstore")
        {
            bool complete =
                NativeMetadataProductionGate.Evaluate(
                    existing, meta.Verify(),
                    postgresRuntimeDependency: false)
                    .TryGetValue("ok", out var okObj) &&
                okObj is true;
            if (complete)
                return new Dictionary<string, object?>
                {
                    ["format"] = TransitionFormat,
                    ["ok"] = true,
                    ["status"] = "already-flipped",
                    ["marker_record_id"] = existing["record_id"],
                    ["timestamp"] = now,
                };
            return Reaffirm(meta, existing, now);
        }

        var gates = CertificationProbes(meta);
        if (!(bool)gates["ok"]!)
        {
            return new Dictionary<string, object?>
            {
                ["format"] = TransitionFormat,
                ["ok"] = false,
                ["status"] = "gates-failed",
                ["gates"] = gates,
                ["timestamp"] = now,
            };
        }

        var verify = meta.Verify();
        var snap = meta.Snapshot();
        var marker = new Dictionary<string, object?>
        {
            ["record_id"] = MarkerPrefix + now.Replace(":", "")
                .Replace("+", "p").Replace(".", ""),
            ["format"] = TransitionFormat,
            ["old_authority"] = "none",
            ["new_authority"] = "xstore",
            ["snapshot_root"] = snap.TryGetProperty("snapshot_sha256", out var s)
                ? s.GetString() : "",
            ["audit_root"] = verify.TryGetProperty("receipts", out var r) &&
                             r.TryGetProperty("head", out var h)
                ? h.GetString() : "",
            ["gates"] = ((Dictionary<string, object?>)gates["gates"]!)
                .ToDictionary(kv => kv.Key,
                    kv => (object?)((Dictionary<string, object?>)kv.Value)["ok"]),
            ["timestamp"] = now,
        };
        var mutation = meta.PutRecord(
            NativeMetadataClient.Types.MigrationMarker, marker,
            operationId: $"authority-ensure-{now}");

        return new Dictionary<string, object?>
        {
            ["format"] = TransitionFormat,
            ["ok"] = true,
            ["status"] = "genesis-committed",
            ["old_authority"] = "none",
            ["new_authority"] = "xstore",
            ["marker_record_id"] = marker["record_id"],
            ["snapshot_root"] = marker["snapshot_root"],
            ["audit_root"] = marker["audit_root"],
            ["transaction_id"] = mutation.TryGetProperty("transaction_id", out var t)
                ? t.GetString() : "",
            ["gates"] = gates["gates"],
            ["timestamp"] = now,
        };
    }

    /// <summary>Append-only reaffirmation of the committed PG → xstore
    /// transition (§46): carries the original parity evidence forward
    /// verbatim and re-runs the live certification probes so the newest
    /// marker satisfies every production-gate field.</summary>
    private static Dictionary<string, object?> Reaffirm(
        NativeMetadataClient meta,
        Dictionary<string, object?> prior,
        string now)
    {
        var gates = CertificationProbes(meta);
        var verify = meta.Verify();
        var snap = meta.Snapshot();

        var priorGates = prior.TryGetValue("gates", out var pg) &&
                         pg is Dictionary<string, object?> pd
            ? pd : new Dictionary<string, object?>();
        bool priorParity = priorGates.TryGetValue("parity", out var pv) &&
                           pv is true;
        string parityHash = (prior["parity_report_hash"] as string) ?? "";

        bool Probe(string name)
            => ((Dictionary<string, object?>)gates["gates"]!)
                .TryGetValue(name, out var g) &&
               g is Dictionary<string, object?> gd &&
               gd.TryGetValue("ok", out var ok) && ok is true;

        var merged = new Dictionary<string, object?>
        {
            // Backfill + parity ran in Phase A — the carried parity
            // hash is their evidence root.
            ["backfill"] = parityHash.Length == 64 && priorParity,
            ["parity"] = priorParity && parityHash.Length == 64,
            ["audit_verify"] = Probe("audit_verify"),
            ["metadata_integrity"] = Probe("metadata_integrity"),
            ["crash_recovery"] = Probe("crash_recovery"),
            ["concurrent_writer"] = Probe("concurrent_writer"),
            ["replay_restart"] = Probe("replay_restart"),
            ["metadata_snapshot"] =
                snap.TryGetProperty("snapshot_sha256", out var ss) &&
                ss.ValueKind == JsonValueKind.String &&
                (ss.GetString() ?? "").Length == 64,
            ["index_rebuild"] = verify.TryGetProperty("index_fresh",
                out var idx) && idx.ValueKind == JsonValueKind.True,
        };

        var marker = new Dictionary<string, object?>
        {
            ["record_id"] = MarkerPrefix + now.Replace(":", "")
                .Replace("+", "p").Replace(".", ""),
            ["format"] = TransitionFormat,
            ["old_authority"] = "postgresql",
            ["new_authority"] = "xstore",
            ["reaffirms"] = prior["record_id"],
            ["metadata_root"] = verify.TryGetProperty("head_hash",
                out var hh) ? hh.GetString() : "",
            ["snapshot_root"] = snap.TryGetProperty("snapshot_sha256",
                out var s2) ? s2.GetString() : "",
            ["audit_root"] = verify.TryGetProperty("receipts", out var r) &&
                             r.TryGetProperty("head", out var h)
                ? h.GetString() : "",
            ["parity_report_hash"] = parityHash,
            ["gates"] = merged,
            ["timestamp"] = now,
        };
        var mutation = meta.PutRecord(
            NativeMetadataClient.Types.MigrationMarker, marker,
            operationId: $"authority-reaffirm-{now}");

        bool allOk = merged.Values.All(v => v is true);
        return new Dictionary<string, object?>
        {
            ["format"] = TransitionFormat,
            ["ok"] = allOk,
            ["status"] = allOk ? "reaffirmed" : "gates-failed",
            ["old_authority"] = "postgresql",
            ["new_authority"] = "xstore",
            ["marker_record_id"] = marker["record_id"],
            ["reaffirms"] = prior["record_id"],
            ["transaction_id"] = mutation.TryGetProperty("transaction_id",
                out var t) ? t.GetString() : "",
            ["gates"] = merged,
            ["timestamp"] = now,
        };
    }

    // ----------------------------------------------------------- probes --

    private static Dictionary<string, object?> Gate(
        string name, bool ok, string detail)
        => new()
        {
            ["gate"] = name,
            ["ok"] = ok,
            ["detail"] = detail,
        };

    /// <summary>§94-§97 crash matrix on a throwaway store: committed
    /// events survive a torn tail (ignored) while mid-file corruption
    /// fails closed (RECOVERY_REQUIRED, never silently rebuilt).</summary>
    private static Dictionary<string, object?> CrashProbe(
        NativeMetadataClient meta)
    {
        string tmp = Path.Combine(Path.GetTempPath(),
            $"xstore-crash-{Guid.NewGuid():N}");
        try
        {
            var tm = meta.ForStore(tmp);
            tm.PutRecord(NativeMetadataClient.Types.SchemaMetadata,
                new Dictionary<string, object?>
                {
                    ["record_id"] = "crash-probe",
                    ["probe"] = "commit",
                });
            string log = Path.Combine(tmp, "metadata", "events", "events.jsonl");

            // Torn tail: truncated line after the committed event.
            using (var fs = new FileStream(log, FileMode.Append))
            {
                var junk = System.Text.Encoding.UTF8.GetBytes(
                    "{\"format\":\"star-xstore-metadata-event/v1\",\"seq\":2,\"partial");
                fs.Write(junk);
            }
            var v1 = tm.Verify();
            bool tornOk = Truth(v1, "ok") &&
                          v1.TryGetProperty("ignored_tail_bytes", out var tb) &&
                          tb.GetInt64() > 0;

            // Mid-file corruption: flip a byte inside the first event.
            var bytes = File.ReadAllBytes(log);
            int mid = Math.Min(64, bytes.Length / 2);
            bytes[mid] = (byte)(bytes[mid] == (byte)'A' ? 'B' : 'A');
            File.WriteAllBytes(log, bytes);
            bool failClosed;
            try { tm.Verify(); failClosed = false; }
            catch (MetadataError) { failClosed = true; }

            return Gate("crash_recovery", tornOk && failClosed,
                $"torn_tail_ignored={tornOk} midfile_fail_closed={failClosed}");
        }
        catch (Exception e)
        {
            return Gate("crash_recovery", false, e.Message);
        }
        finally
        {
            try { Directory.Delete(tmp, recursive: true); } catch { }
        }
    }

    /// <summary>§75-§77: two writers CAS the same record with the same
    /// expected_revision — exactly one may commit.</summary>
    private static Dictionary<string, object?> ConcurrentProbe(
        NativeMetadataClient meta)
    {
        string tmp = Path.Combine(Path.GetTempPath(),
            $"xstore-cas-{Guid.NewGuid():N}");
        try
        {
            var tm = meta.ForStore(tmp);
            tm.PutRecord(NativeMetadataClient.Types.SchemaMetadata,
                new Dictionary<string, object?>
                {
                    ["record_id"] = "cas-probe",
                    ["probe"] = "rev1",
                });
            var results = new string[2];
            Parallel.For(0, 2, i =>
            {
                try
                {
                    tm.PutRecord(NativeMetadataClient.Types.SchemaMetadata,
                        new Dictionary<string, object?>
                        {
                            ["record_id"] = "cas-probe",
                            ["probe"] = $"rev2-writer{i}",
                        }, expectedRevision: 1);
                    results[i] = "won";
                }
                catch (MetadataError e) { results[i] = e.Code; }
            });
            bool singleWinner = results.Count(r => r == "won") == 1 &&
                                results.Count(r => r == "XSTORE_REVISION_CONFLICT") == 1;
            return Gate("concurrent_writer", singleWinner,
                $"writer0={results[0]} writer1={results[1]}");
        }
        catch (Exception e)
        {
            return Gate("concurrent_writer", false, e.Message);
        }
        finally
        {
            try { Directory.Delete(tmp, recursive: true); } catch { }
        }
    }
}
