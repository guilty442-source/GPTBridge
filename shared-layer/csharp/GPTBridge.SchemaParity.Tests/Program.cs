using System.Reflection;
using System.Text.Json;
using GPTBridge.CodexPipeline;

var type = typeof(SemanticHashToolchain).Assembly.GetType("GPTBridge.CodexPipeline.MachineSchemaParity", true)!;
var evaluate = type.GetMethod("EvaluateEvidence", BindingFlags.Static | BindingFlags.NonPublic)!;
var fence = type.GetMethod("AssertGeneration", BindingFlags.Static | BindingFlags.NonPublic)!;
const string generation = "2026-10-02T11:52:44Z";
var row = new Dictionary<string, object?> { ["schema_code"] = "TEST", ["parity_status"] = "PASS" };
var hash = SemanticHashToolchain.ComputeProducerHash(row);
foreach (var field in new[] { "canonical_semantic_hash", "evidence_producer_hash", "evidence_validator_hash", "evidence_persistence_hash" }) row[field] = hash;
row["evidence_status"] = "PASS";
row["evidence_generation"] = generation;
var passed = 0;
Dictionary<string, object?> Evaluate(Dictionary<string, object?> value) =>
    (Dictionary<string, object?>)evaluate.Invoke(null, new object[] { value, generation })!;
void Equal(object expected, object? actual, string name)
{
    if (!Equals(expected, actual)) throw new Exception("SCHEMA_PARITY_REGRESSION:" + name);
    passed++;
}
Equal("PASS", Evaluate(row)["current_evidence_status"], "complete-current-receipt");
var verified = new Dictionary<string, object?>(row) { ["parity_status"] = "VERIFIED" };
Equal("PASS", Evaluate(verified)["current_evidence_status"], "canonical-registry-label-with-current-receipt");
verified["evidence_generation"] = "old";
Equal("INCOMPLETE_EVIDENCE", Evaluate(verified)["current_evidence_status"], "verified-label-alone-does-not-pass");
Equal("CANONICAL_MISMATCH", SemanticHashToolchain.EvaluateRow(row, new string('0', 64), hash)["persistence_layer"], "content-hash-cannot-mask-canonical-mismatch");
Equal("MATCH", SemanticHashToolchain.EvaluateRow(row, null, hash)["persistence_layer"], "legacy-content-hash-fallback");
foreach (var field in new[] { "parity_status", "evidence_status", "evidence_generation", "canonical_semantic_hash", "evidence_producer_hash", "evidence_validator_hash", "evidence_persistence_hash" })
{
    foreach (var invalid in new object?[] { null, "historical-or-mismatched" })
    {
        var changed = new Dictionary<string, object?>(row) { [field] = invalid };
        Equal("INCOMPLETE_EVIDENCE", Evaluate(changed)["current_evidence_status"], field);
    }
}
var stale = new Dictionary<string, object?>(row) { ["evidence_generation"] = "old" };
Equal("MATCH", Evaluate(stale)["persistence_layer"], "historical-hash-match-retained");
Equal("INCOMPLETE_EVIDENCE", Evaluate(stale)["current_evidence_status"], "match-is-not-current-certification");
var identity = new Dictionary<string, object?> { ["codex_version"] = generation, ["source_sha256"] = new string('a', 64) };
fence.Invoke(null, new object[] { identity, identity });
passed++;
foreach (var field in new[] { "codex_version", "source_sha256" })
{
    var changed = new Dictionary<string, object?>(identity) { [field] = "changed" };
    try { fence.Invoke(null, new object[] { identity, changed }); throw new Exception("GENERATION_DRIFT_ACCEPTED"); }
    catch (TargetInvocationException error) when (error.InnerException?.Message == "BLOCKED_GENERATION_DRIFT") { passed++; }
}
var projection = typeof(SemanticHashToolchain).Assembly.GetType("GPTBridge.CodexPipeline.GenerationProjections", true)!;
foreach (var invalidHash in new object?[] { null, "", "invalid", new string('g', 64), new string('A', 64), new string('a', 63), new string('a', 65) })
{
    var invalid = new Dictionary<string, object?>(identity) { ["source_sha256"] = invalidHash };
    try { fence.Invoke(null, new object[] { invalid, invalid }); throw new Exception("INVALID_GENERATION_HASH_ACCEPTED"); }
    catch (TargetInvocationException error) when (error.InnerException?.Message == "BLOCKED_GENERATION_DRIFT") { passed++; }
}
var matches = projection.GetMethod("SchemaReceiptMatches", BindingFlags.Static | BindingFlags.NonPublic)!;
var receipt = new Dictionary<string, object?> { ["status"] = "PASS", ["validated_against_version"] = generation };
foreach (var field in new[] { "producer_semantic_hash", "validator_semantic_hash", "persistence_semantic_hash", "canonical_semantic_hash" }) receipt[field] = hash;
bool Matches(Dictionary<string, object?> value) => (bool)matches.Invoke(null, new object[] { row, value, generation })!;
Equal(true, Matches(receipt), "projection-current-descriptor-bound-receipt");
foreach (var field in new[] { "status", "validated_against_version", "producer_semantic_hash", "validator_semantic_hash", "persistence_semantic_hash", "canonical_semantic_hash" })
{
    var invalid = new Dictionary<string, object?>(receipt) { [field] = "stale-or-invalid" };
    Equal(false, Matches(invalid), "projection-rejects:" + field);
}
foreach (var field in new[] { "producer_semantic_hash", "validator_semantic_hash", "persistence_semantic_hash", "canonical_semantic_hash" }) receipt[field] = "";
Equal(false, Matches(receipt), "empty-equal-hashes-never-verify");
foreach (var field in new[] { "producer_semantic_hash", "validator_semantic_hash", "persistence_semantic_hash", "canonical_semantic_hash" }) receipt[field] = new string('0', 64);
Equal(false, Matches(receipt), "arbitrary-equal-hashes-never-verify");
if (args.Contains("--integration")) passed += ProjectionIntegration.Run();
var normalize = typeof(SemanticHashToolchain).Assembly.GetType("GPTBridge.CodexPipeline.PgDsn", true)!
    .GetMethod("Normalize", BindingFlags.Static | BindingFlags.NonPublic)!;
var canonicalDsn = new Npgsql.NpgsqlConnectionStringBuilder
{
    Host = "127.0.0.1", Database = "scratch", Username = "fixture", Password = "fixture;quoted value",
}.ConnectionString;
var normalizedDsn = (string)normalize.Invoke(null, new object[] { canonicalDsn })!;
Equal(canonicalDsn, normalizedDsn, "canonical-npgsql-dsn-roundtrip");
normalizedDsn = (string)normalize.Invoke(null, new object[] { "host=127.0.0.1 dbname=scratch user=fixture password='fixture;quoted value'" })!;
Equal("fixture;quoted value", new Npgsql.NpgsqlConnectionStringBuilder(normalizedDsn).Password, "libpq-quoted-semicolon-preserved");
Console.WriteLine(JsonSerializer.Serialize(new { artifact = "schema-parity-evidence-regression", passed, failed = 0 }));
