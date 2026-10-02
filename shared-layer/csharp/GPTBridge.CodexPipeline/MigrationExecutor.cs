using System.Security.Cryptography;
using System.Text;
using Npgsql;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Governed SQL migration executor — C# implementation of the
/// ``sql_migration_executor_contract`` row (identity
/// ``SQL_MIGRATION_EXECUTOR``).  The retired Python lane carried the
/// previous implementation; none exists in the active lanes today.
///
/// Contract order (every step fails closed):
///   read-registry → verify-predecessor → verify-live-source-hash →
///   acquire-global-lock → execute-approved-migration →
///   verify-target-hash → write-receipt → release-lock
///
/// Forbidden actions (contract): unregistered-sql,
/// ordinary-business-write, permission-decision.  ``--apply`` never
/// runs a file that is not the registered row for that sequence.
///
/// ``--migration-status`` is fully read-only (runtime DSN) and emits
/// the registry↔source↔receipt reconciliation report — the evidence a
/// governor needs before any ``--apply`` can legitimately proceed.
/// </summary>
internal static class MigrationExecutor
{
    public const string ExecutorIdentity = "SQL_MIGRATION_EXECUTOR";
    public const string ReceiptSchema = "SQL_MIGRATION_RECEIPT";
    public const string LockName = "sql_migration_executor";

    /// <summary>Deterministic live-schema fingerprint recipe used for
    /// observed_target_hash.  sha256 over the sorted manifest of
    /// ``class|schema|name|definition`` lines for every user object
    /// (tables+columns, indexes, views, functions, triggers, event
    /// triggers, policies, sequences, schemas, extensions).  This is
    /// the executor's own measurement recipe — equality with the
    /// registered ``target_schema_hash`` is REQUIRED for commit; a
    /// mismatch rolls the migration back (fail-closed).</summary>
    public const string ManifestRecipe = "OBJECT_MANIFEST_V1";

    internal sealed record RegistryRow(
        long Sequence, string MigrationId, string? PredecessorId,
        string ForwardIdentity, string SourceSchemaHash,
        string TargetSchemaHash, string MigrationSourceHash,
        string TransactionPolicy, string Status, string IntroducedVersion);

    internal sealed record ReceiptRow(
        string ReceiptId, string MigrationId, string TransactionResult,
        string VerificationResult, string ObservedTargetHash,
        string ReceiptHash);

    private static readonly string[] ContractOrder =
    {
        "read-registry", "verify-predecessor",
        "verify-live-source-hash", "acquire-global-lock",
        "execute-approved-migration", "verify-target-hash",
        "write-receipt", "release-lock",
    };

    private static string Sha256(byte[] bytes) =>
        Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();

    private static string Sha256Text(string text) =>
        Sha256(Encoding.UTF8.GetBytes(text));

