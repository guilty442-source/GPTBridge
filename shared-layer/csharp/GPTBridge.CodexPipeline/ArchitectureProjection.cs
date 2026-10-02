using System.Text;
using System.Text.RegularExpressions;

namespace GPTBridge.CodexPipeline;

/// <summary>
/// Derived-projection refresh for the ``governance_rule/codex/
/// architecture-*.md`` normative view documents.  Hand narrative stays;
/// only machine-derivable marker blocks are rewritten:
///
///   &lt;!-- autogen:&lt;name&gt; --&gt;   …generated body…   &lt;!-- /autogen:&lt;name&gt; --&gt;
///
/// Facts are scanned from the implementation tree across every
/// registered worktree (main + ``.worktrees/*``) so lanes that live on a
/// worker branch still project truthfully.  Rewrites are byte-compared —
/// no diff, no write (plain tmp+move; these are normal tracked sources,
/// never sealed read-only like the zh-TW mirrors).  Missing markers are
/// reported, never invented.  Run inside ``CodexAutomation.Maintain``.
/// </summary>
internal static class ArchitectureProjection
{
    private const string Scanner = "autogen-scanner/v1";
    private const string Open = "<!-- autogen:";
    private const string Close = "<!-- /autogen:";

    private static readonly string[] SourceDirs =
    {
        "Standalone tools/local-model/src",
        "Standalone tools/local-model/contracts",
        "Standalone tools/local-model/runtime/settings",
        "native",
        "main-system/config",
    };

    private static readonly string[] SourceExts =
    {
        ".h", ".hpp", ".cpp", ".cc", ".rs", ".cs",
        ".toml", ".csproj", ".json", ".bat",
    };

    private static readonly string[] SkipDirs =
    {
        "target", "bin", "obj", "node_modules", ".git",
        "dist", "dist-native", "runtime",
    };

    // ---------------------------------------------------------- regexes --

    private static readonly Regex RxMagicQuoted = new(
        "[\"'](XC[A-Z0-9]{2}|XSST|XEB[0-9]|XPA[0-9]|XCB[0-9])[\"']",
        RegexOptions.Compiled);
    private static readonly Regex RxMagicChars = new(
        "\\{\\s*'X'\\s*,\\s*'C'\\s*,\\s*'([A-Z0-9])'\\s*,\\s*'([A-Z0-9])'",
        RegexOptions.Compiled);
    private static readonly Regex RxFormat = new(
        "\"format\"\\s*:\\s*\"([^\"]{3,80})\"", RegexOptions.Compiled);
    private static readonly Regex RxFormatEscaped = new(
        "format\\\\\":\\\\\"([^\\\\\"]{3,80})", RegexOptions.Compiled);
    private static readonly Regex RxEnv = new(
        "(?:std::getenv|env::var|GetEnvironmentVariable)\\s*\\(?\\s*\"" +
        "((?:XCT|XINGCHENG|GPTBRIDGE|XCB)_[A-Z0-9_]+)\"",
        RegexOptions.Compiled);
    private static readonly Regex RxPgTable = new(
        "\\b(transformer_[a-z_]+)\\b", RegexOptions.Compiled);
    private static readonly Regex RxVerbFlags = new(
        "flags\\.Contains\\(\"([a-z0-9-]+)\"\\)", RegexOptions.Compiled);
    private static readonly Regex RxVerbOpts = new(
        "(?:opts|a)\\.TryGetValue\\(\"([a-z0-9-]+)\"", RegexOptions.Compiled);
    private static readonly Regex RxModeRow = new(
        "\\{\\s*\"([a-z0-9-]+)\"\\s*,\\s*\"([A-Z]+)\"\\s*,",
        RegexOptions.Compiled);
    private static readonly Regex RxKernelName = new(
        "name:\\s*\"([a-z0-9-]+)\"", RegexOptions.Compiled);
    private static readonly Regex RxRustKernelRow = new(
        "\\{\\s*\"([a-z0-9-]+)\",", RegexOptions.Compiled);
    private static readonly Regex RxTomlName = new(
        "(?m)^\\s*name\\s*=\\s*\"([^\"]+)\"", RegexOptions.Compiled);
    private static readonly Regex RxCsprojName = new(
        "<AssemblyName>([^<]+)</AssemblyName>", RegexOptions.Compiled);
    private static readonly Regex RxFeTarget = new(
        "/Fe\"?([^\\s\"]+\\.exe)", RegexOptions.Compiled);
    private static readonly Regex RxCrateType = new(
        "crate-type\\s*=\\s*\\[([^\\]]+)\\]", RegexOptions.Compiled);
    private static readonly Regex RxSettingFormat = new(
        "\"format\"\\s*:\\s*\"([^\"]+)\"", RegexOptions.Compiled);

