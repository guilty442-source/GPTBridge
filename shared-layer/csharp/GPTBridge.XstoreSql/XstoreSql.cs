using System.Runtime.InteropServices;
using System.Text.Json;

namespace GPTBridge.XstoreSql;

/// <summary>
/// Managed binding over the xstore native-SQL C ABI (``xstore_sql.h``).
/// One in-process lane, no external SQL tool, service or subprocess:
/// bounded read-only SELECT over a migrated canonical store, evaluated
/// by the Rust engine inside ``xstore.dll`` (cdylib).
///
/// Contract: the request is a caller-owned UTF-8 JSON envelope
/// (``xstore-native-sql-request/v1``, ≤1 MiB); the reply is an
/// engine-owned buffer (≤8 MiB) that must be released exactly once with
/// ``xstore_sql_buffer_free``. Unsupported statements, unknown stores,
/// unregistered tables/columns and every bound violation fail closed as
/// an error envelope — surfaced here as <see cref="InvalidDataException"/>
/// carrying the engine's ``NATIVE_SQL_*`` reason.
/// </summary>
public sealed class XstoreSql : IDisposable
{
    public const string RequestFormat = "xstore-native-sql-request/v1";
    public const string ResultFormat = "xstore-native-sql-result/v1";
    private const int MaxInputBytes = 1024 * 1024;

    [StructLayout(LayoutKind.Sequential)]
    private struct XstoreSqlBuffer
    {
        public IntPtr Data;
        public UIntPtr Length;
    }

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate XstoreSqlBuffer QueryDelegate(IntPtr data, UIntPtr length);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int FreeDelegate(IntPtr data);

    private readonly IntPtr library;
    private readonly QueryDelegate query;
    private readonly FreeDelegate free;
    private bool disposed;

    /// <summary>Resolve ``xstore`` through the default loader search
    /// order (the cdylib staged next to the consuming executable).</summary>
    public XstoreSql() : this("xstore") { }

    /// <summary>Bind an explicit cdylib path or loader-resolvable name.</summary>
    public XstoreSql(string libraryPath)
    {
        library = NativeLibrary.Load(libraryPath);
        query = Marshal.GetDelegateForFunctionPointer<QueryDelegate>(
            NativeLibrary.GetExport(library, "xstore_sql_query_json"));
        free = Marshal.GetDelegateForFunctionPointer<FreeDelegate>(
            NativeLibrary.GetExport(library, "xstore_sql_buffer_free"));
    }

    /// <summary>Run one bounded SELECT against a migrated store
    /// directory. ``parameters`` binds ``$1``…``$n`` positionally;
    /// values are serialized into the request envelope — never into
    /// the SQL text.</summary>
    public IReadOnlyList<IReadOnlyDictionary<string, JsonElement>> Query(
        string store, string sql, IReadOnlyList<object?>? parameters = null)
    {
        ObjectDisposedException.ThrowIf(disposed, this);
        var request = new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["format"] = RequestFormat,
            ["store"] = store,
            ["sql"] = sql,
            ["params"] = parameters ?? (IReadOnlyList<object?>)Array.Empty<object?>(),
        };
        var input = JsonSerializer.SerializeToUtf8Bytes(request);
        if (input.Length == 0 || input.Length > MaxInputBytes)
            throw Error("INPUT_LIMIT");
        var reply = Invoke(input);
        return Rows(reply);
    }

    private byte[] Invoke(byte[] input)
    {
        XstoreSqlBuffer result;
        unsafe
        {
            fixed (byte* pinned = input)
                result = query((IntPtr)pinned, (UIntPtr)input.Length);
        }
        if (result.Data == IntPtr.Zero)
            throw Error("CALL_FAILED");
        try
        {
            var length = checked((int)result.Length);
            var output = new byte[length];
            Marshal.Copy(result.Data, output, 0, length);
            return output;
        }
        finally
        {
            free(result.Data);
        }
    }

    private static IReadOnlyList<IReadOnlyDictionary<string, JsonElement>>
        Rows(byte[] reply)
    {
        using var document = JsonDocument.Parse(reply);
        var root = document.RootElement;
        if (root.ValueKind != JsonValueKind.Object
            || !root.TryGetProperty("format", out var format)
            || format.GetString() != ResultFormat)
            throw Error("ENVELOPE_VERSION");
        if (!root.TryGetProperty("ok", out var ok) || ok.ValueKind is not
            (JsonValueKind.True or JsonValueKind.False))
            throw Error("ENVELOPE_INVALID");
        if (!ok.GetBoolean())
            throw Error(root.TryGetProperty("error", out var error)
                && error.ValueKind == JsonValueKind.String
                ? error.GetString()! : "REPLY_INVALID");
        if (!root.TryGetProperty("rows", out var rows)
            || rows.ValueKind != JsonValueKind.Array)
            throw Error("ENVELOPE_INVALID");
        var result = new List<IReadOnlyDictionary<string, JsonElement>>(
            rows.GetArrayLength());
        foreach (var row in rows.EnumerateArray())
        {
            if (row.ValueKind != JsonValueKind.Object)
                throw Error("ROW_INVALID");
            var fields = new Dictionary<string, JsonElement>(
                StringComparer.Ordinal);
            foreach (var property in row.EnumerateObject())
                fields[property.Name] = property.Value.Clone();
            result.Add(new System.Collections.ObjectModel
                .ReadOnlyDictionary<string, JsonElement>(fields));
        }
        return result;
    }

    private static InvalidDataException Error(string reason)
    {
        const string prefix = "NATIVE_SQL_";
        return new InvalidDataException(reason.StartsWith(prefix,
            StringComparison.Ordinal) ? reason : prefix + reason);
    }

    public void Dispose()
    {
        if (disposed)
            return;
        disposed = true;
        NativeLibrary.Free(library);
    }
}
