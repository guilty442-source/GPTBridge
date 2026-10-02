
namespace GPTBridge.CodexPipeline;


/// <summary>Dual-key authorization for privileged official-entry
/// operations (A435) — port of ``codex_dual_key``.  A grant is a
/// single-use, expiry-bound, version-bound token minted through
/// <see cref="MintDualKeyGrant"/> and consumed by the official entry
/// through <see cref="VerifyDualKeyGrant"/>.</summary>
internal static class CodexDualKey
{
    public const double DefaultGrantTtl = 120.0;

    private static bool GrantExpiryValid(object? value, long now)
    {
        if (value is null) return false;
        try
        {
            var expiry = Convert.ToDouble(value, System.Globalization.CultureInfo.InvariantCulture);
            return double.IsFinite(expiry) && expiry > now;
        }
        catch (Exception error) when (error is FormatException or InvalidCastException or OverflowException)
        {
            return false;
        }
    }

    /// <summary>Whether opening this entry requires a dual-key
    /// grant.</summary>
    public static bool RequiresDualKey(string accessClass,
        string purpose, IReadOnlyCollection<string> scope)
    {
        if (accessClass == CodexSessions.AccessChinese)
            return false; // A173: identity-bound to 星澄, single key.
        return purpose == "amendment-verification"
            || scope.Contains("codex:full");
    }

    private static HashSet<string> SovereignIds()
    {
        try
        {
            return CodexRepository.LoadGovernanceCodex().Sovereigns
                .Select(s => s.Id).ToHashSet(StringComparer.Ordinal);
        }
        catch (Exception)
        {
            return new HashSet<string>(StringComparer.Ordinal);
        }
    }

    private static bool PrimaryEligible(string actor, string accessClass)
    {
        var sovereignIds = SovereignIds();
        if (sovereignIds.Count == 0)
            return false; // codex unreadable → fail closed
        if (accessClass == CodexSessions.AccessReview)
            return sovereignIds.Contains(actor)
                || CodexEntryState.ReviewComponentActors.Contains(actor);
        return sovereignIds.Contains(actor)
            || CodexEntryState.ComponentActors.Contains(actor)
            || CodexEntryState.ReviewComponentActors.Contains(actor)
            || CodexEntryState.XingchengIds.Contains(actor);
    }

    private static bool CountersignerEligible(string actor) =>
        SovereignIds().Contains(actor)
        || CodexEntryState.XingchengIds.Contains(actor);

    private static void Audit(string result, string primary,
        string secondary, string purpose,
        IReadOnlyCollection<string> scope, string correlation) =>
        CodexEntryState.RecordSessionAudit("dual-key", primary,
            purpose, "dual-key", scope, null, correlation,
            $"{result}:secondary={secondary}");

