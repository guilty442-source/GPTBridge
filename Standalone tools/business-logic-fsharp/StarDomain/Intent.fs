namespace StarDomain

open System
open System.Collections.Concurrent
open System.Collections.Generic
open System.Text.RegularExpressions

/// 意圖規則判定（純函式）。優先順序與 Python 實作一致——配息/報價優先於
/// 一般搜尋，避免誤判。fail-closed：空值/超長截斷，不拋例外。
module IntentRules =

    [<Literal>]
    let MaxPromptLength = 2000

    let private calcRegex =
        Regex(@"\d+\s*[\+\-\*\/]", RegexOptions.Compiled)

    let private hasAny (lower: string) (keys: string list) =
        keys |> List.exists (fun k -> lower.Contains k)

    let classify (prompt: string | null) : IntentResult =
        let text =
            match prompt with
            | null -> ""
            | p -> p.Trim()
        let text =
            if text.Length > MaxPromptLength then text.[.. MaxPromptLength - 1]
            else text

        if text.Length = 0 then
            { Primary = StarIntent.Conversation
              Candidates = [| StarIntent.Conversation |]
              Confidence = 0.5
              NormalizedPrompt = "" }
        else
            let lower = text.ToLowerInvariant()
            let intent, confidence =
                if hasAny lower [ "配息"; "distribution"; "除息" ] then StarIntent.Distribution, 0.9
                elif hasAny lower [ "報價"; "淨值"; "quote"; "price" ] then StarIntent.Quote, 0.9
                elif hasAny lower [ "風險"; "risk" ] then StarIntent.Risk, 0.85
                elif hasAny lower [ "分析"; "analysis" ] then StarIntent.Analysis, 0.85
                elif hasAny lower [ "計算"; "calculation" ] || calcRegex.IsMatch lower then StarIntent.Calculation, 0.8
                elif hasAny lower [ "程式"; "code"; "def "; "```" ] then StarIntent.Coding, 0.85
                elif hasAny lower [ "閱讀"; "reading"; "摘要" ] then StarIntent.Reading, 0.8
                elif hasAny lower [ "搜尋"; "search" ] then StarIntent.Search, 0.8
                else StarIntent.Conversation, 0.6

            let candidates =
                [ intent; StarIntent.Conversation ]
                |> List.distinct
                |> List.toArray

            { Primary = intent
              Candidates = candidates
              Confidence = min 1.0 (max 0.0 confidence)
              NormalizedPrompt = text }

/// 快取版分類器（對應 Python COMMAND_UNDERSTANDING_CACHE 的 TTL 快取）。
/// 速度：編譯正則 + LRU 式淘汰；快取是實作細節，判定規則在 IntentRules。
type RuleIntentClassifier() =

    static let cache =
        ConcurrentDictionary<string, struct (IntentResult * int64)>()

    static let maxCache = 128
    static let ttlTicks = TimeSpan.FromMinutes(5.0).Ticks

    member _.Classify(prompt: string | null) : IntentResult =
        let key =
            match prompt with
            | null -> ""
            | p -> p.Trim()

        match cache.TryGetValue(key) with
        | true, struct (cached, ticks) when DateTime.UtcNow.Ticks - ticks < ttlTicks ->
            cached
        | _ ->
            let result = IntentRules.classify prompt
            // 速度：LRU 式淘汰（超過上限移除一個，避免無限增長）
            if cache.Count >= maxCache then
                match Seq.tryHead cache.Keys with
                | Some first -> cache.TryRemove(first) |> ignore
                | None -> ()
            cache.[key] <- struct (result, DateTime.UtcNow.Ticks)
            result

    interface IIntentClassifier with
        member this.Classify(prompt) = this.Classify(prompt)
