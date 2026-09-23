using System.Runtime.InteropServices;
using System.Text;

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

    public NativeModelClient(string engineImagePath, string bundleDir, string modelId = "xingcheng-native")
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
                return new ModelInferenceResponse(
                    text, tokenIds, _modelId,
                    Environment.TickCount64 - started,
                    new Dictionary<string, object> { ["transport"] = "native-abi", ["dual_track"] = true });
            }, cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _gate.Release();
        }
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
