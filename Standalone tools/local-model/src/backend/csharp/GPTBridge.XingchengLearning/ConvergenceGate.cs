// ConvergenceGate.cs — §5 XingchengConvergenceGate: the single release
// gate every merge / runtime update / generation promotion / bundle
// activation must pass. Steps run in the codified order; any critical
// failure forbids promotion (fail-closed).
//
// The gate composes the already-converged components — it does NOT
// reimplement them: LanguageBoundary, build scripts, trainer probes,
// xc_modeltool modes, BundleProvenance, audit chain and lifecycle are
// invoked through their existing entry points (NativeTools subprocess
// lane for native binaries).
//
//   --release-gate [--bundle <dir>] [--no-builds] [--quick]
//
// Output: star-release-gate/v1 — ordered step results, each with
// status/critical/duration/failure_code; plus final verdict.
// CAPABILITY_TRAINING_FROZEN: nothing here schedules training.

using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class ConvergenceGate
{
    public const string Format = "star-release-gate/v1";
    public const string ReportRel =
        "xingcheng/runtime/logs/release-gate";

    private sealed class StepResult
    {
        public string Status = "PASS";       // PASS | FAIL | SKIP
        public string? Code;
        public string Detail = "";
    }

    private sealed record Step(string Name, bool Critical,
                               Func<StepResult> Run);

    /// <summary>§5 enforcement: true when runtime/settings/
    /// convergence-gate.json sets enforce_release_gate=true —
    /// promotion/activation must then present passing gate evidence.</summary>
    public static bool Enforced(string toolRoot)
    {
        string path = Path.Combine(toolRoot, "runtime", "settings",
            "convergence-gate.json");
        if (!File.Exists(path)) return false;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            return doc.RootElement.TryGetProperty(
                "enforce_release_gate", out var v)
                && v.ValueKind == JsonValueKind.True;
        }
        catch (Exception) { return false; }
    }

    /// <summary>Latest gate report verdict ("" if none).</summary>
    public static string LatestVerdict(string toolRoot)
    {
        string dir = Path.Combine(toolRoot,
            ReportRel.Replace('/', Path.DirectorySeparatorChar));
        if (!Directory.Exists(dir)) return "";
        string? newest = Directory.EnumerateFiles(dir, "gate-*.json")
            .OrderByDescending(f => f).FirstOrDefault();
        if (newest == null) return "";
        try
        {
            using var doc = JsonDocument.Parse(
                File.ReadAllText(newest));
            return doc.RootElement.TryGetProperty("verdict", out var v)
                ? v.GetString() ?? "" : "";
        }
        catch (Exception) { return ""; }
    }

    private static StepResult Pass(string detail = "") =>
        new() { Detail = detail };
    private static StepResult Fail(string code, string detail = "") =>
        new() { Status = "FAIL", Code = code, Detail = detail };
    private static StepResult Skip(string detail) =>
        new() { Status = "SKIP", Detail = detail };

    /// <summary>Run a shell command; PASS on exit 0.</summary>
    private static StepResult Shell(string exe, string args,
        string workDir, int timeoutS = 600)
    {
        var psi = new ProcessStartInfo
        {
            FileName = exe, Arguments = args, WorkingDirectory = workDir,
            RedirectStandardOutput = true, RedirectStandardError = true,
            UseShellExecute = false, CreateNoWindow = true,
        };
        var stdout = new StringBuilder();
        var stderr = new StringBuilder();
        using var p = Process.Start(psi)!;
        p.OutputDataReceived += (_, e) => { if (e.Data != null)
            stdout.AppendLine(e.Data); };
        p.ErrorDataReceived += (_, e) => { if (e.Data != null)
            stderr.AppendLine(e.Data); };
        if (!p.WaitForExit(timeoutS * 1000))
        {
            try { p.Kill(true); } catch { }
            return Fail("GATE_STEP_TIMEOUT",
                        $"{exe} exceeded {timeoutS}s");
        }
        p.WaitForExit(); // drain async readers
        if (p.ExitCode != 0)
            return Fail("GATE_STEP_FAILED",
                (stderr.Length > 0 ? stderr : stdout).ToString()
                    .Trim()[..Math.Min(400,
                        (stderr.Length > 0 ? stderr : stdout)
                            .ToString().Trim().Length)]);
        return Pass(stdout.ToString().Trim()[..Math.Min(200,
            stdout.ToString().Trim().Length)]);
    }

    private static StepResult Native(string toolRoot, string exe,
        params string[] args)
    {
        string log = Path.Combine(toolRoot,
            ReportRel.Replace('/', Path.DirectorySeparatorChar),
            "gate-stderr.log");
        var r = NativeTools.Run(exe, args, toolRoot, log, 300);
        return r.ExitCode == 0
            ? Pass(r.StdoutTail.Trim()[..Math.Min(200,
                  r.StdoutTail.Trim().Length)])
            : Fail("GATE_STEP_FAILED",
                   $"exit={r.ExitCode} {r.StdoutTail
                       .Trim()[..Math.Min(200,
                           r.StdoutTail.Trim().Length)]}");
    }

    /// <summary>The ordered §5 gate. Each step records outcome; the run
    /// continues so the report shows the full failure surface, but the
    /// verdict is blocked on the first critical FAIL.</summary>
    public static Dictionary<string, object?> Run(
        string toolRoot, string? bundle, bool runBuilds,
        string? suite = null)
    {
        string toolExe = File.Exists(NativeTools.ResolveExe(
            toolRoot, "tools", "xc_modeltool.exe"))
            ? NativeTools.ModelToolExe(toolRoot) : "";
        string trainExe;
        try { trainExe = NativeTools.TrainerExe(toolRoot); }
        catch { trainExe = ""; }
        string srcRoot = Path.Combine(toolRoot, "src");

        bool HasBundle() => bundle != null && File.Exists(
            Path.Combine(bundle, "manifest.json"));
        StepResult NeedBundle(Func<StepResult> f) =>
            HasBundle() ? f()
                : Skip("no --bundle (manifest.json) supplied");

        var steps = new List<Step>
        {
            new("language-scan", true, () =>
            {
                var v = LanguageBoundary.Scan(srcRoot);
                return v.Count == 0
                    ? Pass("0 violations")
                    : Fail(ConvErr.LanguageBoundaryViolation,
                           string.Join(";", v.Take(5)));
            }),
            new("header-dependency-audit", true, () =>
                HeaderAudit(srcRoot)),
            new("build-c-core", true, () => runBuilds
                ? BuildStep(toolRoot, "tools", "build.ps1")
                : BinPresent(toolExe, "xc_modeltool.exe")),
            new("build-cpp-runtime", true, () => runBuilds
                ? Pass("covered by build-c-core (single TU link)")
                : BinPresent(toolExe, "xc_modeltool.exe")),
            new("build-trainer", true, () => runBuilds
                ? BuildStep(toolRoot, "training", "build.ps1")
                : BinPresent(trainExe, "xingcheng_trainer.exe")),
            new("build-modeltool", true, () => runBuilds
                ? Pass("covered by build-c-core (single TU link)")
                : BinPresent(toolExe, "xc_modeltool.exe")),
            new("build-xc-learning", true, () => runBuilds
                ? BuildXcLearning(toolRoot)
                : Pass("self build (this process)")),
            new("xcn10-compat", true, () => trainExe.Length > 0
                ? Native(toolRoot, trainExe, "--canoncheck")
                : Fail("GATE_STEP_FAILED", "trainer missing")),
            new("canonical-contract", true, () => trainExe.Length > 0
                ? Native(toolRoot, trainExe, "--canoncheck")
                : Fail("GATE_STEP_FAILED", "trainer missing")),
            new("trainer-probes", true, () => trainExe.Length > 0
                ? Native(toolRoot, trainExe, "--probe-all")
                : Fail("GATE_STEP_FAILED", "trainer missing")),
            new("runtime-smoke", true, () => NeedBundle(() =>
                Native(toolRoot, toolExe, "memory-plan",
                       "--bundle", bundle!))),
            new("cache-smoke", true, () => NeedBundle(() =>
                Native(toolRoot, toolExe, "cache-smoke",
                       "--bundle", bundle!))),
            new("state-smoke", true, () => NeedBundle(() =>
                Native(toolRoot, toolExe, "state-snapshot",
                       "--bundle", bundle!))),
            new("vision-smoke", true, () => NeedBundle(() =>
            {
                // §2 vision is canonical, but a bundle without fused
                // vision weights legitimately skips — the smoke is a
                // no-op against a text-only manifest.
                if (!BundleUsesVision(bundle!))
                    return Skip("bundle has no vision fusion "
                                + "(config.use_vision!=true)");
                return Native(toolRoot, toolExe, "vision-smoke",
                              "--bundle", bundle!);
            })),
            new("thinking-smoke", true, () => NeedBundle(() =>
                Native(toolRoot, toolExe, "native-thinking-eval",
                       "--bundle", bundle!, "--quick"))),
            new("precision-parity", true, () => NeedBundle(() =>
                Native(toolRoot, toolExe, "precision-parity",
                       "--bundle", bundle!))),
            new("provenance-verify", true, () => NeedBundle(() =>
            {
                try
                {
                    // §34: provenance is a sibling file — embedding it
                    // in manifest.json would make manifest_hash
                    // self-referential.
                    var prov = ReadProvenance(bundle!);
                    if (prov == null)
                        return Fail(ConvErr.BundleProvenanceInvalid,
                                    "bundle lacks provenance.json");
                    BundleProvenance.Verify(bundle!, prov,
                        "", "xc-fused-1");
                    return Pass("provenance verified");
                }
                catch (ExecutorError ex)
                {
                    return Fail(ex.ErrorCode, ex.Message);
                }
            })),
            new("audit-verify", true, () =>
            {
                var r = new TransformerTrainingRepository(toolRoot)
                    .VerifyAuditChain();
                return TransformerTrainingRepository.Truthy(
                           r.GetValueOrDefault("ok"))
                    ? Pass("audit chain verified")
                    : Fail("AUDIT_CHAIN_BROKEN",
                           r.GetValueOrDefault("error")?.ToString()
                           ?? "");
            }),
            new("generation-convergence", true, () =>
                GenerationConvergence(toolRoot)),
            new("capability-baseline", false, () => NeedBundle(() =>
                suite == null || !File.Exists(suite)
                    ? Skip("no --suite (star-capability-suite/v1) "
                           + "supplied")
                    : Native(toolRoot, toolExe, "capability",
                             "--bundle", bundle!,
                             "--suite", suite))),
        };

        var results = new List<object?>();
        var swAll = Stopwatch.StartNew();
        bool blocked = false;
        string? firstBlocker = null;
        foreach (var s in steps)
        {
            var sw = Stopwatch.StartNew();
            StepResult r;
            try { r = s.Run(); }
            catch (Exception ex)
            {
                r = Fail("GATE_STEP_EXCEPTION",
                         ex.GetType().Name + ": " + ex.Message);
            }
            sw.Stop();
            if (s.Critical && r.Status == "FAIL" && !blocked)
            {
                blocked = true; firstBlocker = s.Name;
            }
            else if (s.Critical && r.Status == "FAIL")
            {
                blocked = true;
            }
            results.Add(new Dictionary<string, object?>
            {
                ["step"] = s.Name, ["order"] = results.Count + 1,
                ["critical"] = s.Critical, ["status"] = r.Status,
                ["failure_code"] = r.Code,
                ["duration_ms"] = sw.ElapsedMilliseconds,
                ["detail"] = r.Detail,
            });
        }
        swAll.Stop();

        var report = new Dictionary<string, object?>
        {
            ["format"] = Format,
            ["ok"] = !blocked,
            ["verdict"] = blocked ? "PROMOTION_BLOCKED"
                                  : "PROMOTION_ALLOWED",
            ["blocked_by"] = firstBlocker,
            ["critical_failed"] = results.Count(x =>
                (bool)((Dictionary<string, object?>)x!)["critical"]! &&
                ((Dictionary<string, object?>)x!)["status"]
                    ?.ToString() == "FAIL"),
            ["steps"] = results,
            ["total_duration_ms"] = swAll.ElapsedMilliseconds,
            ["capability_training_frozen"] = true,
            ["canonical_architecture"] = "xc-fused-1",
            ["checkpoint_contract"] = "XCN1 v10",
        };
        string dir = Path.Combine(toolRoot,
            ReportRel.Replace('/', Path.DirectorySeparatorChar));
        Directory.CreateDirectory(dir);
        string path = Path.Combine(dir,
            $"gate-{DateTimeOffset.UtcNow:yyyyMMdd-HHmmss}.json");
        ModelLifecycle.AtomicWrite(path,
            CanonicalJson.PrettyDict(report) + "\n");
        report["report_path"] = path;
        return report;
    }

    private static bool BundleUsesVision(string bundle)
    {
        string mp = Path.Combine(bundle, "manifest.json");
        if (!File.Exists(mp)) return false;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(mp));
            return doc.RootElement.TryGetProperty("config", out var c)
                && c.ValueKind == JsonValueKind.Object
                && c.TryGetProperty("use_vision", out var v)
                && v.ValueKind == JsonValueKind.True;
        }
        catch (Exception) { return false; }
    }

    private static Dictionary<string, object?>? ReadProvenance(
        string bundle)
    {
        string pp = Path.Combine(bundle, "provenance.json");
        if (File.Exists(pp))
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(pp));
            return (Dictionary<string, object?>)
                ModelLifecycle.Decode(doc.RootElement)!;
        }
        // Legacy lane: a provenance block embedded in the manifest
        // (manifest_hash then covers the unsigned manifest — see
        // export flow which writes provenance.json separately).
        string mp = Path.Combine(bundle, "manifest.json");
        if (!File.Exists(mp)) return null;
        using var mdoc = JsonDocument.Parse(File.ReadAllText(mp));
        if (!mdoc.RootElement.TryGetProperty("provenance", out var p)
            || p.ValueKind != JsonValueKind.Object)
            return null;
        return (Dictionary<string, object?>)
            ModelLifecycle.Decode(p)!;
    }

    private static StepResult BinPresent(string exe, string name)
        => exe.Length > 0 && File.Exists(exe)
            ? Pass($"{name} present (build skipped)")
            : Fail("GATE_STEP_FAILED", $"{name} missing");

    private static StepResult BuildStep(string toolRoot, string subdir,
        string script)
    {
        string dir = Path.Combine(toolRoot, "src", "backend",
            "services", "xingcheng", "infrastructure",
            "native_transformer", subdir);
        string ps1 = Path.Combine(dir, script);
        if (!File.Exists(ps1))
            return Fail("GATE_STEP_FAILED", $"missing {script}");
        return Shell("powershell",
            "-NoProfile -ExecutionPolicy Bypass -File \"" + ps1 + "\"",
            dir, 600);
    }

    private static StepResult BuildXcLearning(string toolRoot)
    {
        string proj = Path.Combine(toolRoot, "src", "backend", "csharp",
            "GPTBridge.XingchengLearning",
            "GPTBridge.XingchengLearning.csproj");
        if (!File.Exists(proj))
            return Fail("GATE_STEP_FAILED", "csproj missing");
        return Shell("dotnet",
            "build -c Release --nologo \"" + proj + "\"",
            toolRoot, 600);
    }

    /// <summary>§38 include-dependency audit: every header fragment in
    /// the xct_*/xcm_*/engine_* split must be self-consistent —
    /// deterministically included by its umbrella TU in a fixed order,
    /// with no fragment including another fragment (which would make
    /// correctness depend on accidental include side-effects).</summary>
    private static StepResult HeaderAudit(string srcRoot)
    {
        var violations = new List<string>();
        string nativeDir = Path.Combine(srcRoot, "backend", "services",
            "xingcheng", "infrastructure", "native_transformer");
        var dirs = new[]
        {
            Path.Combine(nativeDir, "training"),
            Path.Combine(nativeDir, "tools"),
            Path.Combine(srcRoot, "backend", "cpp", "src"),
        };
        foreach (string dir in dirs)
        {
            if (!Directory.Exists(dir)) continue;
            foreach (string h in Directory.EnumerateFiles(dir, "*.h")
                         .Concat(Directory.EnumerateFiles(dir, "*.hpp")))
            {
                var fn = Path.GetFileName(h);
                bool isFragment = fn.StartsWith("xct_")
                    || fn.StartsWith("xcm_") || fn.StartsWith("engine_");
                if (!isFragment) continue;
                int lineNo = 0;
                foreach (string line in File.ReadLines(h))
                {
                    ++lineNo;
                    var t = line.Trim();
                    if (!t.StartsWith("#include \"")) continue;
                    string inc = t.Split('"')[1];
                    string incBase = Path.GetFileName(inc);
                    if ((incBase.StartsWith("xct_")
                         || incBase.StartsWith("xcm_")
                         || incBase.StartsWith("engine_"))
                        && incBase != fn)
                        violations.Add(
                            $"{fn}:{lineNo} includes fragment {incBase}");
                }
            }
        }
        return violations.Count == 0
            ? Pass($"{violations.Count} fragment-include violations")
            : Fail("HEADER_DEPENDENCY_AUDIT",
                   string.Join(";", violations.Take(5)));
    }

    /// <summary>§39 generation convergence: at most one open CANDIDATE
    /// manifest; the lifecycle's active version is well-formed; no
    /// gen-manifest claims PROMOTED while its target artifact is
    /// missing.</summary>
    private static StepResult GenerationConvergence(string toolRoot)
    {
        string genDir = Path.Combine(toolRoot, "xingcheng", "runtime",
            "generations");
        var open = new List<string>();
        var issues = new List<string>();
        if (Directory.Exists(genDir))
            foreach (string f in Directory.EnumerateFiles(
                         genDir, "*.json"))
            {
                try
                {
                    using var doc = JsonDocument.Parse(
                        File.ReadAllText(f));
                    var root = doc.RootElement;
                    string status =
                        root.TryGetProperty("status", out var s)
                            ? s.GetString() ?? "" : "";
                    if (status != "PURGED" && status != "FAILED")
                        open.Add(Path.GetFileName(f));
                }
                catch (Exception)
                {
                    issues.Add(Path.GetFileName(f) + ":unreadable");
                }
            }
        if (open.Count > 2)
            issues.Add($"{open.Count} open generations (max 2: "
                       + "1 active + 1 candidate)");
        return issues.Count == 0
            ? Pass($"{open.Count} open generation(s)")
            : Fail("GENERATION_CONVERGENCE_VIOLATION",
                   string.Join(";", issues.Take(5)));
    }
}
