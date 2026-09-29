// C# orchestration for the native test suites (no Python).
// §10.60.1: TestSuiteOrchestrator is the SOLE native suite orchestrator.
//
// Modes:
//   (default)  aggregate <bin>/native-report.json → orchestration report
//   --run      orchestrate the suite fleet itself: bounded concurrency,
//              per-suite timeout, process cleanup, typed per-suite results,
//              consolidated native-report.json + artifact hash (SHA-256) +
//              source-revision verification against the build manifest.
//
// The suite list comes from the current build manifest
// (<bin>/suite-manifest.json, emitted by build.ps1); with --require-manifest
// a missing/malformed manifest fails closed instead of globbing.
//
// G99: every BLOCKED case is classified through
// <test_suites>/suite_criticality.json — a BLOCKED case on a
// release-critical suite (the default for unregistered suites) makes the
// verdict FAIL, matching push_gate.py; only suites registered
// "experimental" keep INCOMPLETE_EVIDENCE.  A missing/malformed registry
// with blocked cases is itself FAIL (fail-closed).
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;

if (args.Contains("--run"))
{
    return await Orchestrator.RunSuites(args);
}
return Orchestrator.AggregateReport(args);
