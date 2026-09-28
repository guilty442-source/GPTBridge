namespace StarDomain

open System
open System.Collections.Generic

/// 執行計畫規則（純函式）。對應 Python StarNativePlanMixin：意圖 → 工具清單、
/// 任務強度、grounding 需求、生成提示詞。模型仍由 Python 推論，此處僅產生
/// 提示詞與工具清單。
module PlanRules =

    let private toolMap: IReadOnlyDictionary<StarIntent, string[]> =
        dict [
            StarIntent.Search, [| "web_search"; "rag_query" |]
            StarIntent.Reading, [| "rag_query"; "context_builder" |]
            StarIntent.Analysis, [| "rag_query"; "market_data"; "calculation" |]
            StarIntent.Calculation, [| "calculation" |]
            StarIntent.Coding, [| "coding_expert" |]
            StarIntent.Reasoning, [| "reasoning" |]
            StarIntent.Distribution, [| "market_data"; "search" |]
            StarIntent.Quote, [| "market_data" |]
            StarIntent.Risk, [| "analysis"; "calculation" |]
            StarIntent.Conversation, [||]
        ]
        |> Dictionary
        :> IReadOnlyDictionary<StarIntent, string[]>

    /// F# 擁有的唯一工具集合（C# 路由器的允許清單以此為準，不得在 C# 另行手寫）。
    let supportedTools : string[] =
        toolMap.Values
        |> Seq.concat
        |> Seq.distinct
        |> Seq.sort
        |> Seq.toArray

    let build (intent: IntentResult) (context: string) : ExecutionPlan =
        let tools =
            match toolMap.TryGetValue(intent.Primary) with
            | true, t -> t
            | _ -> [||]

        let baseIntensity =
            match intent.Primary with
            | StarIntent.Analysis | StarIntent.Coding | StarIntent.Risk -> 7
            | _ -> 3
        let intensity =
            if not (String.IsNullOrWhiteSpace context) && context.Length > 500 then
                min 10 (baseIntensity + 2)
            else baseIntensity

        let needsGrounding =
            match intent.Primary with
            | StarIntent.Search | StarIntent.Reading | StarIntent.Analysis
            | StarIntent.Distribution | StarIntent.Quote -> true
            | _ -> false

        let prompt =
            if String.IsNullOrWhiteSpace context then intent.NormalizedPrompt
            else $"Context: {context}\n\nInstruction: {intent.NormalizedPrompt}"

        { Intent = intent.Primary
          NormalizedInstruction = intent.NormalizedPrompt
          RequiredTools = tools
          TaskIntensity = intensity
          NeedsGrounding = needsGrounding
          GenerationPrompt = prompt }

type DefaultPlanBuilder() =

    member _.Build(intent: IntentResult, context: string) : ExecutionPlan =
        PlanRules.build intent context

    interface IPlanBuilder with
        member this.Build(intent, context) = this.Build(intent, context)
