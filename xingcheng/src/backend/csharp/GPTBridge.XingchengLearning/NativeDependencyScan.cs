// NativeDependencyScan.cs — scanners for the NativeDependencyGate
// (Native-Only Architecture P0, §98–§104).
//
// One finding = one externally-sourced dependency the Native-Only
// contract forbids in production. Every finding carries the §117 check
// id, kind (cargo-crate | nuget-package | link-input | source-ref |
// pe-import | process-module | dll-load), scope (production | dev-only —
// tests/probes/fixtures never ship and never block), the §104 depclass
// (REQUIRED_PLATFORM / XINGCHENG_OWNED / EXTERNAL_REMOVE /
// LEGACY_MIGRATION_ONLY) and a blocking flag.

using System.Text.RegularExpressions;

namespace GPTBridge.XingchengLearning;

internal sealed class NativeDepFinding
{
    public string Check = "";
    public string Kind = "";
    public string Path = "";
    public string Detail = "";
    public string Scope = "production";
    public string DepClass = "EXTERNAL_REMOVE";
    public string Error = "NATIVE_DEPENDENCY_VIOLATION";
    public bool Blocking = true;
    /// <summary>true when the only evidence is a comment/doc line —
    /// recorded for the audit trail but never blocks the gate.</summary>
    public bool MentionOnly;

    public Dictionary<string, object?> ToDict()
    {
        var d = new Dictionary<string, object?>
        {
            ["check"] = Check,
            ["kind"] = Kind,
            ["path"] = Path,
            ["detail"] = Detail,
            ["scope"] = Scope,
            ["class"] = DepClass,
            ["error"] = Error,
            ["blocking"] = Blocking,
        };
        if (MentionOnly) d["mention_only"] = true;
        return d;
    }
}

internal static class NativeDependencyScan
{
    // ---- path helpers ---------------------------------------------------

    /// <summary>Directories never entered. runtime/ holds data and
    /// historical artifacts, corpus*/.git/node_modules are content —
    /// neither is a build manifest or binary.</summary>
    private static readonly HashSet<string> PruneDirs =
        new(StringComparer.OrdinalIgnoreCase)
        {
            ".git", "node_modules", "obj", "runtime", "corpus",
            "migration-residue", ".venv", "dist", ".fingerprint",
            "incremental",
        };

    private static bool Prune(string root, string dir)
    {
        string name = Path.GetFileName(dir);
        if (PruneDirs.Contains(name) || name.StartsWith(
                "corpus-", StringComparison.OrdinalIgnoreCase))
            return true;
        // under target/, only release artifacts are loadable binaries
        string rel = dir.Substring(root.Length)
            .Replace('\\', '/').TrimStart('/');
        if (rel.Contains("/target/", StringComparison.Ordinal) &&
            !rel.EndsWith("/target", StringComparison.Ordinal) &&
            !rel.Contains("/release", StringComparison.Ordinal))
            return true;
        return false;
    }

    /// <summary>dev-only scope: test suites, probes, fixtures and
    /// anything in a *_-prefixed scratch file.</summary>
    public static string ScopeOf(string rel)
    {
        string r = rel.Replace('\\', '/');
        string baseName = Path.GetFileName(r);
        if (r.Contains("/GPTBridge.XingchengLearning/LegacyMigration/", StringComparison.OrdinalIgnoreCase)
            && r.EndsWith(".cs", StringComparison.OrdinalIgnoreCase))
            return "LEGACY_MIGRATION_ONLY"; // Explicitly excluded by the production csproj.
        bool dev = baseName.StartsWith("_", StringComparison.Ordinal) ||
                   r.Contains("/test_suites/", StringComparison.Ordinal) ||
                   r.Contains("/tests/", StringComparison.Ordinal) ||
                   // *.Tests assemblies are verification tooling — they
                   // never ship inside a production binary.
                   r.Contains(".Tests/", StringComparison.Ordinal) ||
                   r.Contains("/fixtures/", StringComparison.Ordinal) ||
                   r.Contains("/_nfguard/", StringComparison.Ordinal) ||
                   r.Contains("/devin/", StringComparison.Ordinal);
        return dev ? "dev-only" : "production";
    }

