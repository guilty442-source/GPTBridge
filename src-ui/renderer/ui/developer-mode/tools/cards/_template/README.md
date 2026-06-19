# Tool Card 三層模板

新增一張工具卡時，請複製三層模板並改名：

1. `state-hook.template.ts.txt` -> `use<YourCard>State.ts`
2. `action-service.template.ts.txt` -> `create<YourCard>Actions.ts`
3. `ui.template.tsx.txt` -> `<YourCard>UI.tsx`

再新增外層封裝檔：

- `cards/<YourCard>.tsx`
  - 組裝 `state hook + action service + UI`
  - 對外維持穩定 props 介面

最後把 `<YourCard />` 掛到 `ToolRuntimePanel.tsx`。

