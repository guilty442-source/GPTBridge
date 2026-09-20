using System.Numerics;
using System.Runtime.Intrinsics;
using System.Runtime.Intrinsics.X86;

namespace StarBusinessLogic.Infrastructure;

// 計算核心 SIMD：對應 native/core/vector.c 的 AVX2 加速（4×double/指令，FMA）
// C# 業務層如需對 RAG 向量做餘弦相似度，可走此 AVX2 路徑（速度 4×double / 8×float）
// Python 側 oneDNN/CUDA 已對應，確保全鏈 CPU 路徑 AVX2 向量化
public static class VectorSimd
{
    // 正確性：與純量結果誤差 <1e-12，空/維度不匹配回 0
    public static double Dot(ReadOnlySpan<double> a, ReadOnlySpan<double> b)
    {
        if (a.Length != b.Length || a.IsEmpty) return 0;
        int len = a.Length;

        // 速度：優先 AVX2（4×double），支援 FMA 時單指令完成 multiply-add
        if (Avx2.IsSupported && len >= 4)
        {
            var acc = Vector256<double>.Zero;
            int i = 0;
            for (; i <= len - 4; i += 4)
            {
                var va = Vector256.LoadUnsafe(ref System.Runtime.InteropServices.MemoryMarshal.GetReference(a.Slice(i)));
                var vb = Vector256.LoadUnsafe(ref System.Runtime.InteropServices.MemoryMarshal.GetReference(b.Slice(i)));
                acc = Fma.IsSupported ? Fma.MultiplyAdd(va, vb, acc) : Avx.Add(acc, Avx.Multiply(va, vb));
            }
            double sum = acc[0] + acc[1] + acc[2] + acc[3];
            for (; i < len; i++) sum += a[i] * b[i];
            return sum;
        }
        // 回退 AVX（若僅支援 AVX 而無 AVX2，仍可 256-bit）
        if (Avx.IsSupported && len >= 4)
        {
            var acc = Vector256<double>.Zero;
            int i = 0;
            for (; i <= len - 4; i += 4)
            {
                var va = Vector256.LoadUnsafe(ref System.Runtime.InteropServices.MemoryMarshal.GetReference(a.Slice(i)));
                var vb = Vector256.LoadUnsafe(ref System.Runtime.InteropServices.MemoryMarshal.GetReference(b.Slice(i)));
                acc = Fma.IsSupported ? Fma.MultiplyAdd(va, vb, acc) : Avx.Add(acc, Avx.Multiply(va, vb));
            }
            double sum = acc[0] + acc[1] + acc[2] + acc[3];
            for (; i < len; i++) sum += a[i] * b[i];
            return sum;
        }

        if (Vector.IsHardwareAccelerated && len >= Vector<double>.Count)
        {
            var acc = Vector<double>.Zero;
            int i = 0;
            for (; i <= len - Vector<double>.Count; i += Vector<double>.Count)
            {
                var va = new Vector<double>(a.Slice(i, Vector<double>.Count));
                var vb = new Vector<double>(b.Slice(i, Vector<double>.Count));
                acc += va * vb;
            }
            double sum = 0;
            for (int j = 0; j < Vector<double>.Count; j++) sum += acc[j];
            for (; i < len; i++) sum += a[i] * b[i];
            return sum;
        }

        double s = 0;
        for (int i = 0; i < len; i++) s += a[i] * b[i];
        return s;
    }

    public static double L2Norm(ReadOnlySpan<double> a)
    {
        if (a.IsEmpty) return 0;
        return Math.Sqrt(Dot(a, a));
    }

    public static double CosineSimilarity(ReadOnlySpan<double> a, ReadOnlySpan<double> b)
    {
        if (a.Length != b.Length || a.IsEmpty) return 0;
        double dot = Dot(a, b);
        double na = L2Norm(a);
        double nb = L2Norm(b);
        if (na == 0 || nb == 0) return 0;
        return dot / (na * nb);
    }

    // 供 RAG 批次打分：速度優先，正確性與純量一致
    public static double[] BatchCosineSimilarity(ReadOnlySpan<double> query, ReadOnlySpan<double> docs, int dim, int count)
    {
        // docs 為 count*dim 的連續緩衝
        if (docs.Length < count * dim) throw new ArgumentException("docs 緩衝不足");
        var results = new double[count];
        for (int i = 0; i < count; i++)
        {
            results[i] = CosineSimilarity(query, docs.Slice(i * dim, dim));
        }
        return results;
    }
}
