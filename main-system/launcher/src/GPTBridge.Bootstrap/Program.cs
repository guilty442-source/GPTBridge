// GPTBridge.Bootstrap — C# 14 / .NET 10 port of launcher/scripts/start.py.
//
// bootstrap-entry migration (architecture registry: python_residency =
// migrate-csharp; A341 makes C# the orchestration owner). The contract is
// byte-for-byte the Python launcher's: same environment bindings, same
// freshness fingerprints, same journal events, same hidden-process launch.
//
// Differences that are deliberate improvements, not contract changes:
//   * Single-instance uses a real named mutex (Local\GPTBridgeLauncher) —
//     the Python version named its lock file after its own PID, so the
//     exclusion never actually contended.
//   * UI/launcher fingerprints sort input paths; the Python version relied
//     on filesystem enumeration order. This changes the fingerprint value
//     once on cutover (one rebuild), then stays stable.
//
// Bounded delegation preserved by design: the governed MigrationRunner
// (migration_autostart.py) and the installer (install.py) stay in their
// owning runtimes — the entry orchestrates them, it does not re-implement
// governed internals.

using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Win32;

namespace GPTBridge.Bootstrap;

internal static class Program
{
    private const string AppDisplayName = "GPTBridge";
    private const string MutexName = @"Local\GPTBridgeLauncher";
    private const string RuntimeContextEnv = "GPTBRIDGE_RUNTIME_CONTEXT";
    private const double RefreshLockStaleSeconds = 900.0;

    // main-system root: the exe installs at launcher/bin/, but builds also
    // run from nested publish dirs — walk up until the root markers appear.
    // Not readonly: --project-root overrides these in Main before Launch.
    private static string ProjectRoot = ResolveProjectRoot();

    private static string ResolveProjectRoot()
    {
        var candidate = new DirectoryInfo(AppContext.BaseDirectory);
        for (var depth = 0; depth < 10 && candidate is not null; depth++)
        {
            if (Directory.Exists(Path.Combine(candidate.FullName, "launcher"))
                && Directory.Exists(Path.Combine(candidate.FullName, "src-ui")))
            {
                return candidate.FullName;
            }
            candidate = candidate.Parent;
        }
        return Path.GetFullPath(
            Path.Combine(AppContext.BaseDirectory, "..", "..", ".."));
    }

    /// Re-derive every root-dependent path when --project-root overrides
    /// the auto-detected root; otherwise the flag would only rewrite env
    /// vars while file resolution silently kept the detected root.
    private static void ApplyProjectRoot(string root)
    {
        ProjectRoot = Path.GetFullPath(root);
        WorkspaceRoot = Path.GetFullPath(Path.Combine(ProjectRoot, ".."));
        LauncherRoot = Path.Combine(ProjectRoot, "launcher");
        StateRoot = Path.Combine(LauncherRoot, "state");
        UiBuildStampPath = Path.Combine(StateRoot, "ui-build.stamp");
        JournalPath = Path.Combine(StateRoot, "startup-journal.jsonl");
        RequirementsStampPath =
            Path.Combine(StateRoot, "requirements.stamp");
    }

    private static string WorkspaceRoot =
        Path.GetFullPath(Path.Combine(ProjectRoot, ".."));
    private static string LauncherRoot =
        Path.Combine(ProjectRoot, "launcher");
    private static string StateRoot =
        Path.Combine(LauncherRoot, "state");

    private static readonly string LocalAppData =
        Environment.GetEnvironmentVariable("LOCALAPPDATA") ?? "";
    private static readonly string InstallRoot =
        Path.Combine(LocalAppData, "GPTBridgeLauncher");
    private static readonly string LauncherStampPath =
        Path.Combine(InstallRoot, "state", "launcher-stamp.json");
    private static readonly string RefreshLockPath =
        Path.Combine(InstallRoot, "state", "refresh.lock");

    private static string UiBuildStampPath =
        Path.Combine(StateRoot, "ui-build.stamp");
    private static string JournalPath =
        Path.Combine(StateRoot, "startup-journal.jsonl");
    private static string RequirementsStampPath =
        Path.Combine(StateRoot, "requirements.stamp");

    // Sources feeding the desktop bootstrap refresh fingerprint; keep in
    // sync with install.py and scripts/start.py.  The orchestrator's own
    // sources are included so an entry change triggers the same reinstall
    // (which republishes launcher/bin/GPTBridge.Bootstrap.exe).
    private static readonly string[] LauncherBuildSources =
    {
        "src/GPTBridgeLauncher.cpp",
        "src/GPTBridgeLauncher.cs",
        "src/GPTBridge.Bootstrap/Program.cs",
        "src/GPTBridge.Bootstrap/GPTBridge.Bootstrap.csproj",
    };

