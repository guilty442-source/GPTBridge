using GPTBridge.CodexPipeline;

var checks = 0;
void Check(bool condition, string name)
{
    if (!condition) throw new Exception(name);
    checks++;
}
Check(ClosureEvidence.CountOpen(Array.Empty<bool>(), x => !x) == 1,
    "Missing evidence must block closure");
Check(ClosureEvidence.CountOpen(new[] { true }, x => !x) == 0,
    "Executed passing evidence permits component closure");
Check(ClosureEvidence.CountOpen(new[] { true, false }, x => !x) == 1,
    "One passing row cannot mask a failing row");
Check(ClosureEvidence.CountOpen(new[] { false, false }, x => !x) == 2,
    "Every failed evidence row is counted");
Console.WriteLine($"PASS: {checks} closure evidence regressions");
