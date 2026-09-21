using StarBusinessLogic.Infrastructure;

namespace StarBusinessLogic.Tests;

public class VectorSimdTests
{
    [Fact]
    public void Dot_ShouldMatchScalar()
    {
        var a = new double[] { 1, 2, 3, 4, 5, 6, 7, 8 };
        var b = new double[] { 8, 7, 6, 5, 4, 3, 2, 1 };
        double expected = 0;
        for (int i = 0; i < a.Length; i++) expected += a[i] * b[i];
        double simd = VectorSimd.Dot(a, b);
        Assert.Equal(expected, simd, 10);
    }

    [Fact]
    public void Cosine_ShouldBeOneForIdentical()
    {
        var a = new double[] { 1, 0, 1, 0, 1, 0, 1, 0 };
        double c = VectorSimd.CosineSimilarity(a, a);
        Assert.Equal(1.0, c, 10);
    }

    [Fact]
    public void L2Norm_ShouldMatch()
    {
        var a = new double[] { 3, 4 };
        Assert.Equal(5.0, VectorSimd.L2Norm(a), 10);
    }

    [Fact]
    public void Batch_ShouldWork()
    {
        var q = new double[] { 1, 0 };
        var docs = new double[] { 1, 0, 0, 1, 1, 1 };
        var res = VectorSimd.BatchCosineSimilarity(q, docs, 2, 3);
        Assert.Equal(3, res.Length);
        Assert.Equal(1.0, res[0], 10);
        Assert.Equal(0.0, res[1], 10);
    }
}
