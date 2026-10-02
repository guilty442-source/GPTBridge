// NativeDependencyGate.cs — Native-Only Architecture P0 gate
// (§98–§104, §116–§119).
//
// Three read-only verbs on xc-learning:
//
//   --native-only-check          §117 contract. Scans every C++/Rust/
//                                C#/F# build manifest, link input and
//                                source reference under the xingcheng
//                                domain roots plus every built binary's
//                                static PE import table, and fails
//                                (ok=false) while any production-scope
//                                prohibited dependency remains. Emitted
//                                as star-native-only-gate/v1; the
//                                release-gate field is the name P7
//                                promotion will consult
//                                (NATIVE_ONLY_GATE, §119).
//   --cuda-native-check          §118 contract: driver_api_only,
//                                cudart/cublas/cudnn/nvrtc linkage,
//                                external kernel libraries and the
//                                native/certified kernel counts from
//                                the in-tree PTX sources and the
//                                star-kernel-registry emit of
//                                xc_modeltool. star-cuda-native-check/v1.
//   --native-dependency-inventory
//                                §104 inventory: every manifest entry
//                                classified REQUIRED_PLATFORM /
//                                XINGCHENG_OWNED / EXTERNAL_REMOVE /
//                                LEGACY_MIGRATION_ONLY.
//
//   --scan-process <pid>         §102 optional runtime audit: classifies
//                                the loaded modules of a live process.
//
// The gate never mutates anything and never loads a prohibited
// component itself; findings are evidence for P1–P6 remediation.

using System.Diagnostics;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace GPTBridge.XingchengLearning;

internal static class NativeDependencyGate
{
    public const string GateFormat = "star-native-only-gate/v1";
    public const string CudaFormat = "star-cuda-native-check/v1";
    public const string InventoryFormat =
        "star-native-dependency-inventory/v1";

    /// <summary>§117 check ids, emitted in this order even when they
    /// pass so downstream consumers get a stable contract.</summary>
    private static readonly string[] CheckIds =
    {
        "postgresql_required", "ollama_required",
        "cudart", "cublas", "cudnn", "cutlass", "nccl", "nvrtc",
        "third_party_rust_crates", "third_party_nuget",
        "external_tensor_library", "external_vector_db",
        "external_database", "numerical_library", "gpu_library",
        "runtime_compilation", "telemetry_library", "network_library",
        "runtime_module_load", "unrecognized_module", "external_toolkit",
    };

    // ---- roots --------------------------------------------------------

    /// <summary>repo root = the ancestor that owns native/include/
    /// gptbridge_native.h (same marker the C++ build scripts use).</summary>
    private static string? RepoRoot(string toolRoot)
    {
        var d = new DirectoryInfo(toolRoot);
        while (d != null)
        {
            if (File.Exists(Path.Combine(
                    d.FullName, "native", "include",
                    "gptbridge_native.h")))
                return d.FullName;
            d = d.Parent;
        }
        return null;
    }

    /// <summary>The xingcheng dependency domain: the enclave's src tree,
    /// the institution root (runtime/ pruned inside the walk) and the
    /// repo-native core where ollama_service and the C lane live.</summary>
    private static List<(string abs, string rel)> ScanRoots(
        string toolRoot)
    {
        var roots = new List<(string abs, string rel)>();
        void Add(string abs, string rel)
        {
            if (Directory.Exists(abs)) roots.Add((abs, rel));
        }
        Add(Path.Combine(toolRoot, "src"), "xingcheng/src");
        Add(Path.Combine(toolRoot, "xingcheng"), "xingcheng/xingcheng");
        string? repo = RepoRoot(toolRoot);
        if (repo != null)
            Add(Path.Combine(repo, "native"), "native");
        return roots;
    }

    // ---- binary audit ---------------------------------------------------

    private sealed class BinaryAudit
    {
        public string Path = "";
        public List<string> Imports = new();
        public List<string> Forbidden = new();
        public List<string> Unrecognized = new();
    }

