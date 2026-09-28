using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

/// <summary>
/// Audit evidence writers — parity with git_tiers.audit_log (flat
/// git_tier_audit.jsonl) and audit_chain.chained_audit_log (hash-chained
/// git_audit_chain/current.jsonl).
/// </summary>
internal static class Ledger
{
    public const string GenesisHash = "0000000000000000000000000000000000000000000000000000000000000000";
    private const string LedgerRelative =
        "governance_rule/execution/audit/git_tier_audit.jsonl";
    private const string ChainRelative =
        "governance_rule/execution/audit/git_audit_chain";
    private const long RotationBytesDefault = 128L << 20;
    private const double RetentionHoursDefault = 24.0;
    private static readonly object Sync = new();

    private static string LedgerPath(string root) =>
        Path.Combine(root, LedgerRelative.Replace('/', Path.DirectorySeparatorChar));

    private static string ChainDir(string root)
    {
        var overrideDir =
            Environment.GetEnvironmentVariable("GPTBRIDGE_AUDIT_CHAIN_DIR");
        if (!string.IsNullOrWhiteSpace(overrideDir))
            return overrideDir.Trim();
        return Path.Combine(root,
            ChainRelative.Replace('/', Path.DirectorySeparatorChar));
    }

    /// <summary>Light repo snapshot fields embedded in each flat record.</summary>
    private static JsonObject LightSnapshot(string? worktree)
    {
        var snapshot = new JsonObject
        {
            ["head_revision"] = "",
            ["branch"] = "HEAD",
            ["dirty_files"] = new JsonArray(),
            ["staged_files"] = new JsonArray(),
            ["untracked_files"] = new JsonArray(),
        };
        if (string.IsNullOrEmpty(worktree))
            return snapshot;
        var head = Git.Run(worktree, new[] { "rev-parse", "HEAD" });
        snapshot["head_revision"] = head.Code == 0 ? head.Stdout.Trim() : "";
        var branch = Git.Run(worktree, new[] { "rev-parse", "--abbrev-ref", "HEAD" });
        snapshot["branch"] = branch.Code == 0 && branch.Stdout.Trim().Length > 0
            ? branch.Stdout.Trim() : "HEAD";
        snapshot["staged_files"] = NameList(
            Git.Run(worktree, new[] { "diff", "--cached", "--name-only" }));
        snapshot["dirty_files"] = NameList(
            Git.Run(worktree, new[] { "diff", "--name-only" }));
        snapshot["untracked_files"] = NameList(
            Git.Run(worktree, new[] { "ls-files", "--others", "--exclude-standard" }));
        return snapshot;
    }

    private static JsonArray NameList(GitResult result)
    {
        var array = new JsonArray();
        if (result.Code != 0)
            return array;
        foreach (var line in result.Stdout.Split('\n'))
        {
            var trimmed = line.Trim();
            if (trimmed.Length > 0)
                array.Add(trimmed);
        }
        return array;
    }

    /// <summary>Flat audit record (parity with git_tiers.audit_log).</summary>
    public static JsonObject Audit(
        string projectRoot, int tier, string command, string actor,
        bool approved, string detail = "",
        string operation = "", string? worktree = null,
        string phase = "decision", string result = "pending",
        int? returncode = null)
    {
        var snapshot = LightSnapshot(worktree ?? projectRoot);
        if (string.IsNullOrEmpty(operation))
            operation = command.Trim().Split(' ')[0];
        var entry = new JsonObject
        {
            ["timestamp"] = Canon.LocalStamp(),
            ["tier"] = tier,
            ["operation"] = operation,
            ["command"] = command,
            ["actor"] = actor,
            ["approved"] = approved,
            ["phase"] = phase,
            ["result"] = result,
            ["returncode"] = returncode.HasValue
                ? JsonValue.Create(returncode.Value) : null,
            ["detail"] = detail,
            ["head_revision"] = snapshot["head_revision"]!.DeepClone(),
            ["branch"] = snapshot["branch"]!.DeepClone(),
            ["dirty_files"] = snapshot["dirty_files"]!.DeepClone(),
            ["staged_files"] = snapshot["staged_files"]!.DeepClone(),
            ["untracked_files"] = snapshot["untracked_files"]!.DeepClone(),
        };
        lock (Sync)
        {
            var path = LedgerPath(projectRoot);
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            using var document = JsonDocument.Parse(entry.ToJsonString());
            File.AppendAllText(
                path, Canon.Compact(document.RootElement) + "\n",
                new UTF8Encoding(false));
        }
        return entry;
    }

    /// <summary>Flat + hash-chain append (chained_audit_log).</summary>
    public static JsonObject ChainedAudit(
        string projectRoot, int tier, string command, string actor,
        bool approved, string detail = "",
        string operation = "", string? worktree = null,
        string phase = "decision", string result = "pending",
        int? returncode = null)
    {
        var entry = Audit(projectRoot, tier, command, actor, approved,
            detail, operation, worktree, phase, result, returncode);
        try { AppendChain(projectRoot, entry); }
        catch (Exception) { /* chain append is best-effort like Python */ }
        return entry;
    }