    // ------------------------------------------------------------- scan --

    private sealed class Facts
    {
        // token -> set of "<tree>:<relpath>"
        public readonly SortedDictionary<string, SortedSet<string>>
            Magic = new(StringComparer.Ordinal);
        public readonly SortedDictionary<string, SortedSet<string>>
            Formats = new(StringComparer.Ordinal);
        public readonly SortedDictionary<string, SortedSet<string>>
            Env = new(StringComparer.Ordinal);
        public readonly SortedDictionary<string, SortedSet<string>>
            PgTables = new(StringComparer.Ordinal);
        public readonly List<(string Name, string Category, string Src)>
            Modes = new();
        public readonly SortedSet<string> Verbs =
            new(StringComparer.Ordinal);
        public readonly List<(string Lane, int Count, string Src)>
            Kernels = new();
        public readonly List<(string Binary, string Lane, string Src)>
            Binaries = new();
        public readonly List<(string File, string Format, string Src)>
            Settings = new();
        public int FilesScanned;
        public readonly List<string> Trees = new();
    }

    private static void Add(
        SortedDictionary<string, SortedSet<string>> map,
        string key, string src)
    {
        if (!map.TryGetValue(key, out var set))
            map[key] = set = new SortedSet<string>(StringComparer.Ordinal);
        set.Add(src);
    }

    private static bool Skipped(string rel)
    {
        var parts = rel.Replace('\\', '/').Split('/');
        foreach (var p in parts)
            foreach (var s in SkipDirs)
                if (p == s) return true;
        return false;
    }

    private static List<string> WorktreeRoots(string root)
    {
        var roots = new List<string> { root };
        var wt = Path.Combine(root, ".worktrees");
        if (Directory.Exists(wt))
            foreach (var dir in Directory.EnumerateDirectories(wt)
                         .OrderBy(d => d, StringComparer.Ordinal))
                if (Directory.Exists(Path.Combine(
                        dir, "Standalone tools")))
                    roots.Add(dir);
        return roots;
    }

    private static string LaneOf(string rel)
    {
        if (rel.Contains("/rust/")) return "Rust";
        if (rel.Contains("/csharp/") || rel.EndsWith(".csproj",
                StringComparison.Ordinal)) return "C#";
        if (rel.EndsWith(".rs", StringComparison.Ordinal)) return "Rust";
        if (rel.EndsWith(".cs", StringComparison.Ordinal)) return "C#";
        if (rel.Contains("/cpp/") || rel.Contains("native_transformer")
                || rel.StartsWith("native/", StringComparison.Ordinal))
            return "C++";
        return "—";
    }

