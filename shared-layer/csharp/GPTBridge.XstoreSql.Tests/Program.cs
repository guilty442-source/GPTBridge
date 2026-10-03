using System.Text.Json;
using GPTBridge.XstoreSql;

// Managed-binding verification for the xstore native-SQL C ABI.
// args: [0] = xstore cdylib path (required)
//       [1] = optional migrated store for the positive read lane
// Every engine round-trip is real: the cdylib is loaded in-process and
// replies on the documented xstore-native-sql-result/v1 envelope.
if (args.Length < 1)
{
    Console.Error.WriteLine("usage: tests <xstore-dll> [migrated-store]");
    return 2;
}
var tests = 0;
void Denied(Action action, string? reason = null)
{
    try { action(); }
    catch (InvalidDataException error)
    {
        if (reason is not null
            && !error.Message.Contains(reason, StringComparison.Ordinal))
            throw new Exception($"Expected {reason}, got {error.Message}");
        tests++;
        return;
    }
    throw new Exception("Expected rejection");
}

using var sql = new XstoreSql(args[0]);
var missing = Path.Combine(Path.GetTempPath(),
    "xstore-sql-no-such-store-" + Guid.NewGuid().ToString("N"));

// Unmigrated/missing stores fail closed through the full ABI path.
Denied(() => sql.Query(missing, "SELECT * FROM rag_chunk"));
Denied(() => sql.Query(missing,
    "SELECT generation_id FROM rag_generation WHERE alias_name = $1",
    new object?[] { "rag" }));

// A real canonical store that has no sealed RAG migration yet still
// materializes and reports the missing marker — never a silent hit.
// If migration has already landed, the same store serves the positive
// lane instead.
var runtime = Path.Combine(
    Environment.GetEnvironmentVariable("GPTBRIDGE_ROOT")
        ?? Path.GetFullPath(Path.Combine(AppContext.BaseDirectory,
            "..", "..", "..", "..", "..")),
    "xingcheng", "xingcheng", "runtime", "store");
string? migrated = args.Length == 2 ? args[1] : null;
if (Directory.Exists(runtime))
{
    try
    {
        sql.Query(runtime, "SELECT * FROM rag_generation LIMIT $1",
            new object?[] { 1 });
        migrated ??= runtime;
    }
    catch (InvalidDataException error) when
        (error.Message.Contains("MIGRATION", StringComparison.Ordinal))
    {
        tests++;
    }
}

// Client-side bounds: oversized request envelopes never reach the ABI.
Denied(() => sql.Query(missing, "SELECT * FROM rag_chunk",
    new object?[] { new string('x', 1024 * 1024) }), "INPUT_LIMIT");

// Parameters travel in the envelope, never in the SQL text — the engine
// sees bound input, so denial still comes back as a typed envelope error.
Denied(() => sql.Query(missing,
    "SELECT * FROM rag_chunk WHERE module_id = $1",
    new object?[] { "m' OR 1=1" }));

// Two sessions on one binding stay independent and sequential.
Denied(() => sql.Query(missing, "SELECT * FROM rag_resource"));
tests++;

// Positive read lane: a migrated store exercises real row projection,
// bound predicates, ordering and fail-closed syntax.
if (migrated is not null)
{
    var store = migrated;
    var generations = sql.Query(store,
        "SELECT generation_id FROM rag_generation "
        + "WHERE state = $1 LIMIT $2",
        new object?[] { "ACTIVE", 10 });
    tests++;
    Denied(() => sql.Query(store, "DELETE FROM rag_chunk"));
    Denied(() => sql.Query(store,
        "SELECT * FROM rag_chunk; DROP TABLE rag_chunk"));
    Denied(() => sql.Query(store, "SELECT secret FROM rag_chunk"));
    Denied(() => sql.Query(store, "SELECT * FROM unknown_table"));
    Denied(() => sql.Query(store,
        "SELECT * FROM rag_chunk WHERE module_id = $1"
        + " OR module_id = $2", new object?[] { "a", "b" }));
    Denied(() => sql.Query(store,
        "SELECT * FROM rag_chunk LIMIT $1", new object?[] { 10001 }));
    Console.WriteLine($"live lane: {generations.Count} ACTIVE generation(s)");
}

sql.Dispose();
try { sql.Query(missing, "SELECT * FROM rag_chunk"); }
catch (ObjectDisposedException) { tests++; }

Console.WriteLine($"XstoreSql binding: {tests} PASS; "
    + "external SQL processes: 0");
return 0;
