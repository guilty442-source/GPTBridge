// Read-only regression probe for the published deterministic evidence predicates.
using System.Reflection;

var assembly = Assembly.LoadFrom(Path.GetFullPath(
    "shared-layer/csharp/GPTBridge.CodexPipeline/publish/GPTBridge.CodexPipeline.dll"));
var type = assembly.GetType("GPTBridge.CodexPipeline.GenerationProjections", true)!;
var sync = type.GetMethod("DiagramSyncPassed", BindingFlags.NonPublic | BindingFlags.Static)!;
var historical = type.GetMethod("HistoricalSubSovereignClause", BindingFlags.NonPublic | BindingFlags.Static)!;
int checkedCases = 0;
void Check(bool expected, MethodInfo method, params object[] arguments)
{
    var actual = (bool)method.Invoke(null, arguments)!;
    if (actual != expected) throw new Exception($"REGRESSION:{method.Name}:{string.Join(',', arguments)}");
    checkedCases++;
}
Check(true, sync, 13, 13, 13, 0, 0, 0);
Check(false, sync, 13, 13, 8, 13, 0, 0); // original false PASS
Check(false, sync, 13, 13, 13, 1, 0, 0);
Check(false, sync, 13, 12, 12, 0, 1, 0);
Check(false, sync, 13, 13, 12, 0, 0, 0);
Check(false, sync, 13, 13, 13, 0, 0, 1); // applicable read-only requirement
Check(false, sync, 0, 0, 0, 0, 0, 0);
Check(false, historical, "FLOW:decision>matching sub-sovereign execution-control-and-dispatch");
Check(false, historical, "SPLIT:resource and dependency synchronization use single-duty sub-sovereigns");
Check(true, historical, "SUB-SOVEREIGNS:abolished and historical only");
Check(true, historical, "IDENTITY:legacy references do not revive a sub-sovereign.");
Console.WriteLine($"PASS: {checkedCases} deterministic evidence regression cases");