    private static readonly string[] UiBuildInputRoots = { "src-ui" };
    private static readonly string[] UiBuildInputFiles =
    {
        "vite.config.ts",
        "vite.main.config.ts",
        "vite.templates.config.ts",
        "tsconfig.json",
        "package.json",
        "package-lock.json",
    };
    private static readonly string[] UiBuildToolDirs =
    {
        "Standalone tools/*/src/ui",
        "Standalone tools/*/*/src/ui",
    };
    private static readonly string[] UiBuildToolFiles =
    {
        "Standalone tools/*/manifest.json",
        "Standalone tools/*/*/manifest.json",
    };
    private static readonly string[] UiBuildOutputPaths =
    {
        "dist-ui/main/index.js",
        "dist-ui/main/preload.js",
        "dist-ui/renderer/index.html",
        "dist-ui/templates/main.cjs",
    };

    private static int Main(string[] args)
    {
        var prepareOnly = args.Contains("--prepare-only");
        var forceBuild = args.Contains("--force-build");
        var projectRoot = ArgValue(args, "--project-root");
        var uiHost = ArgValue(args, "--ui-host");
        if (string.IsNullOrEmpty(projectRoot))
        {
            projectRoot = ProjectRoot;
        }
        else
        {
            ApplyProjectRoot(projectRoot);
        }

        ApplyRuntimeContextEnvironment();
        Environment.SetEnvironmentVariable(
            "GPTBRIDGE_STATE_ROOT", projectRoot);
        Environment.SetEnvironmentVariable(
            "GPTBRIDGE_WORKSPACE_ROOT", WorkspaceRoot);
        Environment.SetEnvironmentVariable(
            "GPTBRIDGE_PROJECT_ROOT", WorkspaceRoot);
        Environment.SetEnvironmentVariable("NODE_ENV", "production");

        // Real named mutex — one launcher preparation at a time.
        using var mutex = new Mutex(initiallyOwned: true, MutexName,
            out bool createdNew);
        var acquired = createdNew || mutex.WaitOne(0);
        if (!acquired)
        {
            WriteLauncherStatus(
                "Another launcher preparation is already running.");
            return 0;
        }

        try
        {
            return Launch(projectRoot, prepareOnly, forceBuild, uiHost);
        }
        catch (Exception error)
        {
            WriteStartupJournal("launcher.failed",
                new JsonObject { ["message"] = error.Message });
            var message = $"{AppDisplayName} launch failed: {error.Message}";
            WriteLauncherStatus(message);
            ShowLauncherError(message);
            return 1;
        }
        finally
        {
            mutex.ReleaseMutex();
        }
    }

    private static int Launch(
        string projectRoot, bool prepareOnly, bool forceBuild,
        string uiHostArg)
    {
        LoadUserEnvVars();
        Directory.CreateDirectory(StateRoot);

        var migration = RunMigrationAutostart();
        WriteLauncherStatus(
            "Migration autostart: "
            + $"history={migration["history_rows"]} "
            + $"forward={migration["forward_count"]} "
            + $"legacy_gap={migration["legacy_gap_count"]} "
            + $"applied={migration["applied_count"]} "
            + $"error={migration["error"]}");

        var launchStarted = DateTime.UtcNow;
        WriteLauncherStatus(
            $"Launcher start. ProjectRoot={projectRoot} "
            + $"PrepareOnly={prepareOnly} ForceBuild={forceBuild}");
        WriteStartupJournal("launcher.start",
            new JsonObject { ["projectRoot"] = projectRoot });
        RefreshDesktopLauncher();

        // A618/A621/A625: the Rust+Tauri desktop host replaces Electron
        // (MIGRATION_ONLY).  The host is resolved before runtime prep so
        // the Electron toolchain is skipped entirely on the Tauri path.
        var uiHost = ResolveUiHost(uiHostArg);
        WriteStartupJournal("launcher.ui-host.selected",
            new JsonObject { ["host"] = uiHost });

        var electronExe = "";
        if (uiHost == "electron")
        {
            electronExe = EnsureNodeRuntime();
        }
        EnsurePythonRuntime();
        EnsureUiBuild(forceBuild);

        WriteStartupJournal("launcher.phase.prepare.done", new JsonObject
        {
            ["total_ms"] =
                (long)(DateTime.UtcNow - launchStarted).TotalMilliseconds,
        });

        if (prepareOnly)
        {
            WriteLauncherStatus("Preparation complete (build-only).");
            return 0;
        }

        if (uiHost == "tauri")
        {
            return LaunchTauriHost();
        }
        return LaunchElectronHost(electronExe);
    }

