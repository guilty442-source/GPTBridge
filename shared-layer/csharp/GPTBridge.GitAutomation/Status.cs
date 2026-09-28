using System.Text;

namespace GPTBridge.GitAutomation;

/// <summary>
/// Porcelain v2 parser + generation fingerprint + affected-scope —
/// parity with git_tiers.porcelain and generation_snapshot.
/// </summary>
internal sealed record StatusEntry(string Kind, string Xy, string Path,
                                   string OrigPath = "")
{
    public bool Staged =>
        Xy.Length > 0 && Xy[0] is not ('.' or '?' or '!') && Kind != "?";
    public bool Unstaged =>
        (Xy.Length > 1 && Xy[1] is not ('.' or '?' or '!')) || Kind == "u";
}

internal sealed class WorktreeStatus
{
    public string Branch = "";
    public string HeadOid = "";
    public string Upstream = "";
    public int Ahead;
    public int Behind;
    public bool Detached;
    public List<StatusEntry> Entries = new();

    public bool Clean => Entries.Count == 0;

    public Dictionary<string, string> LegacyMap()
    {
        var map = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var entry in Entries)
            map[entry.Path] = entry.Xy;
        return map;
    }

    public List<string> ChangedPaths()
    {
        var paths = new List<string>();
        foreach (var entry in Entries)
        {
            paths.Add(entry.Path);
            if (entry.OrigPath.Length > 0)
                paths.Add(entry.OrigPath);
        }
        return paths;
    }

    /// <summary>generation_snapshot fingerprint parity.</summary>
    public string Fingerprint(string numstat)
    {
        var payload = string.Join(
            "\n", Entries.Select(e => $"{e.Xy} {e.Path}")) + numstat;
        return Canon.Sha256Hex(payload);
    }

    /// <summary>scope_of parity (§3.5).</summary>
    public static string ScopeOf(string path)
    {
        var isDir = path.EndsWith('/');
        var parts = path.Replace('\\', '/')
            .Split('/', StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length == 0)
            return "(root)";
        if (parts.Length > 1
            && parts[0] is "Standalone tools" or ".worktrees")
            return $"{parts[0]}/{parts[1]}";
        if (isDir || parts.Length > 1)
            return parts[0];
        return "(root)";
    }

    public List<string> AffectedScopes() =>
        ChangedPaths().Select(ScopeOf).Distinct(StringComparer.Ordinal)
            .Order(StringComparer.Ordinal).ToList();
}

internal static class Status
{
    public static WorktreeStatus Parse(string text)
    {
        var status = new WorktreeStatus();
        var records = text.Split('\0');
        var index = 0;
        while (index < records.Length)
        {
            var record = records[index++];
            if (record.Length == 0)
                continue;
            if (record.StartsWith("# ", StringComparison.Ordinal))
            {
                var header = record[2..];
                if (header.StartsWith("branch.oid "))
                    status.HeadOid = header["branch.oid ".Length..].Trim();
                else if (header.StartsWith("branch.head "))
                {
                    status.Branch = header["branch.head ".Length..].Trim();
                    status.Detached = status.Branch == "(detached)";
                }
                else if (header.StartsWith("branch.upstream "))
                    status.Upstream =
                        header["branch.upstream ".Length..].Trim();
                else if (header.StartsWith("branch.ab "))
                {
                    foreach (var part in
                             header["branch.ab ".Length..].Split(' '))
                    {
                        if (part.StartsWith('+')
                            && int.TryParse(part[1..], out var ahead))
                            status.Ahead = ahead;
                        else if (part.StartsWith('-')
                                 && int.TryParse(part[1..], out var behind))
                            status.Behind = behind;
                    }
                }
                continue;
            }
            var tag = record[0];
            switch (tag)
            {
                case '1':
                {
                    var fields = record.Split(' ', 9);
                    if (fields.Length == 9)
                        status.Entries.Add(
                            new StatusEntry("1", fields[1], fields[8]));
                    break;
                }
                case '2':
                {
                    var fields = record.Split(' ', 10);
                    if (fields.Length == 10 && index < records.Length)
                        status.Entries.Add(new StatusEntry(
                            "2", fields[1], fields[9], records[index++]));
                    break;
                }
                case 'u':
                {
                    var fields = record.Split(' ', 11);
                    if (fields.Length == 11)
                        status.Entries.Add(
                            new StatusEntry("u", fields[1], fields[10]));
                    break;
                }
                case '?':
                    status.Entries.Add(
                        new StatusEntry("?", "??", record[2..]));
                    break;
                case '!':
                    status.Entries.Add(
                        new StatusEntry("!", "!!", record[2..]));
                    break;
            }
        }
        return status;
    }

    public static WorktreeStatus Capture(string worktree, bool includeBranch = true)
    {
        var args = new List<string> { "status", "--porcelain=v2", "-z" };
        if (includeBranch)
            args.Add("--branch");
        var result = Git.Run(worktree, args);
        return result.Code == 0 ? Parse(result.Stdout) : new WorktreeStatus();
    }

    /// <summary>Snapshot = status v2 + numstat, one capture per sweep tick.</summary>
    public sealed record Snapshot(
        WorktreeStatus Status, string Numstat, string Fingerprint)
    {
        public bool Dirty => !Status.Clean;
        public Dictionary<string, string> Entries => Status.LegacyMap();
        public List<string> AffectedScopes => Status.AffectedScopes();
    }

    public static Snapshot CaptureSnapshot(string worktree)
    {
        var status = Capture(worktree);
        var numstat = Git.Run(worktree, new[] { "diff", "--numstat", "HEAD" });
        var numstatText = numstat.Code == 0 ? numstat.Stdout : "";
        return new Snapshot(status, numstatText,
                            status.Fingerprint(numstatText));
    }
}