    /// <summary>Walk files under root honouring Prune; returns
    /// root-relative ('/' separated) paths.</summary>
    public static List<string> Walk(string root)
    {
        var outp = new List<string>();
        if (!Directory.Exists(root)) return outp;
        var pending = new Stack<string>();
        pending.Push(root);
        while (pending.Count > 0)
        {
            string dir = pending.Pop();
            IEnumerable<string> subs;
            IEnumerable<string> files;
            try
            {
                subs = Directory.EnumerateDirectories(dir);
                files = Directory.EnumerateFiles(dir);
            }
            catch { continue; }
            foreach (var s in subs)
                if (!Prune(root, s)) pending.Push(s);
            foreach (var f in files)
                outp.Add(f.Substring(root.Length)
                    .Replace('\\', '/').TrimStart('/'));
        }
        return outp;
    }

    // ---- module classification (PE imports / process modules / DLL loads)

    private static readonly Regex ForbiddenModule = new(
        "^(cudart|cublas|cudnn|cutlass|nccl|nvrtc|nvjitlink|cufft|" +
        "curand|cusolver|cusparse|npp|nvjpeg|nvgraph|nvml|mkl_|libmkl|" +
        "openblas|libopenblas|libblas|lapack|onednn|dnnl|sqlite3|libpq|" +
        "pq\\.dll|ssleay32|libeay32|libssl|libcrypto|libcurl|curl\\.dll|" +
        "torch|libtorch|onnxruntime|llama|ggml|qdrant)",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);

    private static readonly Regex AllowedModule = new(
        "^(api-ms-|kernel32|user32|gdi32|gdi32full|advapi32|ole32|" +
        "oleaut32|shell32|shlwapi|ws2_32|wsock32|ntdll|msvcrt|ucrtbase|" +
        "vcruntime|msvcp|concrt|msvcr|bcrypt|bcryptprimitives|crypt32|" +
        "cryptbase|secur32|iphlpapi|dbghelp|psapi|comdlg32|comctl32|" +
        "winmm|version|winhttp|wininet|normaliz|rpcrt4|powrprof|" +
        "cfgmgr32|dwmapi|uxtheme|imm32|msimg32|netapi32|wldap32|" +
        "wintrust|cabinet|mscoree|pdh|setupapi|userenv|profapi|sspicli|" +
        "kerberos|mswsock|dnsapi|rasapi32|sechost|wldp|oleacc|msasn1|" +
        "win32u|dxgi|d3d|opencl32|combase|clbcatq|apphelp|ext-ms-|" +
        // NVIDIA driver hardware interface (§2): the only vendor modules
        // the CUDA lane may touch.
        "nvcuda|cuda\\.dll|nvapi|nvwgf2um|nvd3dum|" +
        // .NET host/runtime — official language runtime boundary.
        "hostfxr|hostpolicy|coreclr|clrjit|mscordaccore|createdump|" +
        // Xingcheng-owned modules.
        "xc-|xstore|xcorpus|xtok|xc_modeltool|xingcheng|" +
        "gptbridge_native|transformer|kv_pool)",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);

    public static string ClassifyModule(string module)
    {
        string m = module.ToLowerInvariant();
        if (ForbiddenModule.IsMatch(m)) return "forbidden";
        if (AllowedModule.IsMatch(m)) return "platform";
        return "unrecognized";
    }

