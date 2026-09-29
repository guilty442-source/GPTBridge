namespace GPTBridge.Permission;

/// <summary>
/// ``GPTBridge.Permission.exe`` — resident automation host for the
/// permission plane (native successor of the retired Python permission
/// duty modules, B167/B38).  CLI:
///
///   --watch [--interval s]   resident loop; each enabled flow runs at
///                            its own ``interval_s`` from
///                            ``main-system/config/automation-flows.json``
///                            (re-read every tick — ``enabled=false`` is a
///                            live kill switch)
///   --once                   run every enabled flow exactly once, print
///                            the result map, exit
///   --status                 print the latest persisted state, exit
///   --root <path>            repo-root override (else ``GPTBRIDGE_ROOT``
///                            or ancestor walk for the governance marker)
///
/// Single instance is enforced by ``permission-automation.lock`` in
/// ``main-system/runtime/state``; process lifetime is supervised by the
/// Rust ``permission_host`` lane (managed_by=main-system), cadence is
/// owned here.
/// </summary>
internal static class Program
{
    public static async Task<int> Main(string[] args)
    {
        var watch = false;
        var once = false;
        var status = false;
        string? root = null;
        double? interval = null;
        for (var i = 0; i < args.Length; i++)
        {
            string Value()
            {
                if (i + 1 >= args.Length)
                    throw new ArgumentException(
                        $"missing value for {args[i]}");
                return args[++i];
            }
            switch (args[i])
            {
                case "--watch": watch = true; break;
                case "--once": once = true; break;
                case "--status": status = true; break;
                case "--root": root = Value(); break;
                case "--interval":
                    interval = double.Parse(Value(),
                        System.Globalization.CultureInfo.InvariantCulture);
                    break;
                default:
                    Console.Error.WriteLine($"unknown arg: {args[i]}");
                    return 2;
            }
        }
        if (root is not null)
            PermissionAutomation.SetRoot(root);
        if (status)
        {
            Console.Out.WriteLine(PermissionAutomation.Status());
            return 0;
        }
        if (once)
            return await PermissionAutomation.RunOnceAll();
        if (watch)
            return await PermissionAutomation.RunWatch(interval);
        Console.Error.WriteLine(
            "usage: GPTBridge.Permission --watch|--once|--status " +
            "[--interval s] [--root path]");
        return 2;
    }
}
