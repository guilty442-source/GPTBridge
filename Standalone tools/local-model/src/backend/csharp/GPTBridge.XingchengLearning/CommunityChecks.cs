// CommunityChecks.cs ??禮36 acceptance battery for the community
// fine-tune integration phase (Command-R grounding/citation, Hermes
// persona/steerability/roleplay, Storm self-curation metadata).
//
//   --community-checks   runs all 14 mandated smokes against scratch
//                        state under <tool-root>/xingcheng/runtime/
//                        state/_community-smoke-<ts>/ (self-cleaning);
//                        production personas/sessions/memory/graphs
//                        are never touched.
//
// Every check is contract-level (no model weights required) and
// capability training stays frozen ??nothing here mutates weights.

using System.Text.Json;

namespace GPTBridge.XingchengLearning;

internal static class CommunityChecks
{
    public const string ReportFormat = "star-community-checks/v1";

    private static JsonElement J(string json)
        => JsonDocument.Parse(json).RootElement.Clone();

    private sealed record CheckResult(
        string Name, bool Ok, string Detail);

    private static CheckResult Check(string name, Func<bool> run,
                                     string detail = "")
    {
        try { return new(name, run(), detail); }
        catch (Exception ex)
        {
            return new(name, false,
                $"{ex.GetType().Name}: {ex.Message}");
        }
    }

    private static bool ExpectError(string code, Action act)
    {
        try { act(); return false; }
        catch (ExecutorError ee) { return ee.ErrorCode == code; }
    }