    private static void ScanFile(Facts f, string tree, string rel,
                                 string full)
    {
        string text;
        try { text = File.ReadAllText(full); }
        catch { return; }
        var src = $"{tree}:{rel.Replace('\\', '/')}";
        f.FilesScanned++;

        foreach (Match m in RxMagicQuoted.Matches(text))
            Add(f.Magic, m.Groups[1].Value, src);
        foreach (Match m in RxMagicChars.Matches(text))
            Add(f.Magic, "XC" + m.Groups[1].Value + m.Groups[2].Value, src);
        foreach (Match m in RxFormat.Matches(text))
            Add(f.Formats, m.Groups[1].Value, src);
        foreach (Match m in RxFormatEscaped.Matches(text))
            Add(f.Formats, m.Groups[1].Value, src);
        foreach (Match m in RxEnv.Matches(text))
            Add(f.Env, m.Groups[1].Value, src);
        foreach (Match m in RxPgTable.Matches(text))
            Add(f.PgTables, m.Groups[1].Value, src);

        var name = Path.GetFileName(rel);
        if (name == "xc_modeltool.cpp" || text.Contains("kModeRegistry"))
            foreach (Match m in RxModeRow.Matches(text))
                f.Modes.Add((m.Groups[1].Value, m.Groups[2].Value, src));
        if (name == "Program.cs")
        {
            foreach (Match m in RxVerbFlags.Matches(text))
                f.Verbs.Add(m.Groups[1].Value);
            foreach (Match m in RxVerbOpts.Matches(text))
                f.Verbs.Add(m.Groups[1].Value);
        }
        if (name == "kernels.rs" && text.Contains("KERNELS"))
        {
            var lane = rel.Contains("xstore") ? "xstore"
                     : rel.Contains("xcorpus") ? "xcorpus" : "rust";
            var n = RxKernelName.Matches(text).Count;
            if (n == 0) n = RxRustKernelRow.Matches(text).Count;
            f.Kernels.Add((lane, n, src));
        }
        if (name == "xct_kernels.h" && text.Contains("kKernelRegistry"))
        {
            var start = text.IndexOf("kKernelRegistry[]",
                StringComparison.Ordinal);
            if (start < 0)
                start = text.IndexOf("kKernelRegistry",
                    StringComparison.Ordinal);
            var end = text.IndexOf("};", start, StringComparison.Ordinal);
            if (start >= 0 && end > start)
            {
                var body = text[start..end];
                var n = 0;
                foreach (Match m in
                         Regex.Matches(body, "\\{\\s*\""))
                    n++;
                f.Kernels.Add(("trainer", n, src));
            }
        }
        if (name.EndsWith(".csproj", StringComparison.Ordinal))
        {
            var an = RxCsprojName.Match(text);
            f.Binaries.Add((
                (an.Success ? an.Groups[1].Value
                            : Path.GetFileNameWithoutExtension(name))
                + ".exe",
                "C#", src));
        }
        if (name == "Cargo.toml")
        {
            var pkg = RxTomlName.Match(text);
            var stem = pkg.Success ? pkg.Groups[1].Value : "?";
            var ct = RxCrateType.Match(text);
            var dll = ct.Success && ct.Groups[1].Value.Contains("cdylib");
            f.Binaries.Add((stem + ".exe", "Rust", src));
            if (dll) f.Binaries.Add((stem + ".dll", "Rust", src));
        }
        if (name.EndsWith(".bat", StringComparison.Ordinal))
            foreach (Match m in RxFeTarget.Matches(text))
            {
                var bin = Path.GetFileName(
                    m.Groups[1].Value.Replace("%~dp0", ""));
                if (bin.Contains(':') || bin.Contains('%')
                    || !bin.EndsWith(".exe", StringComparison.Ordinal))
                    continue;
                f.Binaries.Add((bin, LaneOf(rel), src));
            }
        if (rel.Replace('\\', '/')
                .Contains("runtime/settings/")
            && name.EndsWith(".json", StringComparison.Ordinal))
        {
            var fm = RxSettingFormat.Match(text);
            f.Settings.Add((name,
                            fm.Success ? fm.Groups[1].Value : "—", src));
        }
    }

    // --------------------------------------------------------- renders --

    // Deterministic stamp: no wall-clock — identical inputs must yield
    // identical bytes or every Maintain pass would churn the docs.
    private static string Stamp(Facts f) =>
        $"*{Scanner} · {f.FilesScanned} files"
        + $" · {string.Join("+", f.Trees)}*\n";

    private static string SrcList(SortedSet<string> set, int max = 4)
    {
        var list = set.Take(max).ToList();
        var s = string.Join(", ", list.Select(x =>
            x.Split(':', 2)[0] == "main"
                ? x.Split(':', 2)[1] : x));
        return set.Count > max ? s + $" (+{set.Count - max})" : s;
    }

