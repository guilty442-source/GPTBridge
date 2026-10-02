// Program.fs — governed evaluation verdict owner (star-fsharp-eval-verdict/v1).
//
// Codex B139 / B132 / B141: F# owns Xingcheng training evaluation and
// correctness analysis. The native lane (xc_modeltool, C++23) measures —
// perplexity, tokens/sec, per-category pass rates; this tool owns the gate
// verdict. The C# lane (xc-learning) orchestrates and records evidence but
// never decides pass/fail itself.
//
//   xc-eval gate --input <json>
//       input:  {format, quality_gates, candidate, baseline,
//                engine_comparison}
//       stdout: {ok:true, format:"star-fsharp-eval-verdict/v1",
//                passed, comparison}
//       exit 0 = pass, 2 = gate failed, 1 = input/tool error
//
// Gate semantics mirror the native engine's comparison contract exactly so
// the authoritative verdict is a re-derivation, not a reinterpretation:
//   star-native-eval-suite/v1: perplexity regression %, generation
//   requirement, absolute + baseline-ratio tokens/sec gates.
//   star-capability-suite/v1: per-category pass_rate must not drop below
//   the baseline report (any drop fails the candidate); a run without a
//   baseline cannot prove non-regression and fails closed.
//
// LANGUAGE-ARCHITECTURE FREEZE (2026-10-02, human-governor directive):
// F# holds no new Production responsibility in 星澄. This lane is frozen:
// bugfix-only, no new features. Eval-verdict ownership migrates to the C#
// governance lane (EvalVerdict.cs, parity-tracked); the verdict_owner flip
// awaits the governed codex amendment. B166 remains the sole language
// authority; this notice is owner-local implementation guidance, not law.

module GPTBridge.XingchengEval.Program

open System
open System.IO
open System.Text.Json
open System.Text.Json.Nodes

let private eps = 1e-9

let private tryNode (o: JsonObject) (k: string) : JsonNode option =
    match o[k] with
    | null -> None
    | v -> Some v

let private tryNum (o: JsonObject) (k: string) : float option =
    match tryNode o k with
    | Some (:? JsonValue as v) ->
        match v.TryGetValue<float>() with
        | true, d when Double.IsFinite d -> Some d
        | _ -> None
    | _ -> None

let private tryBool (o: JsonObject) (k: string) : bool option =
    match tryNode o k with
    | Some (:? JsonValue as v) ->
        match v.TryGetValue<bool>() with
        | true, b -> Some b
        | _ -> None
    | _ -> None

let private tryStr (o: JsonObject) (k: string) : string option =
    match tryNode o k with
    | Some (:? JsonValue as v) ->
        match v.TryGetValue<string>() with
        | true, s -> Some s
        | _ -> None
    | _ -> None

let private tryObj (o: JsonObject) (k: string) : JsonObject option =
    match tryNode o k with
    | Some (:? JsonObject as c) -> Some c
    | _ -> None

let private numOr (o: JsonObject) (k: string) (dflt: float) =
    tryNum o k |> Option.defaultValue dflt

// Mirror of the native compare_metrics gate (xc_modeltool eval).
let private evalNative (gates: JsonObject) (cand: JsonObject)
                       (baseline: JsonObject) : JsonObject * bool =
    let maxRegr = numOr gates "max_perplexity_regression_pct" 5.0
    let requireGen =
        tryBool gates "require_generation" |> Option.defaultValue false
    let minTps = numOr gates "min_tokens_per_second" 0.0
    let tpsRatio = numOr gates "min_tps_baseline_ratio" 0.0

    let basePpl = tryNum baseline "perplexity"
    let candPpl = tryNum cand "perplexity"
    let mutable deltaOk = false
    let mutable pplDelta = 0.0
    let mutable pplOk = true
    match basePpl, candPpl with
    | Some b, Some c when b > 0.0 ->
        pplDelta <- (c - b) / b * 100.0
        pplOk <- pplDelta <= maxRegr
        deltaOk <- true
    | _ -> ()

    let genOk =
        if requireGen then tryBool cand "generation_ok" = Some true
        else true

    let candTps = tryNum cand "tokens_per_second" |> Option.defaultValue 0.0
    let baseTps = tryNum baseline "tokens_per_second"
    let mutable tpsOk = minTps <= 0.0 || candTps >= minTps
    let tpsRatioOk =
        if tpsRatio > 0.0 then
            match baseTps with
            | Some b when b > 0.0 -> candTps >= b * tpsRatio
            | _ -> false
        else true
    tpsOk <- tpsOk && tpsRatioOk

    let passed = pplOk && genOk && tpsOk
    let cmp = JsonObject()
    cmp["perplexity_delta_pct"] <-
        if deltaOk then JsonValue.Create(pplDelta) else null
    cmp["perplexity_ok"] <- pplOk
    cmp["generation_ok"] <- genOk
    cmp["tokens_per_second_ok"] <- tpsOk
    cmp["tokens_per_second_ratio_ok"] <- tpsRatioOk
    cmp["baseline_tokens_per_second"] <-
        match baseTps with
        | Some b -> JsonValue.Create(b)
        | None -> null
    (cmp, passed)

