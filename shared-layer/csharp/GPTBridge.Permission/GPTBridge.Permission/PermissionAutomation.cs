using System.Text.Json.Nodes;

namespace GPTBridge.Permission;

/// <summary>
/// Permission-plane automation host — the resident loop that replaces
/// the retired Python permission duty drivers (B167/B38).  Owns the six
/// governed flows from ``main-system/config/automation-flows.json``
/// (re-read every tick so a ``enabled=false`` kill switch propagates
/// within one interval):
///
///   permission-automation-lifecycle   — grant expiry sweep,
///                                       revoke/suspend bookkeeping,
///                                       terminal cleanup.  Renewal
///                                       adjudication stays sovereign:
///                                       no delegate is injected, so an
///                                       expiring grant fails closed to
///                                       EXPIRED rather than silently
///                                       auto-renewing.
///   permission-automation-directory   — managed-registry sync hash
///                                       drift detection.
///   permission-automation-compliance  — violation ingestion/dedup,
///                                       directory integrity,
///                                       ledger-vs-directory drift
///                                       (A436), risk-score decay.
///   permission-automation-healing     — four health checks with
///                                       bounded delegate probes
///                                       (directory access, governance
///                                       connectivity, sovereign state,
///                                       directory permissions).
///                                       Repair delegates stay
///                                       sovereign-injected (A297): the
///                                       host records degraded issues
///                                       fail-closed.
///   permission-automation-audit       — bounded native audit-engine
///                                       invocation, metadata-only
///                                       history.
///   permission-automation-identity    — sealed-group registration,
///                                       sovereign-decided deletion of
///                                       unregistered retired-tool
///                                       groups (no decider is injected
///                                       so proposals remain
///                                       fail-closed pending),
///                                       metadata-only ledger.
///
/// State: ``main-system/runtime/state/permission-automation.json``;
/// single instance enforced by ``permission-automation.lock``.
/// </summary>
internal static class PermissionAutomation
{
    private const string LifecycleFlow = "permission-automation-lifecycle";
    private const string DirectoryFlow = "permission-automation-directory";
    private const string ComplianceFlow = "permission-automation-compliance";
    private const string HealingFlow = "permission-automation-healing";
    private const string AuditFlow = "permission-automation-audit";
    private const string IdentityFlow = "permission-automation-identity";

    private static readonly string[] FlowNames =
    {
        LifecycleFlow, DirectoryFlow, ComplianceFlow,
        HealingFlow, AuditFlow, IdentityFlow,
    };

    private const double DefaultTickSeconds = 15;
    private const double DefaultFlowInterval = 300;

    /// <summary>Per-flow execution bound.  Covers the audit flow's own
    /// 60 s engine timeout plus bounded overhead; any flow exceeding it
    /// is cancelled and recorded as ``error:timeout``.</summary>
    private static readonly TimeSpan FlowTimeout =
        TimeSpan.FromSeconds(150);

    // --------------------------------------------------------------
    //  repo root — same fail-closed discovery as CodexPipeline.Repo
    // --------------------------------------------------------------

    private static string? _root;

    public static void SetRoot(string root) =>
        _root = Path.GetFullPath(root);

    public static string Root()
    {
        if (_root is not null)
            return _root;
        var env = Environment.GetEnvironmentVariable("GPTBRIDGE_ROOT");
        if (!string.IsNullOrWhiteSpace(env))
        {
            _root = Path.GetFullPath(env);
            return _root;
        }
        var dir = new DirectoryInfo(Directory.GetCurrentDirectory());
        while (dir is not null)
        {
            if (Directory.Exists(Path.Combine(
                    dir.FullName, "governance_rule", "execution"))
                && Directory.Exists(
                    Path.Combine(dir.FullName, "main-system")))
            {
                _root = dir.FullName;
                return _root;
            }
            dir = dir.Parent;
        }
        throw new InvalidOperationException("REPO_ROOT_UNRESOLVED");
    }

    private static string StateDir() =>
        Path.Combine(Root(), "main-system", "runtime", "state");

    private static string FlowsPath() =>
        Path.Combine(Root(), "main-system", "config",
            "automation-flows.json");

    private static string StatePath() =>
        Path.Combine(StateDir(), "permission-automation.json");

