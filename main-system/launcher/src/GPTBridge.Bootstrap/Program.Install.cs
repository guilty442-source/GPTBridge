using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.Bootstrap;

internal static partial class Program
{
    // install.py retired with the Python lane (B167/B38): the desktop
    // installer contract is now owned in-process by the bootstrap itself.
    // Identical artifacts as the Python installer produced:
    //   * launcher\bin\GPTBridge.Bootstrap.exe — dotnet publish of this
    //     project (framework-dependent, win-x64, R2R)
    //   * %LOCALAPPDATA%\GPTBridgeLauncher\bin\專案程式庫.exe — outer exe,
    //     MSVC cl.exe on GPTBridgeLauncher.cpp preferred, csc.exe on
    //     GPTBridgeLauncher.cs as the no-MSVC fallback
    //   * %LOCALAPPDATA%\GPTBridgeLauncher\config\root.txt — project root
    //   * %LOCALAPPDATA%\GPTBridgeLauncher\state\launcher-stamp.json —
    //     {fingerprint, built_at, sources}
    //   * Desktop hardlink 專案程式庫.exe -> the installed outer exe
    private const string DesktopLauncherExeName = "專案程式庫.exe";

    [DllImport("kernel32.dll", SetLastError = true,
        CharSet = CharSet.Unicode)]
    private static extern bool CreateHardLinkW(
        string lpFileName, string lpExistingFileName,
        IntPtr lpSecurityAttributes);

    /// --install-desktop entry: runs without the launch mutex (the refresh
    /// lock single-flights installs), so a detached reinstall spawned from
    /// inside Launch() is never blocked by the parent holding the mutex.
    private static int InstallDesktopLauncher()
    {
        try
        {
            WriteStartupJournal("launcher.install.started", new JsonObject());
            PublishBootstrap();
            InstallOuterExe();
            WriteInstallMarkers();
            LinkDesktopEntry();
            WriteStartupJournal("launcher.install.completed",
                new JsonObject
                {
                    ["sources"] = new JsonArray(
                        LauncherBuildSources
                            .Select(s => JsonValue.Create(s)).ToArray()),
                });
            return 0;
        }
        catch (Exception error)
        {
            WriteStartupJournal("launcher.install.failed",
                new JsonObject { ["message"] = error.Message });
            return 1;
        }
    }

    private static void PublishBootstrap()
    {
        var staging = Path.Combine(LauncherRoot, "bin.staging");
        var binDir = Path.Combine(LauncherRoot, "bin");
        var csproj = Path.Combine(LauncherRoot, "src",
            "GPTBridge.Bootstrap", "GPTBridge.Bootstrap.csproj");
        if (Directory.Exists(staging))
        {
            Directory.Delete(staging, recursive: true);
        }
        var dotnet = Which("dotnet.exe") ?? "dotnet";
        InvokeLauncherCommand(dotnet, new[]
        {
            "publish", csproj, "-c", "Release", "-r", "win-x64",
            "--self-contained", "false", "-o", staging,
        });
        Directory.CreateDirectory(binDir);
        foreach (var staged in Directory.GetFiles(staging))
        {
            var name = Path.GetFileName(staged);
            var dest = Path.Combine(binDir, name);
            try
            {
                File.Copy(staged, dest, overwrite: true);
            }
            catch (IOException)
            {
                MoveLockedAside(dest);
                File.Copy(staged, dest, overwrite: true);
            }
            catch (UnauthorizedAccessException)
            {
                MoveLockedAside(dest);
                File.Copy(staged, dest, overwrite: true);
            }
        }
        Directory.Delete(staging, recursive: true);
    }

