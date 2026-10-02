// MetadataAuthority.cs — Phase C authority flip (§42-§49) and the
// NATIVE_METADATA_AUTHORITY_GATE report (§99-§100).
//
// Authority state lives in the xstore metadata plane itself as
// append-only `migration_marker` records carrying
// `star-metadata-authority-transition/v1` payloads (§46). The CURRENT
// authority is the new_authority of the newest transition marker; no
// marker means the pre-migration default (postgresql). Resolution goes
// through NativeMetadataClient only — C# never reads store files (§31).
//
// The flip is a single governed transaction (§45): every pre-flight
// gate (§42) must pass BEFORE the marker is written. Once the marker
// commits, TransformerTrainingRepository routes every operation to
// xstore and any surviving PG path answers EXTERNAL_DATABASE_DENIED
// (§47).

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
    /// migration has not flipped (Phase A default).</summary>
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

    /// <summary>Current metadata authority: "xstore" after the §45
    /// flip, "postgresql" while no transition marker exists. An
    /// unreachable xstore plane resolves to the Phase A default — the
    /// plane cannot be authoritative if it cannot answer.</summary>
    public static string Current(NativeMetadataClient meta)
        => (LatestTransition(meta)?["new_authority"] as string) == "xstore"
            ? "xstore"
            : "postgresql";

    // ------------------------------------------------------------ gates --

    /// <summary>§42 pre-flip gates. Returns the gate report; every
    /// entry must pass before Flip() writes the marker. Crash and
    /// concurrency probes run on a THROWAWAY store — the production
    /// store is never deliberately corrupted (§95-§97 matrix).</summary>
    public static Dictionary<string, object?> PreFlipGates(
        TransformerTrainingRepository repo, NativeMetadataClient meta)
    {
        var gates = new Dictionary<string, object?>();

        var parity = MetadataParityCheck.Run(repo);
        gates["parity"] = Gate("parity", (bool)(parity["ok"] ?? false),
            (parity["status"] as string) ?? "?");

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

    /// <summary>§100 gate — also emitted standalone after the flip so
    /// release gates can re-verify without re-running probes.</summary>
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
            string authority = marker != null
                ? (marker["new_authority"] as string) ?? "postgresql"
                : "postgresql";
            report["metadata_authority"] = authority;
            report["postgres_required"] = authority != "xstore";
            report["shadow_mode"] = authority != "xstore";
            report["dual_write"] = false;
            report["audit_root_valid"] = auditRoot;
            report["metadata_integrity"] = integrity ? "PASS" : "FAIL";
            var strict = NativeMetadataProductionGate.Evaluate(marker, verify,
                NativeMetadataProductionGate.HasExternalReference(
                    typeof(MetadataAuthority).Assembly));
            report["ok"] = strict["ok"];
            report["status"] = strict["status"];
            report["failures"] = strict["failures"];
        }
        catch (Exception e)
        {
            report["ok"] = false;
            report["status"] = "ERROR";
            report["error"] = e.Message;
        }
        return report;
    }

    // ------------------------------------------------------------- flip --

    /// <summary>§45-§46: gated authority transition. Writes the
    /// `star-metadata-authority-transition/v1` marker as an append-only
    /// migration_marker record — the receipt IS the record.</summary>
    public static Dictionary<string, object?> Flip(
        TransformerTrainingRepository repo, NativeMetadataClient meta)
    {
        if (LatestTransition(meta) is not null)
        {
            var existing = Gate(meta);
            existing["transition"] = "already-recorded";
            return existing; // Never manufacture a second authority history.
        }
        string now = TransformerTrainingRepository.Now();
        var gates = PreFlipGates(repo, meta);
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

        var parity = MetadataParityCheck.Run(repo);
        var verify = meta.Verify();
        var snap = meta.Snapshot();
        string parityHash = TransformerTrainingRepository.Sha256Text(
            CanonicalJson.CanonicalDict(parity));

        var marker = new Dictionary<string, object?>
        {
            ["record_id"] = MarkerPrefix + now.Replace(":", "")
                .Replace("+", "p").Replace(".", ""),
            ["format"] = TransitionFormat,
            ["old_authority"] = "postgresql",
            ["new_authority"] = "xstore",
            ["snapshot_root"] = snap.TryGetProperty("snapshot_sha256", out var s)
                ? s.GetString() : "",
            ["audit_root"] = verify.TryGetProperty("receipts", out var r) &&
                             r.TryGetProperty("head", out var h)
                ? h.GetString() : "",
            ["parity_report_hash"] = parityHash,
            ["gates"] = ((Dictionary<string, object?>)gates["gates"]!)
                .ToDictionary(kv => kv.Key,
                    kv => (object?)((Dictionary<string, object?>)kv.Value)["ok"]),
            ["timestamp"] = now,
        };
        var mutation = meta.PutRecord(
            NativeMetadataClient.Types.MigrationMarker, marker,
            operationId: $"authority-flip-{now}");

        return new Dictionary<string, object?>
        {
            ["format"] = TransitionFormat,
            ["ok"] = true,
            ["status"] = "flipped",
            ["old_authority"] = "postgresql",
            ["new_authority"] = "xstore",
            ["marker_record_id"] = marker["record_id"],
            ["snapshot_root"] = marker["snapshot_root"],
            ["audit_root"] = marker["audit_root"],
            ["parity_report_hash"] = parityHash,
            ["transaction_id"] = mutation.TryGetProperty("transaction_id", out var t)
                ? t.GetString() : "",
            ["gates"] = gates["gates"],
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
