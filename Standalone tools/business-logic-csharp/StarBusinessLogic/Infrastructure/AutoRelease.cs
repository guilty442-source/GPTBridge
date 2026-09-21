using System.Collections.Concurrent;

namespace StarBusinessLogic.Infrastructure;

// 對應 Python AutoReleaseManager：C# 業務層資源閒置自動釋放
// 速度：避免長期持有大陣列；正確性：釋放後可重建，不影響業務
public sealed class AutoReleaseManager : IDisposable
{
    private readonly ConcurrentDictionary<string, (WeakReference<object> Ref, DateTime LastUsed, Action<object> Release, long Size)> _resources = new();
    private readonly Timer _timer;
    private readonly TimeSpan _idle;

    public AutoReleaseManager(TimeSpan? idle = null, TimeSpan? checkInterval = null)
    {
        _idle = idle ?? TimeSpan.FromMinutes(5);
        var interval = checkInterval ?? TimeSpan.FromMinutes(1);
        if (interval <= TimeSpan.Zero) throw new ArgumentOutOfRangeException(nameof(checkInterval));
        _timer = new Timer(Check, null, interval, interval);
    }

    public void Register(string key, object obj, Action<object> release, long size = 0)
    {
        _resources[key] = (new WeakReference<object>(obj), DateTime.UtcNow, release, size);
    }

    public void Touch(string key)
    {
        if (_resources.TryGetValue(key, out var v))
            _resources[key] = (v.Ref, DateTime.UtcNow, v.Release, v.Size);
    }

    public bool Release(string key)
    {
        if (!_resources.TryRemove(key, out var v)) return false;
        if (v.Ref.TryGetTarget(out var obj))
        {
            try { v.Release(obj); } catch { }
        }
        return true;
    }

    private void Check(object? _)
    {
        var now = DateTime.UtcNow;
        foreach (var kv in _resources)
        {
            if (now - kv.Value.LastUsed > _idle)
                Release(kv.Key);
        }
        // 壓力檢查：若記憶體 >80%，釋放最久未用者
        var mem = GC.GetGCMemoryInfo();
        double pressure = (double)mem.HeapSizeBytes / mem.TotalAvailableMemoryBytes;
        if (pressure > 0.8)
        {
            var oldest = _resources.OrderBy(kv => kv.Value.LastUsed).FirstOrDefault();
            if (oldest.Key != null) Release(oldest.Key);
        }
    }

    public void Dispose()
    {
        _timer.Dispose();
        foreach (var k in _resources.Keys.ToList()) Release(k);
    }

    private static readonly Lazy<AutoReleaseManager> _global = new(() => new AutoReleaseManager());
    public static AutoReleaseManager Global => _global.Value;
}

public static class AutoReleaseExtensions
{
    public static void AutoRelease(this object obj, string key, Action<object> release, long size = 0)
        => AutoReleaseManager.Global.Register(key, obj, release, size);
}
