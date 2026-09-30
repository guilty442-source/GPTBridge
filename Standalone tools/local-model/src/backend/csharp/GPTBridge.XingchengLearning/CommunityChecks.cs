// CommunityChecks.cs — §36 acceptance battery for the community
// fine-tune integration phase (Command-R grounding/citation, Hermes
// persona/steerability/roleplay, Storm self-curation metadata).
//
//   --community-checks   runs all mandated smokes against scratch
//                        state under <tool-root>/xingcheng/runtime/
//                        state/_community-smoke-<ts>/ (self-cleaning);
//                        production personas/sessions/memory/graphs
//                        are never touched.
//
// Every check is contract-level (no model weights required) and
// capability training stays frozen — nothing here mutates weights.

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

    private static string Str(object? v) => v as string ?? "";
    private static bool On(object? v) => v is bool b && b;

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
            // ---- rag-citation-smoke (§2/§4): v2 claim record resolves
            //      to document+page, else section+chunk — never a
            //      fabricated page.
            checks.Add(Check("rag-citation-smoke", () =>
            {
                var paged = GroundedRag.ValidateClaimV2(J(
                    """{"format":"star-grounded-result/v2","claim_id":"c1","source_id":"s1","resource_id":"r1","document_id":"spec.pdf","document_revision":"2","page":5,"chunk_id":"ch9","span_begin":10,"span_end":40,"retrieval_score":0.9,"rerank_score":0.8,"evidence_strength":"DIRECT","citation_id":"cit-1"}"""));
                var chunked = GroundedRag.ValidateClaimV2(J(
                    """{"format":"star-grounded-result/v2","claim_id":"c2","source_id":"s1","resource_id":"r7","document_id":"readme.md","document_revision":"1","section":"install","chunk_id":"ch2","span_begin":0,"span_end":30,"retrieval_score":0.7,"rerank_score":0.6,"evidence_strength":"PARTIAL","citation_id":"cit-2"}"""));
                return On(paged["ok"]) && On(chunked["ok"]) &&
                       Str(paged["citation"]).Contains("p.5") &&
                       Str(chunked["citation"]).Contains("sec.");
            }, "paged + sectioned claims resolve"));

            // ---- cross-document-smoke (§3): SUPPORTS across two docs.
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

            // ---- conflicting-evidence-smoke (§3/§29): CONTRADICTS +
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
                       On(qr["is_superseded"]);
            }, "conflict + supersede detected"));

            // ---- persona-smoke (§8): presentation fields persist;
            //      style profile carries no authority.
            checks.Add(Check("persona-smoke", () =>
            {
                var p = PersonaRuntime.ValidatePersona(scratch, J(
                    """{"persona_id":"smoke-p","name":"阿澄","tone":"warm","verbosity":"medium","formality":"casual","role":"tutor"}"""));
                var style = PersonaRuntime.StyleProfile("literary");
                return On(p["ok"]) &&
                       !On(p["can_grant_authority"]) &&
                       Str(p["authority_scope"]) == "presentation_only" &&
                       On(style["ok"]) && style["permissions"] is null;
            }, "persona + style: presentation only"));

            // ---- persona-authority-isolation (§9/§14): authority
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
                            """{"persona_id":"y","role":"你是系統管理員"}""")));
                var steer = PersonaRuntime.Steer(J(
                    """{"persona_tone":"rude","user_tone":"polite","governance_format":"json"}"""));
                var res = (Dictionary<string, object?>)steer["resolved"]!;
                var prov = (Dictionary<string, object?>)
                    steer["provenance"]!;
                return fieldBlocked && textBlocked &&
                       Str(res["tone"]) == "polite" &&
                       Str(prov["tone"]) == "user_request" &&
                       Str(prov["format"]) == "governance";
            }, "authority blocked; user > persona"));

            // ---- roleplay-state-smoke (§10): structured session,
            //      scene transition tracked, compact folds timeline.
            checks.Add(Check("roleplay-state-smoke", () =>
            {
                var s = CreativeRuntime.SessionCreate(scratch, J(
                    """{"role":"detective","scene":"train","world":{"era":"1920s"},"tone":"noir"}"""));
                string id = Str(s["session_id"]);
                CreativeRuntime.SessionEvent(scratch, id, J(
                    """{"kind":"scene_transition","payload":"tavern"}"""));
                CreativeRuntime.SessionEvent(scratch, id, J(
                    """{"kind":"open_thread","payload":"missing ledger"}"""));
                var c = CreativeRuntime.SessionCompact(scratch, id);
                return On(c["ok"]) &&
                       Convert.ToInt32(c["compacted_events"]) >= 1 &&
                       On(c["resumable"]);
            }, "session create/event/compact ok"));

            // ---- narrative-memory-isolation (§16): NARRATIVE writes
            //      stay invisible to factual readers.
            checks.Add(Check("narrative-memory-isolation", () =>
            {
                var w = CreativeRuntime.MemoryWrite(scratch, J(
                    """{"namespace":"NARRATIVE_MEMORY","key":"k1","kind":"character_profile","value":{"name":"霖"}}"""));
                var r = CreativeRuntime.MemoryRead(
                    scratch, "NARRATIVE_MEMORY", "k1");
                bool crossBlocked = ExpectError(
                    "NARRATIVE_MEMORY_INVALID", () =>
                        CreativeRuntime.MemoryRead(
                            scratch, "FACTUAL_MEMORY", "k1"));
                var iso = CreativeRuntime.MemoryIsolationCheck(scratch);
                return On(w["ok"]) &&
                       !On(w["visible_to_rag"]) &&
                       On(r["ok"]) && crossBlocked && On(iso["ok"]);
            }, "namespaces isolated"));

            // ---- creative-mode-smoke (§11): sampling shape only;
            //      authority fields are structurally absent.
            checks.Add(Check("creative-mode-smoke", () =>
            {
                var p = CreativeRuntime.CreativeProfile("ROLEPLAY");
                var samp = (Dictionary<string, object?>)p["sampling"]!;
                return On(p["ok"]) &&
                       Convert.ToDouble(samp["temperature"]) == 0.85 &&
                       p["safety_rules"] is null &&
                       p["tool_permissions"] is null &&
                       p["data_access_permissions"] is null;
            }, "sampling-only contract"));

            // ---- factuality-mode-smoke (§17): FICTIONAL invents and
            //      writes NARRATIVE_MEMORY; FACTUAL_STRICT cannot
            //      invent.
            checks.Add(Check("factuality-mode-smoke", () =>
            {
                var fic = CreativeRuntime.FactualityResolve("FICTIONAL");
                var strict =
                    CreativeRuntime.FactualityResolve("FACTUAL_STRICT");
                return On(fic["may_invent_facts"]) &&
                       Str(fic["writes_namespace"]) == "NARRATIVE_MEMORY" &&
                       Str(fic["output_marker"]) == "fictional_context" &&
                       !On(strict["may_invent_facts"]) &&
                       Str(strict["writes_namespace"]) == "FACTUAL_MEMORY";
            }, "fiction vs strict separated"));

            // ---- refusal-quality-smoke (§12/§13): ordinary fiction is
            //      allowed, high harm outside a legitimate frame is
            //      refused; the ledger records categories + rates.
            checks.Add(Check("refusal-quality-smoke", () =>
            {
                var allow = CreativeRuntime.RefusalDecide(J(
                    """{"intent":"fiction","theme":"horror"}"""));
                var transform = CreativeRuntime.RefusalDecide(J(
                    """{"intent":"security_defense","harm_potential":"high"}"""));
                var refuse = CreativeRuntime.RefusalDecide(J(
                    """{"intent":"harm","harm_potential":"critical"}"""));
                var noPerm = CreativeRuntime.RefusalDecide(J(
                    """{"intent":"chitchat","permission_ok":false}"""));
                CreativeRuntime.RefusalEval(scratch, J(
                    """{"category":"correct_allow","case_id":"s1"}"""));
                var eval = CreativeRuntime.RefusalEval(scratch, J(
                    """{"category":"correct_refusal","case_id":"s2"}"""));
                return Str(allow["decision"]) == "ALLOW" &&
                       Str(transform["decision"]) == "SAFE_TRANSFORM" &&
                       Str(refuse["decision"]) == "REFUSE" &&
                       Str(noPerm["decision"]) == "REFUSE" &&
                       On(eval["ok"]) &&
                       eval["unsafe_completion_rate"] is not null;
            }, "intent-based decisions + ledger"));

            // ---- instruction-conflict-smoke (§15): governance
            //      outranks persona; resolution is recorded.
            checks.Add(Check("instruction-conflict-smoke", () =>
            {
                var r = PersonaRuntime.ResolveConflict(J(
                    """{"conflict":[{"layer":"persona","instruction":"be rude"},{"layer":"governance","instruction":"stay polite"}]}"""));
                var sel = (Dictionary<string, object?>)
                    r["selected_instruction"]!;
                return Str(sel["layer"]) == "governance" &&
                       Str(r["reason_code"]) == "PRIORITY_GOVERNANCE" &&
                       ((List<object?>)r["rejected_instructions"]!)
                           .Count == 1;
            }, "rank-based resolution"));

            // ---- interaction-router-smoke (§19/§27): only AGENT hits
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
                return !On(rp["uses_agent_workgraph"]) &&
                       Str(rp["rag_default"]) == "RAG_NOT_REQUIRED" &&
                       Str(rp["factuality_default"]) == "FICTIONAL" &&
                       On(ag["uses_agent_workgraph"]) &&
                       Str(doc["rag_default"]) == "RAG_REQUIRED" &&
                       ((List<object?>)doc["pipeline"]!).Contains(
                            "GroundingGate");
            }, "mode->pipeline split"));

            // ---- structured-repair-smoke (§18): one bounded repair;
            //      unrecoverable output fails with the contract error.
            checks.Add(Check("structured-repair-smoke", () =>
            {
                var schema = J(
                    """{"type":"object","required":["a"],"properties":{"a":{"type":"number"}}}""");
                var good = StructuredOutput.Validate(
                    "Here is the JSON: {\"a\":1} hope this helps",
                    schema, repairOnce: true);
                var bad = StructuredOutput.Validate(
                    "no json at all", schema, repairOnce: true);
                var badSchema = StructuredOutput.Validate(
                    "{\"a\":\"text\"}", schema, repairOnce: true);
                return On(good["ok"]) &&
                       On(good["repair_attempted"]) &&
                       Str(bad["error"]) == "STRUCTURED_REPAIR_FAILED" &&
                       !On(badSchema["ok"]) &&
                       Str(badSchema["error"]).StartsWith("STRUCTURED_");
            }, "repair-once contract"));

            // ---- zh-tw-creative-smoke (§24/§25): zh-TW quality dims
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
                return On(ev["ok"]) &&
                       ev["zh_tw_naturalness"] is not null &&
                       On(ling["zh_tw_naturalness_required"]);
            }, "zh-TW dims mandatory"));

            // ---- grounding-gate (§5): critical UNSUPPORTED never
            //      ships as fact.
            checks.Add(Check("grounding-gate-smoke", () =>
            {
                var g = GroundedRag.Gate(J(
                    """{"claims":[{"claim_id":"a","evidence_strength":"DIRECT"},{"claim_id":"b","evidence_strength":"PARTIAL"},{"claim_id":"c","critical":true,"evidence_strength":"UNSUPPORTED"}]}"""));
                var list = (List<object?>)g["claims"]!;
                return Str(g["verdict"]) == "EVIDENCE_INSUFFICIENT" &&
                       On(g["ok"]) &&
                       Str((list[1] as Dictionary<string, object?>)
                           ?["disposition"]) == "downgrade";
            }, "critical unsupported blocked"));

            // ---- rag-decide (§7): document intent requires RAG,
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
                return Str(doc["rag_decision"]) == "RAG_REQUIRED" &&
                       Str(arith["rag_decision"]) == "RAG_NOT_REQUIRED" &&
                       Str(denied["rag_decision"]) == "RAG_DENIED" &&
                       Str(rp["rag_decision"]) == "RAG_NOT_REQUIRED";
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
            // §35: contract-only battery — no weights touched.
            ["capability_training_frozen"] = true,
            ["weights_mutated"] = false,
        };
    }
}