    /// <summary>Map a module name (PE import or dynamic-load target) to
    /// the §117 check it violates.</summary>
    public static string CheckForModule(string module)
    {
        string stem = module.Split('.')[0].ToLowerInvariant();
        return stem switch
        {
            "cudart" => "cudart",
            "cublas" or "cublaslt" => "cublas",
            "cudnn" => "cudnn",
            "cutlass" => "cutlass",
            "nccl" => "nccl",
            "nvrtc" or "nvjitlink" => "nvrtc",
            "nvml" => "telemetry_library",
            "libpq" or "pq" or "psqlodbc" => "postgresql_required",
            "sqlite3" or "sqlite" => "external_database",
            "qdrant" => "external_vector_db",
            "ollama" or "ollama_service" => "ollama_required",
            "mkl" or "libmkl" or "openblas" or "libopenblas" or
                "libblas" or "lapack" or "onednn" or "dnnl" =>
                "numerical_library",
            "torch" or "libtorch" or "onnxruntime" or "llama" or
                "ggml" => "external_tensor_library",
            "ssleay32" or "libeay32" or "libssl" or "libcrypto" or
                "libcurl" or "curl" => "network_library",
            "cufft" or "curand" or "cusolver" or "cusparse" or
                "npp" or "nvjpeg" or "nvgraph" => "external_toolkit",
            _ => "gpu_library",
        };
    }

    // ---- source-level rules --------------------------------------------

    private sealed record SrcRule(
        string Check, Regex Re, string DepClass, string Err);

    private static readonly SrcRule[] SourceRules =
    {
        new("postgresql_required",
            new Regex(@"\b(Npgsql|npgsql|libpq|GPTBRIDGE_POSTGRES\w*" +
                      @"|XINGCHENG_SHARED_PG|postgresql://)\b",
                      RegexOptions.Compiled),
            "LEGACY_MIGRATION_ONLY", "EXTERNAL_DATABASE_DENIED"),
        new("ollama_required",
            new Regex(@"\b(ollama|Ollama)\w*|:11434\b",
                      RegexOptions.Compiled),
            "EXTERNAL_REMOVE", "EXTERNAL_MODEL_SERVICE_DENIED"),
        new("external_vector_db",
            new Regex(@"\b(qdrant|pgvector|milvus|pinecone|weaviate" +
                      @"|chromadb|lancedb|faiss)\b",
                      RegexOptions.IgnoreCase | RegexOptions.Compiled),
            "EXTERNAL_REMOVE", "EXTERNAL_DATABASE_DENIED"),
        new("external_database",
            new Regex(@"\b(sqlite3?|System\.Data\.SQLite" +
                      @"|Microsoft\.Data\.Sqlite|mysql_|mongodb|redis)\b",
                      RegexOptions.IgnoreCase | RegexOptions.Compiled),
            "EXTERNAL_REMOVE", "EXTERNAL_DATABASE_DENIED"),
        new("external_tensor_library",
            new Regex(@"#include\s*[<""](torch|tensorflow|onnxruntime" +
                      @"|ggml|Eigen|boost)" +
                      @"|\b(onnxruntime|libtorch|llama\.cpp)\b",
                      RegexOptions.IgnoreCase | RegexOptions.Compiled),
            "EXTERNAL_REMOVE", "NATIVE_DEPENDENCY_VIOLATION"),
        new("gpu_library",
            new Regex(@"#include\s*[<""](cublas|cudnn|nvrtc|nccl|cutlass" +
                      @"|cub/|thrust/|nvml|cuda_runtime)" +
                      @"|\b(cublas\w*|cudnn\w*|nvrtc\w*|nccl\w*" +
                      @"|nvml\w*)\s*\(",
                      RegexOptions.Compiled),
            "EXTERNAL_REMOVE", "EXTERNAL_GPU_LIBRARY_DENIED"),
        new("runtime_compilation",
            new Regex(@"\b(nvrtc\w*|libclang|Assembly\.Load)\s*\(",
                      RegexOptions.Compiled),
            "EXTERNAL_REMOVE", "RUNTIME_COMPILATION_DENIED"),
        new("numerical_library",
            new Regex(@"\b(mkl_|cblas_|LAPACK|lapack_|openblas|oneapi" +
                      @"|dnnl_)\w*\s*\(|#include\s*[<""](mkl|Eigen" +
                      @"|cblas|lapack|dnnl)",
                      RegexOptions.IgnoreCase | RegexOptions.Compiled),
            "EXTERNAL_REMOVE", "EXTERNAL_NUMERICAL_LIBRARY_DENIED"),
    };

