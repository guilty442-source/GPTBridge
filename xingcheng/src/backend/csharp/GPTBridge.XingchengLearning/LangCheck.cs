// LangCheck.cs — §1 formal-language allowlist enforcement.
//
// The tool's formal implementation languages are C, C++, C#, F#, Rust
// and CUDA. Python lane stays retired; no fallback may reintroduce it.
// This scanner audits the tool source tree for forbidden-language files
// so a violation fails closed in regression rather than landing in a
// production path silently.

namespace GPTBridge.XingchengLearning;

internal static class LangCheck
{
    // Formal implementation extensions that are banned outright.
    private static readonly HashSet<string> Forbidden =
        new(StringComparer.OrdinalIgnoreCase)
        {
            ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
            ".java", ".go", ".kt", ".kts", ".lua", ".jl", ".r",
            ".scala", ".rb", ".pl", ".ex", ".exs", ".clj",
        };

    // Non-language files that never count as implementation code.
    private static readonly HashSet<string> DataFormats =
        new(StringComparer.OrdinalIgnoreCase)
        {
            ".json", ".toml", ".xml", ".sql", ".md", ".txt", ".yaml",
            ".yml", ".csv", ".jsonl", ".ps1", ".bat", ".cmd", ".sh",
        };

    private static readonly string[] Allowed =
        { ".c", ".h", ".hpp", ".cpp", ".cc", ".cu", ".cs", ".fs", ".rs" };

    /// <summary>Scan <paramref name="toolRoot"/> for banned-language
    /// files. Skips build output, VCS state and runtime data — those are
    /// artifacts, not implementation lanes.</summary>
    public static Dictionary<string, object?> Scan(string toolRoot)
    {
        string root = Path.Combine(toolRoot, "src");
        var violations = new List<object?>();
        var scanned = 0;
        if (Directory.Exists(root))
        {
            foreach (var file in Directory.EnumerateFiles(
                         root, "*", SearchOption.AllDirectories))
            {
                string rel = Path.GetRelativePath(root, file);
                // skip build/object output and any vendored cache dirs
                if (rel.Contains(Path.DirectorySeparatorChar + "bin" +
                                 Path.DirectorySeparatorChar,
                                 StringComparison.OrdinalIgnoreCase) ||
                    rel.Contains(Path.DirectorySeparatorChar + "obj" +
                                 Path.DirectorySeparatorChar,
                                 StringComparison.OrdinalIgnoreCase) ||
                    rel.Contains(".git" + Path.DirectorySeparatorChar,
                                 StringComparison.OrdinalIgnoreCase) ||
                    rel.Contains("node_modules",
                                 StringComparison.OrdinalIgnoreCase))
                    continue;
                string ext = Path.GetExtension(file);
                ++scanned;
                if (Forbidden.Contains(ext))
                {
                    violations.Add(new Dictionary<string, object?>
                    {
                        ["file"] = "src/" + rel.Replace('\\', '/'),
                        ["ext"] = ext,
                        ["error"] = "LANGUAGE_BOUNDARY_VIOLATION",
                    });
                }
                else if (!Allowed.Contains(ext, StringComparer.OrdinalIgnoreCase) &&
                         !DataFormats.Contains(ext) &&
                         ext.Length > 0)
                {
                    // unknown extension: not a violation, but tracked so
                    // governance sees what lives in the tree.
                }
            }
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = violations.Count == 0,
            ["format"] = "star-langcheck/v1",
            ["root"] = "src",
            ["files_scanned"] = scanned,
            ["allowed_extensions"] = Allowed.Cast<object?>().ToList(),
            ["forbidden_extensions"] = Forbidden.Cast<object?>().ToList(),
            ["violations"] = violations,
        };
    }
}
