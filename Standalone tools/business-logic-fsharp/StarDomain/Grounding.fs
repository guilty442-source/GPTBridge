namespace StarDomain

open System
open System.Collections.Generic
open System.Text

/// 證據篩選/去重/整併規則（純函式）。對應 Python StarNativeGroundingMixin。
/// fail-closed：空或全無效輸入不拋例外，標記 HasSufficientEvidence 供業務層
/// 決定是否回退或要求補充輸入。
module GroundingRules =

    [<Literal>]
    let MaxEvidences = 6

    let private emptyResult needsGrounding =
        { Evidences = Array.empty<Evidence>
          ConsolidatedContext = ""
          HasSufficientEvidence = not needsGrounding }

    let ground (needsGrounding: bool) (raw: IReadOnlyList<Evidence>) : GroundingResult =
        let noInput =
            match box raw with
            | null -> true
            | _ -> raw.Count = 0

        if noInput then
            emptyResult needsGrounding
        else
            // 速度：單次遍歷過濾無效證據
            let filtered =
                seq {
                    for e in raw do
                        if not (isNull (box e))
                           && not (String.IsNullOrWhiteSpace e.Content)
                           && not (String.IsNullOrWhiteSpace e.ContentHash) then
                            yield e
                }
                |> List.ofSeq

            if filtered.IsEmpty then
                emptyResult needsGrounding
            else
                // 穩定排序：Relevance 降序；List.sortByDescending 為穩定排序，
                // 相同 Relevance 保持原序以確保可重現。
                let selected =
                    filtered
                    |> List.sortByDescending (fun e -> e.Relevance)
                    |> Seq.distinctBy (fun e -> e.ContentHash)
                    |> Seq.truncate MaxEvidences
                    |> List.ofSeq

                let sufficient = (not needsGrounding) || not selected.IsEmpty

                // 速度：StringBuilder 避免 Join 多次分配
                let sb = StringBuilder(selected.Length * 128)
                selected
                |> List.iteri (fun i e ->
                    if i > 0 then sb.Append("\n\n---\n\n") |> ignore
                    sb.Append($"[Evidence {i + 1} | {e.SourceType} | {e.SourceId}]\n") |> ignore
                    sb.Append(e.Content) |> ignore)

                { Evidences = (selected |> List.toArray) :> IReadOnlyList<Evidence>
                  ConsolidatedContext = sb.ToString()
                  HasSufficientEvidence = sufficient }

type DefaultGroundingService() =

    member _.Ground(plan: ExecutionPlan, raw: IReadOnlyList<Evidence>) : GroundingResult =
        GroundingRules.ground (not (isNull (box plan)) && plan.NeedsGrounding) raw

    interface IGroundingService with
        member this.Ground(plan, raw) = this.Ground(plan, raw)
