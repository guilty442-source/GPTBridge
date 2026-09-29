using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Win32;

namespace GPTBridge.Bootstrap;

internal static partial class Program
{

    private static JsonObject RunMigrationAutostart()
    {
        // The governed MigrationRunner (migration_autostart.py) was retired
        // with the Python lane (B167/B38) and no native owner has been
        // designated for the lane yet. Fail-open exactly as before —
        // startup never blocks here — but report the honest state instead
        // of hunting for an interpreter that can no longer exist.
        return new JsonObject
        {
            ["history_rows"] = "-",
            ["forward_count"] = "-",
            ["legacy_gap_count"] = "-",
            ["applied_count"] = "-",
            ["error"] = "MIGRATION_RUNNER_RETIRED",
        };
    }

    // ------------------------------------------------------------------
    // desktop launcher refresh
    // ------------------------------------------------------------------

}
