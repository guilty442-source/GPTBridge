using System.Diagnostics;
using System.Text.Json;

// The copied apphost is a native stand-in; never starts the real system.
if (Path.GetFileName(Environment.ProcessPath) == "GPTBridge.Bootstrap.exe")
{
    var index = Array.IndexOf(args, "--project-root");
    if (index < 0 || index + 1 >= args.Length) return 2;
    File.WriteAllText(Path.Combine(Path.GetFullPath(args[index + 1]), "entry-result.json"), JsonSerializer.Serialize(args));
    return args.Contains("--install-desktop") ? 7 : 0;
}
var repo = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "../../../../../.."));
var root = Path.Combine(Path.GetTempPath(), "GPTBridge entry 空白 " + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(root);
var wrappers = new[] { "build_exe.bat", "run-interface.bat", "啟動主程式.bat", "自動化工具.bat", "start-hidden.vbs", "啟動主程式.vbs", "自動化工具.vbs" };
var passed = 0;
void Require(bool ok, string name)
{
    if (!ok) throw new Exception("LAUNCHER_ENTRY_FAILED:" + name);
    passed++;
}
async Task<int> Run(string wrapper)
{
    var vbs = wrapper.EndsWith(".vbs");
    var start = new ProcessStartInfo(vbs ? "cscript.exe" : "cmd.exe")
    {
        WorkingDirectory = Path.GetTempPath(), UseShellExecute = false,
        CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
    };
    if (vbs) { start.ArgumentList.Add("//Nologo"); start.ArgumentList.Add(Path.Combine(root, wrapper)); }
    else { start.Arguments = "/d /c \"\"" + Path.Combine(root, wrapper) + "\"\""; }
    using var process = Process.Start(start)!;
    using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
    var stdout = process.StandardOutput.ReadToEndAsync(timeout.Token);
    var stderr = process.StandardError.ReadToEndAsync(timeout.Token);
    try { await process.WaitForExitAsync(timeout.Token); await Task.WhenAll(stdout, stderr); }
    catch { if (!process.HasExited) process.Kill(entireProcessTree: true); throw; }
    if (File.Exists(Path.Combine(root, "launcher", "bin", "GPTBridge.Bootstrap.exe")))
        Require(string.IsNullOrWhiteSpace(await stdout) && string.IsNullOrWhiteSpace(await stderr), "clean-wrapper-output:" + wrapper);
    return process.ExitCode;
}
try
{
    foreach (var wrapper in wrappers)
        File.Copy(Path.Combine(repo, "main-system", wrapper), Path.Combine(root, wrapper));
    foreach (var wrapper in wrappers)
        Require(await Run(wrapper) == 1, "missing-native-entry:" + wrapper);
    var bin = Path.Combine(root, "launcher", "bin");
    Directory.CreateDirectory(bin);
    foreach (var file in Directory.GetFiles(AppContext.BaseDirectory))
        File.Copy(file, Path.Combine(bin, Path.GetFileName(file)));
    File.Copy(Environment.ProcessPath!, Path.Combine(bin, "GPTBridge.Bootstrap.exe"));
    var receipt = Path.Combine(root, "entry-result.json");
    foreach (var wrapper in wrappers)
    {
        if (File.Exists(receipt)) File.Delete(receipt);
        Require(await Run(wrapper) == (wrapper == "build_exe.bat" ? 7 : 0), "exit-code:" + wrapper);
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while (!File.Exists(receipt) && DateTime.UtcNow < deadline) await Task.Delay(50);
        Require(File.Exists(receipt), "native-launch:" + wrapper);
        var observed = JsonSerializer.Deserialize<string[]>(File.ReadAllText(receipt))!;
        Require(Path.GetFullPath(observed[1]) == Path.GetFullPath(root), "root-binding:" + wrapper);
        Require(observed.Contains("--install-desktop") == (wrapper == "build_exe.bat"), "mode:" + wrapper);
    }
    Console.WriteLine(JsonSerializer.Serialize(new { artifact = "native-launcher-entry-regression", passed, failed = 0 }));
    return 0;
}
finally { Directory.Delete(root, recursive: true); }
