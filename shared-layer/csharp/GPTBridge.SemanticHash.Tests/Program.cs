using System.Reflection;
using System.Text.Json;
using GPTBridge.CodexPipeline;

var passed = 0;
void Equal(object? expected, object? actual, string name)
{
    if (!Equals(expected, actual)) throw new Exception($"FAIL:{name}; expected={expected}; actual={actual}");
    passed++;
}
// Literal CPython JSON oracle vectors: no retired interpreter is executed.
foreach (var (input, expected) in new (double, string)[]
{
    (0.1, "0.1"), (1.0, "1.0"), (-0.0, "-0.0"),
    (1e-4, "0.0001"), (1e-5, "1e-05"), (1e15, "1000000000000000.0"),
    (1e16, "1e+16"), (1e20, "1e+20"), (1.25e16, "1.25e+16"),
    (double.NaN, "NaN"), (double.PositiveInfinity, "Infinity"),
}) Equal(expected, SemanticHashToolchain.CanonicalJson(input), $"float:{expected}");

var row = new Dictionary<string, object?>
{
    ["schema_code"] = "REGRESSION",
    ["schema_version"] = 3L,
    ["required_fields"] = new List<object?> { "id", true, 0.1 },
    ["optional_fields"] = "name|value",
    ["field_types"] = JsonSerializer.Deserialize<JsonElement>("{\"score\":0.1,\"enabled\":true}"),
    ["redaction_rule"] = "e\u0301",
};
var descriptor = SemanticHashToolchain.BuildDescriptor(row);
Equal(3L, descriptor["schema_version"], "numeric-registry-value-retained");
Equal("[\"id\",true,0.1]", SemanticHashToolchain.CanonicalJson(descriptor["required_fields"]), "typed-jsonb-retained");
Equal("[\"name\",\"value\"]", SemanticHashToolchain.CanonicalJson(descriptor["optional_fields"]), "pipe-fallback");
Equal("{\"enabled\":true,\"score\":0.1}", SemanticHashToolchain.CanonicalJson(descriptor["field_types"]), "json-element-retained");
Equal("\"\\u00e9\"", SemanticHashToolchain.CanonicalJson(descriptor["redaction_rule"]), "nfc-and-ascii-escape");
var probe = typeof(SemanticHashToolchain).Assembly.GetType("GPTBridge.CodexPipeline.MachineSchemaParity", true)!;
var diagnostic = probe.GetMethod("CanonicalDescriptorJson", BindingFlags.Static | BindingFlags.NonPublic)!;
Equal(SemanticHashToolchain.CanonicalDescriptorJson(row), diagnostic.Invoke(null, new object[] { row }), "live-probe-uses-shared-descriptor");
Equal("CANONICAL_MISMATCH", SemanticHashToolchain.EvaluateRow(row, new string('0', 64))["persistence_layer"], "mismatch-remains-fail-closed");
Equal("MATCH", SemanticHashToolchain.EvaluateRow(row, SemanticHashToolchain.ComputeProducerHash(row))["persistence_layer"], "matching-canonical");
Console.WriteLine(JsonSerializer.Serialize(new { artifact = "semantic-hash-regression", passed, failed = 0 }));
