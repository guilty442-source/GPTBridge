// DataBoundary.cs — 星澄 Data Residency (`xingcheng-internal`) enforcement.
//
// Native successor of the retired
// ``native_transformer/cpp_runtime.py::assert_inside_xingcheng``:
// every xingcheng-owned path (bundle export/staging targets, the
// execution ledger, the pinned serving artifact, retention victims)
// must resolve inside one of the two registered domain roots:
//
//   XINGCHENG_INSTITUTION_ROOT = <toolRoot>/xingcheng/
//   (the retired STAR_DIRECTORY under model-dialogue was removed
//    with the enclave move; the institution root is the single
//    custody root.)
//
// Enforcement is fail-closed: an unresolvable path is treated as
// outside, and Junction/symlink components are resolved before the
// prefix test so a link cannot smuggle an out-of-boundary target.
using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal sealed class DataBoundaryException : Exception
{
    public const string ErrorCode = "XINGCHENG_DATA_BOUNDARY";

    public DataBoundaryException(string path)
        : base($"{ErrorCode}: {path}")
    {
        Path = path;
    }

    public string Path { get; }
}

internal static class DataBoundary
{
    /// <summary>XINGCHENG_INSTITUTION_ROOT —
    /// ``<toolRoot>/xingcheng``.</summary>
    public static string InstitutionRoot(string toolRoot) =>
        Path.GetFullPath(Path.Combine(toolRoot, "xingcheng"));

    /// <summary>All registered domain roots (canonicalized).</summary>
    public static string[] Roots(string toolRoot) => new[]
    {
        InstitutionRoot(toolRoot),
    };

    /// <summary>Canonicalize: GetFullPath plus junction/symlink
    /// resolution on every existing ancestor component, so a link
    /// cannot redirect an in-boundary spelling to an outside target.</summary>
    public static string Resolve(string path)
    {
        var full = Path.GetFullPath(path);
        // Resolve junction/symlink components: find the deepest
        // existing ancestor; when it is a reparse point rebase the
        // remaining suffix on its link target and repeat (bounded).
        for (var guard = 0; guard < 32; guard++)
        {
            DirectoryInfo? existing = new(full.TrimEnd(
                Path.DirectorySeparatorChar));
            while (existing is not null && !existing.Exists)
                existing = existing.Parent;
            if (existing is null) return full;
            var remainder = full.Length > existing.FullName.Length
                ? full[(existing.FullName.TrimEnd(
                      Path.DirectorySeparatorChar).Length)..]
                    .TrimStart(Path.DirectorySeparatorChar,
                        Path.AltDirectorySeparatorChar)
                : "";
            var target = existing.ResolveLinkTarget(false);
            if (target is null) return full;
            full = Path.GetFullPath(
                Path.Combine(target.FullName, remainder));
        }
        return full;
    }

    /// <summary>True when ``path`` resolves inside a registered
    /// xingcheng domain root. Unresolvable paths return false
    /// (fail-closed).</summary>
    public static bool IsInside(string toolRoot, string path)
    {
        string resolved;
        try { resolved = Resolve(path); }
        catch { return false; }
        foreach (var root in Roots(toolRoot))
        {
            if (resolved.Equals(root, StringComparison.OrdinalIgnoreCase)
                || resolved.StartsWith(
                    root + Path.DirectorySeparatorChar,
                    StringComparison.OrdinalIgnoreCase))
                return true;
        }
        return false;
    }

    /// <summary>assert_inside_xingcheng: returns the resolved path or
    /// throws <see cref="DataBoundaryException"/>.</summary>
    public static string AssertInside(string toolRoot, string path)
    {
        if (!IsInside(toolRoot, path))
            throw new DataBoundaryException(path);
        return Resolve(path);
    }

    /// <summary>Read-side audit view: classify a path without
    /// throwing (for diagnostics / retention victims).</summary>
    public static Dictionary<string, object?> Check(
        string toolRoot, string path)
    {
        var inside = IsInside(toolRoot, path);
        string? resolved = null;
        try { resolved = Resolve(path); } catch { }
        return new Dictionary<string, object?>
        {
            ["path"] = path,
            ["resolved"] = resolved,
            ["inside"] = inside,
            ["roots"] = Roots(toolRoot).Cast<object?>().ToList(),
        };
    }
}