    // -- hash chain --------------------------------------------------------

    private static JsonObject ReadJsonObject(string path)
    {
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(path));
            return node as JsonObject ?? new JsonObject();
        }
        catch (IOException) { return new JsonObject(); }
        catch (JsonException) { return new JsonObject(); }
    }

    private static long RotationBytes()
    {
        var raw = Environment.GetEnvironmentVariable(
            "GPTBRIDGE_AUDIT_CHAIN_ROTATION_BYTES");
        return long.TryParse(raw, out var value) && value > 0
            ? value : RotationBytesDefault;
    }

    private static double RetentionHours()
    {
        var raw = Environment.GetEnvironmentVariable(
            "GPTBRIDGE_AUDIT_CHAIN_RETENTION_HOURS");
        return double.TryParse(raw, out var value) && value > 0
            ? value : RetentionHoursDefault;
    }

    private static JsonObject LoadState(string directory)
    {
        var state = ReadJsonObject(Path.Combine(directory, "chain_state.json"));
        if (state["epoch"] is null)
            return new JsonObject
            {
                ["epoch"] = 1,
                ["next_sequence"] = 1,
                ["last_hash"] = GenesisHash,
                ["record_count"] = 0,
                ["current_file"] = "current.jsonl",
                ["created_at"] = Canon.UtcNow(),
            };
        return state;
    }

    private static void EnsureEpoch(string directory, JsonObject state)
    {
        var path = Path.Combine(directory, "audit_chain_manifest.json");
        var manifest = ReadJsonObject(path);
        if (manifest["epochs"] is not JsonArray epochs)
        {
            manifest = new JsonObject
            {
                ["schema"] = "audit-chain-manifest-v1",
                ["epochs"] = new JsonArray(),
            };
            epochs = (JsonArray)manifest["epochs"]!;
        }
        var epoch = state["epoch"]!.GetValue<int>();
        if (epochs.Count > 0
            && epochs[^1]?["epoch"]?.GetValue<int>() == epoch)
            return;
        epochs.Add(new JsonObject
        {
            ["epoch"] = epoch,
            ["first_sequence"] = state["next_sequence"]!.GetValue<int>(),
            ["previous_epoch_digest"] =
                epochs.Count > 0
                    ? epochs[^1]?["final_digest"]?.GetValue<string>() ?? GenesisHash
                    : GenesisHash,
            ["created_at"] = Canon.UtcNow(),
            ["file"] = "current.jsonl",
            ["final_digest"] = null,
        });
        Canon.WriteJsonAtomic(path, Indented(manifest));
    }

    private static string Indented(JsonNode node)
    {
        using var document = JsonDocument.Parse(node.ToJsonString());
        return Canon.Indented(document.RootElement) + "\n";
    }

    public static JsonObject AppendChain(string projectRoot, JsonObject record)
    {
        var directory = ChainDir(projectRoot);
        Directory.CreateDirectory(directory);
        MaybeRotate(directory);
        var lockPath = Path.Combine(directory, "audit-append.lock");
        // Advisory byte-range equivalent: exclusive handle while appending —
        // every governed writer in this runtime serializes through it.
        using (new FileStream(lockPath, FileMode.OpenOrCreate,
                   FileAccess.ReadWrite, FileShare.None))
        {
            var state = LoadState(directory);
            EnsureEpoch(directory, state);
            var sequence = state["next_sequence"]!.GetValue<int>();
            var chained = (JsonObject)record.DeepClone();
            chained["sequence"] = sequence;
            chained["epoch"] = state["epoch"]!.GetValue<int>();
            chained["previous_hash"] =
                state["last_hash"]!.GetValue<string>();
            chained["record_hash"] = RecordHash(chained);
            var current = Path.Combine(directory, "current.jsonl");
            using (var stream = new FileStream(
                       current, FileMode.Append, FileAccess.Write,
                       FileShare.None))
            {
                var bytes = Encoding.UTF8.GetBytes(
                    Canon.CompactNode(chained) + "\n");
                stream.Write(bytes);
                stream.Flush(flushToDisk: true);
            }
            state["next_sequence"] = sequence + 1;
            state["last_hash"] = chained["record_hash"]!.GetValue<string>();
            state["record_count"] =
                (state["record_count"]?.GetValue<int>() ?? 0) + 1;
            Canon.WriteJsonAtomic(
                Path.Combine(directory, "chain_state.json"),
                Indented(state));
            return chained;
        }
    }

    private static string RecordHash(JsonObject chained)
    {
        var body = (JsonObject)chained.DeepClone();
        body.Remove("record_hash");
        return Canon.Sha256Hex(Canon.CompactNode(body));
    }

    private static void MaybeRotate(string directory)
    {
        var current = Path.Combine(directory, "current.jsonl");
        try
        {
            if (!File.Exists(current)
                || new FileInfo(current).Length < RotationBytes())
                return;
        }
        catch (IOException) { return; }
        Rotate(directory, RotationBytes());
    }

    public static JsonObject Rotate(
        string directory, long maxBytes, bool force = false)
    {
        var lockPath = Path.Combine(directory, "audit-append.lock");
        using (new FileStream(lockPath, FileMode.OpenOrCreate,
                   FileAccess.ReadWrite, FileShare.None))
        {
            var state = LoadState(directory);
            var current = Path.Combine(directory, "current.jsonl");
            long size;
            try { size = new FileInfo(current).Length; }
            catch (IOException) { size = 0; }
            if (!force && size < maxBytes)
                return new JsonObject
                {
                    ["rotated"] = false,
                    ["reason"] = "below-threshold",
                    ["size"] = size,
                };
            if (!File.Exists(current) || size == 0)
                return new JsonObject
                {
                    ["rotated"] = false, ["reason"] = "empty", ["size"] = size,
                };
            var finalDigest = Canon.Sha256File(current);
            var month = DateTime.UtcNow.ToString("yyyy-MM");
            var archiveDir = Path.Combine(directory, "archive", month);
            Directory.CreateDirectory(archiveDir);
            var manifestPath = Path.Combine(
                directory, "audit_chain_manifest.json");
            var manifest = ReadJsonObject(manifestPath);
            var epochs = manifest["epochs"] as JsonArray ?? new JsonArray();
            var firstSeq = epochs.Count > 0
                ? epochs[^1]?["first_sequence"]?.GetValue<int>() ?? 1 : 1;
            var lastSeq = state["next_sequence"]!.GetValue<int>() - 1;
            var epoch = state["epoch"]!.GetValue<int>();
            var archiveName = $"epoch-{epoch}-seq-{firstSeq}-{lastSeq}.jsonl";
            var archivePath = Path.Combine(archiveDir, archiveName);
            File.Move(current, archivePath, overwrite: true);
            if (epochs.Count > 0)
            {
                var last = (JsonObject)epochs[^1]!;
                last["final_digest"] = finalDigest;
                last["archive_path"] =
                    Path.Combine("archive", month, archiveName)
                        .Replace('\\', '/');
                last["rotated_at"] = Canon.UtcNow();
                last["last_sequence"] = lastSeq;
                last["last_record_hash"] =
                    state["last_hash"]!.GetValue<string>();
            }
            manifest["epochs"] = epochs;
            Canon.WriteJsonAtomic(manifestPath, Indented(manifest));
            state["epoch"] = epoch + 1;
            state["current_file"] = "current.jsonl";
            Canon.WriteJsonAtomic(
                Path.Combine(directory, "chain_state.json"),
                Indented(state));
            EnsureEpoch(directory, state);
            var marker = new JsonObject
            {
                ["event"] = "epoch-start",
                ["timestamp"] = Canon.UtcNow(),
                ["epoch"] = state["epoch"]!.GetValue<int>(),
                ["rotated_digest"] = finalDigest,
                ["sequence"] = state["next_sequence"]!.GetValue<int>(),
                ["previous_hash"] = state["last_hash"]!.GetValue<string>(),
            };
            marker["record_hash"] = RecordHash(marker);
            using (var stream = new FileStream(
                       current, FileMode.Append, FileAccess.Write,
                       FileShare.None))
            {
                var bytes = Encoding.UTF8.GetBytes(
                    Canon.CompactNode(marker) + "\n");
                stream.Write(bytes);
                stream.Flush(flushToDisk: true);
            }
            state["next_sequence"] =
                state["next_sequence"]!.GetValue<int>() + 1;
            state["last_hash"] = marker["record_hash"]!.GetValue<string>();
            state["record_count"] =
                (state["record_count"]?.GetValue<int>() ?? 0) + 1;
            Canon.WriteJsonAtomic(
                Path.Combine(directory, "chain_state.json"),
                Indented(state));
            PruneArchived(directory);
            return new JsonObject
            {
                ["rotated"] = true,
                ["epoch"] = state["epoch"]!.GetValue<int>(),
                ["archive"] = archivePath,
                ["final_digest"] = finalDigest,
            };
        }
    }

    private static void PruneArchived(string directory)
    {
        var cutoff = DateTime.UtcNow.AddHours(-RetentionHours());
        var archiveRoot = Path.Combine(directory, "archive");
        if (!Directory.Exists(archiveRoot))
            return;
        foreach (var file in Directory.EnumerateFiles(
                     archiveRoot, "*.jsonl", SearchOption.AllDirectories))
        {
            try
            {
                if (File.GetLastWriteTimeUtc(file) < cutoff)
                    File.Delete(file);
            }
            catch (IOException) { }
        }
    }
}
