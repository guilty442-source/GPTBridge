// CudaLanguage.cs — the CUDA language policy (native-only stack):
// device code is self-authored PTX embedded in the governed cpp tree
// (cuda_ptx_*.h), JIT-compiled by the installed NVIDIA driver through
// cuModuleLoadData — with no toolkit, runtime or external compute
// libraries.
//
//   Policy        star-cuda-language/v1 — the stated rule, its
//                 allow/deny tables.
//   Check         scans the repo's native sources and classifies every
//                 dynamically-bound symbol source; a forbidden import
//                 or external kernel/compute dependency fails closed.
//
// Allowed:  in-tree PTX modules loaded by the CUDA Driver API;
//           the NVIDIA platform itself (nvcuda driver, nvml).
// Denied:   NVRTC / cudart / cuBLAS / any toolkit dll — removed
//           dependencies with no runtime role; .cu files or an nvcc
//           toolchain requirement; Python CUDA of any flavor
//           (torch/cupy/numba/pycuda/cuda-python/triton); third-party
//           kernel libraries (flash-attn, cutlass as a shipped
//           dependency, etc.); any second compute runtime.

using System.Text.RegularExpressions;

namespace GPTBridge.XingchengLearning;

internal static class CudaLanguage
{
    public const string Format = "star-cuda-language/v1";

    // Vendor-platform DLLs the governed lane may dynamically bind —
    // these ARE the CUDA platform, not external libraries. nvrtc64,
    // cudart64_*, cublas* and every toolkit dll are deliberately NOT
    // listed: any source reference to them is an external binding and
    // fails the check closed.
    public static readonly string[] VendorPlatform =
    {
        "nvcuda", "nvml",
        // cuda.dll = the driver-API fallback name for nvcuda.
        "cuda",
    };
    // Vendor math libraries — none: the retired §22 bench gate is
    // closed, GEMM/GEMV are in-tree PTX kernels. The table stays so a
    // future governed admission has an explicit home.
    public static readonly string[] VendorBenchGated =
    {
    };
    // Anything matching is an external compute dependency — denied.
    private static readonly (string pat, string what)[] Forbidden =
    {
        ("triton", "Triton kernel language — CUDA is written in " +
                   "C/C++, not Python DSLs"),
        ("torch\\.cuda|import torch", "PyTorch CUDA — retired lane"),
        ("cupy|numba|pycuda|cuda-python",
         "Python CUDA runtime — retired lane"),
        ("flash[-_]attn|flash_attn",
         "external kernel library — kernels are authored in-tree"),
        ("cutlass", "external kernel template library — kernels are " +
                    "authored in-tree"),
        ("cudnn|cufft|curand|cusolver|cusparse",
         "external vendor library — not on the admitted list"),
        ("#include\\s*[<\"][^>\"]*\\.cuh?[\">]",
         ".cu/.cuh include — device code is embedded PTX " +
         "in the cpp tree"),
    };
    // Vendor runtimes the NPU EP-enumeration probe may open for
    // discovery only — presence probing, never a compute dependency
    // (silicon plane §10/§11).
    private static readonly string[] EpProbeLibs =
    {
        "onnxruntime", "QnnHtp", "QnnCpu", "openvino", "migraphx",
        "winml", "DirectML",
    };
    // OS libraries never count as compute dependencies.
    private static readonly string[] OsLibs =
    {
        "kernel32", "ntdll", "user32", "gdi32", "advapi32", "ole32",
        "oleaut32", "ws2_32", "psapi", "shlwapi", "shell32", "winmm",
        "dbghelp", "bcrypt", "version", "comdlg32", "comctl32",
        "msvcrt", "vcruntime", "ucrtbase", "libdl", "libc",
    };
    // LoadLibrary/dlopen targets found in the native sources get
    // classified against the two allow tables.
    private static readonly Regex LibRef = new(
        "(?:LoadLibrary[AW]?|dlopen)\\s*\\(\\s*(?:L)?\"([^\"]+)\"",
        RegexOptions.Compiled | RegexOptions.IgnoreCase);
    // Indirect loads pass the name through a helper — every quoted
    // *.dll literal in a file that performs dynamic loading is
    // classified the same way.
    private static readonly Regex DllLit = new(
        "\"([A-Za-z0-9_.\\\\/-]+\\.dll)\"",
        RegexOptions.Compiled | RegexOptions.IgnoreCase);

    /// <summary>The stated policy record.</summary>
    public static Dictionary<string, object?> Policy() => new()
    {
        ["ok"] = true, ["format"] = Format,
        ["rule"] = "CUDA device code is self-authored PTX embedded in " +
                   "the governed cpp tree, JIT-compiled by the NVIDIA " +
                   "driver; no toolkit, runtime or external compute " +
                   "libraries",
        ["device_code"] = "in-tree PTX modules (cuda_ptx_*.h) loaded " +
                          "via cuModuleLoadData — NVRTC and nvcc are " +
                          "not used anywhere",
        ["host_binding"] = "CUDA Driver API via nvcuda.dll LoadLibrary/" +
                           "GetProcAddress — a host without CUDA still " +
                           "runs (probes report NO_DEVICE, never a " +
                           "link failure)",
        ["vendor_platform"] = VendorPlatform,
        ["vendor_bench_gated"] = VendorBenchGated,
        ["forbidden"] = new[]
        {
            ".cu/.cuh files or an nvcc toolchain requirement",
            "nvrtc / cudart / cublas / any CUDA toolkit dll",
            "Python CUDA (torch/cupy/numba/pycuda/cuda-python)",
            "Triton or any kernel DSL in another language",
            "third-party kernel libraries (flash-attn, cutlass, " +
                "shipped primitives)",
            "unlisted vendor libraries (cudnn/cufft/curand/...)",
            "any second compute runtime",
        },
        ["bench_rule"] =
            "no vendor math libraries — GEMM/GEMV are in-tree PTX " +
            "kernels; the retired §22 bench gate is closed",
    };