    private static readonly Regex LoadLibraryRe = new(
        @"\b(LoadLibrary\w*|dlopen|NativeLibrary\.Load)\s*\(\s*""([^""]+)""",
        RegexOptions.Compiled);

    // ---- scanners --------------------------------------------------------

    /// <summary>All manifest + source + build-script findings under the
    /// given roots. Each root entry is (absDir, repoRelPrefix).</summary>
    public static List<NativeDepFinding> Scan(
        IReadOnlyList<(string abs, string rel)> roots)
    {
        var findings = new List<NativeDepFinding>();
        foreach (var (abs, rel) in roots)
        {
            foreach (string file in Walk(abs))
            {
                string relPath = rel.Length > 0 ? rel + "/" + file : file;
                string scope = ScopeOf(relPath);
                string ext = Path.GetExtension(file).ToLowerInvariant();
                string name = Path.GetFileName(file);
                string full = Path.Combine(abs, file);
                switch (ext)
                {
                    case ".csproj":
                    case ".fsproj":
                    case ".props":
                    case ".targets":
                        ScanDotnetManifest(full, relPath, scope, findings);
                        break;
                    case ".toml":
                        if (name.Equals("Cargo.toml",
                                StringComparison.OrdinalIgnoreCase))
                            ScanCargo(full, relPath, scope, findings);
                        break;
                    case ".ps1":
                    case ".bat":
                    case ".cmd":
                    case ".cmake":
                    case ".vcxproj":
                        ScanBuildScript(full, relPath, scope, findings);
                        break;
                    case ".config":
                        if (name.Equals("packages.config",
                                StringComparison.OrdinalIgnoreCase))
                            ScanDotnetManifest(
                                full, relPath, scope, findings);
                        break;
                    case ".cs":
                    case ".cpp":
                    case ".h":
                    case ".hpp":
                    case ".c":
                    case ".rs":
                    case ".fs":
                        // The gate's own pattern tables hold forbidden
                        // names as data, not dependencies — skip them.
                        if (!name.StartsWith("NativeDependency",
                                StringComparison.Ordinal))
                            ScanSource(full, relPath, scope, findings);
                        break;
                }
                if (name.Equals("CMakeLists.txt",
                        StringComparison.OrdinalIgnoreCase) ||
                    name.Equals("Makefile",
                        StringComparison.OrdinalIgnoreCase))
                    ScanBuildScript(full, relPath, scope, findings);
            }
        }
        return findings;
    }

    private static string[] ReadLines(string path)
    {
        try { return File.ReadAllLines(path); }
        catch { return Array.Empty<string>(); }
    }

    private static readonly Regex PackageRefRe = new(
        @"<\s*PackageReference\b[^>]*Include\s*=\s*""([^""]+)""[^>]*" +
        @"(?:Version\s*=\s*""([^""]+)"")?",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);
    private static readonly Regex PackagesConfigRe = new(
        @"<\s*package\b[^>]*id\s*=\s*""([^""]+)""[^>]*" +
        @"version\s*=\s*""([^""]+)""",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);

    private static void ScanDotnetManifest(
        string full, string rel, string scope,
        List<NativeDepFinding> findings)
    {
        foreach (string line in ReadLines(full))
        {
            foreach (Match m in PackageRefRe.Matches(line))
                AddNuget(findings, rel, scope, m.Groups[1].Value,
                    m.Groups[2].Success ? m.Groups[2].Value : "");
            foreach (Match m in PackagesConfigRe.Matches(line))
                AddNuget(findings, rel, scope, m.Groups[1].Value,
                    m.Groups[2].Value);
            ScanLineTokens(full, rel, scope, line, findings);
        }
    }

