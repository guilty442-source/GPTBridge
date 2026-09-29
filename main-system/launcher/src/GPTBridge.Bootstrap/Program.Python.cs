using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Win32;

namespace GPTBridge.Bootstrap;

internal static partial class Program
{

    // B167/B38: Python runtime retired — venv provisioning and pip
    // installs are no longer part of the launch path.  This resolver
    // remains only for the fail-open delegated lanes (migration
    // autostart, desktop-launcher refresh) that degrade honestly to a
    // typed unavailable when no interpreter exists.
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

}