    /// <summary>Mint a single-use dual-key grant; returns the grant
    /// nonce.  Both keys validated independently; the grant binds to
    /// operation, actors, purpose, scope hash, codex version and
    /// revocation generation.</summary>
    public static string MintDualKeyGrant(string operation,
        string primaryActor, string secondaryActor, string purpose,
        IEnumerable<string> scope,
        string accessClass = CodexSessions.AccessReview,
        double ttlSeconds = DefaultGrantTtl)
    {
        if (!double.IsFinite(ttlSeconds))
            throw new CodexReadDenied("CODEX_GRANT_TTL_INVALID");
        var primary = (primaryActor ?? "").Trim();
        var secondary = (secondaryActor ?? "").Trim();
        operation = (operation ?? "").Trim();
        purpose = (purpose ?? "").Trim();
        HashSet<string> parsedScope;
        try
        {
            parsedScope = CodexEntryState.ParseScope(scope);
        }
        catch (CodexReadDenied)
        {
            Audit("DENIED_SCOPE", primary, secondary, purpose,
                Array.Empty<string>(), "");
            throw;
        }
        string? denial = null;
        if (operation.Length == 0)
            denial = "CODEX_OPERATION_REQUIRED";
        else if (primary.Length == 0 || secondary.Length == 0)
            denial = "CODEX_DUAL_KEY_REQUIRED";
        else if (primary == secondary)
            denial = "CODEX_DUAL_KEY_DUPLICATE";
        else if (!CodexEntryState.GovernedPurposes.Contains(purpose))
            denial = "CODEX_PURPOSE_REQUIRED";
        else if (!PrimaryEligible(primary, accessClass))
            denial = "CODEX_PRIMARY_DENIED";
        else if (!CountersignerEligible(secondary))
            denial = "CODEX_SECONDARY_DENIED";
        if (denial is not null)
        {
            Audit(denial, primary, secondary, purpose, parsedScope, "");
            throw new CodexReadDenied(denial);
        }
        var nonce = Convert.ToHexString(System.Security.Cryptography
            .RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
        long codexVersion;
        try
        {
            codexVersion = CodexRepository.LoadGovernanceCodex()
                .CodexVersion;
        }
        catch (Exception)
        {
            Audit("CODEX_UNAVAILABLE", primary, secondary, purpose,
                parsedScope, "");
            throw new CodexReadDenied("CODEX_UNAVAILABLE");
        }
        var record = new Dictionary<string, object?>(
            StringComparer.Ordinal)
        {
            ["operation"] = operation,
            ["primary"] = primary,
            ["secondary"] = secondary,
            ["purpose"] = purpose,
            ["access_class"] = accessClass,
            ["scope_hash"] = CodexEntryState.ScopeHash(parsedScope),
            ["codex_version"] = codexVersion,
            ["generation"] = CodexEntryState.CurrentRevocation(),
            ["expires_at"] = DateTimeOffset.UtcNow.ToUnixTimeSeconds()
                + Math.Max(1.0, ttlSeconds),
            ["consumed"] = false,
        };
        CodexEntryState.MutateEntryState(state =>
        {
            var grants = (Dictionary<string, object?>)state["grants"];
            var consumed =
                (Dictionary<string, object?>)state["consumed_nonces"];
            if (grants.ContainsKey(nonce) || consumed.ContainsKey(nonce))
                throw new CodexReadDenied("CODEX_NONCE_REPLAY");
            grants[nonce] = record;
        });
        Audit("MINTED", primary, secondary, purpose, parsedScope, nonce);
        return nonce;
    }

    /// <summary>Consume a dual-key grant; throws CodexReadDenied on any
    /// mismatch — replayed, expired, revoked, wrong-purpose/scope/
    /// operation/version grants are denied.</summary>
    public static void VerifyDualKeyGrant(string grantId,
        string operation, string actor, string purpose,
        IEnumerable<string> scope, string accessClass)
    {
        var nonce = (grantId ?? "").Trim();
        var parsedScope = CodexEntryState.ParseScope(scope);
        actor = (actor ?? "").Trim();
        purpose = (purpose ?? "").Trim();
        string? denial = null;
        Dictionary<string, object?>? record = null;

        CodexEntryState.MutateEntryState(state =>
        {
            var grants = (Dictionary<string, object?>)state["grants"];
            var consumed =
                (Dictionary<string, object?>)state["consumed_nonces"];
            record = grants.TryGetValue(nonce, out var r)
                ? r as Dictionary<string, object?> : null;
            if (consumed.ContainsKey(nonce))
                denial = "CODEX_GRANT_REPLAY";
            else if (record is null)
                denial = "CODEX_GRANT_UNKNOWN";
            else if (record.TryGetValue("consumed", out var c)
                && c is true)
                denial = "CODEX_GRANT_REPLAY";
            else if (Convert.ToInt64(
                    record.GetValueOrDefault("generation"))
                != Convert.ToInt64(state["revocation_generation"]))
                denial = "CODEX_GRANT_REVOKED";
            else if (!GrantExpiryValid(record.GetValueOrDefault("expires_at"),
                    DateTimeOffset.UtcNow.ToUnixTimeSeconds()))
                denial = "CODEX_GRANT_EXPIRED";
            else
            {
                record["consumed"] = true;
                consumed[nonce] = CodexEntryState.UtcNow();
            }
        });
        var secondary = record?.GetValueOrDefault("secondary")
            ?.ToString() ?? "";
        if (denial is null)
        {
            if (record!["operation"]?.ToString() != operation)
                denial = "CODEX_GRANT_OPERATION_MISMATCH";
            else if (record["primary"]?.ToString() != actor)
                denial = "CODEX_GRANT_ACTOR_MISMATCH";
            else if (record["purpose"]?.ToString() != purpose)
                denial = "CODEX_GRANT_PURPOSE_MISMATCH";
            else if (record["scope_hash"]?.ToString()
                != CodexEntryState.ScopeHash(parsedScope))
                denial = "CODEX_GRANT_SCOPE_MISMATCH";
            else if (record["access_class"]?.ToString() != accessClass)
                denial = "CODEX_GRANT_CLASS_MISMATCH";
            else
            {
                long? currentVersion = null;
                try
                {
                    currentVersion = CodexRepository
                        .LoadGovernanceCodex().CodexVersion;
                }
                catch (Exception)
                {
                }
                if (Convert.ToInt64(record["codex_version"])
                    != currentVersion)
                    denial = "CODEX_VERSION_CHANGED";
            }
        }
        if (denial is not null)
        {
            Audit(denial, actor, secondary, purpose, parsedScope, nonce);
            throw new CodexReadDenied(denial);
        }
        Audit("CONSUMED", actor, secondary, purpose, parsedScope, nonce);
    }
}