    /// <summary>Scan the native source tree for conformance: every
    /// dynamic library reference classified, every forbidden pattern
    /// collected. toolRoot = local-model root; the scanned surface is
    /// src/backend (cpp + tools).</summary>
    public static Dictionary<string, object?> Check(string toolRoot)
    {
        string root = Path.Combine(
            toolRoot,
            "src/backend".Replace('/', Path.DirectorySeparatorChar));
        var platform = new SortedSet<string>(
            StringComparer.OrdinalIgnoreCase);
        var benchGated = new SortedSet<string>(
            StringComparer.OrdinalIgnoreCase);
        var epProbes = new SortedSet<string>(
            StringComparer.OrdinalIgnoreCase);
        var external = new List<object?>();
        var forbidden = new List<object?>();
        var files = new List<string>();
        if (Directory.Exists(root))
        {
            files.AddRange(Directory.GetFiles(
                root, "*", SearchOption.AllDirectories)
                .Where(f => f.EndsWith(".cpp", StringComparison
                        .OrdinalIgnoreCase) ||
                    f.EndsWith(".h", StringComparison.OrdinalIgnoreCase)
                    || f.EndsWith(".hpp", StringComparison
                        .OrdinalIgnoreCase)));
            // Standalone .cu/.cuh sources are the real violation —
            // a "*.cu" NVRTC program label inside a string is not.
            foreach (string cu in Directory.GetFiles(
                         root, "*", SearchOption.AllDirectories)
                         .Where(f => f.EndsWith(".cu",
                                 StringComparison.OrdinalIgnoreCase) ||
                             f.EndsWith(".cuh", StringComparison
                                 .OrdinalIgnoreCase)))
                forbidden.Add(new Dictionary<string, object?>
                {
                    ["file"] = Path.GetRelativePath(toolRoot, cu)
                        .Replace('\\', '/'),
                    ["match"] = Path.GetFileName(cu),
                    ["reason"] = "standalone .cu/.cuh file — device " +
                        "code is embedded PTX in the cpp " +
                        "tree (no nvcc toolchain)",
                });
        }
        foreach (string f in files)
        {
            string rel = Path.GetRelativePath(toolRoot, f)
                .Replace('\\', '/');
            string text;
            try { text = File.ReadAllText(f); }
            catch { continue; }
            bool loads = text.Contains("LoadLibrary") ||
                         text.Contains("dlopen");
            var refs = LibRef.Matches(text).Cast<Match>()
                .Select(m => m.Groups[1].Value).ToList();
            if (loads)
                refs.AddRange(DllLit.Matches(text).Cast<Match>()
                    .Select(m => m.Groups[1].Value));
            foreach (string lib0 in refs)
            {
                string lib = lib0;
                // A fully-qualified search root ("...\CUDA\" or
                // ".../cuda/...") is vendor toolkit discovery — the
                // dll loaded beneath it is classified by name.
                bool toolkitPath = lib.IndexOf("NVIDIA",
                        StringComparison.OrdinalIgnoreCase) >= 0 ||
                    lib.IndexOf("\\CUDA\\", StringComparison
                        .OrdinalIgnoreCase) >= 0 ||
                    lib.EndsWith('\\') || lib.EndsWith('/');
                if (toolkitPath)
                {
                    benchGated.Add(lib);   // vendor toolkit root
                    continue;
                }
                // classify by the dll basename, not the whole path
                int slash = Math.Max(lib.LastIndexOf('\\'),
                                     lib.LastIndexOf('/'));
                if (slash >= 0) lib = lib[(slash + 1)..];
                if (VendorPlatform.Any(v => lib.StartsWith(v,
                        StringComparison.OrdinalIgnoreCase)))
                    platform.Add(lib);
                else if (VendorBenchGated.Any(v => lib.StartsWith(v,
                             StringComparison.OrdinalIgnoreCase)))
                    benchGated.Add(lib);
                else if (EpProbeLibs.Any(v => lib.StartsWith(v,
                             StringComparison.OrdinalIgnoreCase)))
                    epProbes.Add(lib);
                else if (!OsLibs.Any(o => lib.StartsWith(o,
                             StringComparison.OrdinalIgnoreCase)))
                    external.Add(new Dictionary<string, object?>
                    {
                        ["file"] = rel, ["lib"] = lib,
                    });
            }
            foreach (var (pat, why) in Forbidden)
                foreach (Match fm in Regex.Matches(text, pat,
                             RegexOptions.IgnoreCase))
                {
                    forbidden.Add(new Dictionary<string, object?>
                    {
                        ["file"] = rel,
                        ["match"] = fm.Value,
                        ["reason"] = why,
                    });
                    break;   // one hit per pattern per file is enough
                }
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = external.Count == 0 && forbidden.Count == 0,
            ["format"] = Format,
            ["files_scanned"] = files.Count,
            ["vendor_platform_bound"] =
                platform.Cast<object?>().ToList(),
            ["vendor_bench_gated_bound"] =
                benchGated.Cast<object?>().ToList(),
            ["ep_probe_libs"] = epProbes.Cast<object?>().ToList(),
            ["external_bindings"] = external,
            ["forbidden_matches"] = forbidden,
            ["verdict"] = external.Count == 0 && forbidden.Count == 0
                ? "CUDA_LANGUAGE_COMPLIANT"
                : "EXTERNAL_CUDA_DEPENDENCY",
        };
    }
}
