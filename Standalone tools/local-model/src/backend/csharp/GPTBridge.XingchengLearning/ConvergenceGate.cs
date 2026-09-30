// ConvergenceGate.cs — XingchengConvergenceGate: the single release gate
// every merge / runtime update / generation promotion / bundle activation
// must pass (Native Production Convergence II). Steps run in fixed order;
// any critical failure forbids promotion (fail-closed).
//
// The gate composes main's already-converged components — it does NOT
// reimplement them: LangCheck, build scripts, trainer --probe-all,
// AxisChecks / LayaMiMoChecks (System-1) / CommunityChecks, FeatureCatalog,
// xc_modeltool modes, TransformerTrainingRepository audit chain,
// Retention and ModelLifecycle are invoked through their existing entry
// points (NativeTools subprocess lane for native binaries).
//
//   --release-gate [--bundle <dir>] [--no-builds] [--suite <f.json>]
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

    /// <summary>Enforcement: true when runtime/settings/
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
        string detail = (stderr + "\n" + stdout).Trim();
        if (p.ExitCode != 0)
            return Fail("GATE_STEP_FAILED",
                detail.Length > 0
                    ? detail[^Math.Min(400, detail.Length)..]
                    : $"exit={p.ExitCode} (no output captured)");
        return Pass(detail.Length > 0
            ? detail[^Math.Min(200, detail.Length)..]
            : "exit=0");
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

    /// <summary>Run a native step and require a JSON field to hold a
    /// value — used where exit 0 alone cannot prove the contract
    /// (e.g. mtp-runtime's final_output_parity).</summary>
    private static StepResult NativeField(string toolRoot, string exe,
        string[] args, string field, string expect)
    {
        string log = Path.Combine(toolRoot,
            ReportRel.Replace('/', Path.DirectorySeparatorChar),
            "gate-stderr.log");
        var r = NativeTools.Run(exe, args, toolRoot, log, 300);
        if (r.ExitCode != 0)
            return Fail("GATE_STEP_FAILED",
                $"exit={r.ExitCode} {r.StdoutTail
                    .Trim()[..Math.Min(200,
                        r.StdoutTail.Trim().Length)]}");
        string tail = r.StdoutTail.Trim();
        int nl = tail.LastIndexOf('\n');
        string last = nl >= 0 ? tail[(nl + 1)..] : tail;
        try
        {
            using var doc = JsonDocument.Parse(last);
            if (doc.RootElement.TryGetProperty(field, out var v) &&
                v.ToString() == expect)
                return Pass($"{field}={expect}");
            return Fail("GATE_FIELD_MISMATCH",
                $"{field} expected {expect}: {last[..Math.Min(160, last.Length)]}");
        }
        catch (Exception)
        {
            return Fail("GATE_OUTPUT_UNPARSEABLE",
                last[..Math.Min(160, last.Length)]);
        }
    }

    /// <summary>The ordered release gate. Each step records outcome; the
    /// run continues so the report shows the full failure surface, but
    /// the verdict is blocked on the first critical FAIL.</summary>
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
            // ---------- governance / boundary ----------
            new("language-scan", true, () =>
            {
                var v = LangCheck.Scan(toolRoot);
                string viol = JsonSerializer.Serialize(
                    v["violations"]);
                return TransformerTrainingRepository.Truthy(v["ok"])
                    ? Pass($"{v["files_scanned"]} files scanned")
                    : Fail("LANGUAGE_BOUNDARY_VIOLATION",
                           viol[..Math.Min(300, viol.Length)]);
            }),
            new("header-dependency-audit", true, () =>
                HeaderAudit(srcRoot)),
            // ---------- builds ----------
            new("build-modeltool", true, () => runBuilds
                ? BuildStep(toolRoot, "tools", "build.ps1")
                : BinPresent(toolExe, "xc_modeltool.exe")),
            new("build-trainer", true, () => runBuilds
                ? BuildStep(toolRoot, "training", "build.ps1")
                : BinPresent(trainExe, "xingcheng_trainer.exe")),
            new("build-xc-learning", true, () => runBuilds
                ? BuildXcLearning(toolRoot)
                : Pass("self build (this process)")),
            // ---------- self-test ----------
            new("self-test", true, () => SelfTestStep(toolRoot)),
            // ---------- trainer probes (16/16 aggregate) ----------
            new("trainer-probes", true, () => trainExe.Length > 0
                ? NativeField(toolRoot, trainExe,
                    new[] { "--probe-all" }, "ok", "True")
                : Fail("GATE_STEP_FAILED", "trainer missing")),
            // ---------- check batteries ----------
            new("axis-checks", true, () =>
            {
                var r = AxisChecks.Run(toolRoot);
                return TruthyField(r, "ok", "axis-checks");
            }),
            new("system1-checks", true, () =>
            {
                var r = LayaMiMoChecks.Run(toolRoot);
                return TruthyField(r, "ok", "system1-checks");
            }),
            new("community-checks", true, () =>
            {
                var r = CommunityChecks.Run(toolRoot);
                return TruthyField(r, "ok", "community-checks");
            }),
            new("catalog-validate", true, () =>
            {
                var emitted = FeatureCatalog.Emit(toolRoot);
                string file = Path.Combine(toolRoot,
                    FeatureCatalog.Rel.Replace('/',
                        Path.DirectorySeparatorChar));
                var v = FeatureCatalog.Validate(file);
                return TruthyField(v, "ok",
                    $"catalog-validate emitted={emitted.Count}");
            }),
            // ---------- bundle-bound runtime steps ----------
            new("architecture-drift", true, () => NeedBundle(() =>
                ArchitectureDrift(bundle!))),
            new("runtime-smoke", true, () => NeedBundle(() =>
                Native(toolRoot, toolExe, "memplan",
                       "--bundle", bundle!))),
            new("checkpoint-validation", true, () => NeedBundle(() =>
                Native(toolRoot, toolExe, "provenance-check",
                       "--bundle", bundle!))),
            new("state-validation", true, () => NeedBundle(() =>
                Native(toolRoot, toolExe, "statebench",
                       "--bundle", bundle!,
                       "--generation", ManifestGeneration(bundle!),
                       "--tokens", "32"))),
            new("cache-validation", true, () => NeedBundle(() =>
                Native(toolRoot, toolExe, "cache-smoke",
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
            new("mtp-contract", true, () => NeedBundle(() =>
                NativeField(toolRoot, toolExe,
                    new[] { "mtp-runtime", "--bundle", bundle!,
                            "--tokens", "8" },
                    "final_output_parity", "True"))),
            // ---------- hardware / provenance / audit ----------
            new("cuda-probe", false, () =>
                Native(toolRoot, toolExe, "probe-cuda")),
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
            new("resource-cert", true, () => NeedBundle(() =>
                Native(toolRoot, toolExe, "scale-status",
                       "--bundle", bundle!))),
            // ---------- release invariants ----------
            new("dataset-retention-invariants", true, () =>
                DatasetRetentionInvariants(toolRoot)),
            new("lifecycle-succession-cycle", true, () =>
                LifecycleSuccessionCycle(toolRoot)),
            // ---------- capability baseline (evidence, non-blocking) --
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
            ["release_source"] = "main",
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

    private static StepResult TruthyField(
        Dictionary<string, object?> r, string field, string name) =>
        TransformerTrainingRepository.Truthy(r.GetValueOrDefault(field))
            ? Pass($"{name} ok")
            : Fail($"{name.ToUpperInvariant().Replace('-', '_')}_FAILED",
                   JsonSerializer.Serialize(r)[..Math.Min(300,
                       JsonSerializer.Serialize(r).Length)]);

    private static StepResult SelfTestStep(string toolRoot)
    {
        string? self = Environment.ProcessPath;
        if (self == null || !File.Exists(self))
            return Fail("GATE_STEP_FAILED", "xc-learning exe unresolved");
        var psi = new ProcessStartInfo
        {
            FileName = self,
            Arguments = $"--tool-root \"{toolRoot}\" --self-test",
            WorkingDirectory = toolRoot,
            RedirectStandardOutput = true, RedirectStandardError = true,
            UseShellExecute = false, CreateNoWindow = true,
        };
        var stdout = new StringBuilder();
        using var p = Process.Start(psi)!;
        p.OutputDataReceived += (_, e) => { if (e.Data != null)
            stdout.AppendLine(e.Data); };
        if (!p.WaitForExit(300_000))
        {
            try { p.Kill(true); } catch { }
            return Fail("GATE_STEP_TIMEOUT", "self-test exceeded 300s");
        }
        p.WaitForExit();
        string tail = stdout.ToString().Trim();
        try
        {
            int nl = tail.LastIndexOf('\n');
            using var doc = JsonDocument.Parse(
                nl >= 0 ? tail[(nl + 1)..] : tail);
            bool ok = doc.RootElement.TryGetProperty("ok", out var v) &&
                      v.ValueKind == JsonValueKind.True;
            return ok ? Pass("self-test ok")
                      : Fail("SELF_TEST_FAILED",
                             tail[..Math.Min(200, tail.Length)]);
        }
        catch (Exception)
        {
            return p.ExitCode == 0
                ? Pass($"self-test exit=0 ({tail.Length}B)")
                : Fail("SELF_TEST_FAILED",
                       $"exit={p.ExitCode} " +
                       tail[..Math.Min(160, tail.Length)]);
        }
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

    /// <summary>The engine's generation() is manifest
    /// `architecture_generation` ("" on pre-convergence bundles).
    /// statebench only needs a consistent tag — fall back to the
    /// legacy claims and finally "unversioned".</summary>
    private static string ManifestGeneration(string bundle)
    {
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(
                Path.Combine(bundle, "manifest.json")));
            var root = doc.RootElement;
            foreach (var k in new[] { "architecture_generation",
                                      "generation" })
                if (root.TryGetProperty(k, out var g) &&
                    g.ValueKind == JsonValueKind.String &&
                    (g.GetString() ?? "").Length > 0)
                    return g.GetString()!;
            if (root.TryGetProperty("config", out var c) &&
                c.ValueKind == JsonValueKind.Object)
                foreach (var k in new[] { "generation", "architecture" })
                    if (c.TryGetProperty(k, out var cg) &&
                        cg.ValueKind == JsonValueKind.String &&
                        (cg.GetString() ?? "").Length > 0)
                        return cg.GetString()!;
        }
        catch (Exception) { }
        return "unversioned";
    }

    /// <summary>Architecture drift: the bundle must claim xc-fused-1
    /// and carry no quarantined axes (CSA / MLA / aux-free lb_bias)
    /// under the canonical label. provenance.json is the signed
    /// evidence block and its architecture_profile is authoritative;
    /// the manifest's architecture_generation is a legacy export-time
    /// tag consulted only when no provenance exists — a pre-rename
    /// tag like "current-compatible-profile" can only certify
    /// xc-fused-1 through provenance, never on its own.</summary>
    private static StepResult ArchitectureDrift(string bundle)
    {
        try
        {
            string arch = "";
            string archSrc = "";
            string provPath = Path.Combine(bundle, "provenance.json");
            if (File.Exists(provPath))
            {
                using var pdoc = JsonDocument.Parse(
                    File.ReadAllText(provPath));
                if (pdoc.RootElement.TryGetProperty(
                        "architecture_profile", out var ap) &&
                    ap.ValueKind == JsonValueKind.String &&
                    (ap.GetString() ?? "").Length > 0)
                { arch = ap.GetString()!; archSrc = "provenance"; }
            }
            using var doc = JsonDocument.Parse(File.ReadAllText(
                Path.Combine(bundle, "manifest.json")));
            var root = doc.RootElement;
            JsonElement cfg = root;
            if (root.TryGetProperty("config", out var c) &&
                c.ValueKind == JsonValueKind.Object)
                cfg = c;
            string manArch = "";
            foreach (var k in new[] { "architecture",
                                     "architecture_generation",
                                     "model_type", "generation" })
                if (cfg.TryGetProperty(k, out var a) &&
                    a.ValueKind == JsonValueKind.String)
                { manArch = a.GetString() ?? "";
                  if (manArch.Length > 0) break; }
            if (manArch.Length == 0)
                foreach (var k in new[] { "architecture",
                                         "architecture_generation" })
                    if (root.TryGetProperty(k, out var a) &&
                        a.ValueKind == JsonValueKind.String)
                    { manArch = a.GetString() ?? "";
                      if (manArch.Length > 0) break; }
            if (arch.Length == 0)
            { arch = manArch; archSrc = "manifest"; }
            else if (manArch.Length > 0 && manArch != arch &&
                     manArch != "current-compatible-profile" &&
                     manArch != "unversioned")
                return Fail("ARCHITECTURE_DRIFT",
                    $"manifest declares {manArch} but provenance "
                    + $"certifies {arch}");
            if (arch.Length > 0 && arch != "xc-fused-1" &&
                arch != "xc_fused_1" && !arch.StartsWith("xc-fused-1"))
                return Fail("ARCHITECTURE_DRIFT",
                            $"{archSrc} declares {arch}");
            var quarantined = new List<string>();
            foreach (var k in new[] { "use_csa", "use_mla",
                                      "aux_free_lb_bias" })
                if (cfg.TryGetProperty(k, out var q) &&
                    q.ValueKind == JsonValueKind.True)
                    quarantined.Add(k);
            if (quarantined.Count > 0)
                return Fail("CANONICAL_CONTRACT_VIOLATION",
                            string.Join(",", quarantined));
            return Pass(arch.Length > 0
                ? $"architecture={arch} ({archSrc})"
                : "no architecture claim (legacy manifest)");
        }
        catch (Exception ex)
        {
            return Fail("ARCHITECTURE_DRIFT", ex.Message);
        }
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
            dir, 1200);
    }

    private static StepResult BuildXcLearning(string toolRoot)
    {
        string proj = Path.Combine(toolRoot, "src", "backend", "csharp",
            "GPTBridge.XingchengLearning",
            "GPTBridge.XingchengLearning.csproj");
        if (!File.Exists(proj))
            return Fail("GATE_STEP_FAILED", "csproj missing");
        return Shell("dotnet",
            "build -c Release --nologo --no-restore /nr:false \"" +
            proj + "\"",
            toolRoot, 1200);
    }

    /// <summary>Header audit: every header fragment in the xct_*/xcm_*/
    /// engine_* split must be self-consistent — deterministically
    /// included by its umbrella TU in a fixed order, with no fragment
    /// including another fragment (which would make correctness depend
    /// on accidental include side-effects).</summary>
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
            ? Pass("0 fragment-include violations")
            : Fail("HEADER_DEPENDENCY_AUDIT",
                   string.Join(";", violations.Take(5)));
    }

    /// <summary>Generation convergence: at most 2 open manifests (1
    /// active + 1 candidate); none unreadable.</summary>
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

    /// <summary>Release invariant: dataset identity is the compound key
    /// (content_sha256, snapshot_sha256). Same content + same bytes
    /// dedups to one row; same content + new bytes registers a new row;
    /// every registered snapshot path is retention-protected (dry-run
    /// must never plan its deletion). Two deterministic synthetic rows
    /// persist — re-runs dedup to the same ids.</summary>
    private static StepResult DatasetRetentionInvariants(string toolRoot)
    {
        string scratchDir = Path.Combine(toolRoot, "xingcheng",
            "runtime", "state", "_gate-dataset-invariants");
        List<Dictionary<string, object?>>? norm = null;
        string diag = "";
        try
        {
            Directory.CreateDirectory(scratchDir);
            string sha = TransformerTrainingRepository.Sha256Text(
                "gate-dataset-invariant-content");
            string snapA = Path.Combine(scratchDir, "snap-a.json");
            string snapB = Path.Combine(scratchDir, "snap-b.json");
            File.WriteAllText(snapA,
                "{\"format\":\"star-transformer-sft/v1\",\"gate\":true,"
                + "\"examples\":[]}");
            File.WriteAllText(snapB,
                "{\"format\":\"star-transformer-sft/v1\",\"gate\":true,"
                + "\"examples\":[],\"rev\":2}");
            string shaA = TransformerTrainingRepository.Sha256File(snapA);
            string shaB = TransformerTrainingRepository.Sha256File(snapB);
            var repo = new TransformerTrainingRepository(toolRoot);
            var ex = new List<IReadOnlyDictionary<string, object?>>
            {
                new Dictionary<string, object?>
                {
                    ["split"] = "train",
                    ["database_scope"] = "main",
                    ["content_sha256"] =
                        TransformerTrainingRepository.Sha256Text(
                            "gate-invariant-example"),
                    ["source_revision"] = 1,
                    ["quality_score"] = 0.9,
                    ["owner_model_id"] = "gate-invariant",
                    ["source_example_id"] = "gate-ex-1",
                    ["source_type"] = "convergence-gate",
                },
                new Dictionary<string, object?>
                {
                    ["split"] = "validation",
                    ["database_scope"] = "main",
                    ["content_sha256"] =
                        TransformerTrainingRepository.Sha256Text(
                            "gate-invariant-example-val"),
                    ["source_revision"] = 1,
                    ["quality_score"] = 0.9,
                    ["owner_model_id"] = "gate-invariant",
                    ["source_example_id"] = "gate-ex-2",
                    ["source_type"] = "convergence-gate",
                },
            };
            var manifest = new Dictionary<string, object?>
            {
                ["source"] = "convergence-gate",
                ["purpose"] = "dataset-identity-invariant",
            };
            norm = TransformerTrainingRepository
                .NormalizeDatasetExamples(ex);
            var missing = norm.Where(e =>
                string.IsNullOrEmpty((string?)e["owner_model_id"]) ||
                string.IsNullOrEmpty((string?)e["source_example_id"]) ||
                string.IsNullOrEmpty((string?)e["source_type"]))
                .Select(e => string.Join(",",
                    e.Keys.Where(k =>
                        string.IsNullOrEmpty(
                            e[k]?.ToString()))));
            diag = string.Join(";", missing);
            var r1 = repo.CreateDataset(sha, snapA, shaA, ex, manifest,
                createdBy: "convergence-gate");
            var r2 = repo.CreateDataset(sha, snapA, shaA, ex, manifest,
                createdBy: "convergence-gate");
            var r3 = repo.CreateDataset(sha, snapB, shaB, ex, manifest,
                createdBy: "convergence-gate");
            string id1 = r1.GetValueOrDefault("dataset_id")?.ToString()
                ?? "";
            string id2 = r2.GetValueOrDefault("dataset_id")?.ToString()
                ?? "";
            string id3 = r3.GetValueOrDefault("dataset_id")?.ToString()
                ?? "";
            if (id1.Length == 0 || id1 != id2)
                return Fail("DATASET_IDENTITY_DEDUP_FAILED",
                            $"same-content/same-bytes: {id1} vs {id2}");
            if (id3.Length == 0 || id3 == id1)
                return Fail("DATASET_IDENTITY_NEWSNAPSHOT_FAILED",
                            $"same-content/new-bytes: {id3} vs {id1}");

            // Registered snapshot paths must never appear in a
            // retention deletion plan.
            // Every registered snapshot path is part of a dataset
            // row's identity — assert ours landed and that a dry-run
            // never plans deletion for ANY registered snapshot.
            var registered = repo.DatasetSnapshotPaths()
                .Select(p => Path.IsPathRooted(p)
                    ? Path.GetFullPath(p)
                    : Path.GetFullPath(Path.Combine(
                        toolRoot, "xingcheng", p)))
                .ToList();
            string absA = Path.GetFullPath(snapA);
            if (!registered.Contains(absA))
                return Fail("REGISTERED_SNAPSHOT_MISSING",
                            "dataset snapshot path absent from registry");
            var plan = Retention.ApplyRetention(toolRoot, dryRun: true);
            if (plan.TryGetValue("deleted", out var delObj) &&
                delObj is List<Dictionary<string, object?>> del)
            {
                var regSet = new HashSet<string>(
                    registered, StringComparer.OrdinalIgnoreCase);
                var hit = del
                    .Select(d => d.GetValueOrDefault("path")?.ToString()
                                 ?? "")
                    .Where(p => p.Length > 0)
                    .Select(p => { try { return Path.GetFullPath(p); }
                                   catch { return p; } })
                    .FirstOrDefault(regSet.Contains);
                if (hit != null)
                    return Fail("REGISTERED_SNAPSHOT_PRUNED",
                                $"retention planned {hit}");
            }

            // Unreadable registry -> preserve: with a dead DSN the
            // fallback must protect every snapshot file in the
            // snapshot dir (fail closed, never delete blind).
            string scratchRoot = Path.Combine(scratchDir, "fake-tool");
            string snapDir = Path.Combine(scratchRoot,
                XcPaths.SelfLearningSnapshotRel.Replace('/',
                    Path.DirectorySeparatorChar));
            Directory.CreateDirectory(snapDir);
            string orphan = Path.Combine(snapDir, "orphan.jsonl");
            File.WriteAllText(orphan, "{}\n");
            string? savedDsn = Environment.GetEnvironmentVariable(
                "GPTBRIDGE_POSTGRES_DSN");
            try
            {
                Environment.SetEnvironmentVariable(
                    "GPTBRIDGE_POSTGRES_DSN",
                    "host=127.0.0.1;port=1;connect_timeout=1");
                var blind = Retention.ApplyRetention(scratchRoot,
                    new RetentionPolicy { Enabled = true,
                                          KeepSnapshots = 0 },
                    dryRun: true);
                if (blind.TryGetValue("deleted", out var bObj) &&
                    bObj is List<Dictionary<string, object?>> bDel)
                {
                    string absO = Path.GetFullPath(orphan);
                    bool plannedDelete = bDel.Any(d =>
                        string.Equals(
                            d.GetValueOrDefault("path")?.ToString()
                                is string dp
                                ? Path.GetFullPath(dp) : "",
                            absO, StringComparison.OrdinalIgnoreCase));
                    if (plannedDelete)
                        return Fail("UNREADABLE_REGISTRY_PRUNED",
                                    "retention planned deletion with "
                                    + "unreadable registry");
                }
            }
            finally
            {
                Environment.SetEnvironmentVariable(
                    "GPTBRIDGE_POSTGRES_DSN", savedDsn);
            }
            return Pass($"dedup={id1 == id2} newrow={id3 != id1} "
                        + "protected=true blind_preserve=true");
        }
        catch (Exception ex)
        {
            // Registry unreachable / DB down — fail closed: the
            // invariant is untestable, never silently green.
            return Fail("DATASET_REGISTRY_UNAVAILABLE",
                        ex.GetType().Name + ": " +
                        ex.Message[..Math.Min(160, ex.Message.Length)]
                        + " @" + (ex.StackTrace ?? "")
                            .Split('\n').FirstOrDefault("?")
                            .Trim()[..Math.Min(140,
                                (ex.StackTrace ?? "")
                                    .Split('\n').FirstOrDefault("?")
                                    .Trim().Length)]
                        + " emptykeys=" + (norm != null
                            ? diag : "norm-failed"));
        }
        finally
        {
            // snap-a/snap-b stay — the registered dataset rows
            // reference their paths (deleting them would leave a
            // protected-but-missing snapshot). Only the fake-tool
            // scratch tree goes.
            try
            {
                string fake = Path.Combine(scratchDir, "fake-tool");
                if (Directory.Exists(fake))
                    Directory.Delete(fake, true);
            }
            catch { }
        }
    }

    /// <summary>Release invariant: lifecycle succession must be a
    /// serialized snapshot, never a live object reference. Exercises
    /// serialize → deserialize → resave → rollback → retire on a
    /// scratch model id; guards the succeeded_from deep-copy
    /// regression (StackOverflow on cyclic references).</summary>
    private static StepResult LifecycleSuccessionCycle(string toolRoot)
    {
        string dir = Path.Combine(toolRoot, "xingcheng", "runtime",
            "state", "_gate-lifecycle-cycle");
        try
        {
            if (Directory.Exists(dir)) Directory.Delete(dir, true);
            Directory.CreateDirectory(dir);

            // 1. register v1 then v2 — v2 metadata records
            //    succeeded_from as a plain decoded dict snapshot.
            var lc = ModelLifecycle.LoadOrCreate(dir, "gate-cycle");
            lc.Transition("INITIALIZED", "gate");
            string w1 = Path.Combine(dir, "w1.bin");
            string w2 = Path.Combine(dir, "w2.bin");
            File.WriteAllText(w1, "gate-weight-v1");
            File.WriteAllText(w2, "gate-weight-v2");
            lc.RegisterArtifact("weights", w1,
                new Dictionary<string, object?> { ["lane"] = "gate" },
                activate: true);
            lc.RegisterArtifact("weights", w2,
                new Dictionary<string, object?> { ["lane"] = "gate" },
                activate: true);
            string saved = lc.Save(dir);
            if (!File.Exists(saved))
                return Fail("LIFECYCLE_SERIALIZE_FAILED",
                            "lifecycle.json not written");

            // 2. deserialize: succeeded_from must be data, not a live
            //    reference — mutating it must not touch the active
            //    entry, and re-serializing must not recurse.
            var reloaded = ModelLifecycle.Load(dir);
            var active = reloaded.ActiveWeights();
            if (active == null)
                return Fail("LIFECYCLE_NO_ACTIVE",
                            "reloaded lifecycle lost active weights");
            Dictionary<string, object?>? succ = null;
            if (active.TryGetValue("metadata", out var meta) &&
                meta is Dictionary<string, object?> md)
                succ = md.GetValueOrDefault("succeeded_from")
                    as Dictionary<string, object?>;
            if (succ == null)
                return Fail("LIFECYCLE_SUCCESSION_MISSING",
                            "v2 metadata lacks succeeded_from snapshot");
            succ["__gate_probe__"] = "mutated";
            string resaved = reloaded.Save(dir);
            if (!File.Exists(resaved))
                return Fail("LIFECYCLE_RESAVE_FAILED",
                            "resave failed after metadata mutation");
            var resavedDoc = ModelLifecycle.Load(dir);
            var active2 = resavedDoc.ActiveWeights();
            if (active2 == null)
                return Fail("LIFECYCLE_RESAVE_LOST_ACTIVE",
                            "resaved lifecycle lost active weights");

            // 3. rollback + retire exercise the succession path
            //    without holding cyclic references.
            try { resavedDoc.RollbackWeights(1); }
            catch (Exception ex)
            {
                return Fail("LIFECYCLE_ROLLBACK_FAILED",
                            ex.GetType().Name + ": " + ex.Message);
            }
            // v2 is inactive after the rollback — retire it carrying
            // data attribution to v1 (the successor that superseded it).
            try { resavedDoc.RetireWeightVersion(2, 1); }
            catch (Exception ex)
            {
                return Fail("LIFECYCLE_RETIRE_FAILED",
                            ex.GetType().Name + ": " + ex.Message);
            }
            string final = resavedDoc.Save(dir);
            if (!File.Exists(final))
                return Fail("LIFECYCLE_FINAL_SAVE_FAILED",
                            "post-retire save failed");
            _ = ModelLifecycle.Load(dir); // must not stack-overflow

            return Pass("serialize->deserialize->resave->rollback->"
                        + "retire ok; succeeded_from is data");
        }
        catch (Exception ex)
        {
            return Fail("LIFECYCLE_CYCLE_EXCEPTION",
                        ex.GetType().Name + ": " + ex.Message);
        }
        finally
        {
            try { if (Directory.Exists(dir))
                Directory.Delete(dir, true); }
            catch { }
        }
    }
}
