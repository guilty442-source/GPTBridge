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
Console.WriteLine(JsonSerializer.Serialize(new { artifact = "schema-parity-evidence-regression", passed, failed = 0 }));
