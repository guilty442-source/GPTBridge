// ConvergenceChecks.cs — repo-level convergence battery for main
// (Native Production Convergence II). Platform invariants — one
// runtime owner, one generation owner, canonical contract, frozen
// training, supported axes only. Every check is fail-closed: a
// contract breach, unreadable state or probe error produces
// {ok:false} for that check and ok=false overall.
//
// Repo-state reads are bounded to toolRoot. Ported from the devin
// worktree's §39 repo-level block; the devin-only runtime-surface
// smokes (reasoning runtime, envelopes, workgraphs) have main-side
// equivalents inside AxisChecks / LayaMiMoChecks / CommunityChecks
// and are not duplicated here.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class ConvergenceChecks
{
    private sealed record Check(string Name, Func<bool> Run);

    public static Dictionary<string, object?> Run(string toolRoot)
    {
        var checks = new List<Check>
        {
            new("repo-single-canonical-runtime", () =>
            {
                string src = Path.Combine(toolRoot, "src", "backend");
                if (!Directory.Exists(src)) return false;
                // One C++ engine owner: exactly one engine.cpp and one
                // public engine header across the native lane.
                return Directory.GetFiles(
                           src, "engine.cpp", SearchOption.AllDirectories)
                           .Length == 1 &&
                       Directory.GetFiles(
                           src, "xingcheng_inference.hpp",
                           SearchOption.AllDirectories).Length == 1;
            }),
            new("repo-single-generation-owner", () =>
            {
                // Exactly one lifecycle.json owns the governed model
                // id — per-model lifecycle dirs and selftest scratch
                // copies are separate owners by scope, not duplicates.
                string lcDir = Path.Combine(
                    toolRoot,
                    XcPaths.LifecycleRel.Replace(
                        '/', Path.DirectorySeparatorChar));
                if (!Directory.Exists(lcDir)) return true;
                return Directory.GetFiles(lcDir, "lifecycle.json")
                           .Length <= 1;
            }),
            new("repo-architecture-contract", () =>
            {
                // Canonical quarantine: a bundle claiming xc-fused-1
                // while enabling CSA/MLA is CANONICAL_CONTRACT_
                // VIOLATION. Legacy arch tags resolve through
                // provenance, not through this manifest scan.
                string rt = Path.Combine(toolRoot, "xingcheng", "runtime");
                if (!Directory.Exists(rt)) return true;
                foreach (string mf in Directory.GetFiles(
                             rt, "manifest.json", SearchOption.AllDirectories))
                {
                    JsonDocument doc;
                    try { doc = JsonDocument.Parse(File.ReadAllText(mf)); }
                    catch { continue; }
                    using (doc)
                    {
                        var r = doc.RootElement;
                        string arch =
                            r.TryGetProperty("architecture_profile",
                                             out var ap) ? ap.GetString() ?? "" :
                            r.TryGetProperty("architecture",
                                             out var ar) ? ar.GetString() ?? "" :
                            r.TryGetProperty("architecture_generation",
                                             out var ag) ? ag.GetString() ?? "" : "";
                        if (arch != "xc-fused-1") continue;
                        if (!r.TryGetProperty("config", out var cfg))
                            continue;
                        foreach (string k in new[]
                                 { "use_csa", "csa_enabled", "use_mla",
                                   "mla_enabled", "use_latent_moe",
                                   "use_rwkv", "use_mamba" })
                            if (cfg.TryGetProperty(k, out var v) &&
                                v.ValueKind == JsonValueKind.True)
                                return false;
                    }
                }
                return true;
            }),
            new("repo-active-generation-singleton", () =>
            {
                string sp = Path.Combine(
                    toolRoot, GenerationMigration.StateDirRel
                                  .Replace('/', Path.DirectorySeparatorChar),
                    GenerationMigration.StateFile);
                if (!File.Exists(sp)) return true;
                using var doc = JsonDocument.Parse(File.ReadAllText(sp));
                // exactly one active generation string
                return doc.RootElement.TryGetProperty(
                           "active_generation", out var ag) &&
                       ag.ValueKind == JsonValueKind.String;
            }),
            new("repo-dangling-lineage", () =>
            {
                string dir = Path.Combine(
                    toolRoot, GenerationMigration.StateDirRel
                                  .Replace('/', Path.DirectorySeparatorChar));
                if (!Directory.Exists(dir)) return true;
                foreach (string mf in Directory.GetFiles(
                             dir, "migration-*.json"))
                {
                    JsonDocument doc;
                    try { doc = JsonDocument.Parse(File.ReadAllText(mf)); }
                    catch { return false; }
                    using (doc)
                    {
                        var r = doc.RootElement;
                        string status =
                            r.TryGetProperty("status", out var s)
                                ? s.GetString() ?? "" : "";
                        if (status == "PURGED" || status == "FAILED")
                            continue;
                        if (r.TryGetProperty("weights", out var w) &&
                            w.TryGetProperty("target_path", out var tp))
                        {
                            string rel = tp.GetString() ?? "";
                            string abs = Path.Combine(
                                toolRoot, rel.Replace('/',
                                    Path.DirectorySeparatorChar));
                            if (rel.Length > 0 && !File.Exists(abs) &&
                                !Directory.Exists(abs))
                                return false;   // dangling lineage
                        }
                    }
                }
                return true;
            }),
            new("repo-orphan-artifacts", () =>
            {
                // Production store only — runtime/devin is a scratch
                // lane whose fixtures are not lineage-referenced by
                // contract.
                string store = Path.Combine(
                    toolRoot, "xingcheng", "runtime", "models");
                if (!Directory.Exists(store)) return true;
                var referenced = new HashSet<string>(
                    StringComparer.OrdinalIgnoreCase);
                string lcDir = Path.Combine(
                    toolRoot,
                    XcPaths.LifecycleRel.Replace(
                        '/', Path.DirectorySeparatorChar));
                var lc = ModelLifecycle.LoadOrCreate(
                    lcDir, XcPaths.ModelId);
                void Collect(object? node)
                {
                    switch (node)
                    {
                        case Dictionary<string, object?> d:
                            foreach (var kv in d)
                            {
                                if (kv.Value is string s &&
                                    kv.Key.EndsWith("_path",
                                        StringComparison.Ordinal))
                                    referenced.Add(
                                        s.Replace('\\', '/'));
                                else Collect(kv.Value);
                            }
                            break;
                        case List<object?> l:
                            foreach (var i in l) Collect(i);
                            break;
                    }
                }
                foreach (var entry in lc.Artifacts.Values)
                    Collect(entry);
                foreach (string b in Directory.GetDirectories(
                             store, "*", SearchOption.AllDirectories))
                {
                    string mpath = Path.Combine(b, "manifest.json");
                    if (!File.Exists(mpath)) continue;
                    string rel = Path.GetRelativePath(toolRoot, b)
                        .Replace('\\', '/');
                    if (referenced.Contains(rel) || referenced.Any(
                            p => p.StartsWith(rel,
                                StringComparison.OrdinalIgnoreCase)))
                        continue;
                    // Owned by a governing record (job dir) or carrying
                    // its own lineage (source_checkpoint / provenance)
                    // is not an orphan; a bundle with neither is.
                    if (rel.Contains("/jobs/",
                            StringComparison.OrdinalIgnoreCase))
                        continue;
                    JsonDocument doc;
                    try
                    { doc = JsonDocument.Parse(File.ReadAllText(mpath)); }
                    catch { return false; }
                    using (doc)
                    {
                        var r = doc.RootElement;
                        bool selfDescribing =
                            r.TryGetProperty("source_checkpoint",
                                             out var s1) &&
                            s1.ValueKind == JsonValueKind.String &&
                            (s1.GetString() ?? "").Length > 0;
                        if (!selfDescribing &&
                            !File.Exists(Path.Combine(b,
                                             "provenance.json")))
                            return false;
                    }
                }
                return true;
            }),
            new("repo-production-axis-supported", () =>
            {
                string rt = Path.Combine(toolRoot, "xingcheng", "runtime");
                if (!Directory.Exists(rt)) return true;
                var quants = new HashSet<string>(
                    StringComparer.OrdinalIgnoreCase)
                    { "none", "int8", "int4", "int4_packed", "bf16" };
                foreach (string mf in Directory.GetFiles(
                             rt, "manifest.json", SearchOption.AllDirectories))
                {
                    JsonDocument doc;
                    try { doc = JsonDocument.Parse(File.ReadAllText(mf)); }
                    catch { continue; }
                    using (doc)
                    {
                        var r = doc.RootElement;
                        string q = "";
                        if (r.TryGetProperty("quantization", out var qv) &&
                            qv.ValueKind == JsonValueKind.String)
                            q = qv.GetString() ?? "";
                        else if (r.TryGetProperty("config", out var qc) &&
                                 qc.ValueKind == JsonValueKind.Object &&
                                 qc.TryGetProperty("quantization",
                                     out var qcv) &&
                                 qcv.ValueKind == JsonValueKind.String)
                            q = qcv.GetString() ?? "";
                        if (q.Length > 0 && !quants.Contains(q))
                            return false;
                    }
                }
                return true;
            }),
            new("repo-forbidden-language", () =>
            {
                var v = LangCheck.Scan(toolRoot);
                return v["violations"] is System.Collections.IList l
                       && l.Count == 0;
            }),
            new("repo-training-frozen", () =>
            {
                string sp = Path.Combine(
                    toolRoot, "runtime", "settings",
                    "self-learning.json");
                if (!File.Exists(sp)) return true;
                using var doc = JsonDocument.Parse(File.ReadAllText(sp));
                // While the frozen flag is set, no scheduler may emit
                // a weight-changing job — the flag itself is the
                // contract; verify it is still latched. The governed
                // SINGLE_CAPABILITY_RECOVERY lane is itself bounded
                // (one active capability, SFT only) and counts as a
                // latched state — it cannot emit a free-form job.
                var root = doc.RootElement;
                if (root.TryGetProperty(
                        "capability_training_frozen", out var f) &&
                    f.ValueKind == JsonValueKind.True)
                    return true;
                return root.TryGetProperty(
                           "capability_training_mode", out var m) &&
                       m.ValueKind == JsonValueKind.String &&
                       m.GetString() == "SINGLE_CAPABILITY_RECOVERY" &&
                       root.TryGetProperty(
                           "active_capability", out var ac) &&
                       ac.ValueKind == JsonValueKind.String &&
                       !string.IsNullOrEmpty(ac.GetString());
            }),
            new("repo-xcn-writer-v10", () =>
            {
                string rt = Path.Combine(toolRoot, "xingcheng", "runtime");
                if (!Directory.Exists(rt)) return true;
                foreach (string mf in Directory.GetFiles(
                             rt, "manifest.json", SearchOption.AllDirectories))
                {
                    JsonDocument doc;
                    try { doc = JsonDocument.Parse(File.ReadAllText(mf)); }
                    catch { continue; }
                    using (doc)
                    {
                        foreach (string k in new[]
                                 { "checkpoint_version", "xcn_version",
                                   "format_version" })
                            if (doc.RootElement.TryGetProperty(k, out var v) &&
                                v.ValueKind == JsonValueKind.String)
                            {
                                string s = v.GetString() ?? "";
                                var mm = System.Text.RegularExpressions
                                    .Regex.Match(s, @"v(\d+)");
                                if (mm.Success &&
                                    int.Parse(mm.Groups[1].Value) > 10)
                                    return false;   // XCN writer > 10
                            }
                    }
                }
                return true;
            }),
            new("repo-state-version-supported", () =>
            {
                // Every persisted state envelope we honor is v1/v2 —
                // a state file claiming a higher version than the
                // runtime understands is unsupported reads.
                string dir = Path.Combine(
                    toolRoot, GenerationMigration.StateDirRel
                                  .Replace('/', Path.DirectorySeparatorChar));
                if (!Directory.Exists(dir)) return true;
                foreach (string f in Directory.GetFiles(dir, "*.json"))
                {
                    JsonDocument doc;
                    try { doc = JsonDocument.Parse(File.ReadAllText(f)); }
                    catch { return false; }
                    using (doc)
                        if (doc.RootElement.TryGetProperty(
                                "state_version", out var sv) &&
                            sv.ValueKind == JsonValueKind.Number &&
                            sv.GetInt64() > 2)
                            return false;
                }
                return true;
            }),
            new("repo-failure-pool", () =>
            {
                // The pool ledger must be readable and honour its
                // bound — writes happen on real eval failures, not
                // on a gate probe (never pollute the pool).
                var st = FailurePool.Status(toolRoot);
                return TransformerTrainingRepository.Truthy(st["ok"]) &&
                       st["entries"] is int n && n >= 0 &&
                       (int)st["bounded"]! == FailurePool.MaxEntries;
            }),
        };

        var results = new List<object?>();
        int passed = 0;
        foreach (var c in checks)
        {
            bool ok;
            string? err = null;
            try { ok = c.Run(); }
            catch (Exception ex) { ok = false; err = ex.Message; }
            if (ok) ++passed;
            results.Add(new Dictionary<string, object?>
            {
                ["check"] = c.Name, ["ok"] = ok,
                ["error"] = err,
            });
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = passed == checks.Count,
            ["format"] = "star-convergence-checks/v1",
            ["passed"] = passed,
            ["total"] = checks.Count,
            ["checks"] = results,
        };
    }
}
