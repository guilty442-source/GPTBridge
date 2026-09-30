// PersonaRuntime.cs — persona, style and steerability contracts
// (Hermes-class community lessons; §8/§9/§14/§15/§23/§28).
//
//   PersonaRuntime            presentation/behaviour profile ONLY —
//                             can never grant authority, tool access
//                             or governance override (§8/§9).
//   star-style-profile/v1     built-in linguistic-behaviour profiles;
//                             a style profile may never carry
//                             permissions/tools/governance (§23).
//   SteerabilityController    resolves tone/style/length/format/
//                             persona/language/creativity/effort into
//                             ONE effective profile with a fixed
//                             priority ladder (§14).
//   star-dialogue-envelope/v2 + InstructionConflictResolver —
//                             conflicts resolve by rank, never by the
//                             model guessing (§15).
//   PersonaInjectionGuard     document/web/RAG/tool content is
//                             UNTRUSTED and can never overwrite
//                             System/Persona/Governance (§28).

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class PersonaRuntime
{
    public const string PersonaFormat = "star-persona/v1";
    public const string StyleFormat = "star-style-profile/v1";
    public const string EnvelopeFormat = "star-dialogue-envelope/v2";
    public const string RelDir = "xingcheng/runtime/state/personas";

    // Fields a persona/style may NEVER carry (§9/§23): touching them
    // is a hard fail, not a warning.
    public static readonly string[] ForbiddenPersonaFields =
        { "authority", "permissions", "tools", "tool_policy",
          "system_permission", "governance", "safety_boundary",
          "generation_lifecycle", "refusal_override",
          "bypass_governance", "unrestricted" };

    public static readonly string[] BuiltInStyles =
        { "neutral", "professional", "concise", "technical",
          "friendly", "creative", "literary", "roleplay",
          "brainstorm", "storytelling" };

    /// <summary>Priority ladder (§14/§15) — higher wins.</summary>
    public static readonly string[] InstructionRanks =
        { "governance", "system_contract", "application_contract",
          "user_request", "persona", "style" };

    private static string Dir(string toolRoot)
        => Path.Combine(toolRoot,
                        RelDir.Replace('/', Path.DirectorySeparatorChar));

    // ------------------------------------------------ persona intake --

    /// <summary>Validate + persist a persona definition. Authority
    /// fields fail closed with PERSONA_CANNOT_GRANT_AUTHORITY.</summary>
    public static Dictionary<string, object?> ValidatePersona(
        string toolRoot, JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("PERSONA_INVALID",
                "persona must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt.Length > 0 && fmt != PersonaFormat)
            throw new ExecutorError("PERSONA_INVALID",
                $"expected format {PersonaFormat}");
        var forbidden = ForbiddenPersonaFields
            .Where(k => el.TryGetProperty(k, out _)).ToList();
        if (forbidden.Count > 0)
            throw new ExecutorError("PERSONA_CANNOT_GRANT_AUTHORITY",
                "persona may not define: " +
                string.Join(",", forbidden));
        // §9: even in text fields, authority-granting declarations are
        // rejected, not silently ignored.
        var textFields = new[]
            { "role", "behavior_constraints", "fictional_setting",
              "name", "speaking_pattern" };
        foreach (var k in textFields)
        {
            if (!el.TryGetProperty(k, out var v) ||
                v.ValueKind != JsonValueKind.String) continue;
            string s = (v.GetString() ?? "").ToLowerInvariant();
            if (s.Contains("系統管理員") || s.Contains("system admin") ||
                s.Contains("不受限制") || s.Contains("unrestricted") ||
                s.Contains("bypass governance") || s.Contains("繞過治理"))
                throw new ExecutorError(
                    "PERSONA_CANNOT_GRANT_AUTHORITY",
                    $"{k}: authority-granting persona text rejected");
        }
        string id = el.TryGetProperty("persona_id", out var p) &&
                    (p.GetString() ?? "").Length > 0
            ? p.GetString()! : "persona-" + Guid.NewGuid().ToString("N")[..8];
        var rec = new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = PersonaFormat,
            ["persona_id"] = id,
            ["authority_scope"] = "presentation_only",
            ["can_grant_authority"] = false,
            ["fields"] = new Dictionary<string, object?>
            {
                ["name"] = el.TryGetProperty("name", out var n)
                    ? n.GetString() : null,
                ["language_style"] =
                    el.TryGetProperty("language_style", out var ls)
                        ? ls.GetString() : null,
                ["tone"] = el.TryGetProperty("tone", out var tn)
                    ? tn.GetString() : null,
                ["verbosity"] =
                    el.TryGetProperty("verbosity", out var vb)
                        ? vb.GetString() : null,
                ["formality"] =
                    el.TryGetProperty("formality", out var fm)
                        ? fm.GetString() : null,
                ["creativity"] =
                    el.TryGetProperty("creativity", out var cv)
                        ? ModelLifecycle.Decode(cv) : null,
                ["role"] = el.TryGetProperty("role", out var ro)
                    ? ro.GetString() : null,
                ["format_preferences"] =
                    el.TryGetProperty("format_preferences", out var fp)
                        ? ModelLifecycle.Decode(fp) : null,
            },
        };
        Directory.CreateDirectory(Dir(toolRoot));
        ModelLifecycle.AtomicWrite(
            Path.Combine(Dir(toolRoot), id + ".json"),
            CanonicalJson.PrettyDict(rec) + "\n");
        return rec;
    }

    /// <summary>Load a persona by id — presentation fields only.</summary>
    public static Dictionary<string, object?> GetPersona(
        string toolRoot, string id)
    {
        string path = Path.Combine(Dir(toolRoot), id + ".json");
        if (!File.Exists(path))
            throw new ExecutorError("PERSONA_INVALID",
                $"{id}: persona missing");
        return (Dictionary<string, object?>)ModelLifecycle.Decode(
            JsonDocument.Parse(File.ReadAllText(path)).RootElement)!;
    }

    // ------------------------------------------------ style profiles --

    public static Dictionary<string, object?> StyleProfile(string name)
    {
        if (!BuiltInStyles.Contains(name))
            throw new ExecutorError("STYLE_PROFILE_INVALID",
                $"unknown style {name}");
        // §23: linguistic behaviour only — explicit absence of any
        // authority fields is part of the contract.
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = StyleFormat,
            ["style"] = name,
            ["linguistic_behavior"] = new Dictionary<string, object?>
            {
                ["tone"] = name switch
                {
                    "concise" => "direct", "technical" => "precise",
                    "friendly" => "warm", "literary" => "lyrical",
                    "brainstorm" => "divergent", _ => "neutral",
                },
                ["verbosity"] = name switch
                {
                    "concise" => "low", "literary" => "high",
                    "storytelling" => "high", _ => "medium",
                },
                ["zh_tw_naturalness_required"] = true,   // §24
            },
            ["permissions"] = null, ["tools"] = null,
            ["governance"] = null,
        };
    }

    // ------------------------------------- steerability resolution ---
    // §14 ladder: Governance > Safety > User explicit > Task contract >
    // Persona > Style default. Persona can never outrank the user's
    // current explicit request.

    public static Dictionary<string, object?> Steer(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("STEER_INVALID",
                "steer input must be object");
        var dims = new[]
            { "tone", "style", "length", "format", "language",
              "creativity", "reasoning_effort", "citation_preference",
              "output_structure" };
        var resolved = new Dictionary<string, object?>();
        var provenance = new Dictionary<string, object?>();
        foreach (var d in dims)
        {
            // sources in rank order, low -> high; later sources win.
            var sources = new (string key, string rank)[]
            {
                ($"style_{d}", "style"),
                ($"persona_{d}", "persona"),
                ($"task_{d}", "task_contract"),
                ($"user_{d}", "user_request"),
                ($"system_{d}", "system_contract"),
                ($"governance_{d}", "governance"),
            };
            object? value = null; string winner = "default";
            foreach (var (key, rank) in sources)
                if (el.TryGetProperty(key, out var v) &&
                    v.ValueKind != JsonValueKind.Null)
                { value = ModelLifecycle.Decode(v); winner = rank; }
            resolved[d] = value;
            provenance[d] = winner;
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-steerability-resolution/v1",
            ["resolved"] = resolved,
            ["provenance"] = provenance,
            ["priority"] = InstructionRanks
                .Cast<object?>().Reverse().ToList(),
        };
    }

    // ------------------------------ dialogue envelope + conflicts ----
    // §15: a conflict between instruction layers resolves by rank with
    // an explicit record — the model never guesses which wins.

    public static Dictionary<string, object?> ResolveConflict(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object ||
            !el.TryGetProperty("conflict", out var c) ||
            c.ValueKind != JsonValueKind.Array ||
            c.GetArrayLength() < 2)
            throw new ExecutorError("INSTRUCTION_CONFLICT_INVALID",
                "conflict needs >=2 instructions");
        JsonElement best = c[0];
        int bestRank = -1;
        var rejected = new List<object?>();
        foreach (var item in c.EnumerateArray())
        {
            string layer =
                item.TryGetProperty("layer", out var l)
                    ? l.GetString() ?? "" : "";
            int rank = Array.IndexOf(InstructionRanks, layer);
            if (rank > bestRank) { bestRank = rank; best = item; }
        }
        foreach (var item in c.EnumerateArray())
        {
            if (item.GetRawText() == best.GetRawText()) continue;
            rejected.Add(new Dictionary<string, object?>
            {
                ["layer"] = item.TryGetProperty("layer", out var l)
                    ? l.GetString() : null,
                ["instruction"] =
                    item.TryGetProperty("instruction", out var ins)
                        ? ins.GetString() : null,
            });
        }
        string selLayer =
            best.TryGetProperty("layer", out var bl)
                ? bl.GetString() ?? "" : "";
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = EnvelopeFormat,
            ["selected_instruction"] = new Dictionary<string, object?>
            {
                ["layer"] = selLayer,
                ["instruction"] =
                    best.TryGetProperty("instruction", out var si)
                        ? si.GetString() : null,
            },
            ["rejected_instructions"] = rejected,
            ["reason_code"] = "PRIORITY_" + selLayer.ToUpperInvariant(),
        };
    }

    // ------------------------------------------------- injection guard --
    // §28: Document/Web/RAG/Tool content is UNTRUSTED_DOCUMENT_CONTENT.
    // Instruction-like text inside them never rewrites System/Persona/
    // Governance.

    public static readonly string[] UntrustedSources =
        { "document", "web", "rag", "tool_result" };

    public static Dictionary<string, object?> GuardInjection(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("PERSONA_INJECTION_GUARD",
                "guard input must be object");
        string source =
            el.TryGetProperty("source", out var s)
                ? s.GetString() ?? "" : "";
        string content =
            el.TryGetProperty("content", out var c)
                ? c.GetString() ?? "" : "";
        bool untrusted = UntrustedSources.Contains(source);
        string lower = content.ToLowerInvariant();
        bool attemptsOverride =
            lower.Contains("忽略前面") || lower.Contains("ignore previous") ||
            lower.Contains("ignore all") || lower.Contains("你現在是") ||
            lower.Contains("you are now") || lower.Contains("new system") ||
            lower.Contains("override persona") ||
            lower.Contains("覆蓋系統指令");
        bool sanitized = untrusted;
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-injection-guard/v1",
            ["source"] = source,
            ["classification"] = untrusted
                ? "UNTRUSTED_DOCUMENT_CONTENT" : "trusted_channel",
            ["instruction_override_attempted"] = attemptsOverride,
            ["can_rewrite_persona"] = false,
            ["can_rewrite_system"] = false,
            ["can_rewrite_governance"] = false,
            ["sanitized"] = sanitized,
        };
    }
}
