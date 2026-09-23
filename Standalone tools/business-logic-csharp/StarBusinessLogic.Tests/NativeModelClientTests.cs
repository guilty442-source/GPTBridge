using System.Text.Json;
using StarBusinessLogic.Application;
using Xunit;

namespace StarBusinessLogic.Tests;

// P11/MS6：native-abi 傳輸端到端——只在環境變數提供真引擎映像＋bundle
// 時執行（整合測試），否則略過。
public class NativeModelClientTests
{
    private static (string Image, string Bundle, string ToolRoot)? Env()
    {
        var image = Environment.GetEnvironmentVariable("XC_TEST_ENGINE_IMAGE");
        var bundle = Environment.GetEnvironmentVariable("XC_TEST_BUNDLE_DIR");
        var root = Environment.GetEnvironmentVariable("XC_TEST_TOOL_ROOT");
        if (string.IsNullOrWhiteSpace(image) || string.IsNullOrWhiteSpace(bundle)
            || string.IsNullOrWhiteSpace(root)) return null;
        if (!File.Exists(image) || !Directory.Exists(bundle)) return null;
        return (image, bundle, root);
    }

    [Fact]
    public async Task Infer_AppendsExecutionLedgerEntry()
    {
        var env = Env();
        if (env is null) return; // 無引擎環境 → 略過
        var (image, bundle, root) = env.Value;
        var ledger = Path.Combine(
            root, "xingcheng", "runtime", "logs",
            "native-engine-executions.jsonl");
        var before = File.Exists(ledger)
            ? File.ReadLines(ledger).Count() : 0;

        using var client = new NativeModelClient(image, bundle, toolRoot: root);
        var response = await client.InferAsync(
            new ModelInferenceRequest("你好", "", "Conversation", MaxNewTokens: 4));

        Assert.False(string.IsNullOrEmpty(response.Text));
        Assert.True(File.Exists(ledger));
        var lines = File.ReadLines(ledger).ToList();
        Assert.True(lines.Count > before);
        using var doc = JsonDocument.Parse(lines[^1]);
        var e = doc.RootElement;
        Assert.Equal("cpp-runtime-execution", e.GetProperty("event").GetString());
        Assert.Equal("native-abi", e.GetProperty("transport").GetString());
        Assert.Equal("csharp-business-logic", e.GetProperty("host").GetString());
        Assert.Equal(64, e.GetProperty("prompt_sha256").GetString()!.Length);
        Assert.Equal(64, e.GetProperty("output_sha256").GetString()!.Length);
        Assert.True(e.GetProperty("eval_count").GetInt32() > 0);
    }
}
