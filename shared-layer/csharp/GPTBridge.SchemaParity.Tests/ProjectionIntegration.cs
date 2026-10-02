using System.Reflection;
using Npgsql;
using GPTBridge.CodexPipeline;

internal static class ProjectionIntegration
{
    public static int Run()
    {
        var assembly = typeof(SemanticHashToolchain).Assembly;
        var normalize = assembly.GetType("GPTBridge.CodexPipeline.PgDsn", true)!.GetMethod("Normalize", BindingFlags.Static | BindingFlags.NonPublic)!;
        var rawDsn = Environment.GetEnvironmentVariable("GPTBRIDGE_POSTGRES_ADMIN_DSN") ?? throw new Exception("ADMIN_DSN_REQUIRED");
        var builder = new NpgsqlConnectionStringBuilder((string)normalize.Invoke(null, new object[] { rawDsn })!) { Database = "postgres" };
        using var admin = new NpgsqlConnection(builder.ConnectionString);
        admin.Open();
        void Execute(NpgsqlConnection connection, string sql)
        {
            using var command = new NpgsqlCommand(sql, connection); command.ExecuteNonQuery();
        }
        var database = "gptbridge_parity_test_" + Guid.NewGuid().ToString("N");
        Execute(admin, $"CREATE DATABASE {database}");
        try
        {
            builder.Database = database;
            using var scratch = new NpgsqlConnection(builder.ConnectionString);
            scratch.Open();
            Execute(scratch, """
                CREATE SCHEMA parity_fixture;
                CREATE TABLE parity_fixture.metadata(key text,value text);
                INSERT INTO parity_fixture.metadata VALUES ('codex_version','current');
                CREATE TABLE parity_fixture.machine_schema_registry(schema_code text,parity_status text);
                INSERT INTO parity_fixture.machine_schema_registry VALUES ('TEST','VERIFIED');
                CREATE TABLE parity_fixture.machine_schema_parity_evidence(schema_code text,status text,
                    validated_against_version text,producer_semantic_hash text,validator_semantic_hash text,
                    persistence_semantic_hash text,canonical_semantic_hash text);
                """);
            var descriptor = new Dictionary<string, object?> { ["schema_code"] = "TEST" };
            var hash = SemanticHashToolchain.ComputeProducerHash(descriptor);
            using (var insert = new NpgsqlCommand("""
                INSERT INTO parity_fixture.machine_schema_parity_evidence VALUES ('TEST','PASS','old',@hash,@hash,@hash,@hash)
                """, scratch))
            {
                insert.Parameters.AddWithValue("hash", hash); insert.ExecuteNonQuery();
            }
            var stageType = assembly.GetType("GPTBridge.CodexPipeline.StageConnection", true)!;
            var projection = assembly.GetType("GPTBridge.CodexPipeline.GenerationProjections", true)!;
            var rebuild = projection.GetMethod("RebuildSchemaParityStatus", BindingFlags.Static | BindingFlags.NonPublic)!;
            int Rebuild()
            {
                using var stage = (IDisposable)Activator.CreateInstance(stageType, BindingFlags.Instance | BindingFlags.NonPublic,
                    null, new object[] { "parity_fixture", builder.ConnectionString, true }, null)!;
                var count = (int)rebuild.Invoke(null, new object[] { stage })!;
                stageType.GetMethod("Commit")!.Invoke(stage, null);
                return count;
            }
            string Status()
            {
                using var read = new NpgsqlCommand("SELECT parity_status FROM parity_fixture.machine_schema_registry", scratch);
                return (string)read.ExecuteScalar()!;
            }
            var passed = 0;
            void Require(bool condition, string name)
            {
                if (!condition) throw new Exception("PARITY_PROJECTION_INTEGRATION_FAILED:" + name);
                passed++;
            }
            Require(Rebuild() == 1 && Status() == "PENDING", "historical-receipt-demotes-verification");
            Execute(scratch, "UPDATE parity_fixture.machine_schema_parity_evidence SET validated_against_version='current'");
            Require(Rebuild() == 1 && Status() == "VERIFIED", "current-descriptor-receipt-verifies");
            Require(Rebuild() == 0 && Status() == "VERIFIED", "idempotent-projection");
            Execute(scratch, "UPDATE parity_fixture.machine_schema_parity_evidence SET producer_semantic_hash='',validator_semantic_hash='',persistence_semantic_hash='',canonical_semantic_hash=''");
            Require(Rebuild() == 1 && Status() == "PENDING", "equal-empty-hashes-denied");
            Execute(scratch, "UPDATE parity_fixture.machine_schema_registry SET parity_status='UNAVAILABLE'");
            Require(Rebuild() == 0 && Status() == "UNAVAILABLE", "owner-state-preserved");
            Execute(scratch, "DROP TABLE parity_fixture.machine_schema_parity_evidence;UPDATE parity_fixture.machine_schema_registry SET parity_status='VERIFIED'");
            Require(Rebuild() == 1 && Status() == "PENDING", "missing-ledger-never-verifies");
            return passed;
        }
        finally
        {
            NpgsqlConnection.ClearAllPools();
            Execute(admin, $"DROP DATABASE {database} WITH (FORCE)");
        }
    }
}
