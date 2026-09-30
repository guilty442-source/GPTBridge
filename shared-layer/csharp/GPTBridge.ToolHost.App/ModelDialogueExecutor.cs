// ModelDialogueExecutor — model-dialogue's governed executor: owns the
// star_chat_* WS command surface behind the star-chat UI.
//
// Routing contract (XingCheng-first): dialogue never spawns a model
// process itself. It discovers the local-model owned xc-model-service
// through the shared descriptor, asks main-system's governed lifecycle
// (toolbox_start_tool over the authenticated WS) to start local-model
// when the service is absent, then calls POST /v1/infer on the
// authenticated loopback endpoint. Prompt rendering lives in the C++
// serve worker — this executor only forwards the message list.
using System.Net;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using GPTBridge.ToolHost;

namespace GPTBridge.ToolHost.App;

internal sealed class ModelDialogueExecutor
    : IGovernedCommandExecutor, IWsCommandSurface, IDisposable
{
    private const string ModelName = "xingcheng-native-transformer";
    private const string LocalModelToolId = "local-model";
    private static readonly TimeSpan ActivationBudget = TimeSpan.FromSeconds(90);
    private static readonly TimeSpan InferTimeout = TimeSpan.FromMinutes(8);

    private static readonly HashSet<string> OwnedCommands = new(
        StringComparer.Ordinal)
    {
        "star_chat_status",
        "star_chat_models",
        "star_chat_send_message",
        "star_chat_codex_alignment",
        "star_chat_architecture_sync",
    };

    private readonly GovernedEnvironment _env;
    private readonly HttpClient _http = new();
    private readonly string _localModelRoot;
    private readonly string _ipcDir;
    private readonly string _workspaceInstanceId;

    public ModelDialogueExecutor(GovernedEnvironment env)
    {
        _env = env;
        _localModelRoot = Path.Combine(
            env.ProjectRoot, "Standalone tools", "local-model");
        _ipcDir = Path.Combine(
            _localModelRoot, "xingcheng", "runtime", "ipc");
        _workspaceInstanceId = env.WorkspaceInstanceId();
    }

    public bool OwnsCommand(string command) => OwnedCommands.Contains(command);

    // Store-claim lane: no model-dialogue commands arrive via the store
    // today; WS is the only live lane (design §10).
    public Task<(string Event, JsonObject Result)> ExecuteAsync(
        string command, JsonObject payload, string requestId,
        CancellationToken cancellationToken)
        => ExecuteWsAsync(command, payload, requestId, null, cancellationToken);

    public async Task<(string Event, JsonObject Result)> ExecuteWsAsync(
        string command, JsonObject payload, string requestId,
        Func<JsonObject, Task>? emitProgress,
        CancellationToken cancellationToken)
    {
        var result = command switch
        {
            "star_chat_status" => await StatusResult(cancellationToken)
                .ConfigureAwait(false),
            "star_chat_models" => ModelsResult(),
            "star_chat_send_message" => await SendMessage(
                    payload, requestId, emitProgress, cancellationToken)
                .ConfigureAwait(false),
            "star_chat_codex_alignment" => CodexAlignmentResult(),
            "star_chat_architecture_sync" => ArchitectureSyncResult(),
            _ => throw new PermissionDeniedException(),
        };
        return ($"{command}_result", result);
    }

    // ----------------------------------------------------------- status --

    private JsonObject SelectableModels() => new()
    {
        ["selectable_models"] = new JsonArray
        {
            new JsonObject
            {
                ["name"] = ModelName,
                ["label"] = "星澄原生模型（本地推論）",
                ["installed"] = BundlePinned(),
            },
        },
        ["routing"] = "xingcheng-first",
        ["transport"] = "model-service-http",
        ["decoder"] = "native-cpp",
    };

    private bool BundlePinned()
    {
        try
        {
            var settings = Path.Combine(
                _localModelRoot, "runtime", "settings",
                "native-engine.json");
            using var doc = JsonDocument.Parse(File.ReadAllText(settings));
            var checkpoint = doc.RootElement
                .TryGetProperty("checkpoint", out var c)
                ? c.GetString() : null;
            if (string.IsNullOrWhiteSpace(checkpoint)) return false;
            var path = Path.GetFullPath(Path.IsPathRooted(checkpoint)
                ? checkpoint : Path.Combine(_localModelRoot, checkpoint));
            return File.Exists(Path.Combine(path, "manifest.json"))
                && File.Exists(Path.Combine(path, "weights.bin"));
        }
        catch { return false; }
    }

    private async Task<JsonObject> StatusResult(CancellationToken ct)
    {
        var service = await ProbeService(ct).ConfigureAwait(false);
        return new JsonObject
        {
            ["ok"] = true,
            ["tool_id"] = _env.ToolId,
            ["service"] = service,
            ["native_runtime"] = SelectableModels(),
            ["executor"] = "model-dialogue/toolhost",
        };
    }

    private JsonObject ModelsResult() => new()
    {
        ["ok"] = true,
        ["tool_id"] = _env.ToolId,
        ["native_runtime"] = SelectableModels(),
    };

    /// <summary>Read the descriptor + probe /v1/status; returns a
    /// truthful snapshot, {running:false} when absent or dead.</summary>
    private async Task<JsonObject> ProbeService(CancellationToken ct)
    {
        var endpoint = DiscoverService();
        if (endpoint is null)
            return new JsonObject { ["running"] = false, ["engine_loaded"] = false };
        try
        {
            using var request = new HttpRequestMessage(
                HttpMethod.Get, endpoint.Endpoint + "/v1/status");
            request.Headers.Add(
                "X-GPTBridge-Session-Token", endpoint.SessionToken);
            using var response = await _http
                .SendAsync(request, ct).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
                return new JsonObject
                {
                    ["running"] = false,
                    ["engine_loaded"] = false,
                    ["http_status"] = (int)response.StatusCode,
                };
            var parsed = JsonNode.Parse(
                await response.Content
                    .ReadAsStringAsync(ct).ConfigureAwait(false)) as JsonObject;
            return new JsonObject
            {
                ["running"] = true,
                ["engine_loaded"] =
                    parsed?["loaded"]?.GetValue<bool>() == true,
                ["model_id"] = parsed?["model_id"]?.GetValue<string>(),
                ["model_version"] =
                    parsed?["model_version"]?.GetValue<string>(),
                ["cuda_active"] =
                    parsed?["cuda_active"]?.GetValue<bool>() == true,
                ["memory_bytes"] = parsed?["memory_bytes"] != null
                    ? parsed["memory_bytes"]!.DeepClone() : null,
            };
        }
        catch (Exception)
        {
            return new JsonObject
            {
                ["running"] = false,
                ["engine_loaded"] = false,
                ["stale_descriptor"] = true,
            };
        }
    }

    /// <summary>star-model-service-descriptor/v1 read — local copy of
    /// ModelServiceLocator.Discover semantics (self-contained App).</summary>
    private sealed record ServiceEndpoint(string Endpoint, string SessionToken);

    private ServiceEndpoint? DiscoverService()
    {
        var descriptorPath = Path.Combine(_ipcDir, "model-service.json");
        if (!File.Exists(descriptorPath)) return null;
        try
        {
            using var doc = JsonDocument.Parse(
                File.ReadAllText(descriptorPath));
            var root = doc.RootElement;
            if (root.TryGetProperty("schema", out var s)
                is false
                || s.GetString() != "star-model-service-descriptor/v1")
                return null;
            var port = root.TryGetProperty("port", out var p)
                ? p.GetInt32() : 0;
            if (port < 1 || port > 65535) return null;
            var tokenFile = root.TryGetProperty("token_file", out var tf)
                ? tf.GetString() : null;
            if (string.IsNullOrWhiteSpace(tokenFile)
                || tokenFile.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0
                || tokenFile.Contains(Path.DirectorySeparatorChar)
                || tokenFile.Contains(Path.AltDirectorySeparatorChar))
                return null;
            var token = File.ReadAllText(
                Path.Combine(_ipcDir, tokenFile)).Trim();
            if (token.Length == 0) return null;
            var owner = root.TryGetProperty("lifecycle_owner", out var lo)
                ? lo.GetString() : null;
            if (owner != LocalModelExecutor.LifecycleOwner) return null;
            if (root.TryGetProperty("consumer_policy", out var cp)
                && cp.GetString() != LocalModelExecutor.ConsumerPolicy)
                return null;
            return new ServiceEndpoint(
                $"http://127.0.0.1:{port}", token);
        }
        catch { return null; }
    }

    // -------------------------------------------------------- activation --

    /// <summary>Governed activation: connect the main-system backend WS
    /// and submit toolbox_start_tool for local-model; then poll the
    /// descriptor until the service answers /v1/status.</summary>
    private async Task<bool> EnsureModelService(
        Func<JsonObject, Task>? emitProgress, CancellationToken ct)
    {
        var endpoint = DiscoverService();
        if (endpoint is not null)
        {
            var probe = await ProbeService(ct).ConfigureAwait(false);
            if (probe["running"]?.GetValue<bool>() == true) return true;
        }
        await Emit(emitProgress, new JsonObject
        {
            ["phase"] = "activating-model",
            ["message"] = "啟動本地模型服務",
        }).ConfigureAwait(false);

        var started = await RequestToolStart(ct).ConfigureAwait(false);
        if (!started) return false;

        var deadline = DateTimeOffset.UtcNow + ActivationBudget;
        while (DateTimeOffset.UtcNow < deadline)
        {
            ct.ThrowIfCancellationRequested();
            var ep = DiscoverService();
            if (ep is not null)
            {
                var probe = await ProbeService(ct).ConfigureAwait(false);
                if (probe["running"]?.GetValue<bool>() == true)
                    return true;
            }
            await Task.Delay(500, ct).ConfigureAwait(false);
        }
        return false;
    }

    private static Task Emit(
        Func<JsonObject, Task>? emitProgress, JsonObject payload)
        => emitProgress is null ? Task.CompletedTask : emitProgress(payload);

    /// <summary>toolbox_start_tool over the authenticated main-system
    /// loopback WS — the governed lifecycle broker owns the spawn.</summary>
    private async Task<bool> RequestToolStart(CancellationToken ct)
    {
        var port = ResolveBackendPort();
        var token = ReadBackendToken();
        if (port == 0 || token is null) return false;
        var uri = new Uri(
            $"ws://127.0.0.1:{port}/?token={token}&instance={_workspaceInstanceId}");
        using var socket = new ClientWebSocket();
        try
        {
            using var connectTimeout = CancellationTokenSource
                .CreateLinkedTokenSource(ct);
            connectTimeout.CancelAfter(TimeSpan.FromSeconds(8));
            await socket.ConnectAsync(uri, connectTimeout.Token)
                .ConfigureAwait(false);
            var frame = Encoding.UTF8.GetBytes(new JsonObject
            {
                ["command"] = "toolbox_start_tool",
                ["payload"] = new JsonObject
                {
                    ["tool_id"] = LocalModelToolId,
                    ["request_id"] =
                        $"md-activate-{Environment.TickCount64}",
                },
            }.ToJsonString());
            await socket.SendAsync(
                frame, WebSocketMessageType.Text, true, ct)
                .ConfigureAwait(false);
            var buffer = new byte[64 * 1024];
            using var receiveTimeout = CancellationTokenSource
                .CreateLinkedTokenSource(ct);
            receiveTimeout.CancelAfter(TimeSpan.FromSeconds(30));
            while (true)
            {
                var result = await socket.ReceiveAsync(
                    buffer, receiveTimeout.Token).ConfigureAwait(false);
                if (result.MessageType == WebSocketMessageType.Close)
                    return false;
                var reply = JsonNode.Parse(
                    buffer.AsSpan(0, result.Count).ToArray()) as JsonObject;
                var evt = reply?["event"]?.GetValue<string>();
                if (evt != "toolbox_start_tool_result") continue;
                var body = reply?["payload"] as JsonObject;
                return body?["ok"]?.GetValue<bool>() == true;
            }
        }
        catch (Exception)
        {
            return false;
        }
    }

    /// <summary>Main backend session token — the shared IPC credential,
    /// NOT this tool's own GPTBRIDGE_IPC_SESSION_TOKEN (env takes
    /// precedence only for the tool's own host).</summary>
    private static string? ReadBackendToken()
    {
        var stateRoot =
            Environment.GetEnvironmentVariable("GPTBRIDGE_IPC_STATE_ROOT");
        if (string.IsNullOrWhiteSpace(stateRoot))
        {
            var local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            if (string.IsNullOrWhiteSpace(local))
            {
                var home = Environment.GetEnvironmentVariable("USERPROFILE");
                if (string.IsNullOrWhiteSpace(home)) return null;
                local = Path.Combine(home, "AppData", "Local");
            }
            stateRoot = Path.Combine(local, "GPTBridge", "ipc");
        }
        var path = Path.Combine(stateRoot, "session-token");
        try
        {
            var token = File.ReadAllText(path).Trim().ToLowerInvariant();
            return token.Length == 64
                && token.All(Uri.IsHexDigit) ? token : null;
        }
        catch { return null; }
    }

    /// <summary>Port resolution mirroring ipc/discovery.rs: configured →
    /// boot-core active → +1/+2, each probed on /health?brief=1 for the
    /// matching workspace_instance_id; configured wins as fallback.</summary>
    private int ResolveBackendPort()
    {
        var configured = 8765;
        try
        {
            var manifest = Path.Combine(
                _env.ProjectRoot, "main-system", "config",
                "startup_manifest.json");
            using var doc = JsonDocument.Parse(File.ReadAllText(manifest));
            var p = doc.RootElement
                .GetProperty("ports").GetProperty("health_probe")
                .GetInt32();
            if (p > 0 && p <= 65535) configured = p;
        }
        catch { /* default */ }
        var candidates = new List<int> { configured };
        try
        {
            var boot = Path.Combine(
                _env.ProjectRoot, "main-system", "runtime", "state",
                "boot-core.json");
            using var doc = JsonDocument.Parse(File.ReadAllText(boot));
            var p = doc.RootElement
                .GetProperty("active_backend_port").GetInt32();
            if (p > 0 && p <= 65535) candidates.Add(p);
        }
        catch { /* none recorded */ }
        candidates.Add(configured + 1);
        candidates.Add(configured + 2);
        foreach (var port in candidates.Distinct())
        {
            try
            {
                using var request = new HttpRequestMessage(
                    HttpMethod.Get,
                    $"http://127.0.0.1:{port}/health?brief=1");
                using var cts = new CancellationTokenSource(
                    TimeSpan.FromMilliseconds(600));
                using var response = _http.Send(request, cts.Token);
                var parsed = JsonNode.Parse(
                    response.Content.ReadAsStringAsync(cts.Token).Result)
                    as JsonObject;
                if (parsed?["workspace_instance_id"]?.GetValue<string>()
                    == _workspaceInstanceId)
                    return port;
            }
            catch { /* next candidate */ }
        }
        return configured;
    }

    // ------------------------------------------------------ send_message --

    private async Task<JsonObject> SendMessage(
        JsonObject payload,
        string requestId,
        Func<JsonObject, Task>? emitProgress,
        CancellationToken ct)
    {
        var message = payload["message"]?.GetValue<string>()?.Trim() ?? "";
        if (message.Length == 0)
            return new JsonObject
            {
                ["ok"] = false,
                ["tool_id"] = _env.ToolId,
                ["error_code"] = "MESSAGE_EMPTY",
                ["message"] = "訊息內容為空。",
            };
        var requested = payload["runtime_model"]?.GetValue<string>()
            ?.Trim() ?? "";
        if (requested.Length > 0 && requested != ModelName)
            return new JsonObject
            {
                ["ok"] = false,
                ["tool_id"] = _env.ToolId,
                ["error_code"] = "MODEL_NOT_INSTALLED",
                ["message"] = $"所選模型未安裝：{requested}",
            };

        var ready = await EnsureModelService(emitProgress, ct)
            .ConfigureAwait(false);
        if (!ready)
            return new JsonObject
            {
                ["ok"] = false,
                ["tool_id"] = _env.ToolId,
                ["error_code"] = "MODEL_SERVICE_UNAVAILABLE",
                ["message"] =
                    "本地模型服務無法啟動或未在時限內就緒。",
            };
        var endpoint = DiscoverService()
            ?? throw new InvalidOperationException(
                "MODEL_SERVICE_DESCRIPTOR_LOST");

        // History: normalize {role,content} pairs, bounded by the UI's
        // context_budget_characters (oldest turns drop first).
        var budget = (int)(payload["context_budget_characters"]
            ?.GetValue<int>() ?? 10000);
        if (budget <= 0) budget = 10000;
        var messages = new JsonArray();
        if (payload["history"] is JsonArray history)
        {
            foreach (var turn in history.Reverse())
            {
                var role = turn?["role"]?.GetValue<string>();
                var content = turn?["content"]?.GetValue<string>();
                if (role is not ("user" or "assistant" or "system")
                    || string.IsNullOrWhiteSpace(content))
                    continue;
                messages.Insert(0, new JsonObject
                {
                    ["role"] = role,
                    ["content"] = content.Trim(),
                });
            }
        }
        messages.Add(new JsonObject
        {
            ["role"] = "user", ["content"] = message,
        });
        // Trim oldest history (keep the newest user message) to budget.
        var serialized = () => messages.ToJsonString().Length;
        while (messages.Count > 1 && serialized() > budget)
            messages.RemoveAt(0);

        var maxNew = (int)(payload["max_output_tokens"]
            ?.GetValue<int>() ?? 512);
        maxNew = Math.Clamp(maxNew, 1, 1024);

        await Emit(emitProgress, new JsonObject
        {
            ["phase"] = "generating",
            ["message"] = "生成回答",
            ["model"] = ModelName,
        }).ConfigureAwait(false);

        var inferBody = new JsonObject
        {
            ["messages"] = messages,
            ["max_new_tokens"] = maxNew,
            ["intent"] = "dialogue",
        };
        if (payload["extra"] is JsonObject extra)
            inferBody["extra"] = extra.DeepClone();

        using var request = new HttpRequestMessage(
            HttpMethod.Post, endpoint.Endpoint + "/v1/infer")
        {
            Content = new ByteArrayContent(
                Encoding.UTF8.GetBytes(inferBody.ToJsonString())),
        };
        request.Content.Headers.ContentType =
            new System.Net.Http.Headers.MediaTypeHeaderValue(
                "application/json");
        request.Headers.Add(
            "X-GPTBridge-Session-Token", endpoint.SessionToken);

        JsonObject reply;
        using (var inferTimeout = CancellationTokenSource
               .CreateLinkedTokenSource(ct))
        {
            inferTimeout.CancelAfter(InferTimeout);
            using var response = await _http
                .SendAsync(request, inferTimeout.Token)
                .ConfigureAwait(false);
            reply = JsonNode.Parse(
                await response.Content
                    .ReadAsStringAsync(inferTimeout.Token)
                    .ConfigureAwait(false)) as JsonObject
                ?? new JsonObject { ["ok"] = false };
        }
        if (reply["ok"]?.GetValue<bool>() != true)
        {
            var code = reply["error_code"]?.GetValue<string>()
                ?? reply["error"]?.GetValue<string>()
                ?? "MODEL_INFERENCE_FAILED";
            return new JsonObject
            {
                ["ok"] = false,
                ["tool_id"] = _env.ToolId,
                ["error_code"] = code,
                ["message"] = "本地模型推論失敗，請稍後再試。",
            };
        }
        var text = reply["text"]?.GetValue<string>()?.Trim() ?? "";
        var modelVersion = reply["model_version"]?.GetValue<string>() ?? "";
        var modelId = reply["model_id"]?.GetValue<string>() ?? ModelName;
        return new JsonObject
        {
            ["ok"] = true,
            ["tool_id"] = _env.ToolId,
            ["response"] = text.Length > 0 ? text : "（模型未產生內容）",
            ["model"] = modelId,
            ["generation"] = new JsonObject
            {
                ["model"] = modelId,
                ["model_version"] = modelVersion,
                ["generated_tokens"] = reply["generated_tokens"]
                    ?.DeepClone(),
                ["latency_ms"] = reply["latency_ms"]?.DeepClone(),
                ["decoder"] = "native-cpp",
                ["cpp_runtime"] = true,
            },
            ["native_runtime"] = SelectableModels(),
            ["route"] = "xingcheng-first",
        };
    }

    // ----------------------------------------------------- diagnostics --

    private JsonObject CodexAlignmentResult()
    {
        var checks = new JsonArray();
        var report = new StringBuilder();
        void Check(string name, bool ok, string detail)
        {
            checks.Add(new JsonObject
            {
                ["name"] = name, ["ok"] = ok, ["detail"] = detail,
            });
            report.AppendLine(
                $"{(ok ? "PASS" : "FAIL")} {name}：{detail}");
        }

        var manifest = Path.Combine(_env.ToolRoot, "manifest.json");
        var manifestOk = File.Exists(manifest);
        Check("manifest-存在", manifestOk, _env.ToolRoot);
        if (manifestOk)
        {
            try
            {
                using var doc = JsonDocument.Parse(
                    File.ReadAllText(manifest));
                var root = doc.RootElement;
                var host = root.TryGetProperty("host_tool_id", out var h)
                    ? h.GetString() : null;
                Check("獨立工具身份", host == "model-dialogue",
                    $"host_tool_id={host ?? "(missing)"}");
                var owner = root.TryGetProperty(
                    "runtime_owner_tool_id", out var o)
                    ? o.GetString() : null;
                Check("執行期擁有者", owner == "model-dialogue",
                    $"runtime_owner_tool_id={owner ?? "(missing)"}");
                var nativeEntry =
                    root.TryGetProperty("runtime", out var rt)
                    && rt.TryGetProperty("native_entry", out var ne)
                        ? ne.GetString() : null;
                Check("原生執行入口",
                    !string.IsNullOrWhiteSpace(nativeEntry)
                    && File.Exists(
                        Path.Combine(_env.ToolRoot, nativeEntry!)),
                    nativeEntry ?? "(missing)");
            }
            catch (Exception ex)
            {
                Check("manifest-解析", false, ex.Message);
            }
        }
        var starChat = Path.Combine(
            _env.ToolRoot, "star-chat", "manifest.json");
        Check("star-chat-同隨", File.Exists(starChat), starChat);
        var descriptor = Path.Combine(_ipcDir, "model-service.json");
        Check("模型服務描述元", File.Exists(descriptor), descriptor);
        var engineSettings = Path.Combine(
            _localModelRoot, "runtime", "settings", "native-engine.json");
        Check("原生引擎設定", File.Exists(engineSettings)
            && BundlePinned(), engineSettings);

        var passed = checks.Count > 0 && checks.All(
            c => c?["ok"]?.GetValue<bool>() == true);
        report.Insert(0,
            $"法典 × 實作對齊檢查：{(passed ? "全部通過" : "存在差異")}\n");
        return new JsonObject
        {
            ["ok"] = true,
            ["tool_id"] = _env.ToolId,
            ["response"] = report.ToString(),
            ["checks"] = checks,
            ["passed"] = passed,
        };
    }

    private JsonObject ArchitectureSyncResult()
    {
        var checks = new JsonArray();
        var report = new StringBuilder();
        void Check(string name, bool ok, string detail)
        {
            checks.Add(new JsonObject
            {
                ["name"] = name, ["ok"] = ok, ["detail"] = detail,
            });
            report.AppendLine(
                $"{(ok ? "PASS" : "FAIL")} {name}：{detail}");
        }

        var surfaces = Path.Combine(
            _env.ProjectRoot, "main-system", "config",
            "native-ui-surfaces.json");
        var registered = false;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(surfaces));
            var surface = doc.RootElement
                .GetProperty("surfaces").GetProperty("model-dialogue");
            registered =
                surface.GetProperty("binary").GetString()
                    is { Length: > 0 }
                && surface.GetProperty("surface").GetString()
                    == "star-chat";
        }
        catch { /* unreadable */ }
        Check("原生視窗註冊", registered,
            "native-ui-surfaces.json → star-chat");

        var wsBind = _env.Port > 0;
        Check("受管通道綁定", wsBind,
            $"loopback:{_env.Port} (token/session)");

        var channels = Path.Combine(
            _env.ToolRoot, "manifest.json");
        try
        {
            using var doc = JsonDocument.Parse(
                File.ReadAllText(channels));
            var rc = doc.RootElement.GetProperty("request_channel");
            var governed =
                rc.GetProperty("model").GetString()
                    == "governance-authenticated-shared-layer"
                && rc.GetProperty("direct_instruction").GetString()
                    == "PERMISSION_DENIED";
            Check("受管請求通道", governed,
                rc.GetProperty("model").GetString() ?? "(missing)");
        }
        catch (Exception ex)
        {
            Check("受管請求通道", false, ex.Message);
        }
        var lifecycle =
            File.Exists(Path.Combine(_ipcDir, "model-service.json"));
        Check("模型服務在線", lifecycle,
            lifecycle
                ? "descriptor present"
                : "descriptor absent（on-demand）");

        var passed = checks.Count > 0 && checks.All(
            c => c?["ok"]?.GetValue<bool>() == true);
        report.Insert(0,
            $"法典 × 架構圖同步檢查：{(passed ? "全部通過" : "存在差異")}\n");
        return new JsonObject
        {
            ["ok"] = true,
            ["tool_id"] = _env.ToolId,
            ["response"] = report.ToString(),
            ["checks"] = checks,
            ["passed"] = passed,
        };
    }

    public JsonObject Health() => new()
    {
        ["executor_state"] = "active:model-dialogue",
        ["owned_ws_commands"] =
            new JsonArray(OwnedCommands.Select(c => (JsonNode)c).ToArray()),
        ["model_service_descriptor"] =
            File.Exists(Path.Combine(_ipcDir, "model-service.json")),
    };

    public void Dispose() => _http.Dispose();
}
