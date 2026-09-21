using StarBusinessLogic.Infrastructure;

namespace StarBusinessLogic.Tests;

public class AutoReleaseTests
{
    [Fact]
    public void Release_ShouldInvokeCallbackOnce()
    {
        var released = 0;
        using var manager = new AutoReleaseManager(TimeSpan.FromHours(1), TimeSpan.FromHours(1));
        manager.Register("model", new object(), _ => Interlocked.Increment(ref released));

        Assert.True(manager.Release("model"));
        Assert.False(manager.Release("model"));
        Assert.Equal(1, released);
    }

    [Fact]
    public async Task IdleResource_ShouldBeReleasedAutomatically()
    {
        var released = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        using var manager = new AutoReleaseManager(TimeSpan.FromMilliseconds(30), TimeSpan.FromMilliseconds(10));
        manager.Register("model", new object(), _ => released.TrySetResult());

        var completed = await Task.WhenAny(released.Task, Task.Delay(TimeSpan.FromSeconds(2)));
        Assert.Same(released.Task, completed);
    }

    [Fact]
    public async Task Touch_ShouldDelayIdleRelease()
    {
        var released = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        using var manager = new AutoReleaseManager(TimeSpan.FromMilliseconds(120), TimeSpan.FromMilliseconds(10));
        manager.Register("model", new object(), _ => released.TrySetResult());

        for (var i = 0; i < 5; i++)
        {
            await Task.Delay(30);
            manager.Touch("model");
        }
        Assert.False(released.Task.IsCompleted);

        var completed = await Task.WhenAny(released.Task, Task.Delay(TimeSpan.FromSeconds(2)));
        Assert.Same(released.Task, completed);
    }

    [Fact]
    public void Dispose_ShouldReleaseRegisteredResources()
    {
        var released = 0;
        var manager = new AutoReleaseManager(TimeSpan.FromHours(1), TimeSpan.FromHours(1));
        manager.Register("model", new object(), _ => Interlocked.Increment(ref released));

        manager.Dispose();
        Assert.Equal(1, released);
    }

    [Fact]
    public void Extension_ShouldRegisterOnGlobalManager()
    {
        var released = 0;
        var key = $"extension-{Guid.NewGuid():N}";
        new object().AutoRelease(key, _ => Interlocked.Increment(ref released));

        Assert.True(AutoReleaseManager.Global.Release(key));
        Assert.Equal(1, released);
    }
}
