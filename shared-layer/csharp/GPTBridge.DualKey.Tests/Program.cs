using System.Reflection;
using System.Text.Json;
using GPTBridge.CodexPipeline;

var type = typeof(SemanticHashToolchain).Assembly.GetType("GPTBridge.CodexPipeline.CodexDualKey", true)!;
var expiry = type.GetMethod("GrantExpiryValid", BindingFlags.Static | BindingFlags.NonPublic)!;
var mint = type.GetMethod("MintDualKeyGrant", BindingFlags.Static | BindingFlags.Public)!;
var passed = 0;
const long now = 100;
foreach (var value in new object?[] { null, double.NaN, double.PositiveInfinity, double.NegativeInfinity, "NaN", "Infinity", "invalid", new object(), 0, 99, 100 })
{
    if ((bool)expiry.Invoke(null, new object?[] { value, now })!) throw new Exception("INVALID_EXPIRY_ACCEPTED");
    passed++;
}
foreach (var value in new object[] { 101L, 100.5, "101" })
{
    if (!(bool)expiry.Invoke(null, new object[] { value, now })!) throw new Exception("LIVE_EXPIRY_DENIED");
    passed++;
}
foreach (var ttl in new[] { double.NaN, double.PositiveInfinity, double.NegativeInfinity })
{
    try
    {
        mint.Invoke(null, new object[] { "fixture", "primary", "secondary", "fixture", Array.Empty<string>(), "review", ttl });
        throw new Exception("NONFINITE_TTL_ACCEPTED");
    }
    catch (TargetInvocationException error) when (error.InnerException?.Message == "CODEX_GRANT_TTL_INVALID") { passed++; }
}
var sessionType = type.Assembly.GetType("GPTBridge.CodexPipeline.CodexReadSession", true)!;
var open = type.Assembly.GetType("GPTBridge.CodexPipeline.CodexSessions", true)!
    .GetMethod("OpenCodexSession", BindingFlags.Static | BindingFlags.Public)!;
foreach (var ttl in new[] { double.NaN, double.PositiveInfinity, double.NegativeInfinity })
{
    foreach (var constructor in new[] { false, true })
    {
        try
        {
            if (constructor)
                Activator.CreateInstance(sessionType, new object[] { "fixture", "fixture", new HashSet<string>(), "review", ttl });
            else
                open.Invoke(null, new object?[] { "fixture", "fixture", Array.Empty<string>(), "review", ttl, "unconsumed-fixture-grant" });
            throw new Exception("INVALID_SESSION_TTL_ACCEPTED");
        }
        catch (TargetInvocationException error) when (error.InnerException?.Message == "CODEX_SESSION_TTL_INVALID") { passed++; }
    }
    var session = System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(sessionType);
    sessionType.GetField("_expires", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(session, ttl);
    if (!(bool)sessionType.GetProperty("Expired")!.GetValue(session)!) throw new Exception("NONFINITE_SESSION_LIVE");
    passed++;
}
Console.WriteLine(JsonSerializer.Serialize(new { artifact = "dual-key-and-session-expiry-regression", passed, failed = 0 }));
