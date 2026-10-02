// file-sorter business engine — native port of the retired Python CLI
// (B167/B38). Implements the governed arg contract over
// ``toolbox_run_tool`` documented in the tool README + codex
// architecture-tool-file-sorter: arg-typed commands with typed
// ``FILE_SORTER_*_JSON=`` stdout receipts.
//
// Behaviour contract (normative: codex tool doc + README V2):
//   * first-level subfolder names are implicit keywords (each maps to
//     itself); explicit rules map a keyword to a first-level child that
//     must already exist — absolute/multi-level/foreign destinations are
//     rejected;
//   * manual organize is preview-first: --preview-json mints a plan_id,
//     --apply-plan <id> re-validates every source (size + mtime +
//     SHA-256) and refuses when anything changed since preview;
//   * moves are journaled, never overwrite (serial suffix on collision)
//     and SHA-256 verified; --undo-last reverses the newest applied
//     transaction and stops on the first occupied/changed path;
//   * incomplete downloads (.crdownload/.part/.tmp/.download/.partial),
//     zero-byte files and locked files are skipped; unclassifiable files
//     stay in place;
//   * --cleanup-scan needs the media/model analysis lane, which has no
//     native successor yet — the command fails closed with
//     CLEANUP_ANALYZER_PENDING instead of fabricating candidates.
//
// State is owner-private bounded operational state (never an
// authority): ``FILE_SORTER_STATE_ROOT`` or
// %LOCALAPPDATA%\GPTBridge\file-sorter holding profiles.json,
// plans/*.json and journal.jsonl.

using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using GPTBridge.ToolHost;

namespace GPTBridge.ToolHost.App;

internal sealed class FileSorterException : Exception
{
    public FileSorterException(string code, string message)
        : base(message) => ErrorCode = code;

    public string ErrorCode { get; }
}

internal static class FileSorterEngine
{
    internal const string FoldersPrefix = "FILE_SORTER_FOLDERS_JSON=";
    internal const string PreviewPrefix = "FILE_SORTER_PREVIEW_JSON=";
    internal const string PlanPrefix = "FILE_SORTER_PLAN_JSON=";
    internal const string ProfilesPrefix = "FILE_SORTER_PROFILES_JSON=";

    private static readonly HashSet<string> IncompleteExtensions = new(
        StringComparer.OrdinalIgnoreCase)
    {
        ".crdownload", ".part", ".partial", ".tmp", ".download", ".!ut",
    };

