using System.Diagnostics;
using System.Text;
using System.Text.Json.Nodes;

namespace GPTBridge.Permission;

/// <summary>One audit-history record (metadata-only — bounded 2000
/// chars of engine output, timestamp, pass flag, return code).</summary>
public sealed class AuditRecord
{
    public required DateTimeOffset Timestamp { get; init; }
    public required bool Passed { get; init; }
    public required string Output { get; init; }
    public int? ReturnCode { get; init; }
    public string? Error { get; init; }
}

/// <summary>
/// Native successor of the retired
/// ``permission_automation_audit.AuditScheduler``.
///
/// Each <see cref="RunOnceAsync"/> tick invokes the native audit engine
/// (the Python ``governance_rule.execution.audit`` entry is retired) with
/// a bounded timeout, records the result in an in-memory audit history,
/// and returns the record. Fail-closed: timeout, missing engine or a
/// non-zero exit all record ``passed=false`` — a failed scheduled audit
/// is itself the compliance evidence.
/// </summary>
public sealed class AuditSchedulerAutomation
{
    public static readonly TimeSpan DefaultInterval =
        TimeSpan.FromSeconds(3600);

    /// <summary>Default bounded engine timeout — same 60s contract as
    /// the retired scheduler.</summary>
    public static readonly TimeSpan DefaultAuditTimeout =
        TimeSpan.FromSeconds(60);

    private const int OutputLimit = 2000;

    private readonly string _enginePath;
    private readonly string _manifestPath;
    private readonly string _projectRoot;
    private readonly Func<IEnumerable<string>>? _extraArgs;
    private readonly Func<string>? _argumentsOverride;
    private readonly TimeSpan _auditTimeout;
    private readonly List<AuditRecord> _history = new();

    /// <param name="projectRoot">Engine working directory.</param>
    /// <param name="enginePath">Native audit engine executable
    /// (``native/test_suites/bin/audit-engine.exe`` by default relative
    /// to the project root).</param>
    /// <param name="manifestPath">Audit-checks manifest path.</param>
    /// <param name="extraArgs">Optional extra engine arguments.</param>
    /// <param name="argumentsOverride">When set, replaces the whole
    /// argument string verbatim (test seam).</param>
    /// <param name="auditTimeout">Overrides the bounded engine timeout
    /// (test seam; production keeps <see cref="DefaultAuditTimeout"/>).</param>
    public AuditSchedulerAutomation(
        string projectRoot,
        string? enginePath = null,
        string? manifestPath = null,
        Func<IEnumerable<string>>? extraArgs = null,
        Func<string>? argumentsOverride = null,
        TimeSpan? auditTimeout = null)
    {
        _projectRoot = projectRoot;
        _enginePath = enginePath ?? Path.Combine(
            projectRoot, "native", "test_suites", "bin",
            "audit-engine.exe");
        _manifestPath = manifestPath ?? Path.Combine(
            projectRoot, "governance_rule", "execution", "audit",
            "audit_checks_manifest.json");
        _extraArgs = extraArgs;
        _argumentsOverride = argumentsOverride;
        _auditTimeout = auditTimeout ?? DefaultAuditTimeout;
    }

    /// <summary>Run one scheduled audit — appends to history.</summary>
    public async Task<AuditRecord> RunOnceAsync(
        CancellationToken ct = default)
    {
        if (!File.Exists(_enginePath))
        {
            var missing = new AuditRecord
            {
                Timestamp = DateTimeOffset.UtcNow,
                Passed = false,
                Output = "",
                Error = $"audit-engine-not-found: {_enginePath}",
            };
            _history.Add(missing);
            return missing;
        }

        var args = _argumentsOverride is not null
            ? new StringBuilder(_argumentsOverride())
            : new StringBuilder()
                .Append("--manifest ").Append('"')
                .Append(_manifestPath)
                .Append('"').Append(" --root ").Append('"')
                .Append(_projectRoot).Append('"');
        if (_extraArgs is not null)
            foreach (var a in _extraArgs())
                args.Append(' ').Append(a);

        using var proc = new Process
        {
            StartInfo = new ProcessStartInfo
            {
                FileName = _enginePath,
                Arguments = args.ToString(),
                WorkingDirectory = _projectRoot,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            },
            EnableRaisingEvents = true,
        };

        var stdout = new StringBuilder();
        var stderr = new StringBuilder();
        proc.OutputDataReceived +=
            (_, e) => { if (e.Data is not null) stdout.AppendLine(e.Data); };
        proc.ErrorDataReceived +=
            (_, e) => { if (e.Data is not null) stderr.AppendLine(e.Data); };

        AuditRecord record;
        try
        {
            proc.Start();
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
            using var timeout = CancellationTokenSource
                .CreateLinkedTokenSource(ct);
            timeout.CancelAfter(_auditTimeout);
            try
            {
                await proc.WaitForExitAsync(timeout.Token);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                TryKill(proc);
                throw;
            }
            catch (OperationCanceledException)
            {
                TryKill(proc);
                record = new AuditRecord
                {
                    Timestamp = DateTimeOffset.UtcNow,
                    Passed = false,
                    Output = "",
                    Error = "timeout",
                };
                _history.Add(record);
                return record;
            }

            var output = stdout.ToString() + stderr;
            if (output.Length > OutputLimit)
                output = output[..OutputLimit];
            // The native engine emits a single-line JSON summary
            // (``{"engine":"star-audit-engine/v1","failed":N,...}``);
            // ``[PASS]`` lines are the legacy per-check format.  Accept
            // either: a parsed summary with zero failures, or an
            // explicit PASS marker.
            var summaryPassed = false;
            try
            {
                var summary = JsonNode.Parse(stdout.ToString());
                summaryPassed = summary?["failed"]
                    ?.GetValue<int>() == 0;
            }
            catch (System.Text.Json.JsonException) { }
            record = new AuditRecord
            {
                Timestamp = DateTimeOffset.UtcNow,
                Passed = proc.ExitCode == 0
                    && (output.Contains("[PASS]") || summaryPassed),
                Output = output,
                ReturnCode = proc.ExitCode,
            };
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            record = new AuditRecord
            {
                Timestamp = DateTimeOffset.UtcNow,
                Passed = false,
                Output = "",
                Error = e.Message,
            };
        }
        _history.Add(record);
        return record;
    }

    private static void TryKill(Process p)
    {
        try { p.Kill(entireProcessTree: true); }
        catch (InvalidOperationException) { /* already exited */ }
    }

    /// <summary>Most recent <paramref name="count"/> records.</summary>
    public IReadOnlyList<AuditRecord> GetAuditHistory(int count = 50)
    {
        var skip = Math.Max(0, _history.Count - count);
        return _history.Skip(skip).ToList();
    }
}