// Mirror of the capability --baseline-report regression gate.
let private evalCapability (cand: JsonObject) (baseline: JsonObject)
                           : JsonObject * bool =
    let regs = JsonArray()
    match tryObj baseline "categories", tryObj cand "categories" with
    | Some bcats, Some ccats ->
        for kv in bcats do
            let crate =
                match ccats[kv.Key] with
                | :? JsonObject as cinfo -> tryNum cinfo "pass_rate"
                | _ -> None
            let brate =
                match kv.Value with
                | :? JsonObject as binfo -> tryNum binfo "pass_rate"
                | _ -> None
            match brate, crate with
            | Some b, Some c when c < b - eps ->
                let r = JsonObject()
                r["category"] <- kv.Key
                r["baseline"] <- b
                r["candidate"] <- c
                regs.Add r
            | _ -> ()
    | _ -> ()

    let noRegression = regs.Count = 0
    let cmp = JsonObject()
    cmp["passed"] <- noRegression
    cmp["regressions"] <- regs
    cmp["baseline_suite"] <-
        tryStr baseline "suite_id" |> Option.defaultValue ""
    cmp["candidate_suite"] <-
        tryStr cand "suite_id" |> Option.defaultValue ""
    // Fail-closed parity with the orchestration lane: a capability run
    // without a baseline report cannot prove non-regression.
    let baselinePresent = baseline.Count > 0
    (cmp, noRegression && baselinePresent)

let private run (argv: string[]) : int =
    match argv with
    | [| "gate"; "--input"; path |] ->
        let raw = File.ReadAllText path
        use doc = JsonDocument.Parse raw
        let root =
            match JsonNode.Parse(raw) with
            | :? JsonObject as o -> o
            | _ -> failwith "EVAL_INPUT_INVALID"
        let format = tryStr root "format" |> Option.defaultValue ""
        let gates =
            tryObj root "quality_gates"
            |> Option.defaultValue (JsonObject())
        let cand =
            tryObj root "candidate" |> Option.defaultValue (JsonObject())
        let baseline =
            tryObj root "baseline" |> Option.defaultValue (JsonObject())
        let cmp, passed =
            if format = "star-capability-suite/v1" then
                evalCapability cand baseline
            elif format = "star-native-eval-suite/v1" || format = "" then
                evalNative gates cand baseline
            else
                failwith $"EVAL_SUITE_FORMAT_UNKNOWN:{format}"
        cmp["verdict_owner"] <- "fsharp"
        let out = JsonObject()
        out["ok"] <- true
        out["format"] <- "star-fsharp-eval-verdict/v1"
        out["passed"] <- passed
        out["comparison"] <- cmp
        Console.Out.WriteLine(out.ToJsonString())
        if passed then 0 else 2
    | _ ->
        eprintfn "usage: xc-eval gate --input <json>"
        1

[<EntryPoint>]
let main argv =
    try
        run argv
    with ex ->
        eprintfn "xc-eval: %s" ex.Message
        Console.Out.WriteLine(
            sprintf "{\"ok\":false,\"error\":\"%s\"}"
                (ex.Message.Replace("\\", "\\\\").Replace("\"", "\\\"")))
        1
