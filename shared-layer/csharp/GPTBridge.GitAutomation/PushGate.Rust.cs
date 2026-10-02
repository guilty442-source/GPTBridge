using System.IO;

namespace GPTBridge.GitAutomation;

/// <summary>
/// Rust tokenizer-backend lane for the push gate — parity with the
/// MSVC suite auto-build. The baseline/dialogue/eval suites resolve
/// the xtok/v1 C ABI from ``xcorpus.dll`` next to the consuming exe
/// (``xtok_abi.h``), and that DLL is a cargo artifact of the
/// ``xingcheng/src/backend/rust/xcorpus`` crate (cdylib), never
/// checked in. When the DLL beside ``native/test_suites/bin`` is
/// missing or older than the crate sources, run one bounded
/// ``cargo build --release`` and stage the DLL next to the suites.
/// Fail-closed detail strings; never throws.
/// </summary>
internal static partial class PushGate
{
    private const string XcorpusCrateRelative =
        "xingcheng/src/backend/rust/xcorpus";
    private const string XcorpusDllName = "xcorpus.dll";

    /// <returns>null when the backend beside <paramref name="binDir"/>
    /// is ready; otherwise a fail-closed detail
    /// (``cargo-unavailable:…`` / ``rust-build-timeout`` /
    /// ``rust-build-failed:rc=…`` / ``xcorpus-stage-failed:…`` /
    /// ``xcorpus-dll-missing-after-build``).</returns>
    internal static string? EnsureXcorpusBackend(
        string root, string binDir, double timeoutS)
    {
        var dllPath = Path.Combine(binDir, XcorpusDllName);
        var crateDir = Rel(root, XcorpusCrateRelative);
        if (IsFresh(crateDir, dllPath))
            return null;
        try
        {
            var build = Git.Exec("cargo", crateDir,
                new[] { "build", "--release" },
                (int)(timeoutS * 1000));
            if (build.TimedOut)
                return "rust-build-timeout";
            if (build.Code != 0)
                return $"rust-build-failed:rc={build.Code}";
        }
        catch (Exception error) when (error is IOException
            or System.ComponentModel.Win32Exception
            or UnauthorizedAccessException)
        {
            return $"cargo-unavailable:{error.GetType().Name}";
        }
        var staged = Path.Combine(crateDir, "target", "release",
            XcorpusDllName);
        try
        {
            if (!File.Exists(staged))
                return "xcorpus-dll-missing-after-build";
            Directory.CreateDirectory(binDir);
            File.Copy(staged, dllPath, overwrite: true);
        }
        catch (Exception error) when (error is IOException
            or UnauthorizedAccessException)
        {
            return $"xcorpus-stage-failed:{error.GetType().Name}";
        }
        return IsFresh(crateDir, dllPath)
            ? null
            : "xcorpus-dll-stale-after-build";
    }

    private static bool IsFresh(string crateDir, string dllPath)
    {
        DateTime dllTime;
        try
        {
            if (!File.Exists(dllPath))
                return false;
            dllTime = File.GetLastWriteTimeUtc(dllPath);
        }
        catch (IOException) { return false; }
        catch (UnauthorizedAccessException) { return false; }
        return NewestCrateSourceTime(crateDir) <= dllTime;
    }

    private static DateTime NewestCrateSourceTime(string crateDir)
    {
        var newest = DateTime.MinValue;
        var srcDir = Path.Combine(crateDir, "src");
        if (Directory.Exists(srcDir))
        {
            foreach (var file in Directory.EnumerateFiles(
                         srcDir, "*.rs", SearchOption.AllDirectories))
            {
                try
                {
                    var time = File.GetLastWriteTimeUtc(file);
                    if (time > newest)
                        newest = time;
                }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
        foreach (var name in new[] { "Cargo.toml", "Cargo.lock" })
        {
            var path = Path.Combine(crateDir, name);
            try
            {
                if (File.Exists(path))
                {
                    var time = File.GetLastWriteTimeUtc(path);
                    if (time > newest)
                        newest = time;
                }
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
        return newest;
    }
}
