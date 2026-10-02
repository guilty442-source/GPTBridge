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
using StarDomain;

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
        "star_chat_agent_task",
        "star_chat_codex_alignment",
        "star_chat_architecture_sync",
    };

    private readonly GovernedEnvironment _env;
    private readonly HttpClient _http = new();
    private readonly string _localModelRoot;
    private readonly string _ipcDir;
    private readonly string _workspaceInstanceId;

    // F# StarDomain live lane: intent classification + execution-plan
    // shaping run in-process on every dialogue/agent input. Pure CPU,
    // cached (128-entry/5-min TTL), thread-safe, never throws outwards
    // (fail-open null) — inference transport and lane labels
    // ("dialogue"/"agent") are untouched; the classification rides along
    // as additive observability until the RAG-grounding phase lands.
    private static readonly RuleIntentClassifier IntentClassifier = new();
    private static readonly DefaultPlanBuilder PlanBuilder = new();

    private static JsonObject? ClassifyForDialogue(string text)
    {
        try
        {
            var intent = IntentClassifier.Classify(text);
            var plan = PlanBuilder.Build(intent, "");
            var candidates = new JsonArray();
            foreach (var candidate in intent.Candidates)
                candidates.Add(candidate.ToString());
            var tools = new JsonArray();
            foreach (var tool in plan.RequiredTools)
                tools.Add(tool);
            return new JsonObject
            {
                ["intent"] = intent.Primary.ToString(),
                ["candidates"] = candidates,
                ["confidence"] = intent.Confidence,
                ["needs_grounding"] = plan.NeedsGrounding,
                ["required_tools"] = tools,
                ["task_intensity"] = plan.TaskIntensity,
            };
        }
        catch { return null; }
    }

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
            "star_chat_agent_task" => await AgentTask(
                    payload, requestId, emitProgress, cancellationToken)
                .ConfigureAwait(false),
            "star_chat_codex_alignment" => await CodexAlignmentResult(
                    cancellationToken).ConfigureAwait(false),
            "star_chat_architecture_sync" => await ArchitectureSyncResult(
                    cancellationToken).ConfigureAwait(false),
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
            // Data residency: an out-of-boundary pin refuses to serve —
            // the bundle must resolve inside a registered xingcheng
            // domain root (institution root or the model-dialogue star
            // directory).
            var roots = new[]
            {
                Path.Combine(_localModelRoot, "xingcheng"),
                Path.GetFullPath(Path.Combine(
                    _localModelRoot, "..", "model-dialogue",
                    "xingcheng")),
            };
            var inBoundary = roots.Any(r =>
            {
                var root = Path.GetFullPath(r);
                return path.Equals(root,
                        StringComparison.OrdinalIgnoreCase)
                    || path.StartsWith(root + Path.DirectorySeparatorChar,
                        StringComparison.OrdinalIgnoreCase);
            });
            if (!inBoundary) return false;
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

    /// <summary>star-model-service-descriptor/v1 read — delegates to the
    /// single owned implementation (ModelServiceDescriptor.TryRead);
    /// this wrapper only adapts to the local ServiceEndpoint shape.</summary>
    private sealed record ServiceEndpoint(string Endpoint, string SessionToken);

    private ServiceEndpoint? DiscoverService()
    {
        var endpoint = ModelServiceDescriptor.TryRead(
            _ipcDir,
            LocalModelExecutor.LifecycleOwner,
            LocalModelExecutor.ConsumerPolicy);
        return endpoint is null
            ? null
            : new ServiceEndpoint(endpoint.Url, endpoint.SessionToken);
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

        // F# live lane: classify before activation so progress events
        // and the result can carry the shaping (observability-only;
        // the infer request below is byte-identical either way).
        var classification = ClassifyForDialogue(message);

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
            ["classification"] = classification?.DeepClone(),
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
            ["classification"] = classification?.DeepClone(),
        };
    }

    // --------------------------------------------------------- agent --

    /// Agentic lane (星澄底層工具調用): the model's trained-in
    /// &lt;tool_call&gt;{json}&lt;/tool_call&gt; output — surfaced by the
    /// serve worker as a parsed ``tool_call`` field — drives governed
    /// embedded-browser ops through the loopback bridge (token-guarded
    /// ``embedded-browser-bridge.json``, the same seam the retired
    /// embedded_browser_client used). Each op's observation loops back
    /// as a user turn until the model answers in plain text or calls
    /// ``task_done``. Bounded by max_steps (default 8, hard cap 24);
    /// every step is recorded in the returned ``steps`` transcript.
    private async Task<JsonObject> AgentTask(
        JsonObject payload,
        string requestId,
        Func<JsonObject, Task>? emitProgress,
        CancellationToken ct)
    {
        var task = payload["task"]?.GetValue<string>()?.Trim() ?? "";
        if (task.Length == 0)
            return new JsonObject
            {
                ["ok"] = false, ["tool_id"] = _env.ToolId,
                ["error_code"] = "TASK_EMPTY",
                ["message"] = "任務內容為空。",
            };
        var maxSteps = Math.Clamp(
            payload["max_steps"]?.GetValue<int>() ?? 8, 1, 24);
        var startUrl = payload["start_url"]?.GetValue<string>()
            ?.Trim() ?? "";

        // F# live lane (same contract as SendMessage): additive
        // observability; the agent loop below is unchanged.
        var classification = ClassifyForDialogue(task);

        var ready = await EnsureModelService(emitProgress, ct)
            .ConfigureAwait(false);
        if (!ready)
            return new JsonObject
            {
                ["ok"] = false, ["tool_id"] = _env.ToolId,
                ["error_code"] = "MODEL_SERVICE_UNAVAILABLE",
                ["message"] = "本地模型服務無法啟動或未在時限內就緒。",
            };
        var endpoint = DiscoverService()
            ?? throw new InvalidOperationException(
                "MODEL_SERVICE_DESCRIPTOR_LOST");
        var bridge = DiscoverBridge();
        if (bridge is null)
            return new JsonObject
            {
                ["ok"] = false, ["tool_id"] = _env.ToolId,
                ["error_code"] = "EMBEDDED_BROWSER_BRIDGE_UNAVAILABLE",
                ["message"] = "內嵌瀏覽器橋接不可用（主系統桌面殼未運行）。",
            };

        var sessionId = "md-agent-" + SanitizeId(requestId);
        var messages = new JsonArray
        {
            new JsonObject
            {
                ["role"] = "system",
                ["content"] = AgentSystemPrompt,
            },
            new JsonObject
            {
                ["role"] = "user", ["content"] = task,
            },
        };
        var steps = new JsonArray();
        var budget = Math.Clamp(
            payload["context_budget_characters"]
                ?.GetValue<int>() ?? 20000, 2000, 200000);
        var maxNew = Math.Clamp(
            payload["max_output_tokens"]?.GetValue<int>() ?? 512, 1, 1024);
        string? finalText = null;
        var finished = false;
        var opsExecuted = 0;

        try
        {
            if (startUrl.Length > 0)
            {
                var open = await AgentBrowserOp(
                        bridge, "embedded-browser:create",
                        new JsonObject
                        {
                            ["id"] = sessionId,
                            ["ownerModule"] = _env.ToolId,
                            ["url"] = startUrl,
                        }, ct)
                    .ConfigureAwait(false);
                steps.Add(StepRecord(steps.Count + 1,
                    "browser_open", new JsonObject { ["url"] = startUrl },
                    open));
                messages.Add(new JsonObject
                {
                    ["role"] = "user",
                    ["content"] =
                        $"[tool_result:browser_open] {open.ToJsonString()}",
                });
            }

            for (var step = steps.Count + 1;
                 step <= maxSteps && !finished;
                 ++step)
            {
                ct.ThrowIfCancellationRequested();
                var inferBody = new JsonObject
                {
                    ["messages"] = messages.DeepClone(),
                    ["max_new_tokens"] = maxNew,
                    ["intent"] = "agent",
                };
                var reply = await InferAsync(endpoint, inferBody, ct)
                    .ConfigureAwait(false);
                if (reply["ok"]?.GetValue<bool>() != true)
                    return AgentFail(steps,
                        reply["error_code"]?.GetValue<string>()
                            ?? "MODEL_INFERENCE_FAILED");

                var text = reply["text"]?.GetValue<string>() ?? "";
                var toolCall = reply["tool_call"] as JsonObject;
                var toolErr =
                    reply["tool_call_error"]?.GetValue<string>();
                if (toolErr is { Length: > 0 })
                {
                    messages.Add(new JsonObject
                    {
                        ["role"] = "assistant", ["content"] = text,
                    });
                    messages.Add(new JsonObject
                    {
                        ["role"] = "user",
                        ["content"] =
                            $"[tool_call_error] {toolErr}；請重新輸出合法的工具呼叫。",
                    });
                    steps.Add(StepRecord(step, "tool_call_error",
                        null, new JsonObject { ["error"] = toolErr }));
                    continue;
                }
                if (toolCall is null)
                {
                    finalText = text.Trim();
                    finished = true;
                    break;
                }
                var name =
                    toolCall["name"]?.GetValue<string>()?.Trim() ?? "";
                var toolArgs = toolCall["arguments"] as JsonObject
                    ?? new JsonObject();
                messages.Add(new JsonObject
                {
                    ["role"] = "assistant",
                    ["content"] = text
                        + "<tool_call>" + toolCall.ToJsonString()
                        + "</tool_call>",
                });
                if (name == "task_done")
                {
                    finalText = toolArgs["answer"]?.GetValue<string>()
                        ?.Trim() ?? "";
                    steps.Add(StepRecord(step, name, toolArgs,
                        new JsonObject { ["ok"] = true }));
                    finished = true;
                    break;
                }
                await Emit(emitProgress, new JsonObject
                {
                    ["phase"] = "agent-step",
                    ["step"] = step,
                    ["tool"] = name,
                }).ConfigureAwait(false);
                var observation = await DispatchAgentTool(
                        bridge, sessionId, name, toolArgs, ct)
                    .ConfigureAwait(false);
                if (observation["ok"]?.GetValue<bool>() == true)
                    ++opsExecuted;
                steps.Add(StepRecord(step, name, toolArgs, observation));
                messages.Add(new JsonObject
                {
                    ["role"] = "user",
                    ["content"] =
                        $"[tool_result:{name}] {observation.ToJsonString()}",
                });
                // Keep the transcript inside the context budget —
                // oldest tool turns drop first, system+task stay.
                while (messages.Count > 3
                       && messages.ToJsonString().Length > budget)
                    messages.RemoveAt(2);
            }
        }
        finally
        {
            if (payload["keep_session"]?.GetValue<bool>() != true
                && bridge is not null)
            {
                try
                {
                    await AgentBrowserOp(
                            bridge, "embedded-browser:close",
                            new JsonObject { ["id"] = sessionId }, ct)
                        .ConfigureAwait(false);
                }
                catch { /* session teardown is best-effort */ }
            }
        }

        if (finalText is null)
            return AgentFail(steps, "AGENT_STEP_BUDGET_EXHAUSTED");
        return new JsonObject
        {
            ["ok"] = true,
            ["tool_id"] = _env.ToolId,
            ["response"] =
                finalText.Length > 0 ? finalText : "（模型未產生內容）",
            ["steps"] = steps,
            ["steps_used"] = steps.Count,
            ["ops_executed"] = opsExecuted,
            ["session_id"] = sessionId,
            ["model"] = ModelName,
            ["generation"] = new JsonObject
            {
                ["model"] = ModelName,
                ["decoder"] = "native-cpp",
                ["cpp_runtime"] = true,
            },
            ["native_runtime"] = SelectableModels(),
            ["route"] = "xingcheng-first-agentic",
            ["classification"] = classification?.DeepClone(),
        };
    }

    private JsonObject AgentFail(JsonArray steps, string code) =>
        new()
        {
            ["ok"] = false,
            ["tool_id"] = _env.ToolId,
            ["error_code"] = code,
            ["steps"] = steps,
            ["message"] = "代理任務未完成。",
        };

    private static JsonObject StepRecord(
        int step, string tool, JsonObject? args, JsonObject observation) =>
        new()
        {
            ["step"] = step,
            ["tool"] = tool,
            ["arguments"] = args?.DeepClone(),
            ["observation"] = observation.DeepClone(),
        };

    private static string SanitizeId(string raw)
    {
        var chars = raw.Select(
            c => char.IsLetterOrDigit(c) ? c : '-').ToArray();
        var id = new string(chars).Trim('-');
        return id.Length > 48 ? id[..48] : id;
    }

    // -------------------------------------------------- bridge client --

    private sealed record BridgeEndpoint(string InvokeUrl, string Token);

    /// ``embedded-browser-bridge.json`` — published per-launch by the
    /// main shell's loopback bridge (loopback.rs). The token is a
    /// workspace-local credential under runtime/state, same trust
    /// domain as the IPC session token.
    private BridgeEndpoint? DiscoverBridge()
    {
        var state = Path.Combine(
            _env.ProjectRoot, "main-system", "runtime", "state",
            "embedded-browser-bridge.json");
        if (!File.Exists(state)) return null;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(state));
            var root = doc.RootElement;
            var port = root.TryGetProperty("port", out var p)
                ? p.GetInt32() : 0;
            var token = root.TryGetProperty("token", out var t)
                ? t.GetString() : null;
            if (port < 1 || port > 65535
                || string.IsNullOrWhiteSpace(token)) return null;
            return new BridgeEndpoint(
                $"http://127.0.0.1:{port}/invoke", token!);
        }
        catch { return null; }
    }

    private async Task<JsonObject> AgentBrowserOp(
        BridgeEndpoint bridge, string channel, JsonObject args,
        CancellationToken ct)
    {
        var body = new JsonObject
        {
            ["channel"] = channel,
            ["args"] = args,
        };
        using var request = new HttpRequestMessage(
            HttpMethod.Post, bridge.InvokeUrl)
        {
            Content = new ByteArrayContent(
                Encoding.UTF8.GetBytes(body.ToJsonString())),
        };
        request.Content.Headers.ContentType =
            new System.Net.Http.Headers.MediaTypeHeaderValue(
                "application/json");
        request.Headers.Add("x-gptbridge-bridge-token", bridge.Token);
        using var timeout = CancellationTokenSource
            .CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TimeSpan.FromSeconds(60));
        try
        {
            using var response = await _http
                .SendAsync(request, timeout.Token).ConfigureAwait(false);
            return JsonNode.Parse(
                await response.Content
                    .ReadAsStringAsync(timeout.Token)
                    .ConfigureAwait(false)) as JsonObject
                ?? new JsonObject { ["ok"] = false };
        }
        catch (Exception ex)
        {
            return new JsonObject
            {
                ["ok"] = false,
                ["error_code"] = "BROWSER_OP_FAILED",
                ["message"] = ex.Message.Length > 200
                    ? ex.Message[..200] : ex.Message,
            };
        }
    }

    // ------------------------------------------------- tool dispatch --

    /// Model-facing browser vocabulary → governed loopback channels.
    /// ``browser_read`` returns a DOM digest of visible interactive
    /// elements (buttons, inputs, links) indexed for click/fill — the
    /// trained-in "native perception" surface: the model refers to
    /// elements by index instead of guessing selectors.
    private async Task<JsonObject> DispatchAgentTool(
        BridgeEndpoint bridge, string sessionId, string name,
        JsonObject args, CancellationToken ct)
    {
        switch (name)
        {
            case "browser_open":
            case "browser_navigate":
            {
                var url = args["url"]?.GetValue<string>()?.Trim() ?? "";
                if (url.Length == 0)
                    return OpFail("INVALID_URL", "url is required");
                if (name == "browser_open")
                    return await AgentBrowserOp(
                            bridge, "embedded-browser:create",
                            new JsonObject
                            {
                                ["id"] = sessionId,
                                ["ownerModule"] = _env.ToolId,
                                ["url"] = url,
                            }, ct)
                        .ConfigureAwait(false);
                return await AgentBrowserOp(
                        bridge, "embedded-browser:navigate",
                        new JsonObject
                        {
                            ["id"] = sessionId, ["url"] = url,
                        }, ct)
                    .ConfigureAwait(false);
            }
            case "browser_read":
                return await AgentBrowserOp(
                        bridge, "embedded-browser:execute",
                        new JsonObject
                        {
                            ["id"] = sessionId,
                            ["script"] = DomDigestScript,
                        }, ct)
                    .ConfigureAwait(false);
            case "browser_click":
                return await AgentBrowserOp(
                        bridge, "embedded-browser:execute",
                        new JsonObject
                        {
                            ["id"] = sessionId,
                            ["script"] = ClickScript(
                                args["index"]?.GetValue<int>() ?? -1),
                        }, ct)
                    .ConfigureAwait(false);
            case "browser_fill":
                return await AgentBrowserOp(
                        bridge, "embedded-browser:execute",
                        new JsonObject
                        {
                            ["id"] = sessionId,
                            ["script"] = FillScript(
                                args["index"]?.GetValue<int>() ?? -1,
                                args["value"]?.GetValue<string>() ?? ""),
                        }, ct)
                    .ConfigureAwait(false);
            case "browser_eval":
            {
                var script =
                    args["script"]?.GetValue<string>()?.Trim() ?? "";
                if (script.Length == 0)
                    return OpFail("INVALID_SCRIPT", "script is required");
                return await AgentBrowserOp(
                        bridge, "embedded-browser:execute",
                        new JsonObject
                        {
                            ["id"] = sessionId, ["script"] = script,
                        }, ct)
                    .ConfigureAwait(false);
            }
            case "browser_close":
                return await AgentBrowserOp(
                        bridge, "embedded-browser:close",
                        new JsonObject { ["id"] = sessionId }, ct)
                    .ConfigureAwait(false);
            default:
                return OpFail("UNSUPPORTED_OP", $"unsupported op: {name}");
        }
    }

    private static JsonObject OpFail(string code, string message) =>
        new()
        {
            ["ok"] = false,
            ["error_code"] = code,
            ["message"] = message,
        };

    /// Shared infer call for the agent lane — same contract as
    /// SendMessage's /v1/infer POST (the reply carries ``text``,
    /// ``tool_call`` and ``tool_call_error``).
    private async Task<JsonObject> InferAsync(
        ServiceEndpoint endpoint, JsonObject inferBody,
        CancellationToken ct)
    {
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
        using var inferTimeout = CancellationTokenSource
            .CreateLinkedTokenSource(ct);
        inferTimeout.CancelAfter(InferTimeout);
        using var response = await _http
            .SendAsync(request, inferTimeout.Token)
            .ConfigureAwait(false);
        return JsonNode.Parse(
            await response.Content
                .ReadAsStringAsync(inferTimeout.Token)
                .ConfigureAwait(false)) as JsonObject
            ?? new JsonObject { ["ok"] = false };
    }

    // ------------------------------------------------- agent scripts --

    // Interactive-element inventory: visible a/button/input/select/
    // textarea/role=button/link/clickable nodes, index-addressed so the
    // model perceives the page as a numbered action surface.
    private const string ElementQuery =
        "[...document.querySelectorAll('a,button,input,select,textarea," +
        "[role=\"button\"],[role=\"link\"],[onclick]," +
        "[contenteditable=\"true\"]')].filter(e=>{" +
        "const r=e.getBoundingClientRect();" +
        "return r.width>0&&r.height>0&&e.offsetParent!==null;})" +
        ".slice(0,120)";

    private const string DomDigestScript =
        "(()=>{const els=" + ElementQuery + ";" +
        "const items=els.map((el,i)=>({i," +
        "tag:el.tagName.toLowerCase()," +
        "type:(el.type||el.getAttribute('role')||'').toLowerCase()," +
        "text:(el.innerText||el.value||el.getAttribute('aria-label')||" +
        "el.getAttribute('placeholder')||'')" +
        ".replace(/\\s+/g,' ').trim().slice(0,80)," +
        "id:el.id||'',name:el.name||''," +
        "href:el.tagName==='A'?el.href:''}));" +
        "return {url:location.href,title:document.title," +
        "elements:items};})()";

    private static string ClickScript(int index) =>
        "(()=>{const els=" + ElementQuery + ";" +
        $"const el=els[{index}];" +
        "if(!el)return {ok:false,error:'ELEMENT_NOT_FOUND'};" +
        "el.scrollIntoView({block:'center'});el.click();" +
        "return {ok:true,tag:el.tagName.toLowerCase()," +
        "text:(el.innerText||el.value||'').trim().slice(0,80)};})()";

    private static string FillScript(int index, string value) =>
        "(()=>{const els=" + ElementQuery + ";" +
        $"const el=els[{index}];" +
        "if(!el)return {ok:false,error:'ELEMENT_NOT_FOUND'};" +
        $"el.focus();el.value={JsonSerializer.Serialize(value)};" +
        "el.dispatchEvent(new Event('input',{bubbles:true}));" +
        "el.dispatchEvent(new Event('change',{bubbles:true}));" +
        "return {ok:true,tag:el.tagName.toLowerCase()," +
        "value:(el.value||'').slice(0,80)};})()";

    private const string AgentSystemPrompt =
        "你是星澄（本地代理），可以直接操作內嵌瀏覽器完成使用者的網頁任務。" +
        "當需要操作網頁時，只輸出一行工具呼叫：" +
        "<tool_call>{\"name\":\"<工具>\",\"arguments\":{...}}</tool_call>" +
        "可用工具：" +
        "browser_open {\"url\":\"...\"} 開啟網頁；" +
        "browser_navigate {\"url\":\"...\"} 前往網址；" +
        "browser_read {} 讀取頁面可互動元件（回傳編號清單）；" +
        "browser_click {\"index\":N} 點擊第 N 號元件；" +
        "browser_fill {\"index\":N,\"value\":\"...\"} 在第 N 號輸入框填入文字；" +
        "browser_eval {\"script\":\"...\"} 執行 JavaScript；" +
        "browser_close {} 關閉瀏覽器；" +
        "task_done {\"answer\":\"...\"} 回報最終答案。" +
        "每次只輸出一個 tool_call；看到 [tool_result] 後再決定下一步；" +
        "不再需要操作時直接輸出答案文字或呼叫 task_done。";

    // ----------------------------------------------------- diagnostics --

    private async Task<JsonObject> CodexAlignmentResult(
        CancellationToken ct)
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

        // Deep lane: architecture registry × docs report via the
        // governed CodexPipeline (read-only --arch-docs verb).
        var archDocs = await CodexDiagnostics.RunAsync(
            _env, "--arch-docs", ct).ConfigureAwait(false);
        CodexDiagnostics.AppendChecks(
            checks, report, "codex-arch-docs", archDocs);

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

    private async Task<JsonObject> ArchitectureSyncResult(
        CancellationToken ct)
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

        // Deep lane: live zh-TW mirror validation via the governed
        // CodexPipeline (read-only --mirror-check verb).
        var mirror = await CodexDiagnostics.RunAsync(
            _env, "--mirror-check", ct).ConfigureAwait(false);
        CodexDiagnostics.AppendChecks(
            checks, report, "codex-mirror", mirror);

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