    private static int LaunchElectronHost(string electronExe)
    {
        var mainEntry = Path.Combine(
            ProjectRoot, "dist-ui", "main", "index.js");
        if (!File.Exists(mainEntry))
        {
            throw new InvalidOperationException(
                $"Production main entry is missing: {mainEntry}");
        }

        Environment.SetEnvironmentVariable("GPTBRIDGE_SOURCE_PRODUCTION", "1");
        Environment.SetEnvironmentVariable("GPTBRIDGE_MANAGE_BACKEND", "1");
        Environment.SetEnvironmentVariable(
            "GPTBRIDGE_WORKSPACE_ROOT", WorkspaceRoot);
        Environment.SetEnvironmentVariable(
            "GPTBRIDGE_PROJECT_ROOT", WorkspaceRoot);

        WriteLauncherStatus("Launching source-production Electron runtime.");
        WriteStartupJournal("launcher.phase.electron.start", null);

        var process = StartHiddenProcess(electronExe, new[] { mainEntry },
            ProjectRoot);
        WaitForEarlyExit(process);

        if (process.HasExited)
        {
            if (process.ExitCode != 0)
            {
                WriteStartupJournal("launcher.electron.exited",
                    new JsonObject { ["code"] = process.ExitCode });
                throw new InvalidOperationException(
                    "Electron exited during startup with code "
                    + process.ExitCode + ".");
            }
            WriteLauncherStatus(
                "Electron handed off to the running instance.");
            WriteStartupJournal("launcher.electron.handoff", null);
        }
        else
        {
            WriteLauncherStatus(
                $"Electron startup accepted. PID={process.Id}");
            WriteStartupJournal("launcher.electron.accepted",
                new JsonObject { ["pid"] = process.Id });
        }

        WriteLauncherStatus(
            "Main system UI launched; Electron Main is starting "
            + "the governed backend.");
        return 0;
    }

    // ------------------------------------------------------------------
    // desktop host selection (A618/A621/A625: Electron MIGRATION_ONLY)
    // ------------------------------------------------------------------

    /// <summary>
    /// Resolve the desktop host for this launch.  Precedence:
    /// <c>--ui-host</c> arg → <c>GPTBRIDGE_UI_HOST</c> env →
    /// <c>launcher/state/ui-host.json</c> → default <c>electron</c>.
    /// The default stays Electron until embedded-browser ops are validated
    /// on a healthy runtime (launcher note in AGENTS.md); writing
    /// <c>{"host":"tauri"}</c> to the marker flips every subsequent launch
    /// without rebuilding the launcher.
    /// </summary>
    private static string ResolveUiHost(string uiHostArg)
    {
        var normalized = NormalizeUiHost(uiHostArg);
        if (normalized is not null)
        {
            return normalized;
        }
        normalized = NormalizeUiHost(
            Environment.GetEnvironmentVariable("GPTBRIDGE_UI_HOST"));
        if (normalized is not null)
        {
            return normalized;
        }
        try
        {
            var payload = JsonNode.Parse(
                File.ReadAllText(
                    Path.Combine(StateRoot, "ui-host.json")))?.AsObject();
            normalized = NormalizeUiHost(
                payload?["host"]?.GetValue<string>());
            if (normalized is not null)
            {
                return normalized;
            }
        }
        catch (Exception)
        {
            // Missing/invalid marker — keep the migration default.
        }
        return "electron";
    }

    private static string? NormalizeUiHost(string? value)
    {
        var host = (value ?? "").Trim().ToLowerInvariant();
        return host is "tauri" or "electron" ? host : null;
    }