    private static void AddNuget(List<NativeDepFinding> findings,
        string rel, string scope, string id, string version)
    {
        bool firstParty = id.StartsWith("System.",
            StringComparison.OrdinalIgnoreCase) ||
            id.StartsWith("Microsoft.", StringComparison.OrdinalIgnoreCase);
        findings.Add(new NativeDepFinding
        {
            Check = "third_party_nuget",
            Kind = "nuget-package",
            Path = rel,
            Detail = $"{id} {version}".Trim() +
                (firstParty ? " (first-party package)" : ""),
            Scope = scope,
            DepClass = "EXTERNAL_REMOVE",
            // §101: production C# may reference framework + project
            // references only — every PackageReference is a breach.
            Error = "NATIVE_DEPENDENCY_VIOLATION",
            Blocking = scope == "production",
        });
    }

    private static void ScanCargo(string full, string rel, string scope,
        List<NativeDepFinding> findings)
    {
        string section = "";
        foreach (string raw in ReadLines(full))
        {
            string line = raw.Trim();
            if (line.StartsWith('['))
            {
                section = line;
                continue;
            }
            bool depSection =
                section.Equals("[dependencies]", StringComparison.Ordinal) ||
                section.StartsWith("[target.", StringComparison.Ordinal) &&
                section.EndsWith(".dependencies]", StringComparison.Ordinal);
            bool devSection = section.Equals("[dev-dependencies]",
                    StringComparison.Ordinal) ||
                section.Equals("[build-dependencies]",
                    StringComparison.Ordinal) ||
                section.EndsWith(".dev-dependencies]",
                    StringComparison.Ordinal) ||
                section.EndsWith(".build-dependencies]",
                    StringComparison.Ordinal);
            if (!depSection && !devSection) continue;
            int eq = line.IndexOf('=');
            if (eq <= 0 || line.StartsWith('#')) continue;
            string name = line[..eq].Trim();
            string value = line[(eq + 1)..];
            if (name.Length == 0 || name == "workspace") continue;
            bool owned = value.Contains("path", StringComparison.Ordinal) ||
                         value.Contains("workspace",
                             StringComparison.Ordinal);
            if (owned) continue;
            findings.Add(new NativeDepFinding
            {
                Check = "third_party_rust_crates",
                Kind = "cargo-crate",
                Path = rel,
                Detail = $"{name}: {value.Trim()}",
                Scope = scope,
                DepClass = "EXTERNAL_REMOVE",
                Error = "NATIVE_DEPENDENCY_VIOLATION",
                // §100 forbids production runtime crates; dev/build
                // crates are still external surface but never ship.
                Blocking = scope == "production" && !devSection,
            });
        }
    }

    private static readonly Regex BuildTokenRe = new(
        @"\b(cudart|cublas|cublaslt|cudnn|cutlass|nccl|nvrtc|" +
        @"nvjitlink|cufft|curand|cusolver|cusparse|nvml|mkl|openblas|" +
        @"onednn|dnnl|eigen|boost|nvcc|libtorch|onnxruntime|qdrant|" +
        @"psql|sqlite|ollama|reqwest|openssl|libcurl)\b" +
        @"|NVIDIA GPU Computing Toolkit|CUDA_PATH|CUDA_PATH_V",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);

    private static void ScanBuildScript(string full, string rel,
        string scope, List<NativeDepFinding> findings)
    {
        foreach (string line in ReadLines(full))
            ScanLineTokens(full, rel, scope, line, findings);
    }

