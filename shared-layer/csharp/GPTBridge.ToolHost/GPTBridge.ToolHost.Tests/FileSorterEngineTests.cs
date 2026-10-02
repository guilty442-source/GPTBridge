using System.Text.Json;
using System.Text.Json.Nodes;
using GPTBridge.ToolHost.App;

namespace GPTBridge.ToolHost.Tests;

/// <summary>
/// file-sorter engine contract tests — exercises the arg-typed
/// toolbox_run_tool surface on a scratch target dir with
/// FILE_SORTER_STATE_ROOT pointed at a per-test temp root.
/// </summary>
public sealed class FileSorterEngineTests : IDisposable
{
    private readonly string _root;
    private readonly string _state;
    private readonly string _target;

    public FileSorterEngineTests()
    {
        _root = Path.Combine(
            Path.GetTempPath(), "fseng-" + Guid.NewGuid().ToString("N"));
        _state = Path.Combine(_root, "state");
        _target = Path.Combine(_root, "target");
        Directory.CreateDirectory(_target);
        Directory.CreateDirectory(Path.Combine(_target, "偶像"));
        Environment.SetEnvironmentVariable(
            "FILE_SORTER_STATE_ROOT", _state);
    }

    public void Dispose() =>
        Environment.SetEnvironmentVariable(
            "FILE_SORTER_STATE_ROOT", null);

    private static string Run(params string[] args) =>
        FileSorterEngine.Run(args, null, CancellationToken.None);

    private static JsonObject ParseFrame(string stdout, string prefix)
    {
        Assert.StartsWith(prefix, stdout);
        return JsonNode.Parse(stdout[prefix.Length..])!.AsObject();
    }

    [Fact]
    public void ListFolders_EmitsDirectChildrenOnly()
    {
        Directory.CreateDirectory(
            Path.Combine(_target, "偶像", "nested"));
        var stdout = Run(_target, "--list-folders");
        Assert.StartsWith(FileSorterEngine.FoldersPrefix, stdout);
        var folders = JsonSerializer.Deserialize<string[]>(
            stdout[FileSorterEngine.FoldersPrefix.Length..])!;
        Assert.Contains("偶像", folders);
        Assert.DoesNotContain("nested", folders);
    }

    [Fact]
    public void UpsertKeyword_RejectsForeignDestination()
    {
        var ex = Assert.Throws<FileSorterException>(() =>
            Run(_target, "--upsert-keyword", "idol",
                "--folder", "..\\outside"));
        Assert.Equal("DESTINATION_INVALID", ex.ErrorCode);
    }

    [Fact]
    public void PreviewApplyUndo_RoundTripsMoves()
    {
        File.WriteAllText(
            Path.Combine(_target, "concert idol photo.jpg"), "payload");
        Run(_target, "--upsert-keyword", "idol", "--folder", "偶像");

        var preview = ParseFrame(
            Run(_target, "--preview-json"),
            FileSorterEngine.PreviewPrefix);
        var planId = preview["plan_id"]!.GetValue<string>();
        Assert.Equal(1, preview["move_count"]!.GetValue<int>());

        var applied = ParseFrame(
            Run(_target, "--apply-plan", planId),
            FileSorterEngine.PlanPrefix);
        Assert.True(applied["ok"]!.GetValue<bool>());
        Assert.True(File.Exists(
            Path.Combine(_target, "偶像", "concert idol photo.jpg")));
        Assert.False(File.Exists(
            Path.Combine(_target, "concert idol photo.jpg")));

        var undo = JsonNode.Parse(
            Run(_target, "--undo-last"))!.AsObject();
        Assert.True(undo["ok"]!.GetValue<bool>());
        Assert.True(File.Exists(
            Path.Combine(_target, "concert idol photo.jpg")));
    }

    [Fact]
    public void ApplyPlan_RejectsStaleSource()
    {
        var source = Path.Combine(_target, "idol clip.mp4");
        File.WriteAllText(source, "v1");
        Run(_target, "--upsert-keyword", "idol", "--folder", "偶像");
        var preview = ParseFrame(
            Run(_target, "--preview-json"),
            FileSorterEngine.PreviewPrefix);
        File.WriteAllText(source, "v2-changed"); // mutate after preview

        var applied = ParseFrame(
            Run(_target, "--apply-plan",
                preview["plan_id"]!.GetValue<string>()),
            FileSorterEngine.PlanPrefix);
        Assert.False(applied["ok"]!.GetValue<bool>());
        Assert.True(File.Exists(source)); // untouched
    }

    [Fact]
    public void Preview_SkipsIncompleteAndLockedFiles()
    {
        File.WriteAllText(Path.Combine(_target, "idol.part"), "x");
        var locked = Path.Combine(_target, "idol live.jpg");
        File.WriteAllText(locked, "x");
        using var handle = new FileStream(
            locked, FileMode.Open, FileAccess.ReadWrite,
            FileShare.None);
        var preview = ParseFrame(
            Run(_target, "--preview-json"),
            FileSorterEngine.PreviewPrefix);
        // Locked/incomplete files never reach the candidate set —
        // neither moved nor reported as unclassifiable.
        Assert.Equal(0, preview["move_count"]!.GetValue<int>());
        Assert.Empty((preview["skipped"] as JsonArray)!);
    }

    [Fact]
    public void CleanupScan_FailsClosed()
    {
        var ex = Assert.Throws<FileSorterException>(() =>
            Run(_target, "--cleanup-scan", "--json"));
        Assert.Equal("CLEANUP_ANALYZER_PENDING", ex.ErrorCode);
    }

    [Fact]
    public void ApplyPlan_NonexistentPlan_FailsClosed()
    {
        var ex = Assert.Throws<FileSorterException>(() =>
            Run(_target, "--apply-plan", "plan-20000101000000-0123456789ab"));
        Assert.Equal("PLAN_NOT_FOUND", ex.ErrorCode);
    }
}