    /// The running bootstrap locks its own exe/dll images; NTFS allows
    /// renaming a running image, so move the locked file aside (and mark it
    /// delete-pending) before dropping the replacement into place.
    private static void MoveLockedAside(string path)
    {
        if (!File.Exists(path))
        {
            return;
        }
        var aside = path + ".replaced-"
            + DateTime.UtcNow.ToString("yyyyMMddHHmmss");
        File.Move(path, aside);
        try
        {
            File.Delete(aside);
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
    }

    private static void InstallOuterExe()
    {
        var binDir = Path.Combine(InstallRoot, "bin");
        Directory.CreateDirectory(binDir);
        var output = Path.Combine(binDir, DesktopLauncherExeName);
        var cpp = Path.Combine(LauncherRoot, "src", "GPTBridgeLauncher.cpp");
        var cs = Path.Combine(LauncherRoot, "src", "GPTBridgeLauncher.cs");
        if (TryCompileOuterExeMsvc(cpp, output))
        {
            return;
        }
        CompileOuterExeCsc(cs, output);
    }

    private static bool TryCompileOuterExeMsvc(string source, string output)
    {
        var vcvars = FindVcvars64();
        if (vcvars is null)
        {
            return false;
        }
        var cmd = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.System),
            "cmd.exe");
        // Raw command line (not ArgumentList): the whole /c payload must
        // sit inside one outer quote pair — without /s, cmd strips the
        // outer quotes and evaluates `call "vcvars" && cl ...` correctly;
        // ArgumentList-style escaping leaves the quotes in place and cmd
        // tries to execute the quoted vcvars path as a command.
        var arguments = "/d /c \"call \"" + vcvars + "\" >nul && "
            + "cl /nologo /O2 /EHsc /std:c++17 /utf-8 "
            + "\"" + source + "\" "
            + "/Fe:\"" + output + "\"\"";
        var startInfo = new ProcessStartInfo
        {
            FileName = cmd,
            Arguments = arguments,
            WorkingDirectory = LauncherRoot,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        try
        {
            using var process = Process.Start(startInfo);
            if (process is null)
            {
                return false;
            }
            var stdout = process.StandardOutput.ReadToEndAsync();
            var stderr = process.StandardError.ReadToEndAsync();
            process.WaitForExit();
            if (process.ExitCode == 0 && File.Exists(output))
            {
                return true;
            }
            WriteStartupJournal("launcher.install.msvc.failed",
                new JsonObject
                {
                    ["exit_code"] = process.ExitCode.ToString(),
                    ["tail"] = string.Join("\n",
                        Tail(stdout.Result + stderr.Result, 15)),
                });
            return false;
        }
        catch (Exception error)
        {
            WriteStartupJournal("launcher.install.msvc.failed",
                new JsonObject { ["message"] = error.Message });
            return false;
        }
    }

    private static string? FindVcvars64()
    {
        var vswhere = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
            "Microsoft Visual Studio", "Installer", "vswhere.exe");
        if (!File.Exists(vswhere))
        {
            return null;
        }
        var startInfo = new ProcessStartInfo
        {
            FileName = vswhere,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        foreach (var arg in new[]
        {
            "-latest", "-products", "*", "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property", "installationPath",
        })
        {
            startInfo.ArgumentList.Add(arg);
        }
        using var process = Process.Start(startInfo);
        if (process is null)
        {
            return null;
        }
        var installPath = process.StandardOutput.ReadToEnd().Trim();
        process.WaitForExit(30_000);
        if (string.IsNullOrEmpty(installPath))
        {
            return null;
        }
        var vcvars = Path.Combine(installPath, "VC", "Auxiliary",
            "Build", "vcvars64.bat");
        return File.Exists(vcvars) ? vcvars : null;
    }

    private static void CompileOuterExeCsc(string source, string output)
    {
        var csc = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.Windows),
            "Microsoft.NET", "Framework64", "v4.0.30319", "csc.exe");
        InvokeLauncherCommand(csc, new[]
        {
            "/nologo", "/target:winexe", "/platform:anycpu",
            "/utf8output", "/out:" + output, source,
        });
    }

    private static void WriteInstallMarkers()
    {
        var configDir = Path.Combine(InstallRoot, "config");
        var stateDir = Path.Combine(InstallRoot, "state");
        Directory.CreateDirectory(configDir);
        Directory.CreateDirectory(stateDir);
        // UTF-8 with BOM — the outer launcher's root.txt reader skips a
        // leading BOM explicitly (installed files were written that way).
        File.WriteAllText(Path.Combine(configDir, "root.txt"),
            ProjectRoot, new UTF8Encoding(encoderShouldEmitUTF8Identifier: true));
        var stamp = new JsonObject
        {
            ["fingerprint"] = LauncherBuildFingerprint(),
            ["built_at"] = DateTime.UtcNow.ToString("o"),
            ["sources"] = new JsonArray(
                LauncherBuildSources
                    .Select(s => JsonValue.Create(s)).ToArray()),
        };
        File.WriteAllText(LauncherStampPath,
            stamp.ToJsonString(), new UTF8Encoding(false));
    }

    private static void LinkDesktopEntry()
    {
        var target = Path.Combine(InstallRoot, "bin", DesktopLauncherExeName);
        var link = Path.Combine(
            Environment.GetFolderPath(
                Environment.SpecialFolder.DesktopDirectory),
            DesktopLauncherExeName);
        try
        {
            if (File.Exists(link))
            {
                File.Delete(link);
            }
            if (!CreateHardLinkW(link, target, IntPtr.Zero))
            {
                WriteStartupJournal("launcher.install.link.failed",
                    new JsonObject
                    {
                        ["win32"] = Marshal.GetLastWin32Error()
                            .ToString(),
                    });
            }
        }
        catch (Exception error)
        {
            WriteStartupJournal("launcher.install.link.failed",
                new JsonObject { ["message"] = error.Message });
        }
    }
}
