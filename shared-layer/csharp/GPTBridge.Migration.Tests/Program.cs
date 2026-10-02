using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using GPTBridge.CodexPipeline;

var type = typeof(SemanticHashToolchain).Assembly.GetType("GPTBridge.CodexPipeline.MigrationExecutor", true)!;
var load = type.GetMethod("VerifiedSource", BindingFlags.Static | BindingFlags.NonPublic)!;
var file = Path.Combine(Path.GetTempPath(), $"gptbridge-migration-{Guid.NewGuid():N}.sql");
var passed = 0;
string Hash(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
string Read(string hash) => (string)load.Invoke(null, new object[] { file, hash })!;
void Equal(string expected, string actual)
{
    if (expected != actual) throw new Exception("MIGRATION_SOURCE_BINDING_FAILED");
    passed++;
}
try
{
    const string sql = "SELECT '星澄';\r\n";
    var bytes = Encoding.UTF8.GetBytes(sql);
    File.WriteAllBytes(file, bytes);
    var approved = Read(Hash(bytes));
    Equal(sql, approved);
    File.WriteAllText(file, "DROP TABLE protected_data;");
    Equal(sql, approved); // A later file replacement cannot change executed SQL.
    try { Read(Hash(bytes)); throw new Exception("CHANGED_SOURCE_ACCEPTED"); }
    catch (TargetInvocationException error) when (error.InnerException?.Message == "SQL_SCHEMA_VERSION_MISMATCH") { passed++; }
    bytes = Encoding.UTF8.GetPreamble().Concat(Encoding.UTF8.GetBytes(sql)).ToArray();
    File.WriteAllBytes(file, bytes);
    Equal(sql, Read(Hash(bytes))); // Hash covers BOM; decoder preserves old read behavior.
    bytes = Encoding.Unicode.GetPreamble().Concat(Encoding.Unicode.GetBytes(sql)).ToArray();
    File.WriteAllBytes(file, bytes);
    Equal(sql, Read(Hash(bytes)));
    File.WriteAllBytes(file, Array.Empty<byte>());
    Equal("", Read(Hash(Array.Empty<byte>())));
}
finally { File.Delete(file); }
if (args.Contains("--integration")) passed += Integration.Run();
Console.WriteLine(JsonSerializer.Serialize(new { artifact = "migration-source-binding-regression", passed, failed = 0 }));
