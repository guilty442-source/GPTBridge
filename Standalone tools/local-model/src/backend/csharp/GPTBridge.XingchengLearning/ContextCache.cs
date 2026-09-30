// ContextCache.cs — MiMo Context Cache v2 (§5) + cache-aware-agent
// hit accounting (§25).
//
//   ContextCacheManager — composite keys, tiered levels, generation
//     binding. A cache key is never just a prompt hash (§5 first rule):
//     model_hash | generation | architecture_profile | tokenizer_hash
//     | text_hash | vision_hash | audio_hash | video_hash |
//     runtime_profile.
//
//   Levels (§5.1): L0_REQUEST | L1_SESSION | L2_PREFIX | L3_CONTENT |
//     L4_ARTIFACT — each with TTL, memory_limit, eviction_policy,
//     generation_binding.
//
//   Forbidden (§5 last rule): storing raw state bound to a retired
//     generation. Lookup under a generation mismatch misses.
//
//   CacheHit record (§25): source | age | generation | validity —
//     every hit is audit-visible.

namespace GPTBridge.XingchengLearning;

/// <summary>§5.1 cache level with per-tier governance.</summary>
internal enum CacheLevel { L0_REQUEST = 0, L1_SESSION = 1, L2_PREFIX = 2,
    L3_CONTENT = 3, L4_ARTIFACT = 4 }

internal sealed class CacheTierPolicy
{
    public long TtlS;
    public long MemoryLimitBytes;
    public string EvictionPolicy = "lru";   // lru | fifo — closed vocab
    public bool GenerationBinding = true;   // §5 last rule

    public static CacheTierPolicy For(CacheLevel l) => l switch
    {
        CacheLevel.L0_REQUEST => new()
            { TtlS = 300, MemoryLimitBytes = 64L << 20 },
        CacheLevel.L1_SESSION => new()
            { TtlS = 86400, MemoryLimitBytes = 512L << 20 },
        CacheLevel.L2_PREFIX => new()
            { TtlS = 7 * 86400, MemoryLimitBytes = 4L << 30 },
        CacheLevel.L3_CONTENT => new()
            { TtlS = 30 * 86400, MemoryLimitBytes = 4L << 30 },
        _ => new() { TtlS = 30 * 86400,
                     MemoryLimitBytes = 16L << 30 },
    };
}

/// <summary>§5 composite cache key — every component required; hashing
/// only the prompt is a contract breach.</summary>
internal sealed class CacheKey
{
    public string ModelHash = "";
    public string Generation = "";
    public string ArchitectureProfile = "";
    public string TokenizerHash = "";
    public string TextHash = "";
    public string VisionHash = "";
    public string AudioHash = "";
    public string VideoHash = "";
    public string RuntimeProfile = "";

    public void Validate()
    {
        foreach (var (name, v) in new (string, string)[]
                 {
                     ("model_hash", ModelHash),
                     ("generation", Generation),
                     ("architecture_profile", ArchitectureProfile),
                     ("tokenizer_hash", TokenizerHash),
                     ("runtime_profile", RuntimeProfile),
                 })
            if (v.Length == 0)
                throw new ExecutorError(
                    ConvErr.StructuredSchemaFailed,
                    $"cache key missing: {name}");
    }

    /// <summary>Canonical key string — order fixed, empty modality
    /// hashes serialize as "-" so key shape is stable.</summary>
    public string Canonical()
    {
        Validate();
        return string.Join('|',
            ModelHash, Generation, ArchitectureProfile, TokenizerHash,
            TextHash.Length > 0 ? TextHash : "-",
            VisionHash.Length > 0 ? VisionHash : "-",
            AudioHash.Length > 0 ? AudioHash : "-",
            VideoHash.Length > 0 ? VideoHash : "-",
            RuntimeProfile);
    }
}

/// <summary>§25 cache-hit record — every hit reports its source, age,
/// bound generation and validity.</summary>
internal sealed class CacheHitRecord
{
    public string Source = "";          // which tier served the hit
    public long AgeS;
    public string Generation = "";
    public string Validity = "VALID";   // VALID | STALE_POLICY | STALE_GEN
}

internal sealed class CacheEntry
{
    public Dictionary<string, object?> Value = new();
    public long StoredUnix;
    public string Generation = "";
    public long SizeBytes;
}

