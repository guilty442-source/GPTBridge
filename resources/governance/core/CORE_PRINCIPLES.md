# [Level 1] GPTBridge 治理核心原則

此文件定義了 GPTBridge 平台的核心治理原則，作為 Level 0 Constitution 的具體化實施。

## 七、架構漂移防治 (Architecture Drift Prevention)
1. 禁止偷偷新增平行架構、alternative runtime 或第二套啟動流。
2. 所有新模組必須對齊既有架構。新增架構需附帶原因、受影響模組、遷移策略及治理影響評估。

## 八、臨時修復有效期 (Temporary Fix Expiration)
1. 任何 Workaround 必須標記 `TEMPORARY`、建立時間、原因及移除條件。

## 九、根目錄純淨化 (Root Directory Purity)
1. 專案根目錄僅限存放設定檔 (`config`)、專案定義 (`package.json`)、啟動腳本 (`run.py`)、治理進入點及文件。
2. 禁止散落 `.tsx`、runtime 或暫存檔。

## 十、治理先行 (Governance Before Feature)
1. 新增功能前必須確認模組放置、啟動影響及治理相容性。

## 十一、執行高於文件 (Enforcement > Documentation)
1. 優先建立自動化 Checker 而非僅撰寫文件。

## 十二、運行時恢復優先 (Runtime Recovery Priority)
1. 發生錯誤時優先選擇 `degrade` (降級)、`recover` (恢復) 或 `isolate` (隔離)，禁止直接導致 App 崩潰。
2. Renderer 進程永遠優先存活。

### 治理建立順序 (二十六、治理建立順序)
正式流程：
1. **先建立治理** -> 2. **再 Audit** -> 3. **再規劃** -> 4. **再重整** -> 5. **再 Build** -> 6. **再 Runtime 驗證**。
**禁止在沒有治理規則前大改程式碼。**