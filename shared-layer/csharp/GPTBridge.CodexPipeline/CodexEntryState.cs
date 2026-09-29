using System.Text;
using System.Text.Json.Nodes;

namespace GPTBridge.CodexPipeline;

/// <summary>Fail-closed denial raised by the official codex entry —
/// the C# equivalent of the Python lane's ``PermissionError(<code>)``.</summary>
internal sealed class CodexReadDenied : Exception
{
    public string Code { get; }

    public CodexReadDenied(string code) : base(code)
    {
        Code = code;
    }
}

/// <summary>
/// Shared state for the official codex entry (A435 access classes) —
/// direct port of ``codex_entry_state``.  One governed vocabulary
/// (access classes, purposes, scope grammar, actor registers), one
/// metadata-only audit sink and one revocation generation; holds no
/// codex content.  Audit is content-free by construction (A435
/// FORBID:content-in-audit).
///
/// Persistent entry state: revocation generation, minted session nonces
/// and dual-key grants survive restarts so replay and revocation
/// evidence cannot be lost by a process boundary.  The store is atomic
/// (tmp+replace) and fail-closed.
/// </summary>
internal static partial class CodexEntryState
{
    public const string AccessBounded = "bounded-machine-lookup";
    public const string AccessReview = "review-session";
    public const string AccessChinese = "xingcheng-chinese-review";

    public static readonly HashSet<string> GovernedPurposes = new(
        StringComparer.Ordinal)
    {
        "self-declaration", "adjudication", "status", "global-review",
        "contract-gate", "diagnostics", "amendment-verification",
        "coordination", "audit",
    };

    public static readonly HashSet<string> XingchengIds = new(
        StringComparer.Ordinal) { "星澄", "xingcheng" };

    /// <summary>Non-sovereign governed components registered for
    /// bounded machine lookups (A435 non-content).</summary>
    public static readonly HashSet<string> ComponentActors = new(
        StringComparer.Ordinal)
    {
        "information-layer", "governance-registries", "startup-executor",
        "authority-reanchor-service", "codex-amendment-executor",
        "xingcheng-fault-diagnostics", "governance-audit",
    };

    /// <summary>Proxy components additionally holding review-session
    /// rights (rule text / adjudication evidence).</summary>
    public static readonly HashSet<string> ReviewComponentActors = new(
        StringComparer.Ordinal)
    {
        "decision-layer", "governance-coordination", "governance-audit",
    };

    public static readonly HashSet<string> ValidScopeKinds = new(
        StringComparer.Ordinal)
    {
        "sovereign", "provision", "edicts", "articles", "principles",
        "registry", "directory", "codex", "chinese",
    };

    public static readonly HashSet<string> BoundedWildcardKinds = new(
        StringComparer.Ordinal) { "registry", "directory" };

    public const double DefaultSessionTtl = 120.0;
    public const double DefaultContextTtl = 300.0;
    public const int DigestFlushBound = 256;

    private const string AuditPathEnv = "GPTBRIDGE_CODEX_AUDIT_PATH";
    private const string EntryStateEnv = "GPTBRIDGE_CODEX_ENTRY_STATE";
    private const string RotationBytesEnv =
        "GPTBRIDGE_CODEX_AUDIT_ROTATION_BYTES";
    private const string RetentionHoursEnv =
        "GPTBRIDGE_CODEX_AUDIT_RETENTION_HOURS";
    private const long RotationBytesDefault = 256L << 20;
    private const int RetentionHoursDefault = 24;

    // Session records die once expired (the nonce can never be presented
    // again); consumed markers die once every session that could mint
    // them has expired; grants die at expiry.  A short grace window
    // keeps recently-closed records available for forensics while
    // bounding the store.
    private const double SessionRetentionSeconds = 300.0;
    private const double GrantRetentionSeconds = 300.0;
    private const double ConsumedRetentionSeconds = 3600.0;

    private static readonly object StateLock = new();
    private static readonly object AuditLock = new();
    private static readonly
        Dictionary<(long Mtime, long Size), string> StateCache = new();

    // -- paths --------------------------------------------------------------

    /// <summary>Codex read-audit ledger; env-overridable so release
    /// payloads (immutable) can redirect writes to a state root.</summary>
    public static string AuditPath()
    {
        var value = (Environment.GetEnvironmentVariable(AuditPathEnv)
            ?? "").Trim();
        if (value.Length > 0)
            return value;
        return Path.Combine(Repo.Root(), "governance_rule", "execution",
            "audit", "codex_read_audit.jsonl");
    }

    public static string StatePath()
    {
        var value = (Environment.GetEnvironmentVariable(EntryStateEnv)
            ?? "").Trim();
        return value.Length > 0 ? value
            : Path.Combine(Path.GetDirectoryName(AuditPath())!,
                "codex_entry_state.json");
    }

    private static int EnvInt(string name, int fallback)
    {
        var raw = (Environment.GetEnvironmentVariable(name) ?? "")
            .Trim();
        return int.TryParse(raw, out var parsed) ? parsed : fallback;
    }

