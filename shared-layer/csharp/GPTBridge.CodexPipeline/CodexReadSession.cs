
namespace GPTBridge.CodexPipeline;


/// <summary>A controlled official-entry read session (A435).  Every
/// read is checked against liveness (closed/expiry/revocation/version)
/// and the declared least scope, then audited.</summary>
internal sealed class CodexReadSession : IDisposable
{
    private static double Monotonic() =>
        Environment.TickCount64 / 1000.0;

    private readonly string _actor;
    private readonly string _purpose;
    private readonly HashSet<string> _scope;
    private readonly string _accessClass;
    private readonly string _nonce;
    private readonly double _expires;
    private readonly long _generation;
    private readonly long _codexVersion;
    private bool _closed;
    private readonly List<(long Seq, string Item)> _digest = new();
    private long _digestSeq;

    public CodexReadSession(string actor, string purpose,
        HashSet<string> scope, string accessClass, double ttlSeconds)
    {
        if (!double.IsFinite(ttlSeconds))
            throw new CodexReadDenied("CODEX_SESSION_TTL_INVALID");
        _actor = actor;
        _purpose = purpose;
        _scope = scope;
        _accessClass = accessClass;
        _nonce = Convert.ToHexString(System.Security.Cryptography
            .RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
        var ttl = Math.Max(1.0, ttlSeconds);
        _expires = Monotonic() + ttl;
        _generation = CodexEntryState.CurrentRevocation();
        try
        {
            _codexVersion = CodexRepository.LoadGovernanceCodex()
                .CodexVersion;
        }
        catch (Exception error)
        {
            throw new CodexReadDenied("CODEX_UNAVAILABLE")
            {
                Source = error.GetType().Name,
            };
        }
        CodexEntryState.RegisterSessionNonce(_nonce, actor, purpose,
            accessClass, scope, _codexVersion, _generation,
            DateTimeOffset.UtcNow.ToUnixTimeSeconds() + ttl);
        if (accessClass != CodexSessions.AccessBounded)
            CodexEntryState.RecordSessionAudit("session-open", actor,
                purpose, accessClass, scope, _codexVersion, _nonce,
                "OPEN");
    }

    public string Nonce => _nonce;
    public long Version => _codexVersion;
    public string VersionText =>
        CodexRepository.FormatCodexVersion(_codexVersion);
    public bool Expired => !double.IsFinite(_expires) || Monotonic() >= _expires;

    public void Close()
    {
        if (_closed)
            return;
        _closed = true;
        try
        {
            CodexEntryState.CloseSessionNonce(_nonce);
        }
        catch (CodexReadDenied)
        {
        }
        if (_accessClass == CodexSessions.AccessBounded)
            FlushDigest("CLOSED");
        else
            CodexEntryState.RecordSessionAudit("session-close", _actor,
                _purpose, _accessClass, _scope, _codexVersion, _nonce,
                "CLOSED");
    }

    public void Dispose() => Close();

    // -- controls ---------------------------------------------------------

    private GovernanceCodex Codex()
    {
        if (_closed)
            throw new CodexReadDenied("CODEX_SESSION_CLOSED");
        if (Expired)
            Deny("CODEX_SESSION_EXPIRED");
        if (_generation != CodexEntryState.CurrentRevocation())
            Deny("CODEX_SESSION_REVOKED");
        var codex = CodexRepository.LoadGovernanceCodex();
        if (codex.CodexVersion != _codexVersion)
            Deny("CODEX_VERSION_CHANGED");
        return codex;
    }

    private bool Allows(string scopeItem)
    {
        var kind = scopeItem.Split(':', 2)[0];
        if (_scope.Contains(scopeItem))
            return true;
        if (!_scope.Contains($"{kind}:*"))
            return false;
        if (_accessClass == CodexSessions.AccessReview)
            return true;
        return CodexEntryState.BoundedWildcardKinds.Contains(kind);
    }

    private GovernanceCodex Require(string scopeItem)
    {
        if (!Allows(scopeItem))
            Deny($"CODEX_SCOPE_DENIED:{scopeItem}");
        var codex = Codex();
        AuditRead(scopeItem, "GRANTED");
        return codex;
    }

    private void AuditRead(string scopeItem, string result)
    {
        if (_accessClass == CodexSessions.AccessBounded)
        {
            _digestSeq += 1;
            _digest.Add((_digestSeq, scopeItem));
            if (_digest.Count >= CodexEntryState.DigestFlushBound)
                FlushDigest("BATCH_BOUND");
            return;
        }
        CodexEntryState.RecordSessionAudit("session-read", _actor,
            _purpose, _accessClass, _scope, _codexVersion, _nonce,
            $"{result}:{scopeItem}");
    }

    private void FlushDigest(string result)
    {
        if (_digest.Count == 0)
            return;
        var count = _digest.Count;
        _digest.Clear();
        CodexEntryState.RecordSessionAudit("bounded-digest", _actor,
            _purpose, _accessClass, _scope, _codexVersion, _nonce,
            result, count);
    }

    private void Deny(string code)
    {
        CodexEntryState.RecordSessionAudit("session-deny", _actor,
            _purpose, _accessClass, _scope, _codexVersion, _nonce, code);
        throw new CodexReadDenied(code);
    }

    // -- typed reads (version-identified, requested-scope-only) ---------

    /// <summary>Codex identity tuple: schema, version, authority rank,
    /// scope.</summary>
    public Dictionary<string, object?> CodexIdentity()
    {
        var codex = Require("codex:identity");
        return new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["schema"] = codex.Schema,
            ["codex_version"] = codex.CodexVersion,
            ["codex_version_text"] = CodexRepository
                .FormatCodexVersion(codex.CodexVersion),
            ["authority_rank"] = codex.Preamble.AuthorityRank,
            ["binding_scope"] = codex.Preamble.BindingScope,
        };
    }

