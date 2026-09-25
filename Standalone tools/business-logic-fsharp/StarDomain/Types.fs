namespace StarDomain

open System.Collections.Generic

// Codex A211/A264: FSharp owns core business logic + data validation +
// transformation + business state transitions. C# consumes these typed
// contracts only (FSHARP-FLOW: typed analysis contracts only).

/// 對應 Python StarNativeIntentMixin / StarModelRegistry 的意圖分類。
/// 模型周邊業務規則由 F# 負責，模型推論仍經 Python/C++ 路徑。
type StarIntent =
    | Conversation = 0
    | Capabilities = 1
    | Status = 2
    | Search = 3
    | Distribution = 4
    | Quote = 5
    | Risk = 6
    | Analysis = 7
    | Calculation = 8
    | Reasoning = 9
    | Statistics = 10
    | DataOrganization = 11
    | Coding = 12
    | SelfUpgrade = 13
    | Reading = 14
    | Unknown = 15

type IntentResult =
    { Primary: StarIntent
      Candidates: IReadOnlyList<StarIntent>
      Confidence: float
      NormalizedPrompt: string }

/// 對應 Python StarNativeGroundingMixin：工具回傳轉為模型可理解的證據格式。
type Evidence =
    { SourceId: string
      Content: string
      ContentHash: string
      Relevance: float
      SourceType: string }

type GroundingResult =
    { Evidences: IReadOnlyList<Evidence>
      ConsolidatedContext: string
      HasSufficientEvidence: bool }

/// 對應 Python StarNativePlanMixin：意圖與正規化需求轉為執行計畫。
type ExecutionPlan =
    { Intent: StarIntent
      NormalizedInstruction: string
      RequiredTools: IReadOnlyList<string>
      TaskIntensity: int
      NeedsGrounding: bool
      GenerationPrompt: string }

type IIntentClassifier =
    abstract member Classify: prompt: string -> IntentResult

type IPlanBuilder =
    abstract member Build: intent: IntentResult * context: string -> ExecutionPlan

type IGroundingService =
    abstract member Ground: plan: ExecutionPlan * raw: IReadOnlyList<Evidence> -> GroundingResult
