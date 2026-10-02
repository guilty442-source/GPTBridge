using GPTBridge.GitAutomation;
using System.Diagnostics;

var root = Path.GetFullPath(args[0]);
var executable = Git.ResolveExecutable();
if (OperatingSystem.IsWindows() && executable.Contains("\\cmd\\", StringComparison.OrdinalIgnoreCase))
    throw new Exception("Git launcher was not bypassed");
foreach (var command in new[] {
    new[] { "worktree", "list", "--porcelain" },
    new[] { "status", "--porcelain=v1" },
    new[] { "ls-files", "--others", "--exclude-standard" },
    new[] { "rev-parse", "HEAD" } })
    for (var i = 0; i < 5; i++)
    {
        var result = Git.Run(root, command, 10000);
        if (result.Code != 0 || result.TimedOut) throw new Exception(result.Stderr);
    }
var scratch = Path.Combine(Path.GetTempPath(), "gptbridge-git-check-" + Guid.NewGuid());
Directory.CreateDirectory(scratch);
foreach (var command in new[] {
    new[] { "init" }, new[] { "config", "user.name", "Regression" },
    new[] { "config", "user.email", "regression@example.invalid" } })
    if (Git.Run(scratch, command).Code != 0) throw new Exception("Scratch init failed");
File.WriteAllText(Path.Combine(scratch, "proof.txt"), "integration proof\n");
if (Git.Run(scratch, new[] { "add", "--", "proof.txt" }).Code != 0
    || Git.Run(scratch, new[] { "commit", "-m", "regression proof", "--", "proof.txt" }).Code != 0
    || Git.Run(scratch, new[] { "status", "--porcelain" }).Stdout.Length != 0)
    throw new Exception("Add/commit regression failed");
var timer = Stopwatch.StartNew();
var timeout = Git.Exec("powershell.exe", scratch,
    new[] { "-NoProfile", "-Command", "Start-Sleep -Seconds 20" }, 250);
if (!timeout.TimedOut || timer.Elapsed.TotalSeconds > 5) throw new Exception("Timeout was not bounded");
Console.WriteLine($"PASS: 20 live queries, scratch add/commit, bounded timeout; executable={executable}");
