using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace StarBusinessLogic.Application;

// P11/MS6：熱路徑去 Python 中介——直接在 C# 行程內載入原生推論引擎。
// 引擎映像即 dist-native/_xingcheng_inference*.pyd（.pyd 本身就是 DLL，
// xc_engine_* C ABI 與 pybind11 模組共用同一映像）。
//
// 治理邊界不變：此 client 只是**傳輸替換**（HTTP loopback → 同行程 ABI），
// 裁決/權限/稽核仍在上游 GovernedIpcClient 與編排層；契約失敗一律
// fail-closed（回傳例外，不偽造回應）。bundle 目錄由受管設定提供。
public sealed class NativeModelClient : IModelClient, IDisposable
{
    // ---- C ABI (xingcheng_engine_c.h) ----
    private delegate IntPtr CreateDelegate();
    private delegate void DestroyDelegate(IntPtr engine);
    private delegate int LoadDelegate(IntPtr engine, [MarshalAs(UnmanagedType.LPUTF8Str)] string bundleDir, byte[] err, nuint errCap);
    private delegate int LoadedDelegate(IntPtr engine);
    private delegate void UnloadDelegate(IntPtr engine);
    private delegate int GenerateTextDelegate(IntPtr engine, [MarshalAs(UnmanagedType.LPUTF8Str)] string prompt, long maxNewTokens, double temperature, int doSample, byte[]? outBuf, ref nuint outLen, byte[] err, nuint errCap);
    private delegate int GenerateExDelegate(IntPtr engine, [MarshalAs(UnmanagedType.LPUTF8Str)] string prompt, long maxNewTokens, double temperature, int doSample, byte[]? outBuf, ref nuint outLen, long[]? idsBuf, ref nuint idsLen, byte[] err, nuint errCap);
    private delegate int DescribeDelegate(IntPtr engine, byte[]? outBuf, ref nuint outLen, byte[] err, nuint errCap);

    private readonly IntPtr _lib;
    private readonly IntPtr _engine;
    private readonly GenerateTextDelegate _generateText;
    private readonly GenerateExDelegate _generateEx;
    private readonly DescribeDelegate _describe;
    private readonly UnloadDelegate _unload;
    private readonly DestroyDelegate _destroy;
    private readonly string _modelId;
    private readonly string? _toolRoot;
    private readonly string _bundleDir;
    private readonly SemaphoreSlim _gate = new(1, 1); // 引擎非執行緒安全——序列化呼叫
    private bool _disposed;

    private static T Bind<T>(IntPtr lib, string name) where T : Delegate
    {
        var ptr = NativeLibrary.GetExport(lib, name);
        if (ptr == IntPtr.Zero) throw new InvalidOperationException($"XC_ABI_EXPORT_MISSING:{name}");
        return Marshal.GetDelegateForFunctionPointer<T>(ptr);
    }

    private static string ReadErr(byte[] err)
    {
        var end = Array.IndexOf(err, (byte)0);
        return Encoding.UTF8.GetString(err, 0, end < 0 ? err.Length : end);
    }

