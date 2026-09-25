# business-logic-fsharp — StarDomain (F# / .NET 10)

Codex A211/A264: **FSharp owns core business logic + data validation +
transformation + business state transitions.** `StarDomain` is the
single-purpose F# library that holds the domain decisions for the
star/xingcheng business surface; C# (`business-logic-csharp`) consumes
it through typed contracts only (FSHARP-FLOW: typed analysis contracts
only — no C# portable business logic, A264/A341).

## Layout

- `StarDomain/Types.fs` — `StarIntent` enum, `IntentResult` /
  `Evidence` / `GroundingResult` / `ExecutionPlan` records, and the
  `IIntentClassifier` / `IPlanBuilder` / `IGroundingService` contracts.
- `StarDomain/Intent.fs` — `IntentRules.classify` (pure rule table,
  same priority order as the Python classifier) and
  `RuleIntentClassifier` (TTL-cached wrapper).
- `StarDomain/Grounding.fs` — `GroundingRules.ground` (filter →
  stable relevance sort → hash dedup → top-6 → consolidated context)
  and `DefaultGroundingService`.
- `StarDomain/Plan.fs` — `PlanRules.build` (intent→tool map, task
  intensity, grounding requirement, generation prompt) and
  `DefaultPlanBuilder`.

All types are C#-consumable (enum + records + classes); every rule is
a pure function — fail-closed on empty/invalid input, never throws.

## Build & test

```powershell
dotnet build ..\business-logic-csharp\StarBusinessLogic.slnx -c Release
dotnet test  ..\business-logic-csharp\StarBusinessLogic.slnx -c Release
```

Orchestrated through `StarBusinessLogic.Tests` (C# owns sole test
orchestration, A615).

## Isolation

Registered in `main-system/config/tool-isolation-policy.json` as
`business-logic-fsharp`: `network_policy=offline`,
`filesystem_policy=none` — pure domain library, in-process inside the
C# host, no network or filesystem access.