    private static void ScanLineTokens(string full, string rel,
        string scope, string line, List<NativeDepFinding> findings)
    {
        var m = BuildTokenRe.Match(line);
        if (!m.Success) return;
        string token = m.Groups[1].Success
            ? m.Groups[1].Value.ToLowerInvariant()
            : m.Value.Trim();
        string check = token switch
        {
            "cublas" or "cublaslt" => "cublas",
            "cudnn" => "cudnn",
            "cutlass" => "cutlass",
            "nccl" => "nccl",
            "nvrtc" or "nvjitlink" => "nvrtc",
            "nvml" => "telemetry_library",
            "cudart" => "cudart",
            "ollama" => "ollama_required",
            "psql" or "postgres" => "postgresql_required",
            "sqlite" => "external_database",
            "qdrant" => "external_vector_db",
            "nvcc" => "runtime_compilation",
            "mkl" or "openblas" or "onednn" or "dnnl" or "eigen" or
                "boost" or "lapack" => "numerical_library",
            "libtorch" or "onnxruntime" => "external_tensor_library",
            "reqwest" or "openssl" or "libcurl" => "network_library",
            _ => "external_toolkit",
        };
        findings.Add(new NativeDepFinding
        {
            Check = check,
            Kind = "link-input",
            Path = rel,
            Detail = token,
            Scope = scope,
            DepClass = "EXTERNAL_REMOVE",
            Error = check switch
            {
                "ollama_required" => "EXTERNAL_MODEL_SERVICE_DENIED",
                "postgresql_required" or "external_database" or
                    "external_vector_db" => "EXTERNAL_DATABASE_DENIED",
                "nvrtc" or "runtime_compilation" =>
                    "RUNTIME_COMPILATION_DENIED",
                "numerical_library" =>
                    "EXTERNAL_NUMERICAL_LIBRARY_DENIED",
                "external_toolkit" or "cublas" or "cudnn" or
                    "cutlass" or "nccl" or "cudart" or
                    "telemetry_library" => "EXTERNAL_GPU_LIBRARY_DENIED",
                _ => "NATIVE_DEPENDENCY_VIOLATION",
            },
            Blocking = scope == "production",
        });
    }

    private static void ScanSource(string full, string rel, string scope,
        List<NativeDepFinding> findings)
    {
        var lines = ReadLines(full);
        var seen = new HashSet<string>(StringComparer.Ordinal);
        bool inBlock = false;
        foreach (string line in lines)
        {
            // comment tracking (// and /* */ — covers C/C++/C#/Rust/F#):
            // a forbidden name inside a comment is mention-only
            // evidence, never a blocking dependency.
            string t = line.TrimStart();
            bool commentOnly;
            if (inBlock)
            {
                commentOnly = true;
                if (line.Contains("*/")) inBlock = false;
            }
            else
            {
                commentOnly = t.StartsWith("//") || t.StartsWith('*');
                int ob = line.IndexOf("/*", StringComparison.Ordinal);
                if (ob >= 0)
                {
                    int cb = line.IndexOf(
                        "*/", ob + 2, StringComparison.Ordinal);
                    if (cb < 0)
                    {
                        inBlock = true;
                        if (ob == 0 || t.StartsWith("/*"))
                            commentOnly = true;
                    }
                }
            }
            foreach (var rule in SourceRules)
            {
                var m = rule.Re.Match(line);
                if (!m.Success) continue;
                string hit = m.Value.Trim();
                if (hit.Length > 48) hit = hit[..48];
                // one finding per (rule, hit) per file keeps reports
                // bounded on comment-heavy sources.
                if (!seen.Add(rule.Check + "|" + hit)) continue;
                findings.Add(new NativeDepFinding
                {
                    Check = rule.Check,
                    Kind = "source-ref",
                    Path = rel,
                    Detail = hit,
                    Scope = scope,
                    DepClass = rule.DepClass,
                    Error = rule.Err,
                    Blocking = scope == "production" && !commentOnly,
                    MentionOnly = commentOnly,
                });
            }
            foreach (Match lm in LoadLibraryRe.Matches(line))
            {
                string target = lm.Groups[2].Value;
                string cls = ClassifyModule(target);
                if (cls == "platform") continue;
                findings.Add(new NativeDepFinding
                {
                    Check = CheckForModule(target),
                    Kind = "dll-load",
                    Path = rel,
                    Detail = $"{lm.Groups[1].Value}(\"{target}\")",
                    Scope = scope,
                    DepClass = "EXTERNAL_REMOVE",
                    Error = "NATIVE_DEPENDENCY_VIOLATION",
                    // unrecognized load targets need review but must
                    // not false-block on OS/driver aliases.
                    Blocking = scope == "production" &&
                               cls == "forbidden" && !commentOnly,
                    MentionOnly = commentOnly,
                });
            }
        }
    }
}