    // CUDA 建置的引擎映像依賴 toolkit 的 cudart64_*/cublas64_* 等 DLL，
    // 這些不在 .NET 預設搜尋路徑（NativeLibrary.Load 不含 USER_DIRS）。
    // 先以絕對路徑載入 toolkit bin 下的 CUDA 執行期 DLL——已載入模組
    // 會直接滿足引擎映像的同名 import。找不到 toolkit 時靜默略過：
    // CPU 映像本來就不需要；CUDA 映像仍由 NativeLibrary.Load
    // fail-closed 拋出，語義不變。
    private static void PreloadCudaRuntime()
    {
        if (!OperatingSystem.IsWindows()) return;
        var roots = new List<string>();
        var cudaPath = Environment.GetEnvironmentVariable("CUDA_PATH");
        if (!string.IsNullOrEmpty(cudaPath)) roots.Add(cudaPath);
        var toolkitRoot = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            "NVIDIA GPU Computing Toolkit", "CUDA");
        if (Directory.Exists(toolkitRoot))
            roots.AddRange(Directory.GetDirectories(toolkitRoot, "v*"));
        foreach (var bin in roots.Distinct().Select(r => Path.Combine(r, "bin")))
        {
            if (!Directory.Exists(bin)) continue;
            foreach (var pattern in new[] { "cudart64_*.dll", "cublas64_*.dll", "cublasLt64_*.dll" })
                foreach (var dll in Directory.GetFiles(bin, pattern))
                    try { NativeLibrary.Load(dll); }
                    catch { /* best-effort preload — 引擎載入才是裁決點 */ }
        }
    }

    public NativeModelClient(string engineImagePath, string bundleDir, string modelId = "xingcheng-native", string? toolRoot = null)
    {
        if (!File.Exists(engineImagePath)) throw new InvalidOperationException($"XC_ENGINE_IMAGE_MISSING:{engineImagePath}");
        PreloadCudaRuntime();
        _lib = NativeLibrary.Load(engineImagePath); // 找不到相依 → 拋出，fail-closed
        var create = Bind<CreateDelegate>(_lib, "xc_engine_create");
        var load = Bind<LoadDelegate>(_lib, "xc_engine_load");
        var loaded = Bind<LoadedDelegate>(_lib, "xc_engine_loaded");
        _unload = Bind<UnloadDelegate>(_lib, "xc_engine_unload");
        _destroy = Bind<DestroyDelegate>(_lib, "xc_engine_destroy");
        _generateText = Bind<GenerateTextDelegate>(_lib, "xc_engine_generate_text");
        _generateEx = Bind<GenerateExDelegate>(_lib, "xc_engine_generate_ex");
        _describe = Bind<DescribeDelegate>(_lib, "xc_engine_describe");
        _modelId = modelId;
        _toolRoot = toolRoot;
        _bundleDir = bundleDir;

        _engine = create();
        if (_engine == IntPtr.Zero) throw new InvalidOperationException("XC_ENGINE_CREATE_FAILED");
        var err = new byte[1024];
        if (load(_engine, bundleDir, err, (nuint)err.Length) != 0)
            throw new InvalidOperationException($"XC_ENGINE_LOAD_FAILED:{ReadErr(err)}");
        if (loaded(_engine) == 0) throw new InvalidOperationException("XC_ENGINE_NOT_LOADED");
    }

    public async Task<ModelInferenceResponse> InferAsync(ModelInferenceRequest request, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(request.Prompt)) throw new ArgumentException("PROMPT_REQUIRED", nameof(request));
        ObjectDisposedException.ThrowIf(_disposed, this);
        var started = Environment.TickCount64;
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            return await Task.Run(() =>
            {
                var err = new byte[1024];
                nuint len = 0;
                nuint idLen = 0;
                // 兩段式緩衝：先取 text＋ids 所需大小，再一次取回。
                // generate_ex 一次生成同時回傳 text 與生成 token ids（不含 prompt）。
                int rc = _generateEx(_engine, request.Prompt, request.MaxNewTokens,
                    request.Temperature, request.Temperature > 0 ? 1 : 0,
                    null, ref len, null, ref idLen, err, (nuint)err.Length);
                if (rc == 1) throw new InvalidOperationException($"XC_GENERATE_FAILED:{ReadErr(err)}");
                var buf = new byte[len + 1];
                var idsBuf = new long[idLen];
                len = (nuint)buf.Length; // *out_len 輸入即容量（ABI 契約）
                rc = _generateEx(_engine, request.Prompt, request.MaxNewTokens,
                    request.Temperature, request.Temperature > 0 ? 1 : 0,
                    buf, ref len, idsBuf, ref idLen, err, (nuint)err.Length);
                if (rc != 0) throw new InvalidOperationException($"XC_GENERATE_FAILED:{ReadErr(err)}");
                var text = Encoding.UTF8.GetString(buf, 0, (int)len);
                var tokenIds = new int[(int)idLen];
                for (var i = 0; i < tokenIds.Length; ++i) tokenIds[i] = (int)idsBuf[i];
                var latencyMs = Environment.TickCount64 - started;
                AppendExecutionLedger(request.Prompt, text, tokenIds.Length, latencyMs);
                return new ModelInferenceResponse(
                    text, tokenIds, _modelId,
                    latencyMs,
                    new Dictionary<string, object> { ["transport"] = "native-abi", ["dual_track"] = true });
            }, cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _gate.Release();
        }
    }

    // 審計鏈等價：HTTP 路徑的 Python 層每次生成都會寫
    // native-engine-executions.jsonl；native-abi 移除此中介後，稽核責任
    // 落到本 client——同一 ledger、同一 schema（transport=native-abi
    // 標記來源），與 Python 端一致採 fail-soft（ledger I/O 失敗不使
    // 推論失敗）。_toolRoot 為 null 時無法定位 ledger → 不寫（與
    // 未接線的舊行為相同，不偽造稽核）。
    private void AppendExecutionLedger(string prompt, string text, int evalCount, long latencyMs)
    {
        if (_toolRoot is null) return;
        try
        {
            var ledger = Path.Combine(
                _toolRoot, "xingcheng", "runtime", "logs",
                "native-engine-executions.jsonl");
            var (checkpoint, weightsSha) = ReadBundleIdentity();
            var entry = new Dictionary<string, object?>
            {
                ["at"] = DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'"),
                ["engine"] = "cpp",
                ["event"] = "cpp-runtime-execution",
                ["transport"] = "native-abi",
                ["host"] = "csharp-business-logic",
                ["checkpoint_path"] = checkpoint,
                ["bundle_dir"] = _bundleDir,
                ["weights_sha256"] = weightsSha,
                ["prompt_sha256"] = Sha256Hex(prompt),
                ["output_sha256"] = Sha256Hex(text),
                ["eval_count"] = evalCount,
                ["latency_ms"] = latencyMs,
                ["device"] = "cpu",
                ["third_party_foundation_weights"] = false,
                ["loopback_runtime_used"] = false,
            };
            Directory.CreateDirectory(Path.GetDirectoryName(ledger)!);
            File.AppendAllText(
                ledger,
                JsonSerializer.Serialize(entry) + "\n",
                new UTF8Encoding(false));
        }
        catch (IOException) { /* fail-soft — 與 Python _ledger_append 相同 */ }
        catch (UnauthorizedAccessException) { }
    }

    private (string? Checkpoint, string? WeightsSha) ReadBundleIdentity()
    {
        try
        {
            var manifestPath = Path.Combine(_bundleDir, "manifest.json");
            if (!File.Exists(manifestPath)) return (null, null);
            using var doc = JsonDocument.Parse(File.ReadAllText(manifestPath));
            var root = doc.RootElement;
            return (
                root.TryGetProperty("source_checkpoint", out var sc) ? sc.GetString() : null,
                root.TryGetProperty("weights_sha256", out var ws) ? ws.GetString() : null);
        }
        catch (JsonException) { return (null, null); }
        catch (IOException) { return (null, null); }
    }

    private static string Sha256Hex(string value)
    {
        var hash = SHA256.HashData(Encoding.UTF8.GetBytes(value));
        return Convert.ToHexString(hash).ToLowerInvariant();
    }

    public string Describe()
    {
        var err = new byte[1024];
        nuint len = 0;
        if (_describe(_engine, null, ref len, err, (nuint)err.Length) == 1)
            throw new InvalidOperationException($"XC_DESCRIBE_FAILED:{ReadErr(err)}");
        var buf = new byte[len + 1];
        len = (nuint)buf.Length; // *out_len 輸入即容量（ABI 契約）
        if (_describe(_engine, buf, ref len, err, (nuint)err.Length) != 0)
            throw new InvalidOperationException($"XC_DESCRIBE_FAILED:{ReadErr(err)}");
        return Encoding.UTF8.GetString(buf, 0, (int)len);
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _gate.Wait();
        try
        {
            _unload(_engine);
            _destroy(_engine);
            NativeLibrary.Free(_lib);
        }
        finally
        {
            _gate.Release();
        }
    }
}
