using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using Npgsql;

internal static class Integration
{
    public static int Run()
    {
        var assembly = typeof(GPTBridge.CodexPipeline.SemanticHashToolchain).Assembly;
        var executor = assembly.GetType("GPTBridge.CodexPipeline.MigrationExecutor", true)!;
        var normalize = assembly.GetType("GPTBridge.CodexPipeline.PgDsn", true)!
            .GetMethod("Normalize", BindingFlags.Static | BindingFlags.NonPublic)!;
        var dsn = Environment.GetEnvironmentVariable("GPTBRIDGE_POSTGRES_ADMIN_DSN")
            ?? throw new Exception("GPTBRIDGE_POSTGRES_ADMIN_DSN_REQUIRED");
        var builder = new NpgsqlConnectionStringBuilder((string)normalize.Invoke(null, new object[] { dsn })!);
        builder.Database = "postgres";
        var database = "gptbridge_migration_test_" + Guid.NewGuid().ToString("N");
        var root = Path.Combine(Path.GetTempPath(), database);
        Directory.CreateDirectory(root);
        using var admin = new NpgsqlConnection(builder.ConnectionString);
        admin.Open();
        void Execute(NpgsqlConnection connection, string sql)
        {
            using var command = new NpgsqlCommand(sql, connection);
            command.ExecuteNonQuery();
        }
        Execute(admin, $"CREATE DATABASE {database}");
        var passed = 0;
        try
        {
            builder.Database = database;
            using var scratch = new NpgsqlConnection(builder.ConnectionString);
            scratch.Open();
            Execute(scratch, """
                CREATE SCHEMA gptbridge_codex;
                CREATE TABLE gptbridge_codex.sql_migration_executor_contract(status text);
                INSERT INTO gptbridge_codex.sql_migration_executor_contract VALUES ('active');
                CREATE TABLE gptbridge_codex.sql_migration_registry(
                    sequence bigint, migration_id text, predecessor_id text, forward_identity text,
                    source_schema_hash text, target_schema_hash text, migration_source_hash text,
                    transaction_policy text, status text, introduced_version text);
                CREATE TABLE gptbridge_codex.sql_migration_receipt_registry(
                    receipt_id text, migration_id text, executor_identity text, source_schema_hash text,
                    expected_target_hash text, observed_target_hash text, migration_source_hash text,
                    migration_lock_identity text, started_at_utc timestamptz, committed_at_utc timestamptz,
                    transaction_result text, verification_result text, previous_receipt_hash text, receipt_hash text);
                """);
            var manifest = executor.GetMethod("LiveManifestHash", BindingFlags.Static | BindingFlags.NonPublic)!;
            var target = (string)manifest.Invoke(null, new object?[] { scratch, null })!;
            var apply = executor.GetMethod("Apply", BindingFlags.Static | BindingFlags.Public)!;
            Dictionary<string, object?> Apply() => (Dictionary<string, object?>)apply.Invoke(null, new object?[] { root, 1L, database })!;
            object? Scalar(string sql)
            {
                using var command = new NpgsqlCommand(sql, scratch);
                return command.ExecuteScalar();
            }
            void Require(bool ok, string name)
            {
                if (!ok) throw new Exception("MIGRATION_INTEGRATION_FAILED:" + name);
                passed++;
            }
            void Register(string sql, string expected)
            {
                File.WriteAllText(Path.Combine(root, "test.sql"), sql, new UTF8Encoding(false));
                var hash = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(Path.Combine(root, "test.sql")))).ToLowerInvariant();
                Execute(scratch, "TRUNCATE gptbridge_codex.sql_migration_registry");
                using var insert = new NpgsqlCommand("""
                    INSERT INTO gptbridge_codex.sql_migration_registry VALUES
                    (1,'TEST_ONLY',NULL,'test.sql',@zero,@target,@source,
                    'single-governed-migration-transaction','ordered-source-verified','test-only')
                    """, scratch);
                insert.Parameters.AddWithValue("zero", new string('0', 64));
                insert.Parameters.AddWithValue("target", expected);
                insert.Parameters.AddWithValue("source", hash);
                insert.ExecuteNonQuery();
            }
            Register("CREATE TABLE public.must_rollback(id integer);", target);
            var result = Apply();
            Require(Equals(result.GetValueOrDefault("denied"), "TARGET_HASH_MISMATCH"), "target-mismatch-denied");
            Require(Equals(Scalar("SELECT to_regclass('public.must_rollback') IS NULL"), true), "DDL-rolled-back");
            Require(Equals(Scalar("SELECT transaction_result FROM gptbridge_codex.sql_migration_receipt_registry"), "ROLLED_BACK"), "rollback-receipt-persisted");
            // The receipt insert must still hold the executor lock.
            Execute(scratch, """
                CREATE FUNCTION public.require_executor_lock() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  IF NOT EXISTS(SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory' AND granted) THEN
                    RAISE EXCEPTION 'EXECUTOR_LOCK_RELEASED_BEFORE_RECEIPT';
                  END IF;
                  RETURN NEW;
                END $$;
                CREATE TRIGGER check_executor_lock BEFORE INSERT ON gptbridge_codex.sql_migration_receipt_registry
                FOR EACH ROW EXECUTE FUNCTION public.require_executor_lock();
                """);
            target = (string)manifest.Invoke(null, new object?[] { scratch, null })!;
            Register("CREATE TABLE public.must_rollback(id integer);", target);
            result = Apply();
            Require(!result.ContainsKey("receipt_write_error") && result.ContainsKey("receipt_id"), "rollback-receipt-holds-lock");
            Register("SELECT 1;", target);
            Execute(scratch, "SELECT pg_advisory_lock(hashtextextended('sql_migration_executor',0))");
            try
            {
                try { Apply(); throw new Exception("LOCKED_EXECUTOR_ACCEPTED"); }
                catch (TargetInvocationException error) when (error.InnerException?.Message == "LOCK_UNAVAILABLE") { passed++; }
            }
            finally { Execute(scratch, "SELECT pg_advisory_unlock(hashtextextended('sql_migration_executor',0))"); }
            result = Apply();
            Require(Equals(result["applied"], true), "approved-migration-committed");
            Require(Equals(Scalar("SELECT count(*) FROM gptbridge_codex.sql_migration_receipt_registry WHERE transaction_result='COMMITTED' AND verification_result='PASS'"), 1L), "single-success-receipt");
            try { Apply(); throw new Exception("DUPLICATE_MIGRATION_ACCEPTED"); }
            catch (TargetInvocationException error) when (error.InnerException?.Message == "ALREADY_COMMITTED") { passed++; }
            using var lockProbe = new NpgsqlCommand("SELECT pg_try_advisory_lock(hashtextextended('sql_migration_executor',0))", scratch);
            Require(Equals(lockProbe.ExecuteScalar(), true), "executor-releases-lock");
            Execute(scratch, "SELECT pg_advisory_unlock(hashtextextended('sql_migration_executor',0))");
            return passed;
        }
        finally
        {
            NpgsqlConnection.ClearAllPools();
            Execute(admin, $"DROP DATABASE {database} WITH (FORCE)");
            File.Delete(Path.Combine(root, "test.sql"));
            Directory.Delete(root);
        }
    }
}