    private static long EnvLong(string name, long fallback)
    {
        var raw = (Environment.GetEnvironmentVariable(name) ?? "")
            .Trim();
        return long.TryParse(raw, out var parsed) ? parsed : fallback;
    }

    // -- audit --------------------------------------------------------------

    public static string UtcNow() =>
        DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ");

    /// <summary>``scope_hash`` — sha256 of the sorted scope list under
    /// Python ``json.dumps(..., ensure_ascii=False)`` separators.</summary>
    public static string ScopeHash(IReadOnlyCollection<string> scope)
    {
        var list = scope.OrderBy(s => s, StringComparer.Ordinal)
            .Select(s => (object?)s).ToList();
        var payload = CanonJson.Write(list, spaced: true);
        return Convert.ToHexString(System.Security.Cryptography.SHA256
            .HashData(CanonJson.CanonicalUtf8(payload)))
            .ToLowerInvariant()[..16];
    }

    /// <summary>``record_session_audit`` — append one metadata-only
    /// audit record (A435: identity/purpose/class/scope-hash/version/
    /// correlation/result only; content never enters audit).</summary>
    public static void RecordSessionAudit(string ev, string actor,
        string purpose, string accessClass,
        IReadOnlyCollection<string> scope, long? codexVersion,
        string correlation, string result, int requestCount = 0)
    {
        var entry = new SortedDictionary<string, object?>(
            StringComparer.Ordinal)
        {
            ["access_class"] = accessClass,
            ["actor"] = actor,
            ["codex_version"] = codexVersion,
            ["correlation"] = correlation,
            ["entry"] = "governance-codex://official",
            ["event"] = ev,
            ["purpose"] = purpose,
            ["request_count"] = (long)requestCount,
            ["result"] = result,
            ["scope_hash"] = ScopeHash(scope),
            ["timestamp"] = UtcNow(),
        };
        var line = CanonJson.SerializeSpaced(entry) + "\n";
        var path = AuditPath();
        try
        {
            Directory.CreateDirectory(
                Path.GetDirectoryName(path)!);
            lock (AuditLock)
            {
                RotateAuditIfLarge(path);
                // Cross-process exclusive append: FileShare.None makes a
                // concurrent writer wait/retry instead of interleaving
                // records (parity with the msvcrt byte-range lock).
                IOException? lastError = null;
                for (var attempt = 0; attempt < 5; attempt++)
                {
                    try
                    {
                        using var stream = new FileStream(path,
                            FileMode.Append, FileAccess.Write,
                            FileShare.None);
                        var bytes = Encoding.UTF8.GetBytes(line);
                        stream.Write(bytes, 0, bytes.Length);
                        return;
                    }
                    catch (IOException error)
                    {
                        lastError = error;
                        Thread.Sleep(10 * (attempt + 1));
                    }
                    catch (UnauthorizedAccessException error)
                    {
                        lastError = new IOException(error.Message,
                            error);
                        Thread.Sleep(10 * (attempt + 1));
                    }
                }
            }
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
    }

    /// <summary>Archive a full codex read-audit segment that exceeds
    /// the ceiling; prune archived segments older than retention.</summary>
    private static void RotateAuditIfLarge(string auditPath)
    {
        var ceiling = EnvLong(RotationBytesEnv, RotationBytesDefault);
        if (ceiling <= 0)
            return;
        try
        {
            if (!File.Exists(auditPath)
                || new FileInfo(auditPath).Length < ceiling)
                return;
        }
        catch (IOException)
        {
            return;
        }
        var month = DateTime.Now.ToString("yyyy-MM");
        var archiveDir = Path.Combine(
            Path.GetDirectoryName(auditPath)!, "archive", "codex-read",
            month);
        var target = Path.Combine(archiveDir,
            $"{Path.GetFileName(auditPath)}-"
            + $"{DateTimeOffset.UtcNow.ToUnixTimeSeconds()}.jsonl");
        try
        {
            Directory.CreateDirectory(archiveDir);
            File.Move(auditPath, target);
        }
        catch (IOException)
        {
            return;
        }
        catch (UnauthorizedAccessException)
        {
            return;
        }
        var retention = EnvInt(RetentionHoursEnv, RetentionHoursDefault);
        if (retention <= 0)
            return;
        var cutoff = DateTime.UtcNow.AddHours(-retention);
        var root = Path.GetDirectoryName(archiveDir)
            ?? Path.Combine(Path.GetDirectoryName(auditPath)!,
                "archive", "codex-read");
        // Prune across every archived month segment.
        root = Path.Combine(Path.GetDirectoryName(auditPath)!,
            "archive", "codex-read");
        if (!Directory.Exists(root))
            return;
        foreach (var candidate in Directory.EnumerateFiles(root,
            "*.jsonl", SearchOption.AllDirectories))
            try
            {
                if (File.GetLastWriteTimeUtc(candidate) < cutoff)
                    File.Delete(candidate);
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
    }

    // -- persistent entry state ----------------------------------------------

}