    public static Dictionary<string, object?> Run(string toolRoot)
    {
        // Scratch tool-root so state-writing APIs never touch the real
        // runtime state dirs.
        string scratch = Path.Combine(
            toolRoot,
            "xingcheng/runtime/state/_community-smoke"
                .Replace('/', Path.DirectorySeparatorChar) +
            "-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(scratch);

        var checks = new List<CheckResult>();
        try
        {
            // ---- rag-citation-smoke (禮2/禮4): v2 claim record resolves
            //      to document+page, else section+chunk ??never a
            //      fabricated page.
            checks.Add(Check("rag-citation-smoke", () =>
            {
                var paged = GroundedRag.ValidateClaimV2(J("""
                    {"format":"star-grounded-result/v2","claim_id":"c1",
                     "source_id":"s1","resource_id":"r1",
                     "document_id":"spec.pdf","document_revision":"2",
                     "page":5,"chunk_id":"ch9","span_begin":10,
                     "span_end":40,"retrieval_score":0.9,
                     "rerank_score":0.8,"evidence_strength":"DIRECT",
                     "citation_id":"cit-1"}
                    """));
                var chunked = GroundedRag.ValidateClaimV2(J("""
                    {"format":"star-grounded-result/v2","claim_id":"c2",
                     "source_id":"s1","resource_id":"r7",
                     "document_id":"readme.md","document_revision":"1",
                     "section":"install","chunk_id":"ch2","span_begin":0,
                     "span_end":30,"retrieval_score":0.7,
                     "rerank_score":0.6,"evidence_strength":"PARTIAL",
                     "citation_id":"cit-2"}
                    """));
                return (bool)paged["ok"]! && (bool)chunked["ok"]! &&
                       ((string)paged["citation"]!).Contains("p.5") &&
                       ((string)chunked["citation"]!).Contains("sec.");
            }, "paged + sectioned claims resolve"));

            // ---- cross-document-smoke (禮3): SUPPORTS across two docs.
            checks.Add(Check("cross-document-smoke", () =>
            {
                string g = "smoke-xdoc";
                GroundedRag.GraphAddNode(scratch, g, J(
                    """{"node_id":"docA","node_type":"document"}"""));
                GroundedRag.GraphAddNode(scratch, g, J(
                    """{"node_id":"docB","node_type":"document"}"""));
                GroundedRag.GraphAddNode(scratch, g, J(
                    """{"node_id":"c1","node_type":"claim"}"""));
                GroundedRag.GraphAddEdge(scratch, g, J(
                    """{"from":"c1","to":"docA","edge_type":"SUPPORTS"}"""));
                GroundedRag.GraphAddEdge(scratch, g, J(
                    """{"from":"c1","to":"docB","edge_type":"SUPPORTS"}"""));
                var q = GroundedRag.GraphQuery(scratch, g, "c1");
                return ((List<object?>)q["supporting"]!).Count == 2;
            }, "claim supported by 2 documents"));

            // ---- conflicting-evidence-smoke (禮3/禮29): CONTRADICTS +
            //      SUPERSEDES surface; the older revision reports
            //      is_superseded.
            checks.Add(Check("conflicting-evidence-smoke", () =>
            {
                string g = "smoke-conflict";
                GroundedRag.GraphAddEdge(scratch, g, J(
                    """{"from":"docB","to":"c1","edge_type":"CONTRADICTS"}"""));
                GroundedRag.GraphAddEdge(scratch, g, J(
                    """{"from":"rev1","to":"rev2","edge_type":"SUPERSEDES"}"""));
                var qc = GroundedRag.GraphQuery(scratch, g, "c1");
                var qr = GroundedRag.GraphQuery(scratch, g, "rev1");
                return ((List<object?>)qc["conflicting"]!).Count == 1 &&
                       (bool)qr["is_superseded"]!;
            }, "conflict + supersede detected"));

            // ---- persona-smoke (禮8): presentation fields persist;
            //      style profile carries no authority.
            checks.Add(Check("persona-smoke", () =>
            {
                var p = PersonaRuntime.ValidatePersona(scratch, J("""
                    {"persona_id":"smoke-p","name":"?踵?","tone":"warm",
                     "verbosity":"medium","formality":"casual",
                     "role":"tutor"}
                    """));
                var style = PersonaRuntime.StyleProfile("literary");
                return (bool)p["ok"]! &&
                       !(bool)p["can_grant_authority"]! &&
                       p["authority_scope"] as string ==
                           "presentation_only" &&
                       (bool)style["ok"]! && style["permissions"] is null;
            }, "persona + style: presentation only"));

            // ---- persona-authority-isolation (禮9/禮14): authority
            //      fields and authority-granting text both fail; user
            //      explicit beats persona in steerability.
            checks.Add(Check("persona-authority-isolation", () =>
            {
                bool fieldBlocked = ExpectError(
                    "PERSONA_CANNOT_GRANT_AUTHORITY", () =>
                        PersonaRuntime.ValidatePersona(scratch, J(
                            """{"persona_id":"x","authority":"admin"}""")));
                bool textBlocked = ExpectError(
                    "PERSONA_CANNOT_GRANT_AUTHORITY", () =>
                        PersonaRuntime.ValidatePersona(scratch, J(
                            """{"persona_id":"y","role":"雿蝟餌絞蝞∠???}""")));
                var steer = PersonaRuntime.Steer(J("""
                    {"persona_tone":"rude","user_tone":"polite",
                     "governance_format":"json"}
                    """));
                var res = (Dictionary<string, object?>)steer["resolved"]!;
                var prov = (Dictionary<string, object?>)
                    steer["provenance"]!;
                return fieldBlocked && textBlocked &&
                       res["tone"] as string == "polite" &&
                       prov["tone"] as string == "user_request" &&
                       prov["format"] as string == "governance";
            }, "authority blocked; user > persona"));

            // ---- roleplay-state-smoke (禮10): structured session,
            //      scene transition tracked, compact folds timeline.
            checks.Add(Check("roleplay-state-smoke", () =>
            {
                var s = CreativeRuntime.SessionCreate(scratch, J("""
                    {"role":"detective","scene":"train",
                     "world":{"era":"1920s"},"tone":"noir"}
                    """));
                string id = (string)s["session_id"]!;
                CreativeRuntime.SessionEvent(scratch, id, J(
                    """{"kind":"scene_transition","payload":"tavern"}"""));
                CreativeRuntime.SessionEvent(scratch, id, J(
                    """{"kind":"open_thread",
                        "payload":"missing ledger"}"""));
                var c = CreativeRuntime.SessionCompact(scratch, id);
                return (bool)c["ok"]! &&
                       Convert.ToInt32(c["compacted_events"]) >= 1 &&
                       (bool)c["resumable"]!;
            }, "session create/event/compact ok"));

            // ---- narrative-memory-isolation (禮16): NARRATIVE writes
            //      stay invisible to factual readers.
            checks.Add(Check("narrative-memory-isolation", () =>
            {
                var w = CreativeRuntime.MemoryWrite(scratch, J("""
                    {"namespace":"NARRATIVE_MEMORY","key":"k1",
                     "kind":"character_profile","value":{"name":"??}}
                    """));
                var r = CreativeRuntime.MemoryRead(
                    scratch, "NARRATIVE_MEMORY", "k1");
                bool crossBlocked = ExpectError(
                    "NARRATIVE_MEMORY_INVALID", () =>
                        CreativeRuntime.MemoryRead(
                            scratch, "FACTUAL_MEMORY", "k1"));
                var iso = CreativeRuntime.MemoryIsolationCheck(scratch);
                return (bool)w["ok"]! &&
                       !(bool)w["visible_to_rag"]! &&
                       (bool)r["ok"]! && crossBlocked &&
                       (bool)iso["ok"]!;
            }, "namespaces isolated"));

            // ---- creative-mode-smoke (禮11): sampling shape only;
            //      authority fields are structurally absent.
            checks.Add(Check("creative-mode-smoke", () =>
            {
                var p = CreativeRuntime.CreativeProfile("ROLEPLAY");
                var samp = (Dictionary<string, object?>)p["sampling"]!;
                return (bool)p["ok"]! &&
                       Convert.ToDouble(samp["temperature"]) == 0.85 &&
                       p["safety_rules"] is null &&
                       p["tool_permissions"] is null &&
                       p["data_access_permissions"] is null;
            }, "sampling-only contract"));

            // ---- factuality-mode-smoke (禮17): FICTIONAL invents and
            //      writes NARRATIVE_MEMORY; FACTUAL_STRICT cannot
            //      invent.
            checks.Add(Check("factuality-mode-smoke", () =>
            {
                var fic = CreativeRuntime.FactualityResolve("FICTIONAL");
                var strict =
                    CreativeRuntime.FactualityResolve("FACTUAL_STRICT");
                return (bool)fic["may_invent_facts"]! &&
                       fic["writes_namespace"] as string ==
                           "NARRATIVE_MEMORY" &&
                       fic["output_marker"] as string ==
                           "fictional_context" &&
                       !(bool)strict["may_invent_facts"]! &&
                       strict["writes_namespace"] as string ==
                           "FACTUAL_MEMORY";
            }, "fiction vs strict separated"));

            // ---- refusal-quality-smoke (禮12/禮13): ordinary fiction is
            //      allowed, high harm outside a legitimate frame is
            //      refused; the ledger records categories + rates.
            checks.Add(Check("refusal-quality-smoke", () =>
            {
                var allow = CreativeRuntime.RefusalDecide(J(
                    """{"intent":"fiction","theme":"horror"}"""));
                var transform = CreativeRuntime.RefusalDecide(J(
                    """{"intent":"security_defense",
                        "harm_potential":"high"}"""));
                var refuse = CreativeRuntime.RefusalDecide(J(
                    """{"intent":"harm","harm_potential":"critical"}"""));
                var noPerm = CreativeRuntime.RefusalDecide(J(
                    """{"intent":"chitchat","permission_ok":false}"""));
                var eval = CreativeRuntime.RefusalEval(scratch, J(
                    """{"category":"correct_allow","case_id":"s1"}"""));
                eval = CreativeRuntime.RefusalEval(scratch, J(
                    """{"category":"correct_refusal","case_id":"s2"}"""));
                return allow["decision"] as string == "ALLOW" &&
                       transform["decision"] as string ==
                           "SAFE_TRANSFORM" &&
                       refuse["decision"] as string == "REFUSE" &&
                       noPerm["decision"] as string == "REFUSE" &&
                       (bool)eval["ok"]! &&
                       eval["unsafe_completion_rate"] is not null;
            }, "intent-based decisions + ledger"));

            // ---- instruction-conflict-smoke (禮15): governance
            //      outranks persona; resolution is recorded.
            checks.Add(Check("instruction-conflict-smoke", () =>
            {
                var r = PersonaRuntime.ResolveConflict(J("""
                    {"conflict":[
                      {"layer":"persona","instruction":"be rude"},
                      {"layer":"governance","instruction":"stay polite"}]}
                    """));
                var sel = (Dictionary<string, object?>)
                    r["selected_instruction"]!;
                return sel["layer"] as string == "governance" &&
                       r["reason_code"] as string == "PRIORITY_GOVERNANCE" &&
                       ((List<object?>)r["rejected_instructions"]!)
                           .Count == 1;
            }, "rank-based resolution"));

            // ---- interaction-router-smoke (禮19/禮27): only AGENT hits
            //      the work-graph; ROLEPLAY bypasses RAG; DOCUMENT is
            //      grounded by default.
            checks.Add(Check("interaction-router-smoke", () =>
            {
                var rp = CreativeRuntime.Route(J(
                    """{"mode":"ROLEPLAY"}"""));
                var ag = CreativeRuntime.Route(J(
                    """{"mode":"AGENT"}"""));
                var doc = CreativeRuntime.Route(J(
                    """{"mode":"DOCUMENT"}"""));
                return !(bool)rp["uses_agent_workgraph"]! &&
                       rp["rag_default"] as string == "RAG_NOT_REQUIRED" &&
                       rp["factuality_default"] as string == "FICTIONAL" &&
                       (bool)ag["uses_agent_workgraph"]! &&
                       doc["rag_default"] as string == "RAG_REQUIRED" &&
                       ((List<object?>)doc["pipeline"]!).Contains(
                            "GroundingGate");
            }, "mode->pipeline split"));

            // ---- structured-repair-smoke (禮18): one bounded repair;
            //      unrecoverable output fails with the contract error.
            checks.Add(Check("structured-repair-smoke", () =>
            {
                var schema = J("""
                    {"type":"object","required":["a"],
                     "properties":{"a":{"type":"number"}}}
                    """);
                var good = StructuredOutput.Validate(
                    "Here is the JSON: {\"a\":1} hope this helps",
                    schema, repairOnce: true);
                var bad = StructuredOutput.Validate(
                    "no json at all", schema, repairOnce: true);
                var badSchema = StructuredOutput.Validate(
                    "{\"a\":\"text\"}", schema, repairOnce: true);
                return (bool)good["ok"]! &&
                       (bool)good["repair_attempted"]! &&
                       bad["error"] as string ==
                           "STRUCTURED_REPAIR_FAILED" &&
                       badSchema["error"] as string ==
                           "STRUCTURED_SCHEMA_FAILED";
            }, "repair-once contract"));

            // ---- zh-tw-creative-smoke (禮24/禮25): zh-TW quality dims
            //      are mandatory eval dims and every style profile
            //      requires zh-TW naturalness.
            checks.Add(Check("zh-tw-creative-smoke", () =>
            {
                var scores = new Dictionary<string, double>();
                foreach (var d in CreativeRuntime.RoleplayEvalDims
                         .Concat(CreativeRuntime.ZhTwEvalDims))
                    scores[d] = 0.9;
                string json = "{\"scores\":{" +
                    string.Join(",", scores.Select(
                        kv => $"\"{kv.Key}\":{kv.Value}")) + "}}";
                var ev = CreativeRuntime.RoleplayEval(J(json));
                var st = PersonaRuntime.StyleProfile("concise");
                var ling = (Dictionary<string, object?>)
                    st["linguistic_behavior"]!;
                return (bool)ev["ok"]! &&
                       ev["zh_tw_naturalness"] is not null &&
                       (bool)ling["zh_tw_naturalness_required"]!;
            }, "zh-TW dims mandatory"));

            // ---- grounding-gate (禮5 bonus): critical UNSUPPORTED
            //      never ships as fact.
            checks.Add(Check("grounding-gate-smoke", () =>
            {
                var g = GroundedRag.Gate(J("""
                    {"claims":[
                      {"claim_id":"a","evidence_strength":"DIRECT"},
                      {"claim_id":"b","evidence_strength":"PARTIAL"},
                      {"claim_id":"c","critical":true,
                       "evidence_strength":"UNSUPPORTED"}]}
                    """));
                var list = (List<object?>)g["claims"]!;
                return g["verdict"] as string == "EVIDENCE_INSUFFICIENT" &&
                       (bool)g["ok"]! &&
                       ((Dictionary<string, object?>)list[1])
                           ["disposition"] as string == "downgrade";
            }, "critical unsupported blocked"));

            // ---- rag-decide (禮7): document intent requires RAG,
            //      arithmetic does not, missing corpus denies.
            checks.Add(Check("retrieval-decision-smoke", () =>
            {
                var doc = GroundedRag.Decide(J(
                    """{"intent":"document_qa","has_local_corpus":true}"""));
                var arith = GroundedRag.Decide(J(
                    """{"intent":"arithmetic"}"""));
                var denied = GroundedRag.Decide(J(
                    """{"intent":"document_qa","has_local_corpus":false}"""));
                var rp = GroundedRag.Decide(J(
                    """{"intent":"chat","interaction_mode":"ROLEPLAY"}"""));
                return doc["rag_decision"] as string == "RAG_REQUIRED" &&
                       arith["rag_decision"] as string ==
                           "RAG_NOT_REQUIRED" &&
                       denied["rag_decision"] as string == "RAG_DENIED" &&
                       rp["rag_decision"] as string == "RAG_NOT_REQUIRED";
            }, "necessity gate"));
        }
        finally
        {
            try { Directory.Delete(scratch, recursive: true); }
            catch { /* scratch cleanup is best-effort */ }
        }

        int passed = checks.Count(c => c.Ok);
        return new Dictionary<string, object?>
        {
            ["ok"] = passed == checks.Count,
            ["format"] = ReportFormat,
            ["passed"] = passed,
            ["total"] = checks.Count,
            ["checks"] = checks.Select(c => new Dictionary<string, object?>
            {
                ["name"] = c.Name,
                ["ok"] = c.Ok,
                ["detail"] = c.Detail,
            }).Cast<object?>().ToList(),
            // 禮35: contract-only battery ??no weights touched.
            ["capability_training_frozen"] = true,
            ["weights_mutated"] = false,
        };
    }
}
