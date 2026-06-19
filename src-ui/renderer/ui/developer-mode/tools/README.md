# 開發模式工具執行層結構

此目錄採用「三層卡片架構」：

- `cards/<tool>/...UI.tsx`：純 UI 呈現層。
- `cards/<tool>/use...State.ts`：狀態映射層（state hook）。
- `cards/<tool>/create...Actions.ts`：行為封裝層（action service）。
- `cards/*.tsx`：相容封裝層（組裝 UI + hook + service，供外部舊路徑繼續使用）。
- `controllers/useToolControlCenter.ts`：統一管理跨卡片的 runtime 狀態、事件監聽、IPC 指令。
- `ToolRuntimePanel.tsx`：組裝卡片與工具執行卡，不放業務細節。
- `types.ts`：工具執行層共用型別。

## 已拆成三層的卡片

- `cards/system-settings/*` + `SystemSettingsCard.tsx`
- `cards/system-check/*` + `SystemCheckToolCard.tsx`
- `cards/sandbox/*` + `SandboxToolCard.tsx`
- `cards/update/*` + `UpdateToolCard.tsx`
- `cards/backup/*` + `BackupToolCard.tsx`
- `cards/log/*` + `LogToolCard.tsx`

## 新增工具卡模板

- 直接複製 `cards/_template/` 內三個模板：
  - `state-hook.template.ts.txt`
  - `action-service.template.ts.txt`
  - `ui.template.tsx.txt`
- 再建立一個外層 `cards/<YourCard>.tsx` 做組裝，並在 `ToolRuntimePanel.tsx` 掛入即可。
