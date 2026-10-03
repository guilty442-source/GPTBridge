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
Check(!ClosureEvidence.ProjectionIncomplete("current", "current", "PASS", 13, 13, 13, 13, 0, 0),
    "Complete current architecture receipt permits closure");
Check(ClosureEvidence.ProjectionIncomplete("current", "previous", "PASS", 13, 13, 13, 13, 0, 0),
    "Historical passing receipt cannot certify the current generation");
Check(ClosureEvidence.ProjectionIncomplete("current", "current", "PASS", 0, 0, 0, 0, 0, 0),
    "Empty passing receipt cannot certify architecture coverage");
Check(ClosureEvidence.ProjectionIncomplete("current", "current", "PASS", 13, 12, 13, 13, 0, 0),
    "Unregistered architecture evidence must block closure");
Check(ClosureEvidence.ProjectionIncomplete("current", "current", "PASS", 13, 13, 12, 13, 0, 0),
    "Missing architecture file cannot be masked by matching hashes");
Check(ClosureEvidence.ProjectionIncomplete("current", "current", "PASS", 13, 13, 13, 12, 0, 0),
    "Hash mismatch blocks closure");
Check(ClosureEvidence.ProjectionIncomplete("current", "current", "PASS", 13, 13, 13, 13, 1, 0),
    "Stale files block closure");
Check(ClosureEvidence.ProjectionIncomplete("current", "current", "PASS", 13, 13, 13, 13, 0, 1),
    "Reported missing evidence blocks closure");
Check(ClosureEvidence.ProjectionIncomplete("current", "current", "FAIL", 13, 13, 13, 13, 0, 0),
    "Failed verifier receipt cannot close architecture parity");
Console.WriteLine($"PASS: {checks} closure evidence regressions");