    /// <summary>One registered sovereign identity/status/binding
    /// record.</summary>
    public CodexSovereign? Sovereign(string sovereignId)
    {
        var sid = (sovereignId ?? "").Trim();
        var codex = Require($"sovereign:{sid}");
        return codex.Sovereigns.FirstOrDefault(s => s.Id == sid);
    }

    /// <summary>All registered sovereign records (review scope
    /// only).</summary>
    public CodexSovereign[] Sovereigns() =>
        Require("sovereign:*").Sovereigns;

    /// <summary>Registered-identity check for a provision token
    /// (bounded).</summary>
    public bool ProvisionExists(string reference)
    {
        var referenceTrimmed = (reference ?? "").Trim();
        var codex = Require($"provision:{referenceTrimmed}");
        return FindProvision(codex, referenceTrimmed) is not null;
    }

    /// <summary>Rule text for a provision token (REVIEW_SESSION
    /// only).</summary>
    public string ProvisionText(string reference)
    {
        if (_accessClass != CodexSessions.AccessReview)
            Deny("CODEX_REVIEW_REQUIRED");
        var referenceTrimmed = (reference ?? "").Trim();
        var codex = Require($"provision:{referenceTrimmed}");
        var found = FindProvision(codex, referenceTrimmed);
        if (found is null)
            throw new KeyNotFoundException(
                $"unknown provision '{referenceTrimmed}'");
        return found;
    }

    private static IEnumerable<(string Id, string Text)> Pools(
        GovernanceCodex codex)
    {
        foreach (var a in codex.Articles)
            yield return (a.Id, a.Rule);
        foreach (var e in codex.Edicts)
            yield return (e.Id, e.Text);
        foreach (var p in codex.Principles)
            yield return (p.Id, p.Statement);
    }

    private static string? Lookup(GovernanceCodex codex,
        string reference)
    {
        foreach (var (id, text) in Pools(codex))
            if (id == reference)
                return text;
        return null;
    }

