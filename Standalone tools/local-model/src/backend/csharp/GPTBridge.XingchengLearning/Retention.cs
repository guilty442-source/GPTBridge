// Retention.cs — xingcheng data-retention sweep (star-retention-policy/v1).
//
// Direct port of retention.py: bounds local-model runtime growth by
// pruning old governed job dirs, logs, maturity/self-learning reports and
// SFT snapshots by count and age. Never deletes paths referenced by any
// lifecycle.json artifact version or the checkpoint pinned in
// xingcheng/runtime/settings/native-engine.json (unresolvable paths are
// fail-closed
// kept). Deletions append to xingcheng/runtime/logs/retention.jsonl; the
// audit log itself is capped at the newest 1000 lines once it exceeds
// 1500. Boundary: retention may never touch anything outside
// ``xingcheng/`` (data-residency).

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class Retention
{
    private static readonly string[] EvidenceGlobs =
    {
        "maturity-*.json",
        "self-learning-*.json",
    };

    private static string UtcNow() => XcPaths.IsoNow();

    // ----------------------------------------------------- protected set --

    private static HashSet<string> ProtectedPaths(string toolRoot)
    {
        var protected_ = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        string lifecycleGlob = Path.Combine(toolRoot, XcPaths.LifecycleGlobRel);
        if (Directory.Exists(lifecycleGlob))
        {
            foreach (string lifecycleFile in Directory.EnumerateFiles(
                         lifecycleGlob, "lifecycle.json",
                         SearchOption.AllDirectories))
            {
                Dictionary<string, object?>? data = ReadJson(lifecycleFile);
                if (data == null) continue;
                if (!data.TryGetValue("artifacts", out object? art) ||
                    art is not Dictionary<string, object?> artifacts)
                    continue;
                foreach (var group in artifacts.Values)
                {
                    if (group is not Dictionary<string, object?> g) continue;
                    if (!g.TryGetValue("versions", out object? vers) ||
                        vers is not List<object?> versions)
                        continue;
                    foreach (var entry in versions)
                    {
                        if (entry is not Dictionary<string, object?> e) continue;
                        string raw = TransformerTrainingRepository
                            .Str(e, "path") ?? "";
                        if (raw.Length == 0) continue;
                        try { protected_.Add(Path.GetFullPath(raw)); }
                        catch { /* unresolvable -> fail-closed elsewhere */ }
                    }
                }
            }
        }

        string settingsFile = Path.Combine(toolRoot, XcPaths.EngineSettingsRel);
        var settings = ReadJson(settingsFile);
        if (settings != null)
        {
            string pinned = TransformerTrainingRepository
                .Str(settings, "checkpoint") ?? "";
            if (pinned.Length > 0)
                protected_.Add(Path.GetFullPath(
                    Path.IsPathRooted(pinned)
                        ? pinned
                        : Path.Combine(toolRoot, pinned)));
        }

        // G65: maturity / self-learning evidence may reference a checkpoint
        // that is no longer present in a lifecycle generation — those
        // references are evidence roots and must survive retention.
        string logsDir = Path.Combine(toolRoot, XcPaths.LogsRel);
        if (Directory.Exists(logsDir))
        {
            foreach (string pattern in EvidenceGlobs)
            {
                foreach (string evidenceFile in Directory.EnumerateFiles(
                             logsDir, pattern))
                {
                    var evidence = ReadJson(evidenceFile);
                    if (evidence == null) continue;
                    var stack = new Stack<object?>();
                    stack.Push(evidence);
                    while (stack.Count > 0)
                    {
                        object? value = stack.Pop();
                        if (value is Dictionary<string, object?> map)
                        {
                            foreach (var (key, child) in map)
                            {
                                if (XcPaths.EvidencePathKeys.Contains(key) &&
                                    child is string s && s.Trim().Length > 0)
                                {
                                    try
                                    {
                                        protected_.Add(Path.GetFullPath(
                                            Path.IsPathRooted(s)
                                                ? s
                                                : Path.Combine(toolRoot, s)));
                                    }
                                    catch { /* unresolvable */ }
                                }
                                else if (child is Dictionary<string, object?> ||
                                         child is List<object?>)
                                {
                                    stack.Push(child);
                                }
                            }
                        }
                        else if (value is List<object?> list)
                        {
                            foreach (var item in list) stack.Push(item);
                        }
                    }
                }
            }
        }
        // Registered datasets are immutable rows whose snapshot_path file
        // is part of the row's identity — pruning one makes the dataset
        // permanently unexecutable, so every registered snapshot is
        // protected. If the registry is unreadable, fail closed by
        // protecting every snapshot file in the directory.
        try
        {
            var repo = new TransformerTrainingRepository(toolRoot);
            foreach (string sp in repo.DatasetSnapshotPaths())
            {
                if (sp.Length == 0) continue;
                string p = Path.IsPathRooted(sp)
                    ? sp : Path.Combine(toolRoot, sp);
                try { protected_.Add(Path.GetFullPath(p)); }
                catch { /* unresolvable -> kept fail-closed elsewhere */ }
            }
        }
        catch
        {
            string snapDir = Path.Combine(
                toolRoot, XcPaths.SelfLearningSnapshotRel);
            if (Directory.Exists(snapDir))
                foreach (string f in Directory.EnumerateFiles(
                             snapDir, "*.jsonl"))
                    try { protected_.Add(Path.GetFullPath(f)); }
                    catch { /* keep */ }
        }
        return protected_;
    }

    private static Dictionary<string, object?>? ReadJson(string path)
    {
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            if (doc.RootElement.ValueKind != JsonValueKind.Object) return null;
            var map = new Dictionary<string, object?>();
            foreach (var p in doc.RootElement.EnumerateObject())
                map[p.Name] = ModelLifecycle.Decode(p.Value);
            return map;
        }
        catch { return null; }
    }

    private static bool IsProtected(string path, HashSet<string> protected_)
    {
        string resolved;
        try { resolved = Path.GetFullPath(path); }
        catch { return true; } // unresolvable -> fail-closed keep
        if (protected_.Contains(resolved)) return true;
        // a directory containing any protected file is kept whole
        foreach (string parent in protected_)
        {
            string parentDir = Path.GetDirectoryName(parent) ?? parent;
            if (resolved.Equals(parentDir, StringComparison.OrdinalIgnoreCase))
                return true;
            if (parentDir.StartsWith(
                    resolved + Path.DirectorySeparatorChar,
                    StringComparison.OrdinalIgnoreCase))
                return true;
        }
        return false;
    }

    private static bool DeletePath(string path, bool dryRun)
    {
        if (dryRun) return true;
        try
        {
            if (Directory.Exists(path))
                Directory.Delete(path, recursive: true);
            else
                File.Delete(path);
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    // --------------------------------------------------------- planners --

    private static List<string> PlanJobDirs(
        string toolRoot, RetentionPolicy policy, HashSet<string> protected_)
    {
        string jobsDir = Path.Combine(toolRoot, XcPaths.JobsRel);
        if (!Directory.Exists(jobsDir)) return new List<string>();
        var dirs = Directory.EnumerateDirectories(jobsDir)
            .Select(d => new DirectoryInfo(d))
            .OrderByDescending(d => d.LastWriteTimeUtc)
            .Select(d => d.FullName)
            .ToList();
        return dirs.Skip(Math.Max(0, policy.KeepJobDirs))
                   .Where(d => !IsProtected(d, protected_))
                   .ToList();
    }

    private static List<string> PlanLogs(string toolRoot, RetentionPolicy policy)
    {
        string logsDir = Path.Combine(toolRoot, XcPaths.LogsRel);
        if (!Directory.Exists(logsDir)) return new List<string>();
        var now = DateTimeOffset.UtcNow;
        var cutoff = now.AddSeconds(-policy.KeepLogsDays * 86400.0);
        var reportCutoff = now.AddSeconds(-policy.KeepReportDays * 86400.0);
        var victims = new List<string>();
        foreach (string file in Directory.EnumerateFiles(logsDir))
        {
            var info = new FileInfo(file);
            if (info.Name == "retention.jsonl") continue;
            var mtime = info.LastWriteTimeUtc;
            if (info.Extension == ".log" && mtime < cutoff)
                victims.Add(file);
            else if ((info.Name.StartsWith("maturity-",
                          StringComparison.Ordinal) ||
                      info.Name.StartsWith("self-learning-",
                          StringComparison.Ordinal)) &&
                     info.Extension == ".json" && mtime < reportCutoff)
                victims.Add(file);
        }
        // count caps apply even when reports are not yet aged out
        foreach (var (prefix, keep) in new (string, int)[]
                 {
                     ("maturity-", policy.KeepMaturityReports),
                     ("self-learning-", policy.KeepSelfLearningReports),
                 })
        {
            var reports = Directory.EnumerateFiles(logsDir, $"{prefix}*.json")
                .Select(f => new FileInfo(f))
                .OrderByDescending(f => f.LastWriteTimeUtc)
                .Select(f => f.FullName)
                .ToList();
            victims.AddRange(reports.Skip(Math.Max(0, keep))
                                    .Where(r => !victims.Contains(r)));
        }
        return victims;
    }

    private static List<string> PlanSnapshots(
        string toolRoot, RetentionPolicy policy,
        HashSet<string> protected_)
    {
        string snapDir = Path.Combine(toolRoot, XcPaths.SelfLearningSnapshotRel);
        if (!Directory.Exists(snapDir)) return new List<string>();
        var snaps = Directory.EnumerateFiles(snapDir, "*.jsonl")
            .Select(f => new FileInfo(f))
            .OrderByDescending(f => f.LastWriteTimeUtc)
            .Select(f => f.FullName)
            .ToList();
        return snaps.Skip(Math.Max(0, policy.KeepSnapshots))
                    .Where(f => !IsProtected(f, protected_))
                    .ToList();
    }

    private static HashSet<string> PinnedCheckpoint(string toolRoot)
    {
        var keep = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var settings = ReadJson(
            Path.Combine(toolRoot, XcPaths.EngineSettingsRel));
        if (settings == null) return keep;
        string pinned = TransformerTrainingRepository
            .Str(settings, "checkpoint") ?? "";
        if (pinned.Length == 0) return keep;
        keep.Add(Path.GetFullPath(
            Path.IsPathRooted(pinned)
                ? pinned : Path.Combine(toolRoot, pinned)));
        return keep;
    }

    private static Dictionary<string, object?> RetireWeightGenerations(
        string toolRoot, RetentionPolicy policy)
    {
        var report = new Dictionary<string, object?>
        {
            ["models"] = new Dictionary<string, object?>(),
            ["retired_versions"] = new List<object?>(),
        };
        var extraKeep = PinnedCheckpoint(toolRoot);
        string lifecycleDir = Path.Combine(toolRoot, XcPaths.LifecycleGlobRel);
        if (!Directory.Exists(lifecycleDir)) return report;
        var models = (Dictionary<string, object?>)report["models"]!;
        var retiredList = (List<object?>)report["retired_versions"]!;
        foreach (string lifecycleFile in Directory.EnumerateFiles(
                     lifecycleDir, "lifecycle.json",
                     SearchOption.AllDirectories).OrderBy(s => s))
        {
            ModelLifecycle lifecycle;
            try
            {
                lifecycle = ModelLifecycle.Load(
                    Path.GetDirectoryName(lifecycleFile)!);
            }
            catch { continue; }
            var retired = lifecycle.RetireWeights(
                policy.KeepWeightVersions, extraKeep);
            if (retired.Count > 0)
            {
                lifecycle.Save(Path.GetDirectoryName(lifecycleFile)!);
                models[lifecycle.ModelId] = retired
                    .Select(e => (object?)Convert.ToInt32(e["version"]))
                    .ToList();
                foreach (var e in retired)
                    retiredList.Add(TransformerTrainingRepository
                        .Str(e, "path") ?? "");
            }
        }
        return report;
    }

    private static List<string> PlanRetiredWeights(
        string toolRoot, HashSet<string> protected_)
    {
        var victims = new List<string>();
        string lifecycleDir = Path.Combine(toolRoot, XcPaths.LifecycleGlobRel);
        if (!Directory.Exists(lifecycleDir)) return victims;
        foreach (string lifecycleFile in Directory.EnumerateFiles(
                     lifecycleDir, "lifecycle.json",
                     SearchOption.AllDirectories).OrderBy(s => s))
        {
            var data = ReadJson(lifecycleFile);
            if (data == null) continue;
            if (!data.TryGetValue("artifacts", out object? art) ||
                art is not Dictionary<string, object?> artifacts)
                continue;
            if (!artifacts.TryGetValue("weights", out object? w) ||
                w is not Dictionary<string, object?> weights)
                continue;
            if (!weights.TryGetValue("retired", out object? r) ||
                r is not List<object?> retired)
                continue;
            foreach (var entry in retired)
            {
                if (entry is not Dictionary<string, object?> e) continue;
                string raw = TransformerTrainingRepository.Str(e, "path") ?? "";
                if (raw.Length == 0) continue;
                string path = Path.IsPathRooted(raw)
                    ? raw : Path.Combine(toolRoot, raw);
                if (File.Exists(path) && !IsProtected(path, protected_))
                    victims.Add(path);
                // retired bundle directories: path may point at a manifest
                // inside a bundle dir or at the dir itself.
                else if (Directory.Exists(path) &&
                         !IsProtected(path, protected_))
                    victims.Add(path);
            }
        }
        return victims;
    }

    // -------------------------------------------------------------- apply --

    public static Dictionary<string, object?> ApplyRetention(
        string toolRoot, RetentionPolicy? policy = null, bool dryRun = false)
    {
        string root = Path.GetFullPath(toolRoot);
        var resolved = policy ?? RetentionPolicy.Load(root);
        if (!resolved.Enabled)
            return new Dictionary<string, object?>
            {
                ["ok"] = true,
                ["action"] = "disabled",
                ["checked_at"] = UtcNow(),
            };

        Dictionary<string, object?> retirement = dryRun
            ? new Dictionary<string, object?>
            {
                ["models"] = new Dictionary<string, object?>(),
                ["retired_versions"] = new List<object?>(),
                ["skipped"] = "dry-run",
            }
            : RetireWeightGenerations(root, resolved);

        var protected_ = ProtectedPaths(root);
        var plans = new Dictionary<string, List<string>>
        {
            ["job_dirs"] = PlanJobDirs(root, resolved, protected_),
            ["logs"] = PlanLogs(root, resolved),
            ["snapshots"] = PlanSnapshots(root, resolved, protected_),
            ["retired_weights"] = PlanRetiredWeights(root, protected_),
        };
        string boundary = Path.GetFullPath(
            Path.Combine(root, "xingcheng"));
        var deleted = new List<Dictionary<string, object?>>();
        int boundarySkipped = 0;
        foreach (var (category, paths) in plans)
        {
            foreach (string path in paths)
            {
                string resolvedPath;
                try { resolvedPath = Path.GetFullPath(path); }
                catch { boundarySkipped++; continue; }
                if (!resolvedPath.StartsWith(
                        boundary + Path.DirectorySeparatorChar,
                        StringComparison.OrdinalIgnoreCase) &&
                    !resolvedPath.Equals(boundary,
                        StringComparison.OrdinalIgnoreCase))
                {
                    boundarySkipped++;
                    continue;
                }
                if (category is "logs" or "retired_weights" &&
                    IsProtected(path, protected_))
                    continue;
                long size = 0;
                try
                {
                    if (Directory.Exists(path))
                        size = Directory.EnumerateFiles(path, "*",
                                SearchOption.AllDirectories)
                            .Sum(f => new FileInfo(f).Length);
                    else
                        size = new FileInfo(path).Length;
                }
                catch { /* size is best-effort */ }
                if (DeletePath(path, dryRun))
                    deleted.Add(new Dictionary<string, object?>
                    {
                        ["category"] = category,
                        ["path"] = path,
                        ["bytes"] = size,
                    });
            }
        }

        var result = new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["action"] = dryRun ? "dry-run" : "applied",
            ["policy"] = resolved.ToDict(),
            ["weight_retirement"] = retirement,
            ["protected_paths"] = protected_.Count,
            ["deleted"] = deleted,
            ["deleted_bytes"] = deleted.Sum(
                d => Convert.ToInt64(d["bytes"])),
            ["boundary_skipped"] = boundarySkipped,
            ["checked_at"] = UtcNow(),
        };
        if (!dryRun)
        {
            string audit = Path.Combine(root, XcPaths.RetentionAuditRel);
            Directory.CreateDirectory(Path.GetDirectoryName(audit)!);
            File.AppendAllText(audit,
                CanonicalJson.PlainDict(new Dictionary<string, object?>
                {
                    ["at"] = result["checked_at"],
                    ["deleted"] = deleted,
                    ["deleted_bytes"] = result["deleted_bytes"],
                }) + "\n", new System.Text.UTF8Encoding(false));
            TrimAuditLog(audit, keepLines: 1000, trimAbove: 1500);
        }
        return result;
    }

    private static void TrimAuditLog(string path, int keepLines, int trimAbove)
    {
        try
        {
            var lines = File.ReadAllLines(path);
            if (lines.Length <= trimAbove) return;
            File.WriteAllLines(path, lines.Skip(lines.Length - keepLines));
        }
        catch { /* fail-open: audit trim never blocks the sweep */ }
    }
}
