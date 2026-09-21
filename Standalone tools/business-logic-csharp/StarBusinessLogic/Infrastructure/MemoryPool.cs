using System.Buffers;

namespace StarBusinessLogic.Infrastructure;

// 對應 native 池與 Python 張量池：C# 業務層的記憶體池
// 速度：重用 RAG 向量、批次打分緩衝，避免高頻 new[]；正確性：歸還前清零防洩漏
public static class StarMemoryPool
{
    private const int MaxPooledSize = 8192; // 超過則不池化，直接分配
    private const int MaxPoolPerSize = 8;

    // 速度：按長度分池，命中時零分配
    public static double[] Rent(int length)
    {
        if (length <= 0) return Array.Empty<double>();
        if (length > MaxPooledSize) return new double[length];
        return ArrayPool<double>.Shared.Rent(length);
    }

    public static void Return(double[] array, bool clear = true)
    {
        if (array == null || array.Length == 0 || array.Length > MaxPooledSize) return;
        if (clear) Array.Clear(array, 0, array.Length);
        ArrayPool<double>.Shared.Return(array, clearArray: false);
    }

    // 正確性：Span 切片不擁有，池化僅用於完整陣列
    public static IMemoryOwner<double> RentOwner(int length)
    {
        if (length > MaxPooledSize) return new SimpleOwner(new double[length]);
        var arr = ArrayPool<double>.Shared.Rent(length);
        return new PooledOwner(arr, length);
    }

    private sealed class SimpleOwner : IMemoryOwner<double>
    {
        public double[] Array { get; }
        public Memory<double> Memory => Array;
        public SimpleOwner(double[] arr) => Array = arr;
        public void Dispose() { }
    }

    private sealed class PooledOwner : IMemoryOwner<double>
    {
        private double[] _arr;
        private readonly int _len;
        public PooledOwner(double[] arr, int len) { _arr = arr; _len = len; }
        public Memory<double> Memory => new Memory<double>(_arr, 0, _len);
        public void Dispose()
        {
            if (_arr != null)
            {
                Array.Clear(_arr, 0, _len);
                ArrayPool<double>.Shared.Return(_arr, clearArray: false);
                _arr = null!;
            }
        }
    }
}
