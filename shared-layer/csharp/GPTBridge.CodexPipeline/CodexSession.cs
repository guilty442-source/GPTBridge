namespace GPTBridge.CodexPipeline;

/// <summary>
/// Controlled codex read sessions — the A113/A435 engine behind
/// ``governance-codex://official``; direct port of ``codex_session`` +
/// ``codex_dual_key`` + ``codex_official``.
///
/// A session is bound to (actor, purpose, scope, nonce, expiry, codex
/// version, revocation generation); expiry, revocation or a codex
/// version change kills it (A435).  Bounded contexts accumulate reads
/// into a bounded batch digest flushed as one metadata-only audit
/// record; review sessions audit each read individually.  Audit never
/// carries codex content (A435 content-in-audit forbidden).
/// </summary>
internal static class CodexSessions
{
    public const string AccessBounded = CodexEntryState.AccessBounded;
    public const string AccessReview = CodexEntryState.AccessReview;
    public const string AccessChinese = CodexEntryState.AccessChinese;

    /// <summary>Deterministic per-request permission review at the
    /// official entry — denial code or null.  The entry owner
    /// (permission-sovereign) manages this review; an unknown or empty
    /// actor is always denied (fail-closed).</summary>
    private static string? ReviewRequest(string actor, string purpose,
        string accessClass)
    {
        if (actor.Length == 0)
            return "CODEX_ACTOR_REQUIRED";
        if (!CodexEntryState.GovernedPurposes.Contains(purpose))
            return "CODEX_PURPOSE_REQUIRED";
        if (accessClass == AccessChinese)
            // A173/A435: 星澄 bypasses permission review only; identity
            // must still be the registered 星澄 identity.
            return CodexEntryState.XingchengIds.Contains(actor)
                ? null : "CODEX_CHINESE_DENIED";
        HashSet<string> sovereignIds;
        try
        {
            sovereignIds = CodexRepository.LoadGovernanceCodex()
                .Sovereigns.Select(s => s.Id)
                .ToHashSet(StringComparer.Ordinal);
        }
        catch (Exception)
        {
            return "CODEX_UNAVAILABLE";
        }
        if (accessClass == AccessReview)
            return sovereignIds.Contains(actor)
                || CodexEntryState.ReviewComponentActors.Contains(actor)
                ? null : "CODEX_REVIEW_DENIED";
        if (accessClass == AccessBounded)
            return sovereignIds.Contains(actor)
                || CodexEntryState.ComponentActors.Contains(actor)
                || CodexEntryState.ReviewComponentActors.Contains(actor)
                || CodexEntryState.XingchengIds.Contains(actor)
                ? null : "CODEX_LOOKUP_DENIED";
        return "CODEX_ACCESS_CLASS_UNKNOWN";
    }

    /// <summary>Open a controlled codex read session through the
    /// official entry — per-request permission review, optional
    /// dual-key gate, nonce+expiry+version+generation binding.</summary>
    public static CodexReadSession OpenCodexSession(string actor,
        string purpose, IEnumerable<string> scope,
        string accessClass = AccessReview,
        double ttlSeconds = CodexEntryState.DefaultSessionTtl,
        string? dualKeyGrant = null)
    {
        actor = (actor ?? "").Trim();
        purpose = (purpose ?? "").Trim();
        HashSet<string> parsedScope;
        try
        {
            parsedScope = CodexEntryState.ParseScope(scope);
        }
        catch (CodexReadDenied)
        {
            CodexEntryState.RecordSessionAudit("session-open", actor,
                purpose, accessClass, Array.Empty<string>(), null, "",
                "DENIED_SCOPE");
            throw;
        }
        var denial = ReviewRequest(actor, purpose, accessClass);
        if (denial is not null)
        {
            CodexEntryState.RecordSessionAudit("session-open", actor,
                purpose, accessClass, parsedScope, null, "", denial);
            throw new CodexReadDenied(denial);
        }
        // A435 two-key boundary: privileged review/amendment opens
        // require a single-use grant countersigned by a distinct
        // registered sovereign.
        if (CodexDualKey.RequiresDualKey(accessClass, purpose,
                parsedScope))
        {
            if (string.IsNullOrEmpty(dualKeyGrant))
            {
                CodexEntryState.RecordSessionAudit("session-open",
                    actor, purpose, accessClass, parsedScope, null, "",
                    "CODEX_DUAL_KEY_REQUIRED");
                throw new CodexReadDenied("CODEX_DUAL_KEY_REQUIRED");
            }
            CodexDualKey.VerifyDualKeyGrant(dualKeyGrant,
                $"codex-open:{accessClass}", actor, purpose, parsedScope,
                accessClass);
        }
        return new CodexReadSession(actor, purpose, parsedScope,
            accessClass, ttlSeconds);
    }

    /// <summary>BOUNDED_MACHINE_LOOKUP context (A435): non-content
    /// exact lookups.</summary>
    public static CodexReadSession OpenBoundedContext(string actor,
        string purpose, IEnumerable<string> scope,
        double ttlSeconds = CodexEntryState.DefaultContextTtl,
        string? dualKeyGrant = null) =>
        OpenCodexSession(actor, purpose, scope, AccessBounded,
            ttlSeconds, dualKeyGrant);

    /// <summary>REVIEW_SESSION (A435): rule text / evidence / citation
    /// reads.</summary>
    public static CodexReadSession OpenReviewSession(string actor,
        string purpose, IEnumerable<string> scope,
        double ttlSeconds = CodexEntryState.DefaultSessionTtl,
        string? dualKeyGrant = null) =>
        OpenCodexSession(actor, purpose, scope, AccessReview,
            ttlSeconds, dualKeyGrant);

    /// <summary>XINGCHENG_CHINESE_REVIEW (A173/A435): 星澄-only,
    /// review exempt.</summary>
    public static CodexReadSession OpenChineseReviewSession(string actor,
        string purpose = "global-review",
        IEnumerable<string>? scope = null,
        double ttlSeconds = CodexEntryState.DefaultSessionTtl) =>
        OpenCodexSession(actor, purpose, scope ?? new[] {
            "chinese:mirror" }, AccessChinese, ttlSeconds);

    /// <summary>Revoke every outstanding context/session (amendment,
    /// recertify).</summary>
    public static void RevokeCodexReadContexts() =>
        CodexEntryState.RevokeCodexReadContexts();
}
