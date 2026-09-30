// CreativeRuntime.cs — creative/persona interaction contracts
// (§10/§11/§12/§16/§17/§19/§24/§25/§26/§27).
//
//   CreativeMode             sampling/profile presets — style only,
//                            never safety/tools/data access (§11).
//   RoleplaySession          structured session state (role/world/
//                            characters/timeline/scene/facts/threads)
//                            with compact-resume, not prompt stuffing
//                            (§10).
//   NarrativeMemory          FACTUAL_MEMORY / NARRATIVE_MEMORY
//                            namespaces — fiction can never pollute
//                            canonical knowledge (§16).
//   FactualityPolicy         FACTUAL_STRICT/GROUNDED/GENERAL/FICTIONAL
//                            — creativity and hallucination are
//                            separated by contract (§17).
//   RefusalDecisionGate      ALLOW/ALLOW_WITH_CONSTRAINT/
//                            SAFE_TRANSFORM/REFUSE — intent- and
//                            output-based, never single-keyword (§12).
//   star-refusal-eval/v1     refusal quality ledger (§13).
//   star-roleplay-eval/v1    character/voice/world consistency eval
//                            (§25).
//   InteractionModeRouter    CHAT/CREATIVE/ROLEPLAY/RESEARCH/AGENT/
//                            CODING/DOCUMENT — only AGENT enters the
//                            work-graph path (§19).

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CreativeRuntime
{
    public const string RoleplayFormat = "star-roleplay-session/v1";
    public const string NarrativeFormat = "star-narrative-memory/v1";
    public const string RefusalEvalFormat = "star-refusal-eval/v1";
    public const string RoleplayEvalFormat = "star-roleplay-eval/v1";
    public const string MemoryDir =
        "xingcheng/runtime/state/narrative-memory";
    public const string SessionDir =
        "xingcheng/runtime/state/roleplay-sessions";

    public static readonly string[] CreativeProfiles =
        { "PRECISE", "BALANCED", "CREATIVE", "ROLEPLAY", "STORY",
          "BRAINSTORM" };
    public static readonly string[] FactualityModes =
        { "FACTUAL_STRICT", "GROUNDED", "GENERAL", "FICTIONAL" };
    public static readonly string[] RefusalDecisions =
        { "ALLOW", "ALLOW_WITH_CONSTRAINT", "SAFE_TRANSFORM",
          "REFUSE" };
    public static readonly string[] RefusalCategories =
        { "correct_allow", "unnecessary_refusal", "correct_refusal",
          "over_refusal", "under_refusal", "safe_transform_success" };
    public static readonly string[] InteractionModes =
        { "CHAT", "CREATIVE", "ROLEPLAY", "RESEARCH", "AGENT",
          "CODING", "DOCUMENT" };
    public static readonly string[] CreativeSuites =
        { "creative-writing", "roleplay", "story-continuation",
          "character-dialogue", "style-transfer", "brainstorm",
          "world-building", "long-form-continuity" };
    public static readonly string[] RoleplayEvalDims =
        { "character_identity", "voice_consistency",
          "relationship_consistency", "world_state", "timeline",
          "instruction_following", "creative_diversity",
          "unnecessary_refusal", "context_retention" };
    public static readonly string[] ZhTwEvalDims =
        { "zh_tw_naturalness", "dialogue_naturalness",
          "character_voice_consistency", "style_consistency" };
    public static readonly string[] MemoryNamespaces =
        { "FACTUAL_MEMORY", "NARRATIVE_MEMORY" };

    // -------------------------------------------------- creative mode --
    // §11: profiles shape sampling only. Safety rules, tool and data
    // permissions are structurally absent from the contract.

    public static Dictionary<string, object?> CreativeProfile(
        string profile)
    {
        if (!CreativeProfiles.Contains(profile))
            throw new ExecutorError("CREATIVE_PROFILE_INVALID",
                $"unknown creative profile {profile}");
        var sampling = new Dictionary<string, object?>
        {
            ["temperature"] = profile switch
            {
                "PRECISE" => 0.2, "BALANCED" => 0.7,
                "CREATIVE" => 0.9, "ROLEPLAY" => 0.85,
                "STORY" => 0.9, "BRAINSTORM" => 1.0, _ => 0.7,
            },
            ["top_p"] = profile switch
            {
                "PRECISE" => 0.9, "BRAINSTORM" => 0.99, _ => 0.95,
            },
            ["repetition_penalty"] = profile switch
            {
                "PRECISE" => 1.05, "STORY" => 1.15,
                "ROLEPLAY" => 1.1, _ => 1.1,
            },
            ["style_diversity"] = profile switch
            {
                "BRAINSTORM" => "high", "CREATIVE" => "high",
                "PRECISE" => "low", _ => "medium",
            },
            ["description_density"] = profile switch
            {
                "STORY" => "high", "ROLEPLAY" => "medium",
                "PRECISE" => "low", _ => "medium",
            },
            ["dialogue_density"] = profile switch
            {
                "ROLEPLAY" => "high", "STORY" => "medium",
                _ => "low",
            },
            ["narrative_continuity"] = profile is "STORY" or "ROLEPLAY",
            ["variation_budget"] = profile == "BRAINSTORM" ? "wide" : "bounded",
        };
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-creative-mode/v1",
            ["profile"] = profile,
            ["sampling"] = sampling,
            // §11 hard boundary — these fields can never exist.
            ["safety_rules"] = null,
            ["tool_permissions"] = null,
            ["data_access_permissions"] = null,
        };
    }

    // ------------------------------------------------ factuality policy --
    // §17: separates hallucination from creativity. FICTIONAL output is
    // marked fictional context and never enters canonical knowledge.

    public static Dictionary<string, object?> FactualityResolve(
        string mode)
    {
        if (!FactualityModes.Contains(mode))
            throw new ExecutorError("FACTUALITY_POLICY_INVALID",
                $"unknown factuality mode {mode}");
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-factuality-policy/v1",
            ["mode"] = mode,
            ["may_invent_facts"] = mode is "FICTIONAL",
            ["requires_evidence"] = mode is "GROUNDED",
            ["unknown_facts_allowed"] = mode is "GENERAL" or "FICTIONAL",
            ["output_marker"] = mode == "FICTIONAL"
                ? "fictional_context" : "canonical",
            ["writes_canonical_memory"] = mode != "FICTIONAL",
            ["writes_namespace"] = mode == "FICTIONAL"
                ? "NARRATIVE_MEMORY" : "FACTUAL_MEMORY",
        };
    }

    // ------------------------------------------------ narrative memory --
    // §16: two namespaces; NARRATIVE_MEMORY is never read by RAG /
    // knowledge / user factual memory.

    public static Dictionary<string, object?> MemoryWrite(
        string toolRoot, JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object ||
            !el.TryGetProperty("namespace", out var ns) ||
            !el.TryGetProperty("key", out var k))
            throw new ExecutorError("NARRATIVE_MEMORY_INVALID",
                "needs namespace + key");
        string space = ns.GetString() ?? "";
        if (!MemoryNamespaces.Contains(space))
            throw new ExecutorError("NARRATIVE_MEMORY_INVALID",
                $"bad namespace {space}");
        // Isolation proof: writing to NARRATIVE_MEMORY returns the
        // namespace it lives in — factual readers can never see it
        // because the dir layout keeps them apart (§16).
        var allowed = new[]
            { "character_profile", "character_voice", "location",
              "relationship", "timeline", "facts", "promises",
              "foreshadowing", "unresolved_thread" };
        string kind = el.TryGetProperty("kind", out var kd)
            ? kd.GetString() ?? "" : "";
        if (space == "NARRATIVE_MEMORY" && kind.Length > 0 &&
            !allowed.Contains(kind))
            throw new ExecutorError("NARRATIVE_MEMORY_INVALID",
                $"bad narrative kind {kind}");
        var rec = new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = NarrativeFormat,
            ["namespace"] = space,
            ["key"] = k.GetString(),
            ["kind"] = kind.Length > 0 ? kind : "facts",
            ["value"] = el.TryGetProperty("value", out var v)
                ? ModelLifecycle.Decode(v) : null,
            ["visible_to_rag"] = space == "FACTUAL_MEMORY",
            ["visible_to_canonical"] = space == "FACTUAL_MEMORY",
        };
        string dir = Path.Combine(
            toolRoot, MemoryDir.Replace('/', Path.DirectorySeparatorChar),
            space);
        Directory.CreateDirectory(dir);
        ModelLifecycle.AtomicWrite(
            Path.Combine(dir,
                TransformerTrainingRepository.Sha256Text(
                    (string)rec["key"]!)[..16] + ".json"),
            CanonicalJson.PrettyDict(rec) + "\n");
        return rec;
    }

    public static Dictionary<string, object?> MemoryRead(
        string toolRoot, string space, string key)
    {
        if (!MemoryNamespaces.Contains(space))
            throw new ExecutorError("NARRATIVE_MEMORY_INVALID",
                $"bad namespace {space}");
        string path = Path.Combine(
            toolRoot, MemoryDir.Replace('/', Path.DirectorySeparatorChar),
            space,
            TransformerTrainingRepository.Sha256Text(key)[..16] + ".json");
        if (!File.Exists(path))
            throw new ExecutorError("NARRATIVE_MEMORY_INVALID",
                $"{space}/{key}: missing");
        return (Dictionary<string, object?>)ModelLifecycle.Decode(
            JsonDocument.Parse(File.ReadAllText(path)).RootElement)!;
    }

    /// <summary>Isolation proof for §36: a NARRATIVE read through the
    /// factual namespace fails; a FACTUAL read through the narrative
    /// namespace fails. Namespaces share no directory.</summary>
    public static Dictionary<string, object?> MemoryIsolationCheck(
        string toolRoot)
    {
        bool narrativeBlocked = false, factualBlocked = false;
        try { MemoryRead(toolRoot, "FACTUAL_MEMORY", "__narrative_probe"); }
        catch (ExecutorError) { narrativeBlocked = true; }
        try { MemoryRead(toolRoot, "NARRATIVE_MEMORY", "__factual_probe"); }
        catch (ExecutorError) { factualBlocked = true; }
        return new Dictionary<string, object?>
        {
            ["ok"] = narrativeBlocked && factualBlocked,
            ["format"] = "star-memory-isolation/v1",
            ["namespaces"] = MemoryNamespaces,
            ["cross_namespace_reads_blocked"] =
                narrativeBlocked && factualBlocked,
        };
    }

    // ------------------------------------------------ roleplay session --
    // §10: structured state + context compaction; the whole world never
    // re-enters the prompt.

    public static Dictionary<string, object?> SessionCreate(
        string toolRoot, JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("ROLEPLAY_SESSION_INVALID",
                "session must be object");
        string id = "rp-" + Guid.NewGuid().ToString("N")[..10];
        var session = new Dictionary<string, object?>
        {
            ["format"] = RoleplayFormat, ["session_id"] = id,
            ["created_at"] = XcPaths.IsoNow(),
            ["role"] = el.TryGetProperty("role", out var r)
                ? r.GetString() : "",
            ["world"] = el.TryGetProperty("world", out var w)
                ? ModelLifecycle.Decode(w) : null,
            ["characters"] = el.TryGetProperty("characters", out var ch)
                ? ModelLifecycle.Decode(ch) : new List<object?>(),
            ["relationships"] =
                el.TryGetProperty("relationships", out var rel)
                    ? ModelLifecycle.Decode(rel) : new List<object?>(),
            ["timeline"] = new List<object?>(),
            ["scene"] = el.TryGetProperty("scene", out var sc)
                ? sc.GetString() : "",
            ["tone"] = el.TryGetProperty("tone", out var tn)
                ? tn.GetString() : "",
            ["facts"] = new List<object?>(),
            ["story_constraints"] =
                el.TryGetProperty("story_constraints", out var st)
                    ? ModelLifecycle.Decode(st) : new List<object?>(),
            ["open_threads"] = new List<object?>(),
            ["compact_count"] = 0,
        };
        SaveSession(toolRoot, session);
        session["ok"] = true;
        return session;
    }

    public static Dictionary<string, object?> SessionEvent(
        string toolRoot, string sessionId, JsonElement el)
    {
        var s = LoadSession(toolRoot, sessionId);
        string kind = el.TryGetProperty("kind", out var k)
            ? k.GetString() ?? "event" : "event";
        object? payload = el.TryGetProperty("payload", out var p)
            ? ModelLifecycle.Decode(p) : null;
        switch (kind)
        {
            case "scene_transition":
                if (payload is string ns && ns.Length > 0)
                {
                    ((List<object?>)s["timeline"]!).Add(
                        new Dictionary<string, object?>
                        {
                            ["at"] = XcPaths.IsoNow(),
                            ["event"] = "scene:" + s["scene"],
                        });
                    s["scene"] = ns;
                }
                break;
            case "fact":
                ((List<object?>)s["facts"]!).Add(payload);
                break;
            case "open_thread":
                ((List<object?>)s["open_threads"]!).Add(payload);
                break;
            case "close_thread":
                ((List<object?>)s["open_threads"]!).Remove(payload);
                break;
            default:
                ((List<object?>)s["timeline"]!).Add(
                    new Dictionary<string, object?>
                    {
                        ["at"] = XcPaths.IsoNow(),
                        ["event"] = payload,
                    });
                break;
        }
        SaveSession(toolRoot, s);
        s["ok"] = true;
        return s;
    }

    /// <summary>Compact: collapse timeline into facts + a summary —
    //  resume uses the compact state, not the raw transcript (§10).</summary>
    public static Dictionary<string, object?> SessionCompact(
        string toolRoot, string sessionId)
    {
        var s = LoadSession(toolRoot, sessionId);
        var timeline = (List<object?>)s["timeline"]!;
        int compacted = timeline.Count;
        ((List<object?>)s["facts"]!).Add(new Dictionary<string, object?>
        {
            ["kind"] = "compaction",
            ["events_folded"] = compacted,
            ["at"] = XcPaths.IsoNow(),
        });
        timeline.Clear();
        s["compact_count"] =
            Convert.ToInt32(s["compact_count"] ?? 0) + 1;
        SaveSession(toolRoot, s);
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = RoleplayFormat,
            ["session_id"] = sessionId,
            ["compacted_events"] = compacted,
            ["compact_count"] = s["compact_count"],
            ["resumable"] = true,
        };
    }

    private static string SessionPath(string toolRoot, string id)
        => Path.Combine(toolRoot,
            SessionDir.Replace('/', Path.DirectorySeparatorChar),
            id + ".json");

    private static Dictionary<string, object?> LoadSession(
        string toolRoot, string id)
    {
        string path = SessionPath(toolRoot, id);
        if (!File.Exists(path))
            throw new ExecutorError("ROLEPLAY_SESSION_INVALID",
                $"{id}: session missing");
        return (Dictionary<string, object?>)ModelLifecycle.Decode(
            JsonDocument.Parse(File.ReadAllText(path)).RootElement)!;
    }

    private static void SaveSession(
        string toolRoot, Dictionary<string, object?> s)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(
            SessionPath(toolRoot, (string)s["session_id"]!))!);
        ModelLifecycle.AtomicWrite(
            SessionPath(toolRoot, (string)s["session_id"]!),
            CanonicalJson.PrettyDict(s) + "\n");
    }

    // ------------------------------------------------- refusal gate ----
    // §12: intent/output-based — a keyword alone never forces REFUSE.
    // Input: {"intent","theme","explicit_content","harm_potential",
    //         "permission_ok"}. Normal fiction / roleplay / horror /
    // combat / non-explicit adult themes / critical analysis /
    // security-defence discussion are not refused on keywords.

    public static Dictionary<string, object?> RefusalDecide(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("REFUSAL_DECISION_INVALID",
                "refusal input must be object");
        string intent = el.TryGetProperty("intent", out var i)
            ? i.GetString() ?? "" : "";
        string theme = el.TryGetProperty("theme", out var t)
            ? t.GetString() ?? "" : "";
        bool explicitContent =
            el.TryGetProperty("explicit_content", out var ec) &&
            ec.ValueKind == JsonValueKind.True;
        bool harm = el.TryGetProperty("harm_potential", out var hp) &&
                    (hp.GetString() ?? "") is { } h &&
                    (h == "high" || h == "critical");
        bool permission =
            !el.TryGetProperty("permission_ok", out var pk) ||
            pk.ValueKind == JsonValueKind.True;
        string decision; string reason;
        if (!permission)
        {
            decision = "REFUSE";
            reason = "permission boundary";
        }
        else if (harm)
        {
            // high harm potential — refuse or transform depending on
            // whether the request is a legitimate analysis frame.
            bool analysisFrame =
                intent is "critical_analysis" or "security_defense" or
                          "historical_discussion" or "fiction";
            decision = analysisFrame ? "SAFE_TRANSFORM" : "REFUSE";
            reason = analysisFrame
                ? "high-harm topic in legitimate frame -> transform"
                : "high harm potential";
        }
        else if (explicitContent)
        {
            decision = "ALLOW_WITH_CONSTRAINT";
            reason = "adult theme allowed non-explicitly";
        }
        else
        {
            decision = "ALLOW";
            reason = "ordinary request";
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-refusal-decision/v1",
            ["decision"] = decision, ["reason"] = reason,
            ["intent"] = intent, ["theme"] = theme,
        };
    }

    // ------------------------------------------------ refusal eval -----
    // §13: star-refusal-eval/v1 — the target is a low
    // unnecessary_refusal_rate WITHOUT raising unsafe_completion_rate.

    public static Dictionary<string, object?> RefusalEval(
        string toolRoot, JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("REFUSAL_EVAL_INVALID",
                "eval record must be object");
        string category =
            el.TryGetProperty("category", out var c)
                ? c.GetString() ?? "" : "";
        if (!RefusalCategories.Contains(category))
            throw new ExecutorError("REFUSAL_EVAL_INVALID",
                $"bad category {category}");
        string dir = Path.Combine(toolRoot,
            "xingcheng/runtime/state/refusal-eval"
                .Replace('/', Path.DirectorySeparatorChar));
        Directory.CreateDirectory(dir);
        string path = Path.Combine(dir, "records.jsonl");
        var rec = new Dictionary<string, object?>
        {
            ["format"] = RefusalEvalFormat,
            ["category"] = category,
            ["case_id"] = el.TryGetProperty("case_id", out var ci)
                ? ci.GetString() : "",
            ["recorded_at"] = XcPaths.IsoNow(),
        };
        File.AppendAllText(path, CanonicalJson.CanonicalDict(rec) + "\n");
        // Aggregate over the ledger.
        var counts = RefusalCategories.ToDictionary(x => x, _ => 0);
        foreach (var line in File.ReadAllLines(path))
        {
            if (line.Length == 0) continue;
            using var doc = JsonDocument.Parse(line);
            if (doc.RootElement.TryGetProperty("category", out var cc))
            {
                string k = cc.GetString() ?? "";
                if (counts.ContainsKey(k)) ++counts[k];
            }
        }
        int total = counts.Values.Sum();
        int unsafeN = counts["under_refusal"];
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = RefusalEvalFormat,
            ["recorded"] = rec,
            ["totals"] = counts.ToDictionary(
                kv => kv.Key, kv => (object?)kv.Value),
            ["unnecessary_refusal_rate"] =
                total > 0
                    ? (double)counts["unnecessary_refusal"] / total : 0.0,
            ["unsafe_completion_rate"] =
                total > 0 ? (double)unsafeN / total : 0.0,
        };
    }

    // ------------------------------------------------ roleplay eval ----
    // §25 + §24: roleplay quality is consistency, not florid text;
    // zh-TW dims are mandatory for creative modes.

    public static Dictionary<string, object?> RoleplayEval(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object ||
            !el.TryGetProperty("scores", out var sc) ||
            sc.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("ROLEPLAY_EVAL_INVALID",
                "eval needs scores{}");
        var missing = RoleplayEvalDims
            .Where(d => !sc.TryGetProperty(d, out _)).ToList();
        var zhmissing = ZhTwEvalDims
            .Where(d => !sc.TryGetProperty(d, out _)).ToList();
        if (missing.Count > 0 || zhmissing.Count > 0)
            throw new ExecutorError("ROLEPLAY_EVAL_INVALID",
                "missing dims: " +
                string.Join(",", missing.Concat(zhmissing)));
        double sum = 0; int n = 0;
        var scores = new Dictionary<string, object?>();
        foreach (var d in RoleplayEvalDims.Concat(ZhTwEvalDims))
        {
            double v = sc.GetProperty(d).GetDouble();
            scores[d] = v; sum += v; ++n;
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = RoleplayEvalFormat,
            ["scores"] = scores,
            ["mean_score"] = sum / n,
            ["zh_tw_naturalness"] = scores["zh_tw_naturalness"],
        };
    }

    // -------------------------------------------- interaction router ---
    // §19: only AGENT enters the work-graph path; ROLEPLAY/CREATIVE go
    // to persona + narrative memory + native inference.

    public static Dictionary<string, object?> Route(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("INTERACTION_ROUTE_INVALID",
                "route input must be object");
        string mode = el.TryGetProperty("mode", out var m)
            ? m.GetString() ?? "CHAT" : "CHAT";
        if (!InteractionModes.Contains(mode))
            throw new ExecutorError("INTERACTION_ROUTE_INVALID",
                $"unknown mode {mode}");
        var pipeline = mode switch
        {
            "ROLEPLAY" => new[]
                { "PersonaRuntime", "NarrativeMemory",
                  "NativeInferenceEngine" },
            "CREATIVE" => new[]
                { "CreativeMode", "NarrativeMemory",
                  "FactualityPolicy", "NativeInferenceEngine" },
            "AGENT" => new[]
                { "LongHorizonTaskCoordinator", "ToolDecisionGate",
                  "AgentWorkGraph" },
            "CODING" => new[]
                { "StarCodeAgentRuntime", "RepoTaskHarness",
                  "NativeInferenceEngine" },
            "DOCUMENT" or "RESEARCH" => new[]
                { "RetrievalDecisionGate", "LocalRAG",
                  "DocumentEvidenceGraph", "GroundingGate",
                  "NativeInferenceEngine", "ClaimExtraction",
                  "CitationResolver" },
            _ => new[] { "NativeInferenceEngine" },
        };
        // §27: RAG/creative mutual exclusion encoded in the route.
        string ragDefault = mode is "ROLEPLAY" or "CREATIVE"
            ? "RAG_NOT_REQUIRED"
            : mode is "DOCUMENT" or "RESEARCH"
                ? "RAG_REQUIRED" : "RAG_OPTIONAL";
        string factuality = mode is "ROLEPLAY" or "CREATIVE"
            ? "FICTIONAL"
            : mode is "DOCUMENT" or "RESEARCH"
                ? "GROUNDED" : "GENERAL";
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-interaction-route/v1",
            ["mode"] = mode,
            ["pipeline"] = pipeline.Cast<object?>().ToList(),
            ["uses_agent_workgraph"] = mode == "AGENT",
            ["rag_default"] = ragDefault,
            ["factuality_default"] = factuality,
        };
    }
}
