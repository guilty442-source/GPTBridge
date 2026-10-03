using System.Security.Cryptography;
using System.Text;
using GPTBridge.CodexPipeline;

const string generation = "2026-10-03T00:00:00Z";
var tests = 0;
byte[] Fixture(string tables = "\"rules\":[{\"id\":\"A1\",\"text\":\"法典\"}]") =>
    Encoding.UTF8.GetBytes($"{{\"artifact\":\"{NativeCodexSnapshot.Format}\",\"generation\":\"{generation}\",\"row_count\":1,\"table_count\":1,\"tables\":{{{tables}}}}}");
NativeCodexSnapshot Read(byte[] bytes, string version = generation, long count = 1) =>
    NativeCodexSnapshot.Read(bytes, version, Convert.ToHexString(SHA256.HashData(bytes)), count, 1);
void Denied(Action action)
{
    try { action(); } catch (InvalidDataException) { tests++; return; }
    throw new Exception("Expected rejection");
}
var good = Fixture();
var snapshot = Read(good);
if (snapshot.Rows("rules")[0].GetProperty("text").GetString() != "法典") throw new Exception("Unicode lost");
tests++;
Denied(() => Read(good, "stale"));
Denied(() => NativeCodexSnapshot.Read(good, generation, new string('0', 64), 1, 1));
Denied(() => Read(good, count: 2));
Denied(() => snapshot.Rows("missing"));
Denied(() => Read(Fixture("\"rules\":[1]")));
Denied(() => Read(Fixture("\"rules\":[{\"id\":1,\"id\":2}]")));
Denied(() => Read(Fixture("\"rules\":[{}],\"rules\":[{}]")));
Denied(() => NativeCodexSnapshot.Read(good, "", new string('0',64), 1, 1));
Denied(() => Read(Encoding.UTF8.GetBytes(Encoding.UTF8.GetString(good).Replace("\"row_count\":1", "\"row_count\":1,\"row_count\":1"))));
var query = new NativeCodexSql(snapshot);
var parameters = new Dictionary<string, object?> { ["id"] = "A1", ["limit"] = 1 };
if (query.Query("SELECT text FROM rules WHERE id = @id ORDER BY id DESC LIMIT @limit", parameters)[0]["text"].GetString() != "法典")
    throw new Exception("SQL projection failed");
tests++;
if (query.Query("SELECT * FROM rules WHERE id <> 'A1'").Count != 0) throw new Exception("SQL filter failed");
tests++;
if (query.Query("SELECT * FROM rules LIMIT 0").Count != 0) throw new Exception("SQL limit failed");
tests++;
Denied(() => query.Query("DELETE FROM rules"));
Denied(() => query.Query("SELECT * FROM rules; DROP TABLE rules"));
Denied(() => query.Query("SELECT * FROM rules WHERE id=@missing"));
Denied(() => query.Query("SELECT * FROM rules LIMIT 10001"));
Denied(() => query.Query("SELECT * FROM rules LIMIT 1.5"));
Denied(() => query.Query("SELECT missing FROM rules"));
Denied(() => query.Query("SELECT id,id FROM rules"));
Denied(() => query.Query("SELECT * FROM rules WHERE id = 1"));
Denied(() => query.Query("SELECT * FROM rules WHERE id=@id", new Dictionary<string, object?> { ["id"] = new[] { 1 } }));
Denied(() => query.Query("SELECT * FROM rules --comment"));
Denied(() => query.Query("SELECT * FROM rules JOIN other ON id=id"));
Denied(() => query.Query("SELECT * FROM rules WHERE id='A1' OR id='A2'"));
var three = Fixture("\"rules\":[{\"id\":3},{\"id\":1},{\"id\":2}]");
three = Encoding.UTF8.GetBytes(Encoding.UTF8.GetString(three).Replace("\"row_count\":1", "\"row_count\":3"));
var numbers = new NativeCodexSql(Read(three, count: 3));
if (numbers.Query("SELECT id FROM rules WHERE id >= 2 AND id < 4 ORDER BY id DESC LIMIT 1")[0]["id"].GetInt32() != 3)
    throw new Exception("SQL numeric ordering failed");
tests++;
Denied(() => numbers.Query("SELECT * FROM rules", maxScannedRows: 2));
if (args.Length == 5)
{
    var live = NativeCodexSnapshot.Read(File.ReadAllBytes(args[0]), args[1], args[2],
        long.Parse(args[3]), int.Parse(args[4]));
    if (live.Rows("codex_authority_state").Count != 1) throw new Exception("Authority marker missing");
    var sql = new NativeCodexSql(live);
    var authority = sql.Query("SELECT codex_version FROM codex_authority_state WHERE codex_version=@version LIMIT 1",
        new Dictionary<string, object?> { ["version"] = args[1] });
    if (authority.Count != 1 || authority[0]["codex_version"].GetString() != args[1]) throw new Exception("Live SQL marker mismatch");
    tests++;
}
Console.WriteLine($"Native Codex snapshot: {tests} PASS; PostgreSQL dependencies: 0");
