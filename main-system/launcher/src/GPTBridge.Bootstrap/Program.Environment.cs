using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Win32;

namespace GPTBridge.Bootstrap;

internal static partial class Program
{

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

}