    private static string FileSha256(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream))
            .ToLowerInvariant();
    }

    // Hash and decode one immutable byte snapshot. Reopening the file after
    // verification could execute different SQL under the approved hash.
    private static string VerifiedSource(string path, string expectedHash)
    {
        var bytes = File.ReadAllBytes(path);
        if (!string.Equals(Sha256(bytes), expectedHash, StringComparison.Ordinal))
            throw new ApplyDenied("SQL_SCHEMA_VERSION_MISMATCH");
        using var stream = new MemoryStream(bytes, writable: false);
        using var reader = new StreamReader(stream, Encoding.UTF8,
            detectEncodingFromByteOrderMarks: true);
        return reader.ReadToEnd();
    }

    private static string RepoFile(string root, string forwardIdentity) =>
        Path.Combine(root,
            forwardIdentity.Replace('/', Path.DirectorySeparatorChar));

    // ------------------------------------------------------------------
    // catalog helpers
    // ------------------------------------------------------------------

    private static List<Dictionary<string, object?>> Query(
        NpgsqlConnection connection, string sql,
        NpgsqlTransaction? tx = null)
    {
        var rows = new List<Dictionary<string, object?>>();
        using var command = new NpgsqlCommand(sql, connection, tx);
        using var reader = command.ExecuteReader();
        while (reader.Read())
        {
            var row = new Dictionary<string, object?>(
                StringComparer.Ordinal);
            for (var i = 0; i < reader.FieldCount; i++)
                row[reader.GetName(i)] =
                    reader.IsDBNull(i) ? null : reader.GetValue(i);
            rows.Add(row);
        }
        return rows;
    }

    private static object? Scalar(NpgsqlConnection connection,
        string sql, NpgsqlTransaction? tx = null)
    {
        using var command = new NpgsqlCommand(sql, connection, tx);
        return command.ExecuteScalar();
    }

    private static List<RegistryRow> ReadRegistry(
        NpgsqlConnection connection)
    {
        var rows = Query(connection,
            $"SELECT sequence, migration_id, predecessor_id, "
            + "forward_identity, source_schema_hash, target_schema_hash, "
            + "migration_source_hash, transaction_policy, status, "
            + "introduced_version FROM "
            + $"{PgDsn.CodexSchema}.sql_migration_registry "
            + "ORDER BY sequence");
        var list = new List<RegistryRow>(rows.Count);
        foreach (var row in rows)
        {
            list.Add(new RegistryRow(
                Convert.ToInt64(row["sequence"]),
                (string)row["migration_id"]!,
                row["predecessor_id"] as string,
                (string)row["forward_identity"]!,
                (string)row["source_schema_hash"]!,
                (string)row["target_schema_hash"]!,
                (string)row["migration_source_hash"]!,
                (string)row["transaction_policy"]!,
                (string)row["status"]!,
                (string)row["introduced_version"]!));
        }
        return list;
    }

    private static List<ReceiptRow> ReadReceipts(
        NpgsqlConnection connection, NpgsqlTransaction? tx = null)
    {
        var rows = Query(connection,
            $"SELECT receipt_id, migration_id, transaction_result, "
            + "verification_result, observed_target_hash, receipt_hash "
            + $"FROM {PgDsn.CodexSchema}.sql_migration_receipt_registry "
            + "ORDER BY started_at_utc, receipt_id", tx);
        var list = new List<ReceiptRow>(rows.Count);
        foreach (var row in rows)
            list.Add(new ReceiptRow(
                (string)row["receipt_id"]!, (string)row["migration_id"]!,
                (string)row["transaction_result"]!,
                (string)row["verification_result"]!,
                row["observed_target_hash"] as string ?? "",
                (string)row["receipt_hash"]!));
        return list;
    }

    /// <summary>Live object manifest — deterministic fingerprint of
    /// every user-schema object currently present (OBJECT_MANIFEST_V1).</summary>
    private static string LiveManifestHash(NpgsqlConnection connection,
        NpgsqlTransaction? tx = null)
    {
        const string sql = @"
SELECT cls, nsp, name, def FROM (
  SELECT 'table' AS cls, n.nspname AS nsp, c.relname AS name, '' AS def
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg\_%'
     AND n.nspname <> 'information_schema'
  UNION ALL
  SELECT 'column', n.nspname, c.relname||'.'||a.attname,
         format_type(a.atttypid,a.atttypmod)||'|notnull='||a.attnotnull::text
    FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
         JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE a.attnum > 0 AND NOT a.attisdropped
     AND c.relkind IN ('r','p') AND n.nspname NOT LIKE 'pg\_%'
     AND n.nspname <> 'information_schema'
  UNION ALL
  SELECT 'index', n.nspname, c.relname, pg_get_indexdef(i.indexrelid)
    FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
         JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname<>'information_schema'
  UNION ALL
  SELECT 'view', n.nspname, c.relname, pg_get_viewdef(c.oid)
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE c.relkind='v' AND n.nspname NOT LIKE 'pg\_%'
     AND n.nspname <> 'information_schema'
  UNION ALL
  SELECT 'function', n.nspname,
         p.proname||'('||oidvectortypes(p.proargtypes)||')',
         pg_get_functiondef(p.oid)
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
   WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname<>'information_schema'
     AND p.prokind <> 'a'
  UNION ALL
  SELECT 'trigger', n.nspname, t.tgname, pg_get_triggerdef(t.oid)
    FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
         JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE NOT t.tgisinternal
  UNION ALL
  SELECT 'event_trigger', '', e.evtname, e.evtevent
    FROM pg_event_trigger e
  UNION ALL
  SELECT 'policy', s.schemaname||'.'||s.tablename, s.policyname,
         COALESCE(s.qual,'')||'|'||COALESCE(s.with_check,'')
    FROM pg_policies s
  UNION ALL
  SELECT 'constraint', n.nspname, c.conname,
         pg_get_constraintdef(c.oid)
    FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid
         JOIN pg_namespace n ON n.oid=r.relnamespace
   WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname<>'information_schema'
  UNION ALL
  SELECT 'sequence', n.nspname, c.relname, ''
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE c.relkind='S' AND n.nspname NOT LIKE 'pg\_%'
  UNION ALL
  SELECT 'schema', '', n.nspname, ''
    FROM pg_namespace n
   WHERE n.nspname NOT LIKE 'pg\_%' AND n.nspname<>'information_schema'
  UNION ALL
  SELECT 'extension', '', e.extname, e.extversion
    FROM pg_extension e
) manifest ORDER BY cls, nsp, name";
        var lines = new List<string>();
        using var command = new NpgsqlCommand(sql, connection, tx);
        using var reader = command.ExecuteReader();
        while (reader.Read())
        {
            var def = reader.IsDBNull(3) ? "" : reader.GetString(3);
            lines.Add($"{reader.GetString(0)}|{reader.GetString(1)}|"
                + $"{reader.GetString(2)}|{Sha256Text(def)}");
        }
        lines.Sort(StringComparer.Ordinal);
        return Sha256Text(string.Join('\n', lines));
    }

    // ------------------------------------------------------------------
    // --migration-status (read-only, runtime DSN)
    // ------------------------------------------------------------------

    public static Dictionary<string, object?> Status(string repoRoot)
    {
        using var connection = PgDsn.Readonly();
        var contract = Query(connection,
            $"SELECT * FROM {PgDsn.CodexSchema}"
            + ".sql_migration_executor_contract LIMIT 1");
        var registry = ReadRegistry(connection);
        var receipts = ReadReceipts(connection);
        var byMigration = receipts
            .GroupBy(r => r.MigrationId)
            .ToDictionary(g => g.Key, g => g.Last());

        var rows = new List<Dictionary<string, object?>>();
        var match = 0; var mismatch = 0; var missingFile = 0;
        long? nextApplicable = null;
        foreach (var row in registry)
        {
            var isGenesisBaseline =
                row.MigrationId == "MIG_PG_BASELINE_000000";
            var file = RepoFile(repoRoot, row.ForwardIdentity);
            string sourceCheck;
            string? liveHash = null;
            if (isGenesisBaseline)
                sourceCheck = "baseline-declared";
            else if (!File.Exists(file))
            {
                sourceCheck = "SOURCE_FILE_MISSING";
                missingFile++;
            }
            else
            {
                liveHash = FileSha256(file);
                if (liveHash == row.MigrationSourceHash)
                {
                    sourceCheck = "match"; match++;
                }
                else
                {
                    sourceCheck = "SQL_SCHEMA_VERSION_MISMATCH";
                    mismatch++;
                }
            }
            byMigration.TryGetValue(row.MigrationId, out var receipt);
            rows.Add(new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["sequence"] = row.Sequence,
                ["migration_id"] = row.MigrationId,
                ["forward_identity"] = row.ForwardIdentity,
                ["registry_status"] = row.Status,
                ["predecessor_id"] = row.PredecessorId,
                ["source_hash"] = sourceCheck,
                ["live_source_sha256"] = liveHash,
                ["receipt"] = receipt is null ? null
                    : $"{receipt.TransactionResult}/{receipt.VerificationResult}",
            });
            // first row that is neither baseline nor receipt-committed
            // is the head of the unapplied chain
            if (nextApplicable is null
                && row.PredecessorId != "MIG_PG_BASELINE_000000")
            { /* chain gate handled below */ }
        }

        // chain gate: seq1 applies from baseline; seqN requires a
        // COMMITTED+PASS receipt for seqN-1.
        var committed = receipts
            .Where(r => r.TransactionResult == "COMMITTED"
                && r.VerificationResult == "PASS")
            .Select(r => r.MigrationId).ToHashSet();
        var byId = registry.ToDictionary(r => r.MigrationId);
        foreach (var row in registry)
        {
            if (row.MigrationId == "MIG_PG_BASELINE_000000")
                continue; // genesis declaration, never "applied"
            var isBaseline = row.PredecessorId
                is null or "MIG_PG_BASELINE_000000";
            var predCommitted = !isBaseline
                && row.PredecessorId is not null
                && committed.Contains(row.PredecessorId);
            if ((isBaseline || predCommitted)
                && !committed.Contains(row.MigrationId))
            {
                nextApplicable = row.Sequence;
                break;
            }
        }

        var filesOnDisk = Directory
            .GetFiles(Path.Combine(repoRoot,
                "shared-layer", "migrations"), "*.sql")
            .Select(Path.GetFileName).ToHashSet();
        var unregistered = filesOnDisk.Where(name =>
            !registry.Any(r =>
                r.ForwardIdentity == $"shared-layer/migrations/{name}"))
            .OrderBy(n => n, StringComparer.Ordinal).ToList();

        var manifest = LiveManifestHash(connection);
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["artifact"] = "migration-executor-status/v1",
            ["executor_identity"] = ExecutorIdentity,
            ["contract_order"] = ContractOrder,
            ["contract"] = contract.Count == 0 ? null : contract[0],
            ["registry_rows"] = registry.Count,
            ["receipts"] = receipts.Count,
            ["committed_receipts"] = committed.Count,
            ["source_hash"] = new Dictionary<string, object?>(
                StringComparer.Ordinal)
            {
                ["match"] = match, ["mismatch"] = mismatch,
                ["source_file_missing"] = missingFile,
            },
            ["unregistered_files"] = unregistered,
            ["next_applicable_sequence"] = nextApplicable,
            ["live_manifest_recipe"] = ManifestRecipe,
            ["live_manifest_sha256"] = manifest,
            ["migrations"] = rows,
        };
    }

    // ------------------------------------------------------------------
    // --migration-apply (write path, admin DSN, global advisory lock)
    // ------------------------------------------------------------------

    internal sealed class ApplyDenied : Exception
    {
        public ApplyDenied(string message) : base(message) { }
    }

    private static string ReceiptHash(
        Dictionary<string, object?> fields, string previousHash) =>
        Sha256Text(CanonJson.Serialize(fields) + "|" + previousHash);

    private static void WriteReceipt(NpgsqlConnection connection,
        NpgsqlTransaction? tx, RegistryRow row, string lockIdentity,
        DateTime startedUtc, DateTime? committedUtc,
        string transactionResult, string verificationResult,
        string observedTargetHash, string previousHash,
        out string receiptId, out string receiptHash)
    {
        var fields = new Dictionary<string, object?>(
            StringComparer.Ordinal)
        {
            ["migration_id"] = row.MigrationId,
            ["executor_identity"] = ExecutorIdentity,
            ["source_schema_hash"] = row.SourceSchemaHash,
            ["expected_target_hash"] = row.TargetSchemaHash,
            ["observed_target_hash"] = observedTargetHash,
            ["migration_source_hash"] = row.MigrationSourceHash,
            ["migration_lock_identity"] = lockIdentity,
            ["started_at_utc"] = startedUtc.ToString(
                "yyyy-MM-dd'T'HH:mm:ss'Z'"),
            ["committed_at_utc"] = committedUtc?.ToString(
                "yyyy-MM-dd'T'HH:mm:ss'Z'"),
            ["transaction_result"] = transactionResult,
            ["verification_result"] = verificationResult,
            ["manifest_recipe"] = ManifestRecipe,
        };
        receiptHash = ReceiptHash(fields, previousHash);
        receiptId = "mrcpt-" + receiptHash[..12];
        using var command = new NpgsqlCommand(
            $"INSERT INTO {PgDsn.CodexSchema}.sql_migration_receipt_registry"
            + " (receipt_id, migration_id, executor_identity,"
            + " source_schema_hash, expected_target_hash,"
            + " observed_target_hash, migration_source_hash,"
            + " migration_lock_identity, started_at_utc, committed_at_utc,"
            + " transaction_result, verification_result,"
            + " previous_receipt_hash, receipt_hash)"
            + " VALUES (@id,@mid,@exec,@src,@exp,@obs,@srch,@lock,"
            + " @st,@co,@txr,@vr,@prev,@rh)",
            connection, tx);
        command.Parameters.AddWithValue("id", receiptId);
        command.Parameters.AddWithValue("mid", row.MigrationId);
        command.Parameters.AddWithValue("exec", ExecutorIdentity);
        command.Parameters.AddWithValue("src", row.SourceSchemaHash);
        command.Parameters.AddWithValue("exp", row.TargetSchemaHash);
        command.Parameters.AddWithValue("obs", observedTargetHash);
        command.Parameters.AddWithValue("srch", row.MigrationSourceHash);
        command.Parameters.AddWithValue("lock", lockIdentity);
        command.Parameters.AddWithValue("st", startedUtc);
        command.Parameters.AddWithValue("co",
            committedUtc.HasValue ? committedUtc.Value : DBNull.Value);
        command.Parameters.AddWithValue("txr", transactionResult);
        command.Parameters.AddWithValue("vr", verificationResult);
        command.Parameters.AddWithValue("prev", previousHash);
        command.Parameters.AddWithValue("rh", receiptHash);
        command.ExecuteNonQuery();
    }

    /// <summary>Governed single-migration apply.  Every contract gate is
    /// fail-closed: unregistered SQL, a missing predecessor receipt, a
    /// source-hash mismatch or an unverifiable target state all deny —
    /// nothing is ever committed on a failed verification.</summary>
    public static Dictionary<string, object?> Apply(string repoRoot,
        long sequence, string? databaseOverride = null)
    {
        var builder = new NpgsqlConnectionStringBuilder(
            PgDsn.Normalize(PgDsn.AdminDsn()));
        if (databaseOverride is not null)
            // test lane: a scratch database carries its own registry
            // copy; never silently redirects — the target is explicit.
            builder.Database = databaseOverride;
        using var connection =
            new NpgsqlConnection(builder.ConnectionString);
        connection.Open();

        var result = new Dictionary<string, object?>(
            StringComparer.Ordinal)
        {
            ["artifact"] = "migration-executor-apply/v1",
            ["executor_identity"] = ExecutorIdentity,
            ["sequence"] = sequence,
            ["applied"] = false,
        };
        var steps = new List<Dictionary<string, object?>>();
        result["steps"] = steps;
        void Step(string name, bool ok, string detail = "") =>
            steps.Add(new Dictionary<string, object?>
            {
                ["step"] = name, ["ok"] = ok, ["detail"] = detail,
            });
        ApplyDenied Deny(string step, string code, string detail)
        {
            Step(step, false, detail);
            return new ApplyDenied(code);
        }

        // 1. read-registry
        var contract = Query(connection,
            $"SELECT * FROM {PgDsn.CodexSchema}"
            + ".sql_migration_executor_contract LIMIT 1");
        if (contract.Count == 0
            || $"{contract[0].GetValueOrDefault("status")}" != "active")
            throw Deny("read-registry", "EXECUTOR_CONTRACT_INACTIVE",
                "no active sql_migration_executor_contract row");
        var registry = ReadRegistry(connection);
        var row = registry.FirstOrDefault(r => r.Sequence == sequence);
        if (row is null)
            throw Deny("read-registry", "REGISTRY_ROW_MISSING",
                $"no registry row for sequence {sequence}");
        if (row.Status != "ordered-source-verified")
            throw Deny("read-registry", "REGISTRY_STATUS_NOT_EXECUTABLE",
                $"status={row.Status}");
        if (row.TransactionPolicy
            != "single-governed-migration-transaction")
            throw Deny("read-registry", "TRANSACTION_POLICY_UNSUPPORTED",
                row.TransactionPolicy);
        Step("read-registry", true, row.MigrationId);

        // 2. verify-predecessor — and refuse to re-run a migration
        // whose receipt is already COMMITTED+PASS.
        var receipts = ReadReceipts(connection);
        var prior = receipts.FirstOrDefault(r =>
            r.MigrationId == row.MigrationId
            && r.TransactionResult == "COMMITTED"
            && r.VerificationResult == "PASS");
        if (prior is not null)
            throw Deny("verify-predecessor", "ALREADY_COMMITTED",
                $"receipt={prior.ReceiptId}");
        var previousHash = receipts.Count == 0
            ? new string('0', 64)
            : receipts[^1].ReceiptHash;
        bool predecessorOk;
        if (row.PredecessorId is null or "MIG_PG_BASELINE_000000")
            predecessorOk =
                row.SourceSchemaHash == new string('0', 64);
        else
        {
            var pred = receipts.FirstOrDefault(r =>
                r.MigrationId == row.PredecessorId
                && r.TransactionResult == "COMMITTED"
                && r.VerificationResult == "PASS");
            predecessorOk = pred is not null
                && pred.TransactionResult == "COMMITTED"
                && pred.VerificationResult == "PASS"
                && pred.ObservedTargetHash == row.SourceSchemaHash;
        }
        if (!predecessorOk)
            throw Deny("verify-predecessor",
                "PREDECESSOR_NOT_COMMITTED",
                $"predecessor={row.PredecessorId}");
        Step("verify-predecessor", true,
            row.PredecessorId ?? "baseline");

        // 3. verify-live-source-hash
        var file = RepoFile(repoRoot, row.ForwardIdentity);
        if (!File.Exists(file))
            throw Deny("verify-live-source-hash", "SOURCE_FILE_MISSING",
                row.ForwardIdentity);
        string approvedSql;
        try { approvedSql = VerifiedSource(file, row.MigrationSourceHash); }
        catch (ApplyDenied)
        {
            throw Deny("verify-live-source-hash",
                "SQL_SCHEMA_VERSION_MISMATCH", row.ForwardIdentity);
        }
        Step("verify-live-source-hash", true, row.MigrationSourceHash);

        // 4. acquire-global-lock — try once; a held lock denies rather
        // than queues (the contract forbids concurrent executors).
        bool locked;
        using (var lockCommand = new NpgsqlCommand(
            "SELECT pg_try_advisory_lock(hashtextextended(@n,0))",
            connection))
        {
            lockCommand.Parameters.AddWithValue("n", LockName);
            locked = lockCommand.ExecuteScalar() is true;
        }
        if (!locked)
            throw Deny("acquire-global-lock", "LOCK_UNAVAILABLE",
                LockName);
        var lockIdentity =
            $"{LockName}/pid:{Scalar(connection, "SELECT pg_backend_pid()")}";
        Step("acquire-global-lock", true, lockIdentity);
        var startedUtc = DateTime.UtcNow;

        string observedHash = "";
        string failureCode = "";
        try
        {
            // Earlier checks preceded the lock. A competing executor may
            // have committed since then, or the governor may have changed
            // the approval. Reconcile both before executing any DDL.
            var lockedContract = Query(connection,
                $"SELECT * FROM {PgDsn.CodexSchema}"
                + ".sql_migration_executor_contract LIMIT 1");
            if (lockedContract.Count == 0
                || $"{lockedContract[0].GetValueOrDefault("status")}" != "active")
                throw Deny("read-registry", "EXECUTOR_CONTRACT_INACTIVE", "changed while acquiring lock");
            var lockedRow = ReadRegistry(connection)
                .FirstOrDefault(r => r.Sequence == sequence);
            if (lockedRow != row)
                throw Deny("read-registry", "REGISTRY_CHANGED", row.MigrationId);
            var lockedReceipts = ReadReceipts(connection);
            if (lockedReceipts.Any(r => r.MigrationId == row.MigrationId
                && r.TransactionResult == "COMMITTED"
                && r.VerificationResult == "PASS"))
                throw Deny("verify-predecessor", "ALREADY_COMMITTED", row.MigrationId);
            if (!lockedReceipts.SequenceEqual(receipts))
                throw Deny("verify-predecessor", "RECEIPT_CHAIN_CHANGED", row.MigrationId);
            // 5. execute-approved-migration (single governed tx)
            using var tx = connection.BeginTransaction();
            try
            {
                using (var flag = new NpgsqlCommand(
                    "SELECT set_config("
                    + "'gptbridge.is_migration_executor','true',true)",
                    connection, tx))
                    flag.ExecuteScalar();
                using (var script = new NpgsqlCommand(
                    approvedSql, connection, tx))
                {
                    script.CommandTimeout = 600;
                    script.ExecuteNonQuery();
                }
                Step("execute-approved-migration", true,
                    row.ForwardIdentity);

                // 6. verify-target-hash — observed manifest must equal
                // the registered expected post-state; anything else is
                // a mismatch and the transaction is rolled back.
                observedHash = LiveManifestHash(connection, tx);
                if (observedHash != row.TargetSchemaHash)
                {
                    Step("verify-target-hash", false,
                        $"observed={observedHash}");
                    tx.Rollback();
                    failureCode = "TARGET_HASH_MISMATCH";
                }
                else
                {
                    Step("verify-target-hash", true, observedHash);
                    // 7. write-receipt inside the same governed tx —
                    // receipt and migration commit atomically.
                    WriteReceipt(connection, tx, row, lockIdentity,
                        startedUtc, DateTime.UtcNow, "COMMITTED",
                        "PASS", observedHash, previousHash,
                        out var rid, out var rh);
                    tx.Commit();
                    Step("write-receipt", true, rid);
                    result["receipt_id"] = rid;
                    result["receipt_hash"] = rh;
                    result["applied"] = true;
                }
            }
            catch (PostgresException error)
            {
                try { tx.Rollback(); } catch { }
                Step("execute-approved-migration", false,
                    $"{error.SqlState}:{error.MessageText}");
                failureCode = "EXECUTION_FAILED";
            }
            catch (Exception error)
            {
                try { tx.Rollback(); } catch { }
                Step("execute-approved-migration", false,
                    $"{error.GetType().Name}:{error.Message}");
                failureCode = "EXECUTION_FAILED";
            }
            // Keep the global lock until a rolled-back attempt has appended
            // its receipt too, so another executor cannot fork the chain.
            if (failureCode.Length > 0)
            {
                try
                {
                    var receiptsNow = ReadReceipts(connection);
                    var prev = receiptsNow.Count == 0
                        ? new string('0', 64) : receiptsNow[^1].ReceiptHash;
                    WriteReceipt(connection, null, row, lockIdentity,
                        startedUtc, null, "ROLLED_BACK", failureCode,
                        observedHash, prev, out var rid, out var rh);
                    result["receipt_id"] = rid;
                    result["receipt_hash"] = rh;
                }
                catch (Exception error)
                {
                    result["receipt_write_error"] =
                        $"{error.GetType().Name}:{error.Message}";
                }
                result["denied"] = failureCode;
            }
        }
        finally
        {
            // 8. release-lock (advisory locks also die with the session)
            try
            {
                using var unlock = new NpgsqlCommand(
                    "SELECT pg_advisory_unlock(hashtextextended(@n,0))",
                    connection);
                unlock.Parameters.AddWithValue("n", LockName);
                unlock.ExecuteScalar();
            }
            catch { }
            Step("release-lock", true, LockName);
        }

        return result;
    }
}
