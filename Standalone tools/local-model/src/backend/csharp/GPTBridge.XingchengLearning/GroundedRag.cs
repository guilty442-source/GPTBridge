// GroundedRag.cs — grounded RAG contracts (Command-R lessons).
//
//   star-grounded-result/v2   per-claim evidence record: every claim
//                             carries document/page/section/chunk/span
//                             + evidence_strength — never a trailing
//                             source list detached from claims (§2).
//   DocumentEvidenceGraph     retrieval/evidence graph — nodes
//                             (document/revision/page/section/chunk/
//                             claim/entity) and typed edges
//                             (SUPPORTS/CONTRADICTS/SUPERSEDES/...)(§3).
//   GroundingGate             claim->evidence alignment: unsupported
//                             claims are never dressed as facts (§5).
//   RetrievalDecisionGate     RAG_REQUIRED/OPTIONAL/NOT_REQUIRED/DENIED
//                             merged with ToolDecisionGate into
//                             ActionDecisionGate (§7).
//   Citation metrics          precision/recall/coverage/unsupported/
//                             wrong-source/wrong-revision/span/conflict
//                             (§6) — never just citation_present.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class GroundedRag
{
    public const string GroundedFormatV2 = "star-grounded-result/v2";
    public const string GraphFormat = "star-evidence-graph/v1";
    public const string CitationMetricsFormat = "star-citation-metrics/v1";
    public const string RelDir = "xingcheng/runtime/state/evidence-graph";

    public static readonly string[] EvidenceStrengths =
        { "DIRECT", "STRONG", "PARTIAL", "CONFLICTING", "UNSUPPORTED" };
    public static readonly string[] EdgeTypes =
        { "SUPPORTS", "CONTRADICTS", "SUPERSEDES", "DUPLICATES",
          "REFERENCES", "SAME_ENTITY", "DERIVED_FROM" };
    public static readonly string[] NodeTypes =
        { "document", "revision", "page", "section", "chunk",
          "claim", "entity" };
    public static readonly string[] RagDecisions =
        { "RAG_REQUIRED", "RAG_OPTIONAL", "RAG_NOT_REQUIRED",
          "RAG_DENIED" };

    private static string Dir(string toolRoot)
        => Path.Combine(toolRoot,
                        RelDir.Replace('/', Path.DirectorySeparatorChar));

    // ------------------------------------------- grounded-result/v2 --

    /// <summary>Validate a star-grounded-result/v2 claim record.
    /// Page/section/chunk/span make the citation resolvable; a record
    /// without page AND without (section or chunk) cannot be cited —
    /// fails closed (§4: never fabricate page numbers).</summary>
    public static Dictionary<string, object?> ValidateClaimV2(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError(
                "GROUNDING_UNSUPPORTED_CLAIM",
                "grounded record must be an object");
        string fmt = el.TryGetProperty("format", out var f)
            ? f.GetString() ?? "" : "";
        if (fmt != GroundedFormatV2)
            throw new ExecutorError(
                "GROUNDING_UNSUPPORTED_CLAIM",
                $"expected format {GroundedFormatV2}");
        var required = new[]
            { "claim_id", "source_id", "resource_id", "document_id",
              "document_revision", "chunk_id", "span_begin", "span_end",
              "retrieval_score", "rerank_score", "evidence_strength",
              "citation_id" };
        foreach (var k in required)
            if (!el.TryGetProperty(k, out _))
                throw new ExecutorError(
                    "GROUNDING_UNSUPPORTED_CLAIM",
                    $"missing field {k}");
        string strength =
            el.GetProperty("evidence_strength").GetString() ?? "";
        if (!EvidenceStrengths.Contains(strength))
            throw new ExecutorError(
                "GROUNDING_UNSUPPORTED_CLAIM",
                $"bad evidence_strength {strength}");
        // Citation resolvability: page, else section, else chunk.
        bool hasPage = el.TryGetProperty("page", out var pg) &&
                       pg.ValueKind == JsonValueKind.Number;
        bool hasSection =
            el.TryGetProperty("section", out var sc) &&
            sc.ValueKind == JsonValueKind.String &&
            (sc.GetString() ?? "").Length > 0;
        string citation;
        if (hasPage)
        {
            string doc = el.GetProperty("document_id").GetString() ?? "";
            citation = $"{doc} p.{pg.GetInt64()}";
        }
        else if (hasSection)
        {
            citation = $"{el.GetProperty("document_id").GetString()} " +
                       $"sec.{sc.GetString()} " +
                       $"chunk.{el.GetProperty("chunk_id").GetString()}";
        }
        else
        {
            citation = $"{el.GetProperty("resource_id").GetString()} " +
                       $"chunk.{el.GetProperty("chunk_id").GetString()}";
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = GroundedFormatV2,
            ["claim_id"] = el.GetProperty("claim_id").GetString(),
            ["evidence_strength"] = strength,
            ["citation"] = citation,
            ["citation_resolvable"] = true,
        };
    }

    // ----------------------------------------- evidence graph (§3) --

    public static Dictionary<string, object?> GraphAddNode(
        string toolRoot, string graphId, JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object ||
            !el.TryGetProperty("node_id", out _) ||
            !el.TryGetProperty("node_type", out var nt))
            throw new ExecutorError("EVIDENCE_GRAPH_INVALID",
                "node needs node_id + node_type");
        string type = nt.GetString() ?? "";
        if (!NodeTypes.Contains(type))
            throw new ExecutorError("EVIDENCE_GRAPH_INVALID",
                $"bad node_type {type}");
        return GraphMutate(toolRoot, graphId, g =>
        {
            var nodes = (List<object?>)g["nodes"]!;
            string id = el.GetProperty("node_id").GetString() ?? "";
            nodes.RemoveAll(n => n is Dictionary<string, object?> d &&
                (string)d["node_id"]! == id);
            nodes.Add(new Dictionary<string, object?>
            {
                ["node_id"] = id, ["node_type"] = type,
                ["label"] = el.TryGetProperty("label", out var lb)
                    ? lb.GetString() : null,
                ["attrs"] = el.TryGetProperty("attrs", out var at)
                    ? ModelLifecycle.Decode(at) : null,
            });
        });
    }

    public static Dictionary<string, object?> GraphAddEdge(
        string toolRoot, string graphId, JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object ||
            !el.TryGetProperty("from", out _) ||
            !el.TryGetProperty("to", out _) ||
            !el.TryGetProperty("edge_type", out var et))
            throw new ExecutorError("EVIDENCE_GRAPH_INVALID",
                "edge needs from/to/edge_type");
        string type = et.GetString() ?? "";
        if (!EdgeTypes.Contains(type))
            throw new ExecutorError("EVIDENCE_GRAPH_INVALID",
                $"bad edge_type {type}");
        return GraphMutate(toolRoot, graphId, g =>
        {
            ((List<object?>)g["edges"]!).Add(new Dictionary<string, object?>
            {
                ["from"] = el.GetProperty("from").GetString(),
                ["to"] = el.GetProperty("to").GetString(),
                ["edge_type"] = type,
            });
        });
    }

    /// <summary>Cross-document answers: which documents conflict /
    /// support / supersede around a claim or entity (§3/§29).</summary>
    public static Dictionary<string, object?> GraphQuery(
        string toolRoot, string graphId, string subject)
    {
        var g = LoadGraph(toolRoot, graphId);
        var edges = (List<object?>)g["edges"]!;
        var nodes = (List<object?>)g["nodes"]!;
        var conflicting = new List<object?>();
        var supporting = new List<object?>();
        var superseded = new List<object?>();
        var current = new List<object?>();
        // SUPERSEDES: newer wins; the target is CURRENT, source is
        // SUPERSEDED (§29).
        var supersededIds = new HashSet<string>();
        foreach (var e in edges.OfType<Dictionary<string, object?>>())
            if ((string)e["edge_type"]! == "SUPERSEDES")
                supersededIds.Add((string)e["from"]!);
        foreach (var e in edges.OfType<Dictionary<string, object?>>())
        {
            string from = (string)e["from"]!, to = (string)e["to"]!;
            if (from != subject && to != subject) continue;
            string other = from == subject ? to : from;
            switch ((string)e["edge_type"]!)
            {
                case "CONTRADICTS":
                    conflicting.Add(other); break;
                case "SUPPORTS":
                    supporting.Add(other); break;
                case "SUPERSEDES":
                    if (from == subject) superseded.Add(to);
                    else current.Add(from);
                    break;
            }
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = GraphFormat,
            ["graph_id"] = graphId, ["subject"] = subject,
            ["conflicting"] = conflicting,
            ["supporting"] = supporting,
            ["superseded_by"] = superseded,
            ["supersedes"] = current,
            ["is_superseded"] = supersededIds.Contains(subject),
            ["node_count"] = nodes.Count,
            ["edge_count"] = edges.Count,
        };
    }

    private static Dictionary<string, object?> GraphMutate(
        string toolRoot, string graphId,
        Action<Dictionary<string, object?>> mutate)
    {
        var g = LoadGraph(toolRoot, graphId);
        mutate(g);
        Directory.CreateDirectory(Dir(toolRoot));
        ModelLifecycle.AtomicWrite(
            Path.Combine(Dir(toolRoot), graphId + ".json"),
            CanonicalJson.PrettyDict(g) + "\n");
        g["ok"] = true;
        return g;
    }

    private static Dictionary<string, object?> LoadGraph(
        string toolRoot, string graphId)
    {
        string path = Path.Combine(Dir(toolRoot), graphId + ".json");
        if (File.Exists(path))
            return (Dictionary<string, object?>)ModelLifecycle.Decode(
                JsonDocument.Parse(File.ReadAllText(path)).RootElement)!;
        return new Dictionary<string, object?>
        {
            ["format"] = GraphFormat, ["graph_id"] = graphId,
            ["nodes"] = new List<object?>(),
            ["edges"] = new List<object?>(),
        };
    }

    // ------------------------------------------------ grounding gate --
    // §5: query -> retrieval -> rerank -> evidence graph -> generation
    // -> claim extraction -> claim/evidence alignment -> citation
    // resolver -> unsupported detection -> final response. A critical
    // UNSUPPORTED claim is never presented as fact.

    /// <summary>Align extracted claims against evidence strength.
    /// Input: {"claims":[{claim_id, critical?, evidence_strength,
    /// text?}]}. Output: verdict + per-claim disposition
    //  (keep / downgrade / remove / EVIDENCE_INSUFFICIENT).</summary>
    public static Dictionary<string, object?> Gate(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object ||
            !el.TryGetProperty("claims", out var cl) ||
            cl.ValueKind != JsonValueKind.Array)
            throw new ExecutorError("GROUNDING_UNAVAILABLE",
                "gate input needs claims[]");
        var dispositions = new List<object?>();
        int criticalUnsupported = 0, unsupported = 0;
        foreach (var c in cl.EnumerateArray())
        {
            string id = c.TryGetProperty("claim_id", out var ci)
                ? ci.GetString() ?? "" : "";
            string strength =
                c.TryGetProperty("evidence_strength", out var es)
                    ? es.GetString() ?? "UNSUPPORTED" : "UNSUPPORTED";
            bool critical =
                c.TryGetProperty("critical", out var cr) &&
                cr.ValueKind == JsonValueKind.True;
            string action;
            switch (strength)
            {
                case "DIRECT":
                case "STRONG":
                    action = "keep"; break;
                case "PARTIAL":
                    action = "downgrade"; break;   // hedge the claim
                case "CONFLICTING":
                    action = "present_conflict"; break; // §29
                default:
                    action = critical ? "evidence_insufficient"
                                      : "remove";
                    ++unsupported;
                    if (critical) ++criticalUnsupported;
                    break;
            }
            dispositions.Add(new Dictionary<string, object?>
            {
                ["claim_id"] = id, ["evidence_strength"] = strength,
                ["disposition"] = action,
            });
        }
        string verdict = criticalUnsupported > 0
            ? "EVIDENCE_INSUFFICIENT"
            : unsupported > 0 ? "PASS_WITH_REMOVALS" : "GROUNDED";
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-grounding-gate/v1",
            ["verdict"] = verdict,
            ["claims"] = dispositions,
            ["unsupported_claim_rate"] = dispositions.Count > 0
                ? (double)unsupported / dispositions.Count : 0.0,
        };
    }

    // ------------------------------------- retrieval decision (§7) --

    /// <summary>RAG necessity gate — declared intent + available local
    /// corpus decide; never "RAG for everything". Composes with
    /// ToolDecisionGate into ActionDecisionGate.</summary>
    public static Dictionary<string, object?> Decide(JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("RETRIEVAL_DECISION_INVALID",
                "decision input must be object");
        string intent =
            el.TryGetProperty("intent", out var i)
                ? i.GetString() ?? "" : "";
        bool hasLocalCorpus =
            el.TryGetProperty("has_local_corpus", out var hc) &&
            hc.ValueKind == JsonValueKind.True;
        bool userRequestedDocs =
            el.TryGetProperty("user_cites_documents", out var ud) &&
            ud.ValueKind == JsonValueKind.True;
        string mode =
            el.TryGetProperty("interaction_mode", out var im)
                ? im.GetString() ?? "" : "";
        string decision;
        string reason;
        if (!hasLocalCorpus && intent == "document_qa")
        {
            decision = "RAG_DENIED";
            reason = "no local corpus";
        }
        else if (intent == "document_qa" || userRequestedDocs ||
                 intent == "financial_document" ||
                 intent == "technical_spec" || intent == "research")
        {
            decision = "RAG_REQUIRED";
            reason = "document-grounded intent";
        }
        else if (intent == "arithmetic" || intent == "chitchat" ||
                 intent == "creative_fiction" ||
                 mode == "ROLEPLAY" || mode == "CREATIVE")
        {
            // §27: fictional/roleplay defaults RAG_NOT_REQUIRED unless
            // the user anchors to documents (handled above).
            decision = "RAG_NOT_REQUIRED";
            reason = "self-contained intent";
        }
        else
        {
            decision = "RAG_OPTIONAL";
            reason = "may benefit from retrieval";
        }
        return new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["format"] = "star-action-decision/v1",
            ["rag_decision"] = decision,
            ["reason"] = reason,
        };
    }

    // --------------------------------------------- citation metrics --

    /// <summary>Score a citation report: {"claims":[{claim_id,
    /// cited_source, cited_revision, span_begin, span_end}],
    /// "truth":[{claim_id, true_source, true_revision, span_begin,
    /// span_end, supported}]}. §6: precision/recall/coverage/
    /// unsupported/wrong-source/wrong-revision/span-accuracy/conflict
    /// detection — not citation_present.</summary>
    public static Dictionary<string, object?> CitationMetrics(
        JsonElement el)
    {
        if (el.ValueKind != JsonValueKind.Object)
            throw new ExecutorError("CITATION_METRICS_INVALID",
                "metrics input must be object");
        var claims = el.TryGetProperty("claims", out var c) &&
                     c.ValueKind == JsonValueKind.Array
            ? c.EnumerateArray().ToList() : new List<JsonElement>();
        var truth = el.TryGetProperty("truth", out var t) &&
                    t.ValueKind == JsonValueKind.Array
            ? t.EnumerateArray().ToList() : new List<JsonElement>();
        var truthByClaim = new Dictionary<string, JsonElement>();
        foreach (var tr in truth)
            if (tr.TryGetProperty("claim_id", out var id))
                truthByClaim[id.GetString() ?? ""] = tr;

        int cited = 0, correct = 0, wrongSource = 0, wrongRev = 0;
        int unsupported = 0, covered = 0, spanHit = 0, conflicts = 0;
        foreach (var cm in claims)
        {
            string cid = cm.TryGetProperty("claim_id", out var i)
                ? i.GetString() ?? "" : "";
            bool hasCitation =
                cm.TryGetProperty("cited_source", out var cs) &&
                cs.ValueKind == JsonValueKind.String &&
                (cs.GetString() ?? "").Length > 0;
            if (!truthByClaim.TryGetValue(cid, out var tr)) continue;
            bool supported =
                tr.TryGetProperty("supported", out var sp) &&
                sp.ValueKind == JsonValueKind.True;
            if (!supported) { ++unsupported; continue; }
            ++covered;
            if (!hasCitation) continue;
            ++cited;
            string trueSrc = tr.TryGetProperty("true_source", out var ts)
                ? ts.GetString() ?? "" : "";
            string trueRev = tr.TryGetProperty("true_revision", out var trv)
                ? trv.GetString() ?? "" : "";
            string citedSrc = cm.GetProperty("cited_source").GetString() ?? "";
            string citedRev =
                cm.TryGetProperty("cited_revision", out var cr)
                    ? cr.GetString() ?? "" : "";
            if (citedSrc != trueSrc) { ++wrongSource; continue; }
            if (trueRev.Length > 0 && citedRev != trueRev)
            { ++wrongRev; continue; }
            ++correct;
            if (cm.TryGetProperty("span_begin", out var b1) &&
                tr.TryGetProperty("span_begin", out var b2) &&
                cm.TryGetProperty("span_end", out var e1) &&
                tr.TryGetProperty("span_end", out var e2) &&
                b1.GetInt64() == b2.GetInt64() &&
                e1.GetInt64() == e2.GetInt64())
                ++spanHit;
        }
        if (el.TryGetProperty("conflicts_detected", out var cd) &&
            cd.ValueKind == JsonValueKind.Number)
            conflicts = cd.GetInt32();
        double Rate(int n, int d) => d > 0 ? (double)n / d : 0.0;
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["format"] = CitationMetricsFormat,
            ["citation_precision"] = Rate(correct, cited),
            ["citation_recall"] = Rate(cited, covered),
            ["claim_coverage"] =
                Rate(covered, covered + unsupported),
            ["unsupported_claim_rate"] =
                Rate(unsupported, covered + unsupported),
            ["wrong_source_rate"] = Rate(wrongSource, cited),
            ["wrong_revision_rate"] = Rate(wrongRev, cited),
            ["citation_span_accuracy"] = Rate(spanHit, correct),
            ["cross_document_conflict_detection"] = conflicts,
            ["claims"] = claims.Count, ["cited"] = cited,
        };
    }
}
