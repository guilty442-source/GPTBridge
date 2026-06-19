# 影音下載自動化

Project ID: `vaultly`

Vaultly 2.3.2 是正式版主程式內的獨立下載中心。它使用 Vaultly 專屬的 Microsoft Edge 工作階段保留登入狀態；只有開啟「影音下載自動化」視窗後，才會啟動專屬瀏覽器；登入 Instagram 或 X 後，背景服務會自動開啟追蹤名單、掃描帳號、套用篩選，再讓使用者勾選帳號、設定條件並建立背景下載工作。

## 使用流程

1. 從程式庫啟動「影音下載自動化」獨立視窗。
2. 按「開啟登入頁」，在 Vaultly 專屬 Edge 完成登入。
3. Vaultly 會自動偵測登入並掃描追蹤名單；「立即重新掃描」可作為手動備援。
4. 視需要新增自訂排除關鍵字或 `@帳號`，並可查閱、還原被移除帳號。
5. 勾選帳號並儲存。
6. 設定照片／影片、日期、關鍵字、最低按讚／觀看數、每帳號上限。
7. 先預覽，或選擇既有資料夾後開始背景下載。

## 安全與檔案規則

- 只讀取目前登入帳號可見的內容，不繞過私密權限、登入、平台限制或 DRM。
- 只下載平台轉接器允許的 HTTPS 媒體網域。
- Instagram 與 X 的直連影片會驗證完整容器；HLS 分段串流會自動合併畫面與音訊後再保存。
- 錯誤頁、HTTP 部分回應、DASH 初始化檔與不完整影片不會保存成影片。
- 「略過已下載媒體」會先驗證歷史檔案，舊的錯誤影片不會阻擋重新下載。
- 下載資料夾必須已存在；Vaultly 不會自動建立子資料夾。
- 下載檔名只包含帳號與下載日期時間，並另外記錄下載歷史避免重複。
- 掃描追蹤名單時會一律保留已認證帳號，未認證帳號才會保守排除明顯的商店、新聞、公司、機構與促銷帳號；無法判定的真人帳號會保留。
- 帳號清單支援即時搜尋；已勾選帳號會自動排列在各平台清單最上方。
- 獨立下載中心與登入／掃描帳號區塊使用較大的預設尺寸，帳號清單可顯示更多內容。
- Instagram 與 X 會同步掃描；背景服務會在使用者開啟下載中心後才初始化專屬瀏覽器，完整掃描從名單頂端開始、每輪一次完成帳號解析與捲動，未掃到底會從目前位置自動接續。
- Instagram 掃描採 Cookie-first 設計：優先使用專屬 Edge 登入工作階段的 Cookie 讀取追蹤清單，只有 Cookie API 暫時不可用時才退回頁面 dialog 掃描。
- Instagram 自動掃描會用導覽列、側欄、目前個人檔案頁與頁面內 viewer 資料多重辨識個人檔案入口，降低平台版面變動造成的「找不到個人檔案入口」。
- 主程式啟動但尚未開啟「影音下載自動化」時，Vaultly 只會待命，不會開啟瀏覽器，也不會恢復中斷的下載導頁。
- 登入頁、追蹤名單掃描頁與下載頁各自獨立，下載導頁或手動瀏覽不會中斷背景掃描。
- 已確認的認證狀態不會因平台暫時漏載徽章而被降級；Instagram 已快取的頭像不會在每次掃描時重複下載。
- 自訂篩選名單可手動增減；手動移除帳號會自動加入精準 `@帳號` 篩選。
- 自動篩選或手動移除的帳號都會顯示在移除紀錄，保存原因、來源與時間；從下載中心還原後會標記為手動保留，避免下次自動掃描再次移除。
- 帳號、條件、工作佇列與下載歷史儲存在 `runtime/state/vaultly.sqlite3`。
- 平台登入資料儲存在 `runtime/browser-profiles/vaultly/shared`，不會污染原始碼或 AI 瀏覽器工作階段。

## 專屬模組

- 正式後端服務：`platform_tools/vaultly/src/backend/services/vaultly/service.py`
- 平台轉接器：`platform_tools/vaultly/src/backend/services/vaultly/adapters.py`
- 條件規則：`platform_tools/vaultly/src/backend/services/vaultly/rules.py`
- 持久化資料庫：`platform_tools/vaultly/src/backend/services/vaultly/repository.py`
- 獨立下載介面：`platform_tools/vaultly/src/ui/VaultlyDownloadCenter.tsx`
