using System.Text.Json;
using System.Text.Json.Nodes;

namespace GPTBridge.GitAutomation;

internal static partial class PushGate
{
    private static double NewestSourceTime(string root)
    {
        double newest = 0;
        foreach (var rel in DepRoots)
        {
            var depRoot = Rel(root, rel);
            if (!Directory.Exists(depRoot))
                continue;
            foreach (var file in Directory.EnumerateFiles(
                         depRoot, "*", SearchOption.AllDirectories))
            {
                if (!CodeSuffixes.Contains(Path.GetExtension(file)))
                    continue;
                var time = new DateTimeOffset(
                    File.GetLastWriteTimeUtc(file)).ToUnixTimeSeconds();
                if (time > newest)
                    newest = time;
            }
        }
        return newest;
    }

    private static List<string> SuiteExes(string binDir) =>
        Directory.Exists(binDir)
            ? Directory.EnumerateFiles(binDir, "*_suite.exe")
                .Where(p => !Path.GetFileName(p).StartsWith('_'))
                .OrderBy(p => p, StringComparer.Ordinal).ToList()
            : new List<string>();

    private static JsonObject? SuiteManifest(string binDir)
    {
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(
                Path.Combine(binDir, "suite-manifest.json")));
            return node as JsonObject;
        }
        catch (IOException) { return null; }
        catch (JsonException) { return null; }
    }

    private static List<string> StaleArtifacts(
        string binDir, JsonObject? manifest)
    {
        var stale = new List<string>();
        if (manifest?["suites"] is not JsonArray suites)
            return stale;
        foreach (var row in suites.OfType<JsonObject>())
        {
            var exe = row["exe"]?.GetValue<string>() ?? "";
            var expected =
                row["sha256"]?.GetValue<string>()?.ToLowerInvariant() ?? "";
            if (exe.Length == 0)
                continue;
            var path = Path.Combine(binDir, exe);
            string actual;
            try { actual = Canon.Sha256File(path); }
            catch (IOException) { actual = ""; }
            if (expected.Length == 0 || actual != expected)
                stale.Add(row["name"]?.GetValue<string>() ?? exe);
        }
        return stale;
    }

    private static string? OrchestratorExe(string root)
    {
        foreach (var rel in new[]
                 { OrchestratorReleaseRelative, OrchestratorDebugRelative })
        {
            var path = Rel(root, rel);
            if (File.Exists(path))
                return path;
        }
        return null;
    }

    private static bool OrchestratorStale(string root, string exe)
    {
        var exeTime = File.GetLastWriteTimeUtc(exe);
        foreach (var rel in new[]
                 { OrchestratorSourceRelative, OrchestratorProjectRelative })
        {
            var src = Rel(root, rel);
            if (File.Exists(src)
                && File.GetLastWriteTimeUtc(src) > exeTime)
                return true;
        }
        return false;
    }

    private static string? EnsureOrchestrator(string root, double timeoutS)
    {
        var exe = OrchestratorExe(root);
        if (exe is not null && !OrchestratorStale(root, exe))
            return exe;
        var run = Git.Exec("dotnet", root, new[]
        {
            "build", Rel(root, OrchestratorProjectRelative),
            "-c", "Release", "--nologo", "-v", "q",
        }, (int)(timeoutS * 1000));
        if (run.Code != 0)
            return null;
        return OrchestratorExe(root);
    }

    private static bool BinariesStale(string root, List<string> exes) =>
        exes.Count == 0
        || NewestSourceTime(root) > new DateTimeOffset(
            exes.Select(File.GetLastWriteTimeUtc).Min())
            .ToUnixTimeSeconds();

    private static string StatePath(string root) =>
        Path.Combine(Git.CommonDir(root), StateFilename);

    private static JsonObject ReadState(string path)
    {
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(path));
            return node as JsonObject ?? new JsonObject();
        }
        catch (IOException) { return new JsonObject(); }
        catch (JsonException) { return new JsonObject(); }
    }

    private static void WriteState(string path, JsonObject state)
    {
        try
        {
            using var document = JsonDocument.Parse(state.ToJsonString());
            Canon.WriteJsonAtomic(
                path, Canon.Indented(document.RootElement) + "\n");
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }
}