    private static string? FindProvision(GovernanceCodex codex,
        string reference)
    {
        // Renumbered provisions keep their current identity in the
        // pools; historical ids resolve through the renumbering
        // registry (HISTORICAL_ALIAS_ONLY) to live text.
        var found = Lookup(codex, reference);
        if (found is not null)
            return found;
        var aliases = new Dictionary<string, string>(
            StringComparer.Ordinal);
        if (codex.Registries.TryGetValue("provision_renumbering_registry",
                out var registry))
            foreach (var row in registry)
            {
                var oldId = row.Get("old_provision_id");
                var newId = row.Get("new_provision_id");
                if (oldId is not null)
                    aliases[oldId] = newId ?? "";
            }
        var seen = new HashSet<string>(StringComparer.Ordinal)
            { reference };
        var target = reference;
        for (var i = 0; i < 8; i++)
        {
            if (!aliases.TryGetValue(target, out var next)
                || next.Length == 0 || seen.Contains(next))
                return null;
            target = next;
            seen.Add(target);
            found = Lookup(codex, target);
            if (found is not null)
                return found;
        }
        return null;
    }

    /// <summary>Edicts governing one area (adjudication evidence,
    /// review).</summary>
    public List<Dictionary<string, string>> EdictsByArea(string area)
    {
        if (_accessClass != CodexSessions.AccessReview)
            Deny("CODEX_REVIEW_REQUIRED");
        var target = (area ?? "").Trim();
        var codex = Require($"edicts:{target}");
        return codex.Edicts.Where(e => e.Area == target)
            .Select(e => new Dictionary<string, string>(
                StringComparer.Ordinal)
            {
                ["id"] = e.Id,
                ["edict"] = e.Text,
                ["immutability"] = e.Immutability,
            }).ToList();
    }

    public CodexArticle[] Articles()
    {
        if (_accessClass != CodexSessions.AccessReview)
            Deny("CODEX_REVIEW_REQUIRED");
        return Require("articles:*").Articles;
    }

    public CodexPrinciple[] Principles()
    {
        if (_accessClass != CodexSessions.AccessReview)
            Deny("CODEX_REVIEW_REQUIRED");
        return Require("principles:*").Principles;
    }

    /// <summary>Registered ``*_registry`` table names (metadata,
    /// non-content).</summary>
    public string[] RegistryNames()
    {
        var codex = Require("registry:*");
        return codex.Registries.Keys.OrderBy(k => k,
            StringComparer.Ordinal).ToArray();
    }

    /// <summary>Registered ``*_directory`` table names (metadata,
    /// non-content).</summary>
    public string[] DirectoryNames()
    {
        var codex = Require("directory:*");
        return codex.Directories.Keys.OrderBy(k => k,
            StringComparer.Ordinal).ToArray();
    }

    /// <summary>Registered non-content registry rows (A334 machine
    /// authority).</summary>
    public Dictionary<string, string>[] Registry(string name)
    {
        var table = (name ?? "").Trim();
        var codex = Require($"registry:{table}");
        return codex.Registries.TryGetValue(table, out var rows)
            ? rows.Select(r => r.AsDict()).ToArray()
            : Array.Empty<Dictionary<string, string>>();
    }

    /// <summary>Registered directory rows (identity/status/
    /// binding).</summary>
    public Dictionary<string, string>[] Directory(string name)
    {
        var table = (name ?? "").Trim();
        var codex = Require($"directory:{table}");
        return codex.Directories.TryGetValue(table, out var rows)
            ? rows.Select(r => r.AsDict()).ToArray()
            : Array.Empty<Dictionary<string, string>>();
    }

    /// <summary>Full codex projection (cross-module inspection; review
    /// only).</summary>
    public GovernanceCodex Snapshot()
    {
        if (_accessClass != CodexSessions.AccessReview)
            Deny("CODEX_REVIEW_REQUIRED");
        return Require("codex:full");
    }

    /// <summary>星澄-only Chinese mirror text (A173; metadata-only
    /// audit).</summary>
    public string ChineseMirror()
    {
        if (_accessClass != CodexSessions.AccessChinese)
            Deny("CODEX_CHINESE_DENIED");
        Require("chinese:mirror");
        return CanonJson.Serialize(
            GPTBridge.CodexPipeline.ChineseMirror.LoadParts(
                UpdatePipeline.CanonicalCodexRoot()));
    }
}