    private static int LaunchTauriHost()
    {
        var shellExe = Path.Combine(
            ProjectRoot, "src-tauri", "target", "release",
            "gptbridge-shell.exe");
        if (!File.Exists(shellExe))
        {
            // The shell binary is a build artifact, not a governed source —
            // never leave the user without a UI: degrade to the migration
            // host instead of hard-failing the launch.
            WriteLauncherStatus(
                "gptbridge-shell.exe not found; falling back to Electron.");
            WriteStartupJournal("launcher.ui-host.fallback",
                new JsonObject { ["missing"] = shellExe });
            return LaunchElectronHost(EnsureNodeRuntime());
        }

        Environment.SetEnvironmentVariable("GPTBRIDGE_SOURCE_PRODUCTION", "1");
        Environment.SetEnvironmentVariable("GPTBRIDGE_MANAGE_BACKEND", "1");
        Environment.SetEnvironmentVariable(
            "GPTBRIDGE_WORKSPACE_ROOT", WorkspaceRoot);
        Environment.SetEnvironmentVariable(
            "GPTBRIDGE_PROJECT_ROOT", WorkspaceRoot);

        WriteLauncherStatus("Launching Tauri desktop host (gptbridge-shell).");
        WriteStartupJournal("launcher.phase.tauri.start", null);

        var process = StartHiddenProcess(
            shellExe, Array.Empty<string>(), ProjectRoot);
        WaitForEarlyExit(process);

        if (process.HasExited)
        {
            if (process.ExitCode != 0)
            {
                WriteStartupJournal("launcher.tauri.exited",
                    new JsonObject { ["code"] = process.ExitCode });
                // A dead shell means no UI at all — degrade to the still-
                // supported migration host rather than a failed launch.
                WriteLauncherStatus(
                    $"gptbridge-shell exited ({process.ExitCode}); "
                    + "falling back to Electron.");
                WriteStartupJournal("launcher.ui-host.fallback",
                    new JsonObject { ["exit"] = process.ExitCode });
                return LaunchElectronHost(EnsureNodeRuntime());
            }
            WriteLauncherStatus(
                "Tauri shell handed off to the running instance.");
            WriteStartupJournal("launcher.tauri.handoff", null);
        }
        else
        {
            WriteLauncherStatus(
                $"Tauri shell startup accepted. PID={process.Id}");
            WriteStartupJournal("launcher.tauri.accepted",
                new JsonObject { ["pid"] = process.Id });
        }

        WriteLauncherStatus(
            "Main system UI launched; gptbridge-shell is supervising "
            + "the governed backend.");
        return 0;
    }

    /// Poll for an early exit so a host that crashes during startup is
    /// caught truthfully — returns as soon as the process exits; a healthy
    /// long-running host waits the full deadline (3 s vs the original
    /// fixed 800 ms, which missed crashes past that window).
    private static void WaitForEarlyExit(Process process)
    {
        var deadline = DateTime.UtcNow.AddSeconds(3);
        while (DateTime.UtcNow < deadline)
        {
            if (process.WaitForExit(250))
            {
                return;
            }
        }
    }

    // ------------------------------------------------------------------
    // environment
    // ------------------------------------------------------------------

    private static void ApplyRuntimeContextEnvironment()
    {
        Environment.SetEnvironmentVariable(RuntimeContextEnv, "1");
    }

    private static void LoadUserEnvVars()
    {
        // The Windows user environment is authoritative for these bindings;
        // a stale inherited process value must not shadow a rotated one.
        foreach (var name in new[]
        {
            "GPTBRIDGE_POSTGRES_DSN",
            "GPTBRIDGE_POSTGRES_ADMIN_DSN",
            "GPTBRIDGE_MODULE_DSNS",
        })
        {
            var value = ReadUserEnvironment(name);
            if (!string.IsNullOrEmpty(value))
            {
                Environment.SetEnvironmentVariable(name, value);
            }
        }
    }