/// <summary>§5 ContextCacheManager — in-process tiered cache. Storage
/// is intentionally plain (a persisted backend plugs in behind the same
/// contract later); the governance semantics are the deliverable.</summary>
internal sealed class ContextCacheManager
{
    private readonly Dictionary<CacheLevel,
        (CacheTierPolicy policy,
         LinkedList<(string key, CacheEntry e)> lru,
         Dictionary<string, LinkedListNode<(string key, CacheEntry e)>>
             idx)> _tiers = new();

    public ContextCacheManager()
    {
        foreach (CacheLevel l in Enum.GetValues<CacheLevel>())
            _tiers[l] = (CacheTierPolicy.For(l),
                         new LinkedList<(string, CacheEntry)>(),
                         new Dictionary<string,
                            LinkedListNode<(string, CacheEntry)>>());
    }

    /// <summary>§5 allowed cache-value payload kinds — tokenized input,
    /// vision projection, prefix KV ref, DeltaNet state snapshot ref,
    /// retrieval evidence, tool result digest.</summary>
    public static readonly string[] ValueKinds =
        { "tokenized_input", "vision_projection", "prefix_kv",
          "deltanet_state_snapshot", "retrieval_evidence",
          "tool_result_digest" };

    public void Put(CacheLevel level, CacheKey key,
                    Dictionary<string, object?> value)
    {
        var (policy, lru, idx) = _tiers[level];
        string k = key.Canonical();
        var entry = new CacheEntry
        {
            Value = value,
            StoredUnix = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
            Generation = key.Generation,
            SizeBytes = CanonicalJson.CanonicalDict(value).Length,
        };
        if (idx.TryGetValue(k, out var old))
        {
            lru.Remove(old);
            idx.Remove(k);
        }
        var node = lru.AddFirst((k, entry));
        idx[k] = node;
        Evict(level, policy, lru, idx);
    }

    private static void Evict(
        CacheLevel level, CacheTierPolicy policy,
        LinkedList<(string key, CacheEntry e)> lru,
        Dictionary<string, LinkedListNode<(string, CacheEntry e)>> idx)
    {
        long bytes = lru.Sum(n => n.e.SizeBytes);
        while (lru.Count > 0 && bytes > policy.MemoryLimitBytes)
        {
            var last = lru.Last!;
            bytes -= last.Value.e.SizeBytes;
            idx.Remove(last.Value.key);
            lru.RemoveLast();
        }
    }

    /// <summary>Lookup — generation-bound by default (§5: a retired
    /// generation's raw state is never served). Returns null on miss;
    /// a hit fills <paramref name="hit"/> with the §25 record.</summary>
    public Dictionary<string, object?>? Get(
        CacheLevel level, CacheKey key, string activeGeneration,
        out CacheHitRecord? hit)
    {
        hit = null;
        var (policy, lru, idx) = _tiers[level];
        string k = key.Canonical();
        if (!idx.TryGetValue(k, out var node)) return null;
        var e = node.Value.e;
        long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        if (now - e.StoredUnix > policy.TtlS)
        {
            idx.Remove(k); lru.Remove(node);
            return null;
        }
        if (policy.GenerationBinding && e.Generation != activeGeneration)
        {
            hit = new CacheHitRecord
            {
                Source = level.ToString(), AgeS = now - e.StoredUnix,
                Generation = e.Generation, Validity = "STALE_GEN",
            };
            return null;   // §5 last rule — stale raw state never served
        }
        lru.Remove(node); lru.AddFirst(node);
        hit = new CacheHitRecord
        {
            Source = level.ToString(), AgeS = now - e.StoredUnix,
            Generation = e.Generation, Validity = "VALID",
        };
        return e.Value;
    }

    /// <summary>Multi-tier probe used by cache-aware agents (§25):
    /// first hit wins, record included either way.</summary>
    public (Dictionary<string, object?>? value, CacheHitRecord? hit)
        GetAny(IEnumerable<CacheLevel> levels, CacheKey key,
               string activeGeneration)
    {
        foreach (var l in levels)
        {
            var v = Get(l, key, activeGeneration, out var hit);
            if (v != null || hit != null) return (v, hit);
        }
        return (null, null);
    }
}