    private static string UtcNow() =>
        DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'",
            System.Globalization.CultureInfo.InvariantCulture);

    // --------------------------------------------------------------
    //  flow config — fail-closed: unreadable/missing entry = disabled.
    // --------------------------------------------------------------

    private static JsonObject? Flow(string name)
    {
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(FlowsPath()));
            return node?["flows"]?[name] as JsonObject;
        }
        catch (IOException) { return null; }
        catch (UnauthorizedAccessException) { return null; }
        catch (System.Text.Json.JsonException) { return null; }
    }

    private static bool FlowEnabled(string name) =>
        Flow(name)?["enabled"]?.GetValue<bool>() ?? false;

    private static double FlowInterval(string name)
    {
        var raw = Flow(name)?["interval_s"];
        return raw is not null
            && double.TryParse(raw.ToString(), out var seconds)
            && seconds > 0
                ? seconds
                : DefaultFlowInterval;
    }

    // --------------------------------------------------------------
    //  injected probes — bounded, dependency-free governance checks
    // --------------------------------------------------------------

    /// <summary>Governance connectivity probe for the self-healing
    /// flow: the PostgreSQL authority DSN must resolve and accept a TCP
    /// connection inside a bounded window.  Missing DSN or unreachable
    /// endpoint both report disconnected — fail-closed.</summary>
    private static bool GovernanceConnected()
    {
        var dsn = Environment.GetEnvironmentVariable(
            "GPTBRIDGE_POSTGRES_DSN")?.Trim() ?? "";
        if (dsn.Length == 0)
            return false;
        string host;
        int port = 5432;
        if (dsn.StartsWith("postgresql://", StringComparison.Ordinal)
            || dsn.StartsWith("postgres://", StringComparison.Ordinal))
        {
            try
            {
                var uri = new Uri(dsn);
                host = uri.Host;
                if (uri.Port > 0)
                    port = uri.Port;
            }
            catch (UriFormatException) { return false; }
        }
        else
        {
            host = "";
            // libpq keyword form is space- or semicolon-separated.
            foreach (var pair in dsn.Split(
                new[] { ';', ' ', '\t' },
                StringSplitOptions.RemoveEmptyEntries))
            {
                var eq = pair.IndexOf('=');
                if (eq <= 0)
                    continue;
                var key = pair[..eq].Trim();
                var value = pair[(eq + 1)..].Trim();
                if (key.Equals("host",
                    StringComparison.OrdinalIgnoreCase))
                    host = value;
                else if (key.Equals("port",
                        StringComparison.OrdinalIgnoreCase)
                    && int.TryParse(value, out var parsed))
                    port = parsed;
            }
        }
        if (host.Length == 0)
            return false;
        try
        {
            using var client = new System.Net.Sockets.TcpClient();
            var connect = client.ConnectAsync(host, port);
            return connect.Wait(TimeSpan.FromMilliseconds(1500))
                && client.Connected;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>Sovereign-state probe: the release pin must resolve a
    /// non-empty sealed codex generation — the governed authority this
    /// runtime answers to.</summary>
    private static bool SovereignStateHealthy()
    {
        try
        {
            var pin = JsonNode.Parse(File.ReadAllText(Path.Combine(
                Root(), "shared-layer", "release-dependencies.json")));
            var version = pin?["governance_references"]?
                ["codex_version"]?.GetValue<string>();
            return !string.IsNullOrEmpty(version);
        }
        catch (IOException) { return false; }
        catch (UnauthorizedAccessException) { return false; }
        catch (System.Text.Json.JsonException) { return false; }
    }

    /// <summary>Tool-inventory status for the identity flow: a tool is
    /// ``retired`` when its manifest disables it or its runtime is the
    /// retired Python lane; missing/unreadable manifests report null so
    /// the lifecycle keeps the group fail-closed.</summary>
    private static string? ToolStatus(string toolId)
    {
        if (string.IsNullOrWhiteSpace(toolId))
            return null;
        var path = Path.Combine(Root(), "Standalone tools", toolId,
            "manifest.json");
        if (!File.Exists(path))
            return null;
        try
        {
            var manifest = JsonNode.Parse(File.ReadAllText(path));
            if (manifest?["enabled"]?.GetValue<bool>() == false)
                return "retired";
            var runtimeType = manifest?["runtime"]?["type"]
                ?.GetValue<string>();
            return runtimeType == "retired-python"
                ? "retired" : "active";
        }
        catch (IOException) { return null; }
        catch (UnauthorizedAccessException) { return null; }
        catch (System.Text.Json.JsonException) { return null; }
    }

    // --------------------------------------------------------------
    //  flow bindings — each entry owns one governed RunOnceAsync call
    // --------------------------------------------------------------

    private sealed class FlowBinding
    {
        public required string Name { get; init; }
        public required string Counter { get; init; }
        public required Func<CancellationToken, Task<object?>>
            Run { get; init; }
    }

    private static List<FlowBinding> Bindings()
    {
        var root = Root();
        var lifecycle = new PermissionLifecycleAutomation();
        var directory = new DirectorySyncAutomation(root);
        var compliance = new ComplianceMonitorAutomation(root);
        var healing = new SelfHealingAutomation(root,
            governanceConnected: GovernanceConnected,
            sovereignHealthy: SovereignStateHealthy);
        var audit = new AuditSchedulerAutomation(root);
        var identity = new IdentityGroupLifecycleAutomation(root,
            toolStatus: ToolStatus,
            ledgerPath: Path.Combine(StateDir(),
                "identity-group-lifecycle.jsonl"));
        var ledger = new PermissionGrantLedger(
            PermissionGrantLedger.DefaultLedgerPath(root));
        return new List<FlowBinding>
        {
            new()
            {
                Name = LifecycleFlow, Counter = "lifecycle_passes",
                Run = async ct =>
                {
                    await lifecycle.RunOnceAsync(ct);
                    return lifecycle.GetStats();
                },
            },
            new()
            {
                Name = DirectoryFlow, Counter = "directory_syncs",
                Run = async ct =>
                {
                    var drift = await directory.RunOnceAsync();
                    return new Dictionary<string, object?>
                    {
                        ["drift"] = drift,
                        ["status"] = directory.GetSyncStatus(),
                    };
                },
            },
            new()
            {
                Name = ComplianceFlow, Counter = "compliance_passes",
                Run = async ct =>
                {
                    await compliance.RunOnceAsync(ledger);
                    return new Dictionary<string, object?>
                    {
                        ["issues"] = compliance.LatestIssues,
                    };
                },
            },
            new()
            {
                Name = HealingFlow, Counter = "healing_passes",
                Run = async ct =>
                {
                    var issues = await healing.RunOnceAsync(ct);
                    return new Dictionary<string, object?>
                    {
                        ["degraded"] = healing.IsDegraded(),
                        ["issues"] = issues
                            .Select(i => i.ToString()).ToList(),
                    };
                },
            },
            new()
            {
                Name = AuditFlow, Counter = "audit_runs",
                Run = async ct =>
                {
                    var record = await audit.RunOnceAsync(ct);
                    return new Dictionary<string, object?>
                    {
                        ["passed"] = record.Passed,
                        ["error"] = record.Error,
                    };
                },
            },
            new()
            {
                Name = IdentityFlow, Counter = "identity_passes",
                Run = async ct =>
                {
                    var report = await identity.RunOnceAsync(ct);
                    var bounded = new Dictionary<string, object?>(
                        report, StringComparer.Ordinal)
                    {
                        ["pending_deletions"] =
                            identity.PendingDeletions.Count,
                    };
                    return bounded;
                },
            },
        };
    }

    // --------------------------------------------------------------
    //  resident loop
    // --------------------------------------------------------------

    private static void PersistState(JsonObject state)
    {
        Directory.CreateDirectory(StateDir());
        var temporary = StatePath() + ".tmp";
        File.WriteAllText(temporary, state.ToJsonString());
        File.Move(temporary, StatePath(), overwrite: true);
    }

    private static JsonObject ResultJson(object? result)
    {
        if (result is null)
            return new JsonObject();
        if (result is JsonObject obj)
            return obj;
        return (JsonObject)(JsonNode.Parse(
            System.Text.Json.JsonSerializer.Serialize(result))
            ?? new JsonObject());
    }

    /// <summary>Run one pass over every enabled flow — the entry point
    /// for both ``--once`` and the watch tick.  Fail-closed per flow:
    /// a disabled/missing flow entry is recorded ``disabled``, a thrown
    /// fault is recorded as a bounded ``error:<type>`` string, a flow
    /// exceeding <see cref="FlowTimeout"/> is cancelled.</summary>
    private static async Task<Dictionary<string, object?>> RunDue(
        List<FlowBinding> bindings,
        Dictionary<string, DateTimeOffset> nextDue,
        Dictionary<string, long> counters,
        bool runAll)
    {
        var now = DateTimeOffset.UtcNow;
        var results = new Dictionary<string, object?>();
        foreach (var binding in bindings)
        {
            if (!runAll && now < nextDue[binding.Name])
                continue;
            nextDue[binding.Name] = now + TimeSpan.FromSeconds(
                FlowInterval(binding.Name));
            if (!FlowEnabled(binding.Name))
            {
                results[binding.Name] = "disabled";
                continue;
            }
            try
            {
                using var timeout = CancellationTokenSource
                    .CreateLinkedTokenSource(
                        CancellationToken.None);
                timeout.CancelAfter(FlowTimeout);
                var outcome = await binding.Run(timeout.Token);
                counters[binding.Counter]++;
                results[binding.Name] = ResultJson(outcome);
            }
            catch (OperationCanceledException)
            {
                results[binding.Name] = "error:timeout";
            }
            catch (Exception error)
            {
                // Bounded by default; the env-gated diagnostic lane
                // adds the message for operator debugging only —
                // state/audit consumers always see the short form.
                results[binding.Name] =
                    Environment.GetEnvironmentVariable(
                        "GPTBRIDGE_PERMISSION_DEBUG") == "1"
                        ? $"error:{error.GetType().Name}:{error.Message}"
                        : $"error:{error.GetType().Name}";
            }
        }
        return results;
    }

    /// <summary>``--once``: run every enabled flow exactly once and
    /// print the bounded result map.</summary>
    public static async Task<int> RunOnceAll()
    {
        var bindings = Bindings();
        var nextDue = FlowNames.ToDictionary(
            name => name, _ => DateTimeOffset.MinValue);
        var counters = bindings.ToDictionary(
            b => b.Counter, _ => 0L);
        var results = await RunDue(bindings, nextDue, counters,
            runAll: true);
        var json = new JsonObject
        {
            ["ok"] = results.Values.All(v => v is not string s
                || !s.StartsWith("error:", StringComparison.Ordinal)),
            ["results"] = JsonNode.Parse(
                System.Text.Json.JsonSerializer.Serialize(results)),
        };
        Console.Out.WriteLine(json.ToJsonString());
        return json["ok"]!.GetValue<bool>() ? 0 : 1;
    }

    /// <summary>``--status``: print the persisted state file verbatim
    /// (or an explicit absent marker).</summary>
    public static string Status() =>
        File.Exists(StatePath())
            ? File.ReadAllText(StatePath())
            : "{\"running\":false,\"state\":\"absent\"}";

    /// <summary>Resident automation loop; one tick evaluates each flow
    /// against its own ``interval_s`` and per-flow last-run stamp.
    /// ``--interval`` overrides the tick granularity only — flow cadence
    /// still comes from the manifest.  Process lifetime is owned by the
    /// Rust supervisor; this loop never respawns itself.</summary>
    public static async Task<int> RunWatch(double? tickOverride)
    {
        Directory.CreateDirectory(StateDir());
        var lockPath = Path.Combine(StateDir(),
            "permission-automation.lock");
        FileStream lockHandle;
        try
        {
            lockHandle = new FileStream(lockPath, FileMode.Create,
                FileAccess.ReadWrite, FileShare.None);
        }
        catch (IOException)
        {
            Console.Error.WriteLine(
                "permission-automation already running (lock held)");
            return 1;
        }
        await using (lockHandle)
        {
            var tick = tickOverride ?? DefaultTickSeconds;
            var bindings = Bindings();
            var nextDue = FlowNames.ToDictionary(
                name => name, _ => DateTimeOffset.MinValue);
            var counters = bindings.ToDictionary(
                b => b.Counter, _ => 0L);
            var lastResult = new Dictionary<string, object?>();
            for (;;)
            {
                var results = await RunDue(
                    bindings, nextDue, counters, runAll: false);
                foreach (var kv in results)
                    lastResult[kv.Key] = kv.Value;
                PersistState(new JsonObject
                {
                    ["running"] = true,
                    ["pid"] = Environment.ProcessId,
                    ["tick_seconds"] = tick,
                    ["updated_at"] = UtcNow(),
                    ["counters"] = JsonNode.Parse(
                        System.Text.Json.JsonSerializer
                            .Serialize(counters)),
                    ["last_tick"] = JsonNode.Parse(
                        System.Text.Json.JsonSerializer
                            .Serialize(lastResult)),
                });
                await Task.Delay(TimeSpan.FromSeconds(tick));
            }
        }
    }
}