    private static string? ReadUserEnvironment(string name)
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey("Environment");
            return key?.GetValue(name) as string;
        }
        catch (Exception)
        {
            return null;
        }
    }

    // ------------------------------------------------------------------
    // logging / journal
    // ------------------------------------------------------------------

    private static void WriteLauncherStatus(string message)
    {
        var timestamp = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff");
        Console.WriteLine($"[{timestamp}] {message}");
    }

    private static void ShowLauncherError(string message)
    {
        try
        {
            var logRoot = Path.Combine(InstallRoot, "logs");
            Directory.CreateDirectory(logRoot);
            var line = string.Format(
                "[{0:yyyy-MM-dd HH:mm:ss.fff}] {1} ERROR: {2}",
                DateTime.Now, AppDisplayName, message);
            File.AppendAllText(
                Path.Combine(logRoot, "launcher.log"),
                line + Environment.NewLine,
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        }
        catch (Exception error)
        {
            Console.WriteLine(
                $"Warning: Unable to write launcher log: {error.Message}");
        }
    }

    private static void WriteStartupJournal(string evt, JsonObject? payload)
    {
        try
        {
            var record = new JsonObject
            {
                ["event"] = evt,
                ["timestamp"] = DateTime.UtcNow.ToString("o"),
            };
            if (payload is not null)
            {
                foreach (var pair in payload)
                {
                    record[pair.Key] = pair.Value?.DeepClone();
                }
            }
            File.AppendAllText(JournalPath,
                record.ToJsonString() + "\n",
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        }
        catch (Exception)
        {
            // Observability only — never block the launch.
        }
    }

    // ------------------------------------------------------------------
    // process helpers
    // ------------------------------------------------------------------

    private static Process StartHiddenProcess(
        string filePath, IReadOnlyList<string> arguments,
        string workingDirectory)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = filePath,
            WorkingDirectory = workingDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        foreach (var arg in arguments)
        {
            startInfo.ArgumentList.Add(arg);
        }
        // Cmd scripts (npm.cmd etc.) resolve through CreateProcess itself —
        // no manual cmd.exe /c quoting layer needed.
        var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException(
                $"Failed to start process: {filePath}");
        return process;
    }

    private static void InvokeLauncherCommand(
        string filePath, IReadOnlyList<string> arguments,
        string? workingDirectory = null)
    {
        var isScript =
            filePath.EndsWith(".cmd", StringComparison.OrdinalIgnoreCase)
            || filePath.EndsWith(".bat", StringComparison.OrdinalIgnoreCase);
        var startInfo = new ProcessStartInfo
        {
            WorkingDirectory = workingDirectory ?? ProjectRoot,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        if (isScript)
        {
            // Batch files cannot run through CreateProcess directly; route
            // through cmd.exe with a single quoted command line (/s keeps
            // the inner quoting literal).
            var inner = "\"" + filePath + "\""
                + (arguments.Count > 0
                    ? " " + string.Join(" ", arguments.Select(QuoteArg))
                    : "");
            startInfo.FileName = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.System),
                "cmd.exe");
            startInfo.Arguments = "/d /s /c \"" + inner + "\"";
        }
        else
        {
            startInfo.FileName = filePath;
            foreach (var arg in arguments)
            {
                startInfo.ArgumentList.Add(arg);
            }
        }
        WriteLauncherStatus(
            $"Run: {filePath} {string.Join(" ", arguments)}");
        using var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException(
                $"Failed to start process: {filePath}");
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        process.WaitForExit();
        if (process.ExitCode != 0)
        {
            foreach (var line in Tail(stdout.Result + stderr.Result, 30))
            {
                WriteLauncherStatus("  | " + line);
            }
            throw new InvalidOperationException(
                $"Command failed with exit code {process.ExitCode}: "
                + filePath);
        }
    }

    private static string QuoteArg(string arg) =>
        arg.Contains(' ') || arg.Contains('"')
            ? "\"" + arg.Replace("\"", "\\\"") + "\""
            : arg;

    private static IEnumerable<string> Tail(string text, int count)
    {
        var lines = text
            .Replace("\r\n", "\n")
            .Split('\n', StringSplitOptions.RemoveEmptyEntries);
        return lines.Skip(Math.Max(0, lines.Length - count));
    }

    private static string? Which(params string[] names)
    {
        var path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (var name in names)
        {
            foreach (var directory in path.Split(Path.PathSeparator))
            {
                if (string.IsNullOrWhiteSpace(directory))
                {
                    continue;
                }
                var candidate = Path.Combine(directory.Trim(), name);
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
        }
        return null;
    }

    // ------------------------------------------------------------------
    // migration autostart (delegated to the governed Python module)
    // ------------------------------------------------------------------

    private static JsonObject RunMigrationAutostart()
    {
        // The governed MigrationRunner owns the report + optional apply;
        // the entry only invokes it. Fail-open: startup never blocks here.
        var result = new JsonObject
        {
            ["history_rows"] = "-",
            ["forward_count"] = "-",
            ["legacy_gap_count"] = "-",
            ["applied_count"] = "-",
            ["error"] = "-",
        };
        try
        {
            var interpreter = ResolvePythonInterpreter(preferWindowed: false);
            if (interpreter is null)
            {
                result["error"] = "PYTHON_UNAVAILABLE";
                return result;
            }
            var scriptsDir = Path.Combine(LauncherRoot, "scripts");
            // The governed runner resolves shared-layer against the
            // WORKSPACE root (the Python launcher passed project_root —
            // main-system — so its autostart never actually ran; that is
            // a latent upstream bug this port corrects).
            var code = "import sys;"
                + "sys.path.insert(0, sys.argv[2]);"
                + "sys.path.insert(0, sys.argv[3]);"
                + "from migration_autostart import run;"
                + "import json;print(json.dumps(run(sys.argv[1])))";
            var startInfo = new ProcessStartInfo
            {
                FileName = interpreter,
                WorkingDirectory = scriptsDir,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            startInfo.ArgumentList.Add("-c");
            startInfo.ArgumentList.Add(code);
            startInfo.ArgumentList.Add(WorkspaceRoot);
            startInfo.ArgumentList.Add(scriptsDir);
            // workspace root itself — governed packages (governance_rule
            // et al.) import under their workspace-relative names.
            startInfo.ArgumentList.Add(WorkspaceRoot);
            using var process = Process.Start(startInfo);
            if (process is null)
            {
                result["error"] = "SPAWN_FAILED";
                return result;
            }
            var stdout = process.StandardOutput.ReadToEnd();
            process.StandardError.ReadToEnd();
            process.WaitForExit(120_000);

            var line = stdout.Trim().Split('\n')
                .LastOrDefault(l => l.TrimStart().StartsWith('{'));
            if (line is null)
            {
                result["error"] = "NO_REPORT";
                return result;
            }
            var report = JsonNode.Parse(line.Trim())?.AsObject();
            if (report is null)
            {
                result["error"] = "REPORT_UNPARSEABLE";
                return result;
            }
            result["history_rows"] = report["history_rows"]?.GetValue<int>()
                .ToString() ?? "0";
            result["forward_count"] = (report["forward_pending"]
                as JsonArray)?.Count.ToString() ?? "0";
            result["legacy_gap_count"] = report["legacy_gap_count"]
                ?.GetValue<int>().ToString() ?? "0";
            result["applied_count"] = (report["applied"] as JsonArray)
                ?.Count.ToString() ?? "0";
            result["error"] = report["error"]?.GetValue<string>();
            if (string.IsNullOrEmpty(result["error"]?.GetValue<string>()))
            {
                result["error"] = "-";
            }
        }
        catch (Exception error)
        {
            result["error"] = $"{error.GetType().Name}: {error.Message}";
        }
        return result;
    }

    // ------------------------------------------------------------------
    // desktop launcher refresh
    // ------------------------------------------------------------------

    private static string LauncherBuildFingerprint()
    {
        var digest = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        foreach (var relative in LauncherBuildSources)
        {
            var path = Path.Combine(LauncherRoot,
                relative.Replace('/', Path.DirectorySeparatorChar));
            try
            {
                var info = new FileInfo(path);
                digest.AppendData(Encoding.UTF8.GetBytes(
                    $"{relative}:{MtimeNs(info)}:{info.Length}\n"));
            }
            catch (IOException)
            {
                digest.AppendData(Encoding.UTF8.GetBytes(
                    $"{relative}:missing\n"));
            }
            catch (UnauthorizedAccessException)
            {
                digest.AppendData(Encoding.UTF8.GetBytes(
                    $"{relative}:missing\n"));
            }
        }
        return Convert.ToHexString(digest.GetHashAndReset()).ToLowerInvariant();
    }

    private static bool DesktopLauncherNeedsRefresh()
    {
        if (!File.Exists(
            Path.Combine(InstallRoot, "config", "root.txt")))
        {
            return false; // desktop bootstrap not installed
        }
        try
        {
            var payload = JsonNode.Parse(
                File.ReadAllText(LauncherStampPath))?.AsObject();
            return payload?["fingerprint"]?.GetValue<string>()
                != LauncherBuildFingerprint();
        }
        catch (Exception)
        {
            return true;
        }
    }

    private static bool RefreshLockIsActive()
    {
        try
        {
            var age = DateTime.UtcNow
                - File.GetLastWriteTimeUtc(RefreshLockPath);
            return age.TotalSeconds < RefreshLockStaleSeconds;
        }
        catch (IOException)
        {
            return false;
        }
    }

    private static void RefreshDesktopLauncher()
    {
        try
        {
            if (!DesktopLauncherNeedsRefresh() || RefreshLockIsActive())
            {
                return;
            }
            var installScript = Path.Combine(
                LauncherRoot, "scripts", "install.py");
            if (!File.Exists(installScript))
            {
                return;
            }
            var interpreter = ResolvePythonInterpreter(preferWindowed: true)
                ?? throw new InvalidOperationException("python unavailable");
            Directory.CreateDirectory(Path.GetDirectoryName(RefreshLockPath)!);
            File.WriteAllText(RefreshLockPath,
                Environment.ProcessId.ToString(), Encoding.ASCII);
            var startInfo = new ProcessStartInfo
            {
                FileName = interpreter,
                WorkingDirectory = ProjectRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
            };
            startInfo.ArgumentList.Add(installScript);
            Process.Start(startInfo);
            WriteStartupJournal("launcher.refresh.spawned", new JsonObject
            {
                ["sources"] = new JsonArray(
                    LauncherBuildSources
                        .Select(s => JsonValue.Create(s)).ToArray()),
            });
        }
        catch (Exception error)
        {
            WriteStartupJournal("launcher.refresh.failed",
                new JsonObject { ["message"] = error.Message });
        }
    }

    // ------------------------------------------------------------------
    // runtimes + frontend freshness
    // ------------------------------------------------------------------

    private static string EnsureNodeRuntime()
    {
        var electronExe = Path.Combine(
            ProjectRoot, "node_modules", "electron", "dist", "electron.exe");
        var nodeLock = Path.Combine(
            ProjectRoot, "node_modules", ".package-lock.json");
        var projectLock = Path.Combine(ProjectRoot, "package-lock.json");

        var needsInstall = !File.Exists(electronExe)
            || !File.Exists(nodeLock)
            || (File.Exists(projectLock)
                && File.GetLastWriteTimeUtc(projectLock)
                    > File.GetLastWriteTimeUtc(nodeLock));

        if (needsInstall)
        {
            var npm = Which("npm.cmd", "npm")
                ?? throw new InvalidOperationException("npm not found in PATH");
            InvokeLauncherCommand(npm, new[] { "install" }, ProjectRoot);
        }
        if (!File.Exists(electronExe))
        {
            throw new InvalidOperationException(
                "Electron runtime is missing after npm install.");
        }
        return electronExe;
    }

    private static string EnsurePythonRuntime()
    {
        var pythonExe = Path.Combine(
            ProjectRoot, ".venv", "Scripts", "python.exe");
        if (!File.Exists(pythonExe))
        {
            var bootstrap = Which("py.exe");
            if (bootstrap is not null)
            {
                InvokeLauncherCommand(bootstrap,
                    new[] { "-3", "-m", "venv",
                        Path.Combine(ProjectRoot, ".venv") });
            }
            else
            {
                bootstrap = Which("python.exe")
                    ?? throw new InvalidOperationException("Python not found");
                InvokeLauncherCommand(bootstrap,
                    new[] { "-m", "venv",
                        Path.Combine(ProjectRoot, ".venv") });
            }
        }

        var requirements = Path.Combine(ProjectRoot, "requirements.txt");
        var needsRequirements = File.Exists(requirements)
            && (!File.Exists(RequirementsStampPath)
                || File.GetLastWriteTimeUtc(requirements)
                    > File.GetLastWriteTimeUtc(RequirementsStampPath));

        if (needsRequirements)
        {
            InvokeLauncherCommand(pythonExe,
                new[] { "-m", "pip", "install", "-r", requirements });
            File.WriteAllText(RequirementsStampPath,
                DateTime.UtcNow.ToString("o") + "\n", Encoding.ASCII);
        }
        if (!File.Exists(pythonExe))
        {
            throw new InvalidOperationException(
                "Python runtime is missing.");
        }
        return pythonExe;
    }

    private static string? ResolvePythonInterpreter(bool preferWindowed)
    {
        var venv = Path.Combine(ProjectRoot, ".venv", "Scripts",
            preferWindowed ? "pythonw.exe" : "python.exe");
        if (File.Exists(venv))
        {
            return venv;
        }
        return Which("py.exe") ?? Which(
            preferWindowed ? "pythonw.exe" : "python.exe");
    }

    private static IEnumerable<string> IterUiBuildInputs()
    {
        var files = new List<string>();
        foreach (var relative in UiBuildInputRoots)
        {
            var baseDir = Path.Combine(ProjectRoot, relative);
            if (Directory.Exists(baseDir))
            {
                files.AddRange(Directory
                    .EnumerateFiles(baseDir, "*", SearchOption.AllDirectories));
            }
        }
        foreach (var relative in UiBuildInputFiles)
        {
            var candidate = Path.Combine(ProjectRoot, relative);
            if (File.Exists(candidate))
            {
                files.Add(candidate);
            }
        }
        foreach (var pattern in UiBuildToolDirs)
        {
            foreach (var match in GlobDirectories(WorkspaceRoot, pattern))
            {
                files.AddRange(Directory
                    .EnumerateFiles(match, "*", SearchOption.AllDirectories));
            }
        }
        foreach (var pattern in UiBuildToolFiles)
        {
            files.AddRange(GlobFiles(WorkspaceRoot, pattern));
        }
        // Deterministic order — the fingerprint must be stable, not depend
        // on filesystem enumeration order.
        files.Sort(StringComparer.Ordinal);
        return files;
    }

    private static IEnumerable<string> GlobDirectories(string root, string pattern)
    {
        var segments = pattern.Split('/');
        var current = new List<string> { root };
        foreach (var segment in segments)
        {
            var next = new List<string>();
            foreach (var dir in current)
            {
                if (segment == "*")
                {
                    if (Directory.Exists(dir))
                    {
                        next.AddRange(Directory.EnumerateDirectories(dir));
                    }
                }
                else
                {
                    var candidate = Path.Combine(dir, segment);
                    if (Directory.Exists(candidate))
                    {
                        next.Add(candidate);
                    }
                }
            }
            current = next;
        }
        current.Sort(StringComparer.Ordinal);
        return current;
    }

    private static IEnumerable<string> GlobFiles(string root, string pattern)
    {
        var segments = pattern.Split('/');
        var fileSegment = segments[^1];
        var dirs = GlobDirectories(root,
            string.Join('/', segments[..^1]));
        var files = new List<string>();
        foreach (var dir in dirs)
        {
            var candidate = Path.Combine(dir, fileSegment);
            if (File.Exists(candidate))
            {
                files.Add(candidate);
            }
        }
        files.Sort(StringComparer.Ordinal);
        return files;
    }

    private static long MtimeNs(FileSystemInfo info)
    {
        // Python st_mtime_ns equivalent: 100ns FILETIME ticks since
        // 1970-01-01, expressed in nanoseconds.
        return (info.LastWriteTimeUtc.Ticks - 621355968000000000L) * 100L;
    }

    private static string UiBuildFingerprint()
    {
        var digest = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        var utf8 = new UTF8Encoding(
            encoderShouldEmitUTF8Identifier: false,
            throwOnInvalidBytes: false);
        foreach (var path in IterUiBuildInputs())
        {
            try
            {
                var info = new FileInfo(path);
                var relative = Path
                    .GetRelativePath(WorkspaceRoot, path)
                    .Replace(Path.DirectorySeparatorChar, '/');
                digest.AppendData(utf8.GetBytes(
                    $"{relative}:{info.Length}:{MtimeNs(info)}\n"));
            }
            catch (IOException)
            {
                continue;
            }
            catch (UnauthorizedAccessException)
            {
                continue;
            }
        }
        return Convert.ToHexString(digest.GetHashAndReset()).ToLowerInvariant();
    }

    private static bool UiBuildIsCurrent()
    {
        try
        {
            var payload = JsonNode.Parse(
                File.ReadAllText(UiBuildStampPath))?.AsObject();
            if (payload?["fingerprint"]?.GetValue<string>()
                != UiBuildFingerprint())
            {
                return false;
            }
        }
        catch (Exception)
        {
            return false;
        }
        return UiBuildOutputPaths.All(relative =>
            File.Exists(Path.Combine(ProjectRoot,
                relative.Replace('/', Path.DirectorySeparatorChar))));
    }

    private static void EnsureUiBuild(bool force)
    {
        if (!force && UiBuildIsCurrent())
        {
            return;
        }
        var npm = Which("npm.cmd", "npm")
            ?? throw new InvalidOperationException(
                "npm not found in PATH; cannot build the frontend from source");
        WriteLauncherStatus(
            "Frontend sources changed; rebuilding (npm run build:app)...");
        WriteStartupJournal("launcher.ui.build.start",
            new JsonObject { ["forced"] = force });
        var stopwatch = Stopwatch.StartNew();
        InvokeLauncherCommand(npm, new[] { "run", "build:app" }, ProjectRoot);
        foreach (var relative in UiBuildOutputPaths)
        {
            var output = Path.Combine(ProjectRoot,
                relative.Replace('/', Path.DirectorySeparatorChar));
            if (!File.Exists(output))
            {
                throw new InvalidOperationException(
                    $"Frontend build output is missing: {output}");
            }
        }
        File.WriteAllText(UiBuildStampPath,
            new JsonObject
            {
                ["fingerprint"] = UiBuildFingerprint(),
                ["built_at"] = DateTime.UtcNow.ToString("o"),
            }.ToJsonString() + "\n",
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        WriteStartupJournal("launcher.ui.build.done", new JsonObject
        {
            ["duration_ms"] = stopwatch.ElapsedMilliseconds,
        });
        WriteLauncherStatus("Frontend rebuilt from source.");
    }

    private static string ArgValue(string[] args, string name)
    {
        for (var index = 0; index < args.Length - 1; index++)
        {
            if (args[index] == name)
            {
                return args[index + 1];
            }
        }
        return "";
    }
}
