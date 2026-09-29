
namespace GPTBridge.CodexPipeline;


/// <summary>Official codex entry (A74/A173/A435) — port of
/// ``codex_official``: single-use read sessions for sovereign
/// declaration reads; metadata-only audit per attempt; never reads the
/// Chinese mirror.</summary>
internal static class CodexOfficial
{
    public const string OfficialEntry = "governance-codex://official";
    public const string AuthorityPath = PgDsn.AuthorityUri;

    private static readonly HashSet<string> RequestPurposes = new(
        StringComparer.Ordinal)
    { "self-declaration", "adjudication", "status" };

    private const double SessionTtlSeconds = 30.0;

    private sealed record SessionTuple(string Sid, string Actor,
        string Provision, string Purpose, double ExpiresAt);

    private static readonly Dictionary<string, SessionTuple> Sessions =
        new(StringComparer.Ordinal);
    private static readonly object SessionLock = new();

    private static double Monotonic() =>
        Environment.TickCount64 / 1000.0;

    private static void PurgeExpiredSessions(double now)
    {
        foreach (var key in Sessions
            .Where(p => p.Value.ExpiresAt < now).Select(p => p.Key)
            .ToList())
            Sessions.Remove(key);
    }

    /// <summary>Mint a single-use codex read session (A435 nonce +
    /// expiry + single-use).  The permission sovereign (entry-owner)
    /// calls this after completing the per-request permission review;
    /// a sovereign may self-mint only its own self-declaration
    /// read.</summary>
    public static string MintCodexReadSession(string sovereignId,
        string requester, string purpose, string provisionId)
    {
        var sid = (sovereignId ?? "").Trim();
        var actor = (requester ?? "").Trim();
        var prov = (provisionId ?? "").Trim();
        var purp = (purpose ?? "").Trim();
        if (sid.Length == 0 || actor.Length == 0 || prov.Length == 0
            || !RequestPurposes.Contains(purp))
            throw new CodexReadDenied("CODEX_SESSION_INVALID_REQUEST");
        if (purp != "self-declaration" && actor != sid
            && actor != "permission-sovereign")
            throw new CodexReadDenied(
                "CODEX_SESSION_UNAUTHORIZED_MINTER");
        var nonce = Convert.ToHexString(System.Security.Cryptography
            .RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
        var now = Monotonic();
        lock (SessionLock)
        {
            PurgeExpiredSessions(now);
            Sessions[nonce] = new SessionTuple(sid, actor, prov, purp,
                now + SessionTtlSeconds);
        }
        return nonce;
    }

    /// <summary>Consume a single-use session exactly once (A435 replay
    /// protection); unknown/expired/mismatched/consumed nonces
    /// fail.</summary>
    private static bool ConsumeSession(string nonce, string sovereignId,
        string requester, string provisionId, string purpose)
    {
        if (string.IsNullOrEmpty(nonce))
            return false;
        var now = Monotonic();
        SessionTuple? entry;
        lock (SessionLock)
        {
            PurgeExpiredSessions(now);
            if (!Sessions.Remove(nonce, out var found))
                return false;
            entry = found;
        }
        if (entry.ExpiresAt < now)
            return false;
        return entry.Sid == sovereignId && entry.Actor == requester
            && entry.Provision == provisionId
            && entry.Purpose == purpose;
    }

    private static void RecordReadAudit(string requester,
        string sovereignId, string provisionId, string purpose,
        string result) =>
        CodexEntryState.RecordSessionAudit("official-read", requester,
            purpose, CodexEntryState.AccessReview, new HashSet<string>(
            StringComparer.Ordinal)
            { $"sovereign:{sovereignId}", $"provision:{provisionId}" },
            null, "", result);

    /// <summary>Read one sovereign declaration from the official
    /// PostgreSQL authority — all A435 controls mandatory,
    /// fail-closed.</summary>
    public static CodexSovereign? OfficialSovereign(string sovereignId,
        string requester, string purpose, string provisionId,
        string sessionNonce)
    {
        var sid = (sovereignId ?? "").Trim();
        var actor = (requester ?? "").Trim();
        var prov = (provisionId ?? "").Trim();
        var purp = (purpose ?? "").Trim();
        string? denial = null;
        if (sid.Length == 0 || actor.Length == 0)
            denial = "DENIED_EMPTY_IDENTITY";
        else if (prov != sid)
            denial = "DENIED_SCOPE_MISMATCH";
        else if (!RequestPurposes.Contains(purp))
            denial = "DENIED_PURPOSE";
        else if (!ConsumeSession(sessionNonce, sid, actor, prov, purp))
            denial = "DENIED_SESSION_REPLAY_OR_INVALID";
        if (denial is not null)
        {
            RecordReadAudit(actor, sid, prov, purp, denial);
            return null;
        }
        GovernanceCodex codex;
        try
        {
            codex = CodexRepository.LoadGovernanceCodex();
        }
        catch (Exception)
        {
            RecordReadAudit(actor, sid, prov, purp, "CODEX_UNAVAILABLE");
            return null;
        }
        var found = codex.Sovereigns.FirstOrDefault(s => s.Id == sid);
        RecordReadAudit(actor, sid, prov, purp,
            found is not null ? "GRANTED" : "NOT_FOUND");
        return found;
    }

    /// <summary>Read a sovereign's own declaration via a self-attested
    /// session (self-minted nonce, requester == sovereign_id, purpose
    /// == self-declaration).</summary>
    public static CodexSovereign? OfficialSelfDeclaration(
        string sovereignId)
    {
        var sid = (sovereignId ?? "").Trim();
        if (sid.Length == 0)
            return null;
        var nonce = MintCodexReadSession(sid, sid, "self-declaration",
            sid);
        return OfficialSovereign(sid, sid, "self-declaration", sid,
            nonce);
    }
}