    private static readonly Dictionary<string,
        Func<Facts, string>> Blocks = new(StringComparer.Ordinal)
    {
        ["xingcheng-containers"] = f =>
        {
            var sb = new StringBuilder(Stamp(f));
            sb.AppendLine("| Magic | 定義/引用來源 |");
            sb.AppendLine("|---|---|");
            foreach (var kv in f.Magic)
                sb.AppendLine(
                    $"| `{kv.Key}` | {SrcList(kv.Value)} |");
            return sb.ToString();
        },
        ["xingcheng-formats"] = f =>
        {
            var sb = new StringBuilder(Stamp(f));
            sb.AppendLine("| format tag | 來源檔 |");
            sb.AppendLine("|---|---|");
            foreach (var kv in f.Formats)
                sb.AppendLine(
                    $"| `{kv.Key}` | {SrcList(kv.Value)} |");
            return sb.ToString();
        },
        ["xingcheng-pgtables"] = f =>
        {
            var sb = new StringBuilder(Stamp(f));
            sb.AppendLine("| 表 | 定義/引用來源 |");
            sb.AppendLine("|---|---|");
            foreach (var kv in f.PgTables)
                sb.AppendLine(
                    $"| `{kv.Key}` | {SrcList(kv.Value)} |");
            return sb.ToString();
        },
        ["xingcheng-env"] = f =>
        {
            var sb = new StringBuilder(Stamp(f));
            sb.AppendLine("| var | 讀取處 |");
            sb.AppendLine("|---|---|");
            foreach (var kv in f.Env.Where(kv => kv.Value.Any(
                         s => s.Contains("local-model"))))
                sb.AppendLine(
                    $"| `{kv.Key}` | {SrcList(kv.Value)} |");
            return sb.ToString();
        },
        ["xingcheng-settings"] = f =>
        {
            var sb = new StringBuilder(Stamp(f));
            sb.AppendLine("| 檔案 | format | 所在樹 |");
            sb.AppendLine("|---|---|---|");
            foreach (var s in f.Settings
                         .OrderBy(x => x.File, StringComparer.Ordinal))
                sb.AppendLine(
                    $"| `{s.File}` | `{s.Format}` | {s.Src} |");
            return sb.ToString();
        },
        ["xingcheng-binaries"] = f =>
        {
            var sb = new StringBuilder(Stamp(f));
            sb.AppendLine("| Binary | 語言 | 來源 |");
            sb.AppendLine("|---|---|---|");
            foreach (var b in f.Binaries
                         .Where(x => x.Src.Contains(
                             "Standalone tools/local-model"))
                         .OrderBy(x => x.Binary, StringComparer.Ordinal)
                         .ThenBy(x => x.Src, StringComparer.Ordinal)
                         .Distinct())
                sb.AppendLine(
                    $"| `{b.Binary}` | {b.Lane} | {b.Src} |");
            return sb.ToString();
        },
        ["xingcheng-modes"] = f =>
        {
            var sb = new StringBuilder(Stamp(f));
            sb.AppendLine("| 類別 | 模式 |");
            sb.AppendLine("|---|---|");
            var seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (var g in f.Modes
                         .Where(m => seen.Add(m.Name))
                         .GroupBy(m => m.Category)
                         .OrderBy(g => g.Key, StringComparer.Ordinal))
                sb.AppendLine(
                    $"| {g.Key} | "
                    + string.Join(", ", g.Select(m => $"`{m.Name}`"))
                    + " |");
            sb.AppendLine(
                $"\n（count={seen.Count}）");
            return sb.ToString();
        },
        ["xingcheng-verbs"] = f =>
        {
            var sb = new StringBuilder(Stamp(f));
            sb.AppendLine("| verbs |");
            sb.AppendLine("|---|");
            sb.AppendLine("| " + string.Join(" ", f.Verbs
                .OrderBy(v => v, StringComparer.Ordinal)
                .Select(v => $"`--{v}`")) + " |");
            sb.AppendLine($"\n（count={f.Verbs.Count}）");
            return sb.ToString();
        },
        ["xingcheng-kernels"] = f =>
        {
            var sb = new StringBuilder(Stamp(f));
            sb.AppendLine("| lane | kernels | 來源 |");
            sb.AppendLine("|---|---|---|");
            foreach (var k in f.Kernels
                         .OrderBy(x => x.Lane, StringComparer.Ordinal))
                sb.AppendLine(
                    $"| {k.Lane} | {k.Count} | {k.Src} |");
            return sb.ToString();
        },
    };