    public static string StateRoot()
    {
        var overrideRoot =
            Environment.GetEnvironmentVariable("FILE_SORTER_STATE_ROOT");
        if (!string.IsNullOrWhiteSpace(overrideRoot))
            return Path.GetFullPath(overrideRoot.Trim());
        return Path.Combine(
            Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData),
            "GPTBridge", "file-sorter");
    }

    private static string NormalizeTarget(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw))
            throw new FileSorterException(
                "TARGET_REQUIRED", "目標資料夾必填");
        var target = Path.GetFullPath(raw.Trim());
        if (!Directory.Exists(target))
            throw new FileSorterException(
                "TARGET_MISSING", "目標資料夾不存在或無法存取");
        return target;
    }

    private static string TargetKey(string target) =>
        NormalizePath(target);

    private static string NormalizePath(string path) =>
        path.Trim().TrimEnd('\\', '/').Replace('/', '\\')
            .ToLowerInvariant();

    private static bool IsDirectChildName(string name)
    {
        var trimmed = name.Trim();
        return trimmed.Length > 0
            && trimmed != "." && trimmed != ".."
            && !trimmed.Contains('/') && !trimmed.Contains('\\')
            && !trimmed.Contains(':');
    }

    // ------------------------------------------------------------- state --

    private sealed class Profiles
    {
        public Dictionary<string, JsonObject> Targets { get; } =
            new(StringComparer.OrdinalIgnoreCase);
    }

    private static string ProfilesPath(string stateRoot) =>
        Path.Combine(stateRoot, "profiles.json");

    private static Profiles LoadProfiles(string stateRoot)
    {
        var profiles = new Profiles();
        var path = ProfilesPath(stateRoot);
        if (!File.Exists(path))
            return profiles;
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(path)) as JsonObject;
            if (node?["targets"] is JsonObject targets)
            {
                foreach (var (key, value) in targets)
                {
                    if (value is JsonObject profile)
                        profiles.Targets[key] = profile;
                }
            }
        }
        catch (JsonException)
        {
            // Corrupt owner-private state: fail closed with an empty
            // profile set rather than trusting partial data.
        }
        return profiles;
    }

    private static void SaveProfiles(string stateRoot, Profiles profiles)
    {
        Directory.CreateDirectory(stateRoot);
        var targets = new JsonObject();
        foreach (var (key, profile) in profiles.Targets)
            targets[key] = profile.DeepClone();
        AtomicWrite(
            ProfilesPath(stateRoot),
            new JsonObject { ["targets"] = targets }.ToJsonString());
    }

    private static JsonObject ProfileFor(
        Profiles profiles, string target)
    {
        if (!profiles.Targets.TryGetValue(TargetKey(target), out var p))
        {
            p = new JsonObject
            {
                ["target_dir"] = target,
                ["enabled"] = false,
                ["duplicate_trash_enabled"] = false,
                ["rules"] = new JsonObject(),
            };
            profiles.Targets[TargetKey(target)] = p;
        }
        p["target_dir"] = target;
        return p;
    }

    private static void AtomicWrite(string path, string content)
    {
        var tmp = path + ".tmp-" + Guid.NewGuid().ToString("N");
        File.WriteAllText(tmp, content);
        File.Move(tmp, path, overwrite: true);
    }

    // ------------------------------------------------------------ rules --

    private static List<string> FirstLevelFolders(string target) =>
        Directory.GetDirectories(target)
            .Select(Path.GetFileName)
            .Where(n => n is { Length: > 0 } && IsDirectChildName(n))
            .Select(n => n!)
            .OrderBy(n => n, StringComparer.OrdinalIgnoreCase)
            .ToList();

    /// <summary>Merged rules: explicit profile rules overlaid on implicit
    /// folder-name keywords. Longest keyword first for deterministic
    /// matching.</summary>
    private static List<(string Keyword, string Folder)> MergedRules(
        string target, JsonObject profile)
    {
        var merged = new Dictionary<string, string>(
            StringComparer.OrdinalIgnoreCase);
        foreach (var folder in FirstLevelFolders(target))
            merged[folder] = folder;
        if (profile["rules"] is JsonObject rules)
        {
            foreach (var (keyword, folderNode) in rules)
            {
                var folder = folderNode?.GetValue<string>() ?? "";
                if (keyword.Trim().Length > 0
                    && IsDirectChildName(folder)
                    && Directory.Exists(Path.Combine(target, folder)))
                    merged[keyword.Trim()] = folder;
            }
        }
        return merged
            .Select(p => (p.Key, p.Value))
            .OrderByDescending(p => p.Key.Length)
            .ToList();
    }

    // ------------------------------------------------------------- ops ---

    public static string Run(
        IReadOnlyList<string> args,
        Func<JsonObject, object>? emitProgress,
        CancellationToken ct)
    {
        if (args.Count < 2)
            throw new FileSorterException(
                "ARGS_INVALID", "缺少目標資料夾或命令旗標");
        var target = NormalizeTarget(args[0]);
        var flags = args.Skip(1).ToList();
        var has = (string flag) => flags.Contains(flag);
        var flagValue = (string flag) =>
        {
            var i = flags.IndexOf(flag);
            return i >= 0 && i + 1 < flags.Count ? flags[i + 1] : null;
        };
        var stateRoot = StateRoot();

        if (has("--cleanup-scan"))
            throw new FileSorterException(
                "CLEANUP_ANALYZER_PENDING",
                "媒體/相似分析車道尚未移植原生實作（需受管模型與"
                + " PostgreSQL 指紋快取）；清理掃描維持 fail-closed");
        if (has("--list-folders"))
            return ListFolders(target);
        if (has("--profiles-json") || has("--select-scan-target"))
            return ProfilesJson(stateRoot, target);
        if (has("--list-keywords"))
            return ListKeywords(target, stateRoot);
        if (has("--list-source-files"))
            return ListSourceFiles(target);
        if (has("--preview-json"))
            return Preview(stateRoot, target, emitProgress, ct);
        if (flagValue("--apply-plan") is { } planId)
            return ApplyPlan(stateRoot, target, planId, emitProgress, ct);
        if (has("--history-json"))
            return History(stateRoot, target);
        if (has("--undo-last"))
            return UndoLast(stateRoot, target, ct);
        if (flagValue("--set-profile-enabled") is { } enabled)
            return SetProfileFlag(
                stateRoot, target, "enabled", enabled);
        if (flagValue("--set-duplicate-trash-enabled") is { } dupTrash)
            return SetProfileFlag(
                stateRoot, target, "duplicate_trash_enabled", dupTrash);
        if (has("--update-keyword"))
            return UpdateKeyword(
                stateRoot, target,
                flagValue("--update-keyword"),
                flagValue("--new-keyword"),
                flagValue("--folder"));
        var upserts = flags
            .Select((f, i) => (f, i))
            .Where(p => p.f is "--upsert-keyword" or "--add-keyword"
                && p.i + 1 < flags.Count
                && !flags[p.i + 1].StartsWith("--"))
            .Select(p => flags[p.i + 1])
            .ToList();
        if (upserts.Count > 0)
            return UpsertKeywords(
                stateRoot, target, upserts, flagValue("--folder"));
        throw new FileSorterException(
            "ARGS_UNSUPPORTED", $"未支援的命令旗標: {string.Join(' ', flags)}");
    }

    private static string ListFolders(string target) =>
        FoldersPrefix + JsonSerializer.Serialize(FirstLevelFolders(target));

    private static string ProfilesJson(string stateRoot, string target)
    {
        var profiles = LoadProfiles(stateRoot);
        SaveProfiles(stateRoot, profiles); // persist lazy creation
        var profile = ProfileFor(profiles, target);
        return ProfilesPrefix + profile.DeepClone().ToJsonString();
    }

    private static string ListKeywords(string stateRoot, string target)
    {
        var profile = ProfileFor(LoadProfiles(stateRoot), target);
        var rules = MergedRules(target, profile).Select(r =>
            new JsonObject
            {
                ["keyword"] = r.Keyword,
                ["folder"] = r.Folder,
            }).ToArray();
        return new JsonObject
        {
            ["ok"] = true,
            ["target_dir"] = target,
            ["rules"] = new JsonArray(rules),
        }.ToJsonString();
    }

    private static string ListSourceFiles(string target)
    {
        var files = CandidateFiles(target, ct: default)
            .Select(f => f.Name).ToArray();
        return new JsonObject
        {
            ["ok"] = true,
            ["target_dir"] = target,
            ["files"] = new JsonArray(
                files.Select(f => (JsonNode)f).ToArray()),
        }.ToJsonString();
    }

    private static string SetProfileFlag(
        string stateRoot, string target, string flag, string raw)
    {
        if (!bool.TryParse(raw, out var value))
            throw new FileSorterException(
                "ARGS_INVALID", $"{flag} 需要 true/false");
        var profiles = LoadProfiles(stateRoot);
        ProfileFor(profiles, target)[flag] = value;
        SaveProfiles(stateRoot, profiles);
        return new JsonObject
        {
            ["ok"] = true,
            ["target_dir"] = target,
            [flag] = value,
        }.ToJsonString();
    }

    private static string UpsertKeywords(
        string stateRoot, string target,
        IReadOnlyList<string> keywords, string? folder)
    {
        if (string.IsNullOrWhiteSpace(folder))
            throw new FileSorterException(
                "ARGS_INVALID", "--folder 目的地必填");
        if (!IsDirectChildName(folder!)
            || !Directory.Exists(Path.Combine(target, folder)))
            throw new FileSorterException(
                "DESTINATION_INVALID",
                "分類目的地必須是目標內已存在的第一層子資料夾");
        var profiles = LoadProfiles(stateRoot);
        var profile = ProfileFor(profiles, target);
        if (profile["rules"] is not JsonObject rules)
        {
            rules = new JsonObject();
            profile["rules"] = rules;
        }
        var applied = new List<string>();
        foreach (var keyword in keywords)
        {
            var trimmed = keyword.Trim();
            if (trimmed.Length == 0)
                continue;
            rules[trimmed] = folder;
            applied.Add(trimmed);
        }
        SaveProfiles(stateRoot, profiles);
        return new JsonObject
        {
            ["ok"] = true,
            ["target_dir"] = target,
            ["folder"] = folder,
            ["keywords"] = new JsonArray(
                applied.Select(k => (JsonNode)k).ToArray()),
        }.ToJsonString();
    }

    private static string UpdateKeyword(
        string stateRoot, string target,
        string? oldKeyword, string? newKeyword, string? folder)
    {
        if (string.IsNullOrWhiteSpace(oldKeyword)
            || string.IsNullOrWhiteSpace(newKeyword))
            throw new FileSorterException(
                "ARGS_INVALID", "--update-keyword 需要舊與新關鍵字");
        var profiles = LoadProfiles(stateRoot);
        var profile = ProfileFor(profiles, target);
        if (profile["rules"] is not JsonObject rules)
        {
            rules = new JsonObject();
            profile["rules"] = rules;
        }
        var dest = !string.IsNullOrWhiteSpace(folder)
            ? folder!.Trim()
            : rules[oldKeyword]?.GetValue<string>();
        if (string.IsNullOrWhiteSpace(dest))
            throw new FileSorterException(
                "DESTINATION_INVALID", "找不到既有規則且未提供 --folder");
        if (!IsDirectChildName(dest)
            || !Directory.Exists(Path.Combine(target, dest)))
            throw new FileSorterException(
                "DESTINATION_INVALID",
                "分類目的地必須是目標內已存在的第一層子資料夾");
        rules.Remove(oldKeyword);
        rules[newKeyword.Trim()] = dest;
        SaveProfiles(stateRoot, profiles);
        return new JsonObject
        {
            ["ok"] = true,
            ["target_dir"] = target,
            ["updated"] = newKeyword.Trim(),
            ["folder"] = dest,
        }.ToJsonString();
    }

    // ------------------------------------------------------------- plan --

    private sealed record Candidate(
        string Name, string FullPath, long Size, DateTime MtimeUtc);

    private static List<Candidate> CandidateFiles(
        string target, CancellationToken ct)
    {
        var files = new List<Candidate>();
        foreach (var path in Directory.EnumerateFiles(target))
        {
            ct.ThrowIfCancellationRequested();
            FileInfo info;
            try
            {
                info = new FileInfo(path);
                if (info.Length == 0
                    || (info.Attributes & (FileAttributes.Hidden
                        | FileAttributes.System)) != 0
                    || IncompleteExtensions.Contains(info.Extension))
                    continue;
                // Skip files still locked by another writer.
                using var probe = info.Open(
                    FileMode.Open, FileAccess.Read, FileShare.None);
            }
            catch (IOException)
            {
                continue;
            }
            catch (UnauthorizedAccessException)
            {
                continue;
            }
            files.Add(new Candidate(
                info.Name, info.FullName, info.Length,
                info.LastWriteTimeUtc));
        }
        return files;
    }

    private static string Sha256Hex(string path)
    {
        using var stream = new FileStream(
            path, FileMode.Open, FileAccess.Read, FileShare.Read);
        return Convert.ToHexString(SHA256.HashData(stream))
            .ToLowerInvariant();
    }

    private static string PlansDir(string stateRoot) =>
        Path.Combine(stateRoot, "plans");

    private static string Preview(
        string stateRoot, string target,
        Func<JsonObject, object>? emitProgress, CancellationToken ct)
    {
        var profile = ProfileFor(LoadProfiles(stateRoot), target);
        var rules = MergedRules(target, profile);
        var moves = new JsonArray();
        var skipped = new JsonArray();
        var candidates = CandidateFiles(target, ct);
        var index = 0;
        foreach (var file in candidates)
        {
            ct.ThrowIfCancellationRequested();
            index++;
            emitProgress?.Invoke(new JsonObject
            {
                ["phase"] = "folder_scan",
                ["current_folder"] = target,
                ["current_file"] = file.Name,
                ["scanned"] = index,
                ["total"] = candidates.Count,
            });
            var stem = Path.GetFileNameWithoutExtension(file.Name);
            var match = rules.FirstOrDefault(r =>
                stem.Contains(r.Keyword, StringComparison.OrdinalIgnoreCase));
            if (match.Keyword is null)
            {
                skipped.Add(file.Name);
                continue;
            }
            moves.Add(new JsonObject
            {
                ["source"] = file.Name,
                ["dest_folder"] = match.Folder,
                ["keyword"] = match.Keyword,
                ["size"] = file.Size,
                ["mtime_utc"] = file.MtimeUtc.ToString("O"),
                ["sha256"] = Sha256Hex(file.FullPath),
            });
        }
        var plan = new JsonObject
        {
            ["target_dir"] = target,
            ["created_utc"] = DateTime.UtcNow.ToString("O"),
            ["moves"] = moves,
            ["skipped"] = skipped,
            ["move_count"] = moves.Count,
        };
        var planId = "plan-"
            + DateTime.UtcNow.ToString("yyyyMMddHHmmss")
            + "-"
            + Convert.ToHexString(
                SHA256.HashData(
                    System.Text.Encoding.UTF8.GetBytes(
                        plan.ToJsonString())))[..12].ToLowerInvariant();
        plan["plan_id"] = planId;
        Directory.CreateDirectory(PlansDir(stateRoot));
        AtomicWrite(
            Path.Combine(PlansDir(stateRoot), planId + ".json"),
            plan.ToJsonString());
        return PreviewPrefix + plan.ToJsonString();
    }

    private static string ApplyPlan(
        string stateRoot, string target, string planId,
        Func<JsonObject, object>? emitProgress, CancellationToken ct)
    {
        if (!System.Text.RegularExpressions.Regex.IsMatch(
                planId, "^plan-[0-9]{14}-[0-9a-f]{12}$"))
            throw new FileSorterException(
                "PLAN_NOT_FOUND", "找不到指定的預覽計畫");
        var planPath = Path.Combine(PlansDir(stateRoot), planId + ".json");
        if (!File.Exists(planPath))
            throw new FileSorterException(
                "PLAN_NOT_FOUND", "找不到指定的預覽計畫");
        var plan = JsonNode.Parse(File.ReadAllText(planPath))
            as JsonObject
            ?? throw new FileSorterException(
                "PLAN_UNREADABLE", "預覽計畫無法解析");
        if (NormalizePath(plan["target_dir"]?.GetValue<string>() ?? "")
            != NormalizePath(target))
            throw new FileSorterException(
                "PLAN_TARGET_MISMATCH", "計畫目標資料夾不符");

        var applied = new JsonArray();
        var failed = new JsonArray();
        var moves = plan["moves"] as JsonArray ?? new JsonArray();
        var index = 0;
        foreach (var moveNode in moves)
        {
            ct.ThrowIfCancellationRequested();
            index++;
            var move = (JsonObject)moveNode!;
            var name = move["source"]!.GetValue<string>();
            var folder = move["dest_folder"]!.GetValue<string>();
            var source = Path.Combine(target, name);
            var destDir = Path.Combine(target, folder);
            emitProgress?.Invoke(new JsonObject
            {
                ["phase"] = "apply",
                ["current_file"] = name,
                ["done"] = index,
                ["total"] = moves.Count,
            });
            try
            {
                // Re-validate the preview-time source before touching it:
                // any size/mtime/hash drift means the plan is stale.
                var info = new FileInfo(source);
                if (!info.Exists
                    || info.Length != move["size"]!.GetValue<long>()
                    || info.LastWriteTimeUtc.ToString("O")
                        != move["mtime_utc"]!.GetValue<string>()
                    || Sha256Hex(source)
                        != move["sha256"]!.GetValue<string>())
                    throw new FileSorterException(
                        "PLAN_STALE",
                        $"{name} 在預覽後已變更，拒絕搬移");
                if (!IsDirectChildName(folder) || !Directory.Exists(destDir))
                    throw new FileSorterException(
                        "DESTINATION_INVALID",
                        $"{folder} 不是目標內既有的第一層子資料夾");
                var finalPath = FreeDestinationPath(destDir, name);
                var pending = Path.Combine(
                    destDir, ".gptbridge-pending-" + name);
                File.Move(source, pending);
                if (Sha256Hex(pending) != move["sha256"]!.GetValue<string>())
                {
                    File.Move(pending, source);
                    throw new FileSorterException(
                        "INTEGRITY_MISMATCH",
                        $"{name} 搬移後校驗失敗，已還原");
                }
                File.Move(pending, finalPath);
                applied.Add(new JsonObject
                {
                    ["source"] = source,
                    ["dest"] = finalPath,
                    ["sha256"] = move["sha256"]!.GetValue<string>(),
                });
            }
            catch (FileSorterException exc)
            {
                failed.Add(new JsonObject
                {
                    ["source"] = name,
                    ["error_code"] = exc.ErrorCode,
                    ["message"] = exc.Message,
                });
            }
            catch (IOException exc)
            {
                failed.Add(new JsonObject
                {
                    ["source"] = name,
                    ["error_code"] = "IO_ERROR",
                    ["message"] = exc.Message,
                });
            }
        }
        var txId = "tx-" + DateTime.UtcNow.ToString("yyyyMMddHHmmss")
            + "-" + Guid.NewGuid().ToString("N")[..8];
        if (applied.Count > 0)
            AppendJournal(stateRoot, new JsonObject
            {
                ["tx_id"] = txId,
                ["target_dir"] = target,
                ["applied_utc"] = DateTime.UtcNow.ToString("O"),
                ["status"] = "applied",
                ["moves"] = applied.DeepClone(),
            });
        File.Delete(planPath);
        return PlanPrefix + new JsonObject
        {
            ["ok"] = failed.Count == 0,
            ["plan_id"] = planId,
            ["tx_id"] = txId,
            ["applied_count"] = applied.Count,
            ["failed_count"] = failed.Count,
            ["applied"] = applied,
            ["failed"] = failed,
        }.ToJsonString();
    }

    private static string FreeDestinationPath(string destDir, string name)
    {
        var candidate = Path.Combine(destDir, name);
        if (!File.Exists(candidate))
            return candidate;
        var stem = Path.GetFileNameWithoutExtension(name);
        var ext = Path.GetExtension(name);
        for (var i = 2; ; i++)
        {
            candidate = Path.Combine(
                destDir, $"{stem} ({i}){ext}");
            if (!File.Exists(candidate))
                return candidate;
        }
    }

    // ---------------------------------------------------------- journal --

    private static string JournalPath(string stateRoot) =>
        Path.Combine(stateRoot, "journal.jsonl");

    private static void AppendJournal(string stateRoot, JsonObject tx)
    {
        Directory.CreateDirectory(stateRoot);
        File.AppendAllText(
            JournalPath(stateRoot), tx.ToJsonString() + "\n");
    }

    private static List<JsonObject> ReadJournal(
        string stateRoot, string target)
    {
        var path = JournalPath(stateRoot);
        var entries = new List<JsonObject>();
        if (!File.Exists(path))
            return entries;
        foreach (var line in File.ReadLines(path))
        {
            if (line.Trim().Length == 0)
                continue;
            try
            {
                if (JsonNode.Parse(line) is JsonObject tx
                    && NormalizePath(
                        tx["target_dir"]?.GetValue<string>() ?? "")
                        == NormalizePath(target))
                    entries.Add(tx);
            }
            catch (JsonException)
            {
                // Corrupt journal line: skip, never trust it.
            }
        }
        return entries;
    }

    private static string History(string stateRoot, string target)
    {
        var entries = ReadJournal(stateRoot, target)
            .OrderByDescending(
                t => t["applied_utc"]?.GetValue<string>() ?? "")
            .Take(50)
            .Select(t => (JsonNode)t.DeepClone())
            .ToArray();
        return new JsonObject
        {
            ["ok"] = true,
            ["target_dir"] = target,
            ["history"] = new JsonArray(entries),
        }.ToJsonString();
    }

    private static string UndoLast(
        string stateRoot, string target, CancellationToken ct)
    {
        var entries = ReadJournal(stateRoot, target);
        var tx = entries.LastOrDefault(
            e => e["status"]?.GetValue<string>() == "applied")
            ?? throw new FileSorterException(
                "UNDO_EMPTY", "沒有可復原的已完成整理");
        var restored = 0;
        foreach (var moveNode in
            (tx["moves"] as JsonArray ?? new JsonArray()).Reverse())
        {
            ct.ThrowIfCancellationRequested();
            var move = (JsonObject)moveNode!;
            var source = move["source"]!.GetValue<string>();
            var dest = move["dest"]!.GetValue<string>();
            var expected = move["sha256"]!.GetValue<string>();
            // Contract: stop on the first occupied/changed path rather
            // than partially reverting a transaction.
            if (!File.Exists(dest)
                || Sha256Hex(dest) != expected)
                throw new FileSorterException(
                    "UNDO_DEST_CHANGED",
                    $"復原停止：{Path.GetFileName(dest)} 已變更或遺失");
            if (File.Exists(source))
                throw new FileSorterException(
                    "UNDO_SOURCE_OCCUPIED",
                    $"復原停止：原路徑已被占用 {Path.GetFileName(source)}");
            File.Move(dest, source);
            restored++;
        }
        tx["status"] = "undone";
        tx["undone_utc"] = DateTime.UtcNow.ToString("O");
        RewriteJournal(stateRoot, target, entries);
        return new JsonObject
        {
            ["ok"] = true,
            ["tx_id"] = tx["tx_id"]?.GetValue<string>(),
            ["restored_count"] = restored,
        }.ToJsonString();
    }

    private static void RewriteJournal(
        string stateRoot, string target, List<JsonObject> targetEntries)
    {
        // Preserve entries belonging to other targets verbatim.
        var path = JournalPath(stateRoot);
        var others = new List<string>();
        if (File.Exists(path))
        {
            foreach (var line in File.ReadLines(path))
            {
                if (line.Trim().Length == 0)
                    continue;
                try
                {
                    if (JsonNode.Parse(line) is JsonObject tx
                        && NormalizePath(
                            tx["target_dir"]?.GetValue<string>() ?? "")
                            != NormalizePath(target))
                        others.Add(line);
                }
                catch (JsonException)
                {
                    others.Add(line);
                }
            }
        }
        var lines = others
            .Concat(targetEntries.Select(t => t.ToJsonString()));
        AtomicWrite(path, string.Join("\n", lines) + "\n");
    }
}
