using System.Net;
using System.Text;
using System.Text.Json;
using StarBusinessLogic.Application;

namespace StarBusinessLogic.Tests;

// G30 契約測試：C# HttpModelClient ↔ Python star-model-service/v1
public class ModelHttpClientTests
{
    private sealed class StubHandler : HttpMessageHandler
    {
        public Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> Responder;
        public HttpRequestMessage? LastRequest;
        public string? LastBody;

        public StubHandler(Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> responder)
            => Responder = responder;

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            LastRequest = request;
            LastBody = request.Content is null ? null : await request.Content.ReadAsStringAsync(cancellationToken);
            return await Responder(request, cancellationToken);
        }
    }

    private static HttpResponseMessage Json(HttpStatusCode status, object payload) =>
        new(status) { Content = new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json") };

    private static ModelInferenceRequest Req() => new("你好", "ctx", "Conversation", MaxNewTokens: 16);

    [Fact]
    public async Task Infer_SendsContractAndParsesResponse()
    {
        var handler = new StubHandler((req, ct) => Task.FromResult(Json(HttpStatusCode.OK, new
        {
            schema = "star-model-service/v1",
            ok = true,
            text = "星澄回應",
            token_ids = new[] { 1, 2, 3 },
            model_id = "native-small",
            latency_ms = 12.5,
            decoder = "greedy",
            cpp_runtime = true,
        })));
        using var client = new HttpModelClient("http://127.0.0.1:9999", sessionToken: "tok-1", handler: handler);
        var resp = await client.InferAsync(Req());

        Assert.Equal("星澄回應", resp.Text);
        Assert.Equal("native-small", resp.ModelId);
        Assert.Equal(new[] { 1, 2, 3 }, resp.TokenIds.ToArray());
        Assert.True((bool)resp.Metadata!["cpp_runtime"]);

        Assert.Equal("/v1/infer", handler.LastRequest!.RequestUri!.AbsolutePath);
        Assert.Equal("tok-1", handler.LastRequest.Headers.GetValues("X-GPTBridge-Session-Token").Single());
        var sent = JsonDocument.Parse(handler.LastBody!).RootElement;
        Assert.Equal("你好", sent.GetProperty("prompt").GetString());
        Assert.Equal(16, sent.GetProperty("max_new_tokens").GetInt32());
    }

    [Fact]
    public async Task Infer_EngineFailure_ThrowsWithErrorCode()
    {
        var handler = new StubHandler((req, ct) => Task.FromResult(Json(HttpStatusCode.BadGateway, new
        {
            schema = "star-model-service/v1", ok = false, error_code = "ENGINE_UNAVAILABLE",
        })));
        using var client = new HttpModelClient("http://localhost:9999", handler: handler);
        var ex = await Assert.ThrowsAsync<InvalidOperationException>(() => client.InferAsync(Req()));
        Assert.Contains("ENGINE_UNAVAILABLE", ex.Message);
    }

    [Fact]
    public async Task Infer_NonSuccessWithoutOkFlag_Throws()
    {
        var handler = new StubHandler((req, ct) => Task.FromResult(
            new HttpResponseMessage(HttpStatusCode.InternalServerError)
            { Content = new StringContent("{\"ok\":true}") }));
        using var client = new HttpModelClient("http://127.0.0.1:9999", handler: handler);
        var ex = await Assert.ThrowsAsync<InvalidOperationException>(() => client.InferAsync(Req()));
        Assert.Contains("MODEL_HTTP_500", ex.Message);
    }

    [Fact]
    public async Task Infer_Timeout_ThrowsTimeoutCode()
    {
        var handler = new StubHandler(async (req, ct) =>
        {
            await Task.Delay(TimeSpan.FromSeconds(30), ct);
            return Json(HttpStatusCode.OK, new { ok = true, text = "" });
        });
        using var client = new HttpModelClient("http://127.0.0.1:9999", handler: handler, timeout: TimeSpan.FromMilliseconds(200));
        var ex = await Assert.ThrowsAsync<InvalidOperationException>(() => client.InferAsync(Req()));
        Assert.Contains("MODEL_INFERENCE_TIMEOUT", ex.Message);
    }

    [Fact]
    public async Task Infer_CallerCancellation_Propagates()
    {
        var handler = new StubHandler(async (req, ct) =>
        {
            await Task.Delay(TimeSpan.FromSeconds(30), ct);
            return Json(HttpStatusCode.OK, new { ok = true, text = "" });
        });
        using var client = new HttpModelClient("http://127.0.0.1:9999", handler: handler);
        using var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(150));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => client.InferAsync(Req(), cts.Token));
    }

    [Theory]
    [InlineData("http://example.com")]
    [InlineData("https://127.0.0.1:8443")]
    [InlineData("http://192.168.1.10")]
    public void Constructor_RejectsNonLoopback(string endpoint)
    {
        Assert.Throws<InvalidOperationException>(() => new HttpModelClient(endpoint));
    }

    [Fact]
    public async Task Status_And_Release_Contracts()
    {
        var handler = new StubHandler((req, ct) =>
        {
            if (req.RequestUri!.AbsolutePath == "/v1/status")
                return Task.FromResult(Json(HttpStatusCode.OK, new { schema = "star-model-service/v1", ok = true, cpp_runtime = "fallback" }));
            return Task.FromResult(Json(HttpStatusCode.OK, new { schema = "star-model-service/v1", ok = true, released = new[] { "native-engine:cpu" } }));
        });
        using var client = new HttpModelClient("http://127.0.0.1:9999", handler: handler);
        var status = await client.StatusAsync();
        Assert.True(status.ContainsKey("cpp_runtime"));
        var released = await client.ReleaseAsync();
        Assert.Contains("native-engine:cpu", released);
    }

    [Fact]
    public void Locator_DiscoversDescriptorContract()
    {
        var root = Path.Combine(Path.GetTempPath(), "blc-locator-" + Guid.NewGuid().ToString("N"));
        var ipc = Path.Combine(root, "xingcheng", "runtime", "ipc");
        Directory.CreateDirectory(ipc);
        File.WriteAllText(Path.Combine(ipc, "model-service-session-token"), "tok-xyz");
        File.WriteAllText(Path.Combine(ipc, "model-service.json"), JsonSerializer.Serialize(new
        {
            schema = "star-model-service-descriptor/v1",
            tool_id = "local-model",
            pid = 1234,
            port = 4567,
            token_file = "model-service-session-token",
            lifecycle_owner = "local-model/channel_runtime.py",
            consumer_policy = "csharp-orchestrator-client-only",
            session_token_sha256 = "abc",
        }));
        try
        {
            var ep = Infrastructure.ModelServiceLocator.Discover(root);
            Assert.Equal("http://127.0.0.1:4567", ep.Endpoint);
            Assert.Equal("tok-xyz", ep.SessionToken);
            Assert.Equal(1234, ep.Pid);
            Assert.Equal("local-model/channel_runtime.py", ep.LifecycleOwner);
            Assert.Equal("csharp-orchestrator-client-only", ep.ConsumerPolicy);
        }
        finally { Directory.Delete(root, true); }
    }

    [Fact]
    public void Locator_FailClosed()
    {
        var root = Path.Combine(Path.GetTempPath(), "blc-locator-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            Assert.Throws<InvalidOperationException>(() => Infrastructure.ModelServiceLocator.Discover(root));
            var ipc = Path.Combine(root, "xingcheng", "runtime", "ipc");
            Directory.CreateDirectory(ipc);
            File.WriteAllText(Path.Combine(ipc, "model-service.json"), "{\"schema\":\"wrong\"}");
            var ex = Assert.Throws<InvalidOperationException>(() => Infrastructure.ModelServiceLocator.Discover(root));
            Assert.Contains("SCHEMA_MISMATCH", ex.Message);
        }
        finally { Directory.Delete(root, true); }
    }

    [Fact]
    public async Task Infer_EmptyPrompt_FailClosed()
    {
        using var client = new HttpModelClient("http://127.0.0.1:9999");
        await Assert.ThrowsAsync<ArgumentException>(() => client.InferAsync(new ModelInferenceRequest("  ", "", "Conversation")));
    }
}
