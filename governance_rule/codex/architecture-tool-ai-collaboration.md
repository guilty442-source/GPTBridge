# AI Collaboration 完整架構圖

```mermaid
flowchart TB
  ENTRY[AI Collaboration Entry] --> APP[Collaboration Domain]
  ENTRY --> UI[Two-column UI]
  UI --> TOP[Integrated compact top card: AI list, browser controls and collaboration input]
  UI --> LEFT[Left 1/4: AI responses without request cards]
  UI --> RIGHT[Right 3/4: embedded browser webpage]
  APP --> AUTO_MEMORY[Automatic collaboration memory recording]
  APP --> AUTH[Identity and Permission Check]
  AUTH --> INFO[Information Layer]
  INFO --> EXT[Registered External Collaboration Boundary]
  APP --> STATE[(AI Collaboration-owned State)]
  APP --> PROC[Independent Process Tree]
  PROC --> SUP[Dedicated Supervisor and Watchdog]
  PROC --> AUDIT[Correlation and Audit Reference]
  PROC --> FAULT[Isolated Failure Boundary]
```

所有外部協作必須經已登錄通道；外部回應不會自動取得專案權威。

同步基線：A528、A537、A538、A540、A544、A545；最上方單一整合卡片承載網址工具列、內建 AI 名單與協作輸入，AI 操作直接控制右側內建瀏覽器。左側 1/4 只顯示各 AI 回覆且不建立任何需求卡片，右側 3/4 為瀏覽器網頁區。獨立 AI 名單、共享記憶與診斷狀態卡全部取消；協作內容與回覆由工具自動記錄。
