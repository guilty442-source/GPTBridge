# [Level 4] Legacy Architecture Tracking

## 1. Legacy Isolation (G-090)
舊有或已過時的程式碼必須被隔離、明確標註並追蹤其日落計畫。禁止舊有程式碼混入正式運行時。

## 2. Migration Integrity (G-091)
架構遷移不得破壞 Alias、運行時權威、治理權威或啟動確定性。

## 3. Legacy Startup Flow
已廢棄的啟動流程：`Bootstrap -> Wait Services -> Render UI`。