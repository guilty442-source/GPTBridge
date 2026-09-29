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
// Python lane fully retired (B167/B38): the renderer build and the
// desktop installer run in-process in this assembly; the governed
// MigrationRunner lane reports runner-retired until a native owner is
// designated — the entry orchestrates, it does not re-implement governed
// internals.

using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Win32;

namespace GPTBridge.Bootstrap;

internal static partial class Program
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

    // Sources feeding the desktop bootstrap refresh fingerprint; keep in
    // sync with the native install lane (Program.Install.cs).  The
    // orchestrator's own
    // sources are included so an entry change triggers the same reinstall
    // (which republishes launcher/bin/GPTBridge.Bootstrap.exe).
    private static readonly string[] LauncherBuildSources =
    {
        "src/GPTBridgeLauncher.cpp",
        "src/GPTBridgeLauncher.cs",
        "src/GPTBridge.Bootstrap/Program.cs",
        "src/GPTBridge.Bootstrap/Program.Install.cs",
        "src/GPTBridge.Bootstrap/Program.RendererBuild.cs",
        "src/GPTBridge.Bootstrap/GPTBridge.Bootstrap.csproj",
    };

    private static readonly string[] UiBuildInputRoots = { "src-ui" };
    private static readonly string[] UiBuildInputFiles =
    {
        "../.swcrc",
        "launcher/src/GPTBridge.Bootstrap/Program.RendererBuild.cs",
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
        "dist-ui/renderer/index.html",
        "dist-ui/renderer/assets/index.js",
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

        // Detached self-install spawned by RefreshDesktopLauncher: runs
        // outside the launch mutex (the refresh lock single-flights it),
        // so it is never blocked by a parent holding the mutex.
        if (args.Contains("--install-desktop"))
        {
            LoadUserEnvVars();
            Directory.CreateDirectory(StateRoot);
            return InstallDesktopLauncher();
        }

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

        // A618/A621/A625: the Rust+Tauri desktop host is the only host —
        // Electron is retired.  The --ui-host/GPTBRIDGE_UI_HOST/marker
        // chain is still honoured for compatibility, but any non-"tauri"
        // value resolves to the Tauri host.
        var uiHost = ResolveUiHost(uiHostArg);
        WriteStartupJournal("launcher.ui-host.selected",
            new JsonObject { ["host"] = uiHost });

        // Python runtime retired (B167/B38): no interpreter provisioning
        // on the launch path — backend + renderer build are native.
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

        return LaunchTauriHost();
    }

}