    private static List<BinaryAudit> AuditBinaries(
        List<(string abs, string rel)> roots)
    {
        var outp = new List<BinaryAudit>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var (abs, rel) in roots)
        {
            foreach (string file in NativeDependencyScan.Walk(abs))
            {
                string ext = Path.GetExtension(file);
                if (!ext.Equals(".exe", StringComparison.OrdinalIgnoreCase) &&
                    !ext.Equals(".dll", StringComparison.OrdinalIgnoreCase))
                    continue;
                string full = Path.Combine(abs, file);
                if (!seen.Add(full)) continue;
                var imports = NativeDependencyPe.ImportModules(full);
                var a = new BinaryAudit { Path = rel + "/" + file };
                if (imports == null)
                {
                    a.Unrecognized.Add("<unparseable-pe>");
                }
                else
                {
                    a.Imports = imports;
                    foreach (var m in imports)
                    {
                        switch (NativeDependencyScan.ClassifyModule(m))
                        {
                            case "forbidden": a.Forbidden.Add(m); break;
                            case "unrecognized":
                                a.Unrecognized.Add(m); break;
                        }
                    }
                }
                outp.Add(a);
            }
        }
        return outp;
    }

    // ---- §117 native-only-check ----------------------------------------

    public static Dictionary<string, object?> NativeOnlyCheck(
        string toolRoot, int scanPid)
    {
        var roots = ScanRoots(toolRoot);
        var findings = NativeDependencyScan.Scan(roots);
        var binaries = AuditBinaries(roots);

        // Fold PE import results into findings.
        foreach (var b in binaries)
        {
            string scope = NativeDependencyScan.ScopeOf(b.Path);
            foreach (var m in b.Forbidden)
            {
                findings.Add(new NativeDepFinding
                {
                    Check = NativeDependencyScan.CheckForModule(m),
                    Kind = "pe-import", Path = b.Path,
                    Detail = m, Scope = scope,
                    DepClass = "EXTERNAL_REMOVE",
                    Blocking = scope == "production",
                });
            }
            foreach (var m in b.Unrecognized)
                findings.Add(new NativeDepFinding
                {
                    Check = "unrecognized_module", Kind = "pe-import",
                    Path = b.Path, Detail = m, Scope = scope,
                    DepClass = "EXTERNAL_REMOVE",
                    // fringe OS/driver aliases need human review, not a
                    // false block — §102 lists them as violations only
                    // once confirmed external.
                    Blocking = false,
                });
        }

        if (scanPid >= 0)
        {
            try
            {
                using var p = Process.GetProcessById(scanPid);
                foreach (ProcessModule mod in p.Modules)
                {
                    string name = Path.GetFileName(mod.FileName)
                        .ToLowerInvariant();
                    string cls = NativeDependencyScan.ClassifyModule(name);
                    if (cls == "platform") continue;
                    findings.Add(new NativeDepFinding
                    {
                        Check = cls == "forbidden"
                            ? NativeDependencyScan.CheckForModule(name)
                            : "unrecognized_module",
                        Kind = "process-module",
                        Path = $"pid:{scanPid}",
                        Detail = mod.FileName,
                        Scope = "production",
                        DepClass = "EXTERNAL_REMOVE",
                        Blocking = cls == "forbidden",
                    });
                }
            }
            catch (Exception e)
            {
                findings.Add(new NativeDepFinding
                {
                    Check = "unrecognized_module",
                    Kind = "process-module",
                    Path = $"pid:{scanPid}",
                    Detail = $"scan failed: {e.Message}",
                    Scope = "production",
                    Blocking = false,
                });
            }
        }

        var checks = new Dictionary<string, object?>();
        var devFindings = new List<object?>();
        foreach (var f in findings.Where(f => f.Scope != "production"))
            devFindings.Add(f.ToDict());
        foreach (string id in CheckIds)
        {
            var prod = findings.Where(
                f => f.Check == id && f.Scope == "production").ToList();
            var dev = findings.Count(
                f => f.Check == id && f.Scope != "production");
            checks[id] = new Dictionary<string, object?>
            {
                ["result"] = prod.Any(f => f.Blocking) ? "fail" : "pass",
                ["blocking"] = prod.Count(f => f.Blocking),
                ["dev_only"] = dev,
                ["findings"] = prod.Select(
                    f => (object?)f.ToDict()).ToList(),
            };
        }
        bool ok = findings.All(f => !f.Blocking ||
                                    f.Scope != "production");
        var classes = findings
            .GroupBy(f => f.DepClass)
            .ToDictionary(g => (object?)g.Key,
                g => (object?)g.Count());
        return new Dictionary<string, object?>
        {
            ["ok"] = ok,
            ["format"] = GateFormat,
            ["release_gate"] = "NATIVE_ONLY_GATE",
            ["promotion_allowed"] = ok,
            ["checked_at"] = XcPaths.IsoNow(),
            ["roots"] = roots.Select(r => (object?)r.rel).ToList(),
            ["checks"] = checks,
            ["dev_findings"] = devFindings,
            ["dependency_classes"] = classes,
            ["binaries_audited"] = binaries.Count,
            ["binaries_with_forbidden_imports"] = binaries
                .Where(b => b.Forbidden.Count > 0)
                .Select(b => (object?)b.Path).ToList(),
        };
    }

    // ---- §118 cuda-native-check -----------------------------------------

    private static readonly Regex PtxEntryRe = new(
        @"\.visible\s+\.entry\s+([A-Za-z_]\w*)",
        RegexOptions.Compiled);

    public static Dictionary<string, object?> CudaNativeCheck(
        string toolRoot)
    {
        var roots = ScanRoots(toolRoot);
        var findings = NativeDependencyScan.Scan(roots);
        var binaries = AuditBinaries(roots);

        bool Linked(string prefix) => binaries.Any(b =>
            b.Forbidden.Any(m => m.StartsWith(prefix,
                StringComparison.Ordinal)) &&
            NativeDependencyScan.ScopeOf(b.Path) == "production");
        bool cudart = Linked("cudart");
        bool cublas = Linked("cublas");
        bool cudnn = Linked("cudnn");
        bool nvrtc = Linked("nvrtc") || Linked("nvjitlink");
        bool nvml = Linked("nvml");

        // Dynamic-load audit: every dll-load target the sources reach
        // for must classify platform (driver/own); a toolkit load breaks
        // driver_api_only even without a static import.
        var dynLoads = findings.Where(f => f.Kind == "dll-load")
            .Select(f => (string)f.ToDict()["detail"]!).ToList();
        var extKernelLibs = findings.Where(f =>
                f.Check == "gpu_library" ||
                f.Check == "cutlass").Select(f => (object?)f.ToDict())
            .ToList();
        bool driverApiOnly = !(cudart || cublas || cudnn || nvrtc) &&
            dynLoads.All(d => d.Contains("nvcuda.dll") ||
                              d.Contains("cuda.dll"));

        // Native kernel counts: in-tree PTX .entry symbols plus the
        // governed star-kernel-registry emit when the tool is built.
        var ptxEntries = new List<string>();
        foreach (var (abs, _) in roots)
            foreach (string file in NativeDependencyScan.Walk(abs))
                if (file.EndsWith(".h", StringComparison.Ordinal) ||
                    file.EndsWith(".cpp", StringComparison.Ordinal))
                    foreach (string line in SafeLines(
                                 Path.Combine(abs, file)))
                        foreach (Match m in PtxEntryRe.Matches(line))
                            ptxEntries.Add(m.Groups[1].Value);

        int registryCount = -1, certified = 0;
        var pending = new List<object?>();
        string modeltool = Path.Combine(toolRoot, "src", "backend",
            "services", "xingcheng", "infrastructure",
            "native_transformer", "tools", "xc_modeltool.exe");
        string registryState = "unavailable";
        if (File.Exists(modeltool))
        {
            try
            {
                var rr = NativeTools.Run(
                    modeltool, new[] { "kernel-registry" }, toolRoot,
                    Path.Combine(toolRoot, "xingcheng", "runtime",
                        "logs", "native-dependency-gate.stderr.log"),
                    timeoutS: 30);
                var doc = JsonDocument.Parse(rr.StdoutTail.Trim());
                registryCount = doc.RootElement
                    .GetProperty("count").GetInt32();
                foreach (var k in doc.RootElement
                             .GetProperty("kernels").EnumerateArray())
                {
                    bool pendingK = k.GetProperty("variants")
                        .EnumerateArray().Any(v =>
                            v.GetProperty("parity").GetString() ==
                            "device-cert-pending");
                    if (pendingK)
                        pending.Add(k.GetProperty("name").GetString());
                    else certified++;
                }
                registryState = "emitted";
            }
            catch { registryState = "error"; }
        }
        int nativeCount = registryCount >= 0
            ? registryCount : ptxEntries.Count;

        var binaryDicts = binaries.Select(b =>
            (object?)new Dictionary<string, object?>
            {
                ["path"] = b.Path,
                ["imports"] = b.Imports.Select(i => (object?)i).ToList(),
                ["forbidden"] = b.Forbidden
                    .Select(i => (object?)i).ToList(),
                ["unrecognized"] = b.Unrecognized
                    .Select(i => (object?)i).ToList(),
                ["scope"] = NativeDependencyScan.ScopeOf(b.Path),
            }).ToList();

        bool ok = driverApiOnly && extKernelLibs.All(
            f => !(bool)((Dictionary<string, object?>)f)["blocking"]!);
        return new Dictionary<string, object?>
        {
            ["ok"] = ok,
            ["format"] = CudaFormat,
            ["checked_at"] = XcPaths.IsoNow(),
            ["driver_api_only"] = driverApiOnly,
            ["cudart_linked"] = cudart,
            ["cublas_linked"] = cublas,
            ["cudnn_linked"] = cudnn,
            ["nvrtc_linked"] = nvrtc,
            ["nvml_linked"] = nvml,
            ["external_kernel_libs"] = extKernelLibs,
            ["dynamic_module_loads"] = dynLoads,
            ["native_kernel_count"] = nativeCount,
            ["certified_kernel_count"] = certified,
            ["device_cert_pending_kernels"] = pending,
            ["ptx_entry_count"] = ptxEntries.Count,
            ["ptx_entries"] = ptxEntries.Select(
                e => (object?)e).ToList(),
            ["kernel_registry"] = registryState,
            ["binaries"] = binaryDicts,
        };
    }

    // ---- §104 inventory --------------------------------------------------

    public static Dictionary<string, object?> Inventory(
        string toolRoot)
    {
        var roots = ScanRoots(toolRoot);
        var findings = NativeDependencyScan.Scan(roots);
        var entries = new List<object?>();
        var classCounts = new Dictionary<string, int>(
            StringComparer.Ordinal)
        {
            ["REQUIRED_PLATFORM"] = 0,
            ["XINGCHENG_OWNED"] = 0,
            ["EXTERNAL_REMOVE"] = 0,
            ["LEGACY_MIGRATION_ONLY"] = 0,
        };

        // Manifest inventory: dotnet PackageReferences and Cargo entries
        // are the declared surface; scan findings already classify them.
        foreach (var f in findings.Where(
                     f => f.Kind is "nuget-package" or "cargo-crate"))
        {
            classCounts[f.DepClass]++;
            entries.Add(f.ToDict());
        }
        // Owned edges for contrast: project refs + workspace crates.
        foreach (var (abs, rel) in roots)
            foreach (string file in NativeDependencyScan.Walk(abs))
            {
                string relPath = rel + "/" + file;
                if (file.EndsWith(".csproj", StringComparison.Ordinal) ||
                    file.EndsWith(".fsproj", StringComparison.Ordinal))
                {
                    foreach (string line in SafeLines(
                                 Path.Combine(abs, file)))
                    {
                        var pm = Regex.Match(line,
                            @"ProjectReference\b[^>]*Include\s*=\s*""([^""]+)""");
                        if (pm.Success)
                        {
                            classCounts["XINGCHENG_OWNED"]++;
                            entries.Add(new Dictionary<string, object?>
                            {
                                ["kind"] = "project-reference",
                                ["path"] = relPath,
                                ["detail"] = pm.Groups[1].Value,
                                ["class"] = "XINGCHENG_OWNED",
                            });
                        }
                    }
                }
            }
        classCounts["REQUIRED_PLATFORM"] += 3;
        entries.Add(new Dictionary<string, object?>
        {
            ["kind"] = "platform",
            ["path"] = "-",
            ["detail"] = "Windows OS API + MSVC/.NET/Rust toolchains " +
                "+ NVIDIA driver (nvcuda.dll, LoadLibrary-only)",
            ["class"] = "REQUIRED_PLATFORM",
        });
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = InventoryFormat,
            ["checked_at"] = XcPaths.IsoNow(),
            ["roots"] = roots.Select(r => (object?)r.rel).ToList(),
            ["classes"] = classCounts.ToDictionary(
                kv => (object?)kv.Key, kv => (object?)kv.Value),
            ["entries"] = entries,
        };
    }

    private static string[] SafeLines(string path)
    {
        try { return File.ReadAllLines(path); }
        catch { return Array.Empty<string>(); }
    }
}