    // --------------------------------------------------------- refresh --

    /// <summary>Rewrite every autogen block found in the
    /// architecture-*.md projections.  Never mutates hand sections;
    /// docs without markers are skipped; an unclosed marker fails
    /// closed (block skipped, reported in markers_missing, ok=false).
    /// Output is deterministic — same source tree, same bytes.</summary>
    public static Dictionary<string, object?> Refresh(string root)
    {
        var facts = new Facts();
        var changed = new List<string>();
        var errors = new List<string>();
        var updatedBlocks = new List<string>();
        var missing = new List<string>();
        try
        {
            var trees = WorktreeRoots(root);
            foreach (var tree in trees)
            {
                var label = tree == root
                    ? "main"
                    : Path.GetFileName(tree);
                var any = false;
                foreach (var dir in SourceDirs)
                {
                    var abs = Path.Combine(tree, dir);
                    if (!Directory.Exists(abs)) continue;
                    var isSettings = dir.EndsWith("settings",
                        StringComparison.Ordinal);
                    foreach (var file in Directory.EnumerateFiles(
                                 abs, "*",
                                 isSettings
                                     ? SearchOption.TopDirectoryOnly
                                     : SearchOption.AllDirectories))
                    {
                        var relToRoot = Path.GetRelativePath(tree, file);
                        if (!isSettings && Skipped(relToRoot))
                            continue;
                        var ext = Path.GetExtension(file)
                            .ToLowerInvariant();
                        if (!SourceExts.Contains(ext)) continue;
                        any = true;
                        ScanFile(facts, label, relToRoot, file);
                    }
                }
                if (any) facts.Trees.Add(label);
            }

            var codexDir = Path.Combine(root, "governance_rule",
                "codex");
            foreach (var doc in Directory.Exists(codexDir)
                         ? Directory.EnumerateFiles(codexDir,
                             "architecture-*.md")
                         : Enumerable.Empty<string>())
            {
                var text = File.ReadAllText(doc);
                var docName = Path.GetFileName(doc);
                var outText = text;
                var touched = false;
                foreach (var block in Blocks)
                {
                    var open = $"{Open}{block.Key} -->";
                    var close = $"{Close}{block.Key} -->";
                    var i = outText.IndexOf(open,
                        StringComparison.Ordinal);
                    if (i < 0) continue;
                    i += open.Length;
                    var j = outText.IndexOf(close, i,
                        StringComparison.Ordinal);
                    if (j < 0)
                    {
                        // Unclosed marker is malformed input — fail
                        // closed, leave the doc untouched for this block.
                        missing.Add($"{docName}:{block.Key}");
                        continue;
                    }
                    var body = "\n" + block.Value(facts);
                    outText = outText[..i] + body + outText[j..];
                    touched = true;
                    updatedBlocks.Add($"{docName}:{block.Key}");
                }
                if (touched && outText != text)
                {
                    var tmp = doc + ".autogen-tmp";
                    File.WriteAllText(tmp, outText);
                    File.Move(tmp, doc, overwrite: true);
                    changed.Add(docName);
                }
            }
        }
        catch (Exception error)
        {
            errors.Add($"{error.GetType().Name}: {error.Message}");
        }
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["ok"] = errors.Count == 0 && missing.Count == 0,
            ["scanner"] = Scanner,
            ["files_scanned"] = facts.FilesScanned,
            ["trees"] = facts.Trees.Cast<object?>().ToList(),
            ["blocks_updated"] = updatedBlocks.Cast<object?>().ToList(),
            ["markers_missing"] = missing.Cast<object?>().ToList(),
            ["changed"] = changed.Cast<object?>().ToList(),
            ["errors"] = errors.Cast<object?>().ToList(),
        };
    }
}
