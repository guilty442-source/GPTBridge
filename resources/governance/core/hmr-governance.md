# [Level 3] 熱更新治理規則

核心原則：

UI / renderer 開發期間，
熱更新（HMR）屬於強制開發能力。

禁止：
每次修改 UI 都要求手動重啟整個系統。

==================================================
HMR 強制規則
==================================================

1. 修改 src-ui/renderer/* 後，
必須優先使用 Vite HMR。

2. 修改後必須驗證：
畫面是否立即更新。

3. 若畫面未更新：

依序：

1. Vite HMR
2. renderer reload
3. Electron window reload
4. 清除 Vite cache (node_modules/.vite)
5. 重啟 dev server
6. 重啟整個 GPTBridge 工具 (Fallback)

禁止：
直接假設功能壞掉。
不要卡在舊畫面。
不要讓使用者手動反覆重啟。

==================================================
Renderer Reload 規則
==================================================

若 HMR 失效：

允許：

- renderer reload
- BrowserWindow reload
- webContents.reloadIgnoringCache()

禁止：

直接重啟 backend workflow。

==================================================
Backend 隔離規則
==================================================

UI 熱更新：

不得：

- 重啟 provider
- 重啟 browser session
- 重啟 runtime
- 重啟 AI workflow
- 重啟 sandbox

除非：

renderer reload 無法恢復。

==================================================
開發流程規則
==================================================

所有 UI 修改流程：

必須：

1. 修改 UI
2. 驗證 HMR
3. 驗證 renderer reload
4. 驗證 console
5. 再判定功能問題

禁止：

修改後未驗證 HMR 就繼續 debug。

==================================================
可見驗證規則
==================================================

每次大型 UI 修改：

允許暫時加入：

HMR TEST
UI VERSION
DEBUG LABEL

確認畫面是否真的更新。

確認後可移除。

==================================================
HMR 失效處理
==================================================

若 HMR 長期失效：

必須檢查：

- vite.config.ts
- renderer root
- Electron reload flow
- alias path
- watcher scope
- cache
- preload isolation
- dev server port

禁止：

把 HMR 問題誤判成：
- React bug
- provider bug
- backend bug

==================================================
自動重啟治理規則 (Fallback)
==================================================

若 HMR / renderer reload / dev server 重啟皆失敗，
允許系統自動重啟整個 GPTBridge 工具。

重啟順序原則：
1. 熱更新優先
2. reload 次之
3. 最後才重啟整個工具

資料保護規則 (重啟整個工具時)：
- 優先保留 session / cookies / provider login。
- 禁止清除 runtime session。
- 禁止清除 provider config。
- 禁止清除 .GPTBridge_RuntimeSandbox 根目錄。
- 僅允許重啟 process / dev server / Electron window。

==================================================
UI 顯示與日誌規則
==================================================

UI 顯示：
- 重新載入介面中... (用於 reload 階段)
- 重啟工具中... (用於工具重啟階段)

詳細記錄：
- 所有自動恢復與重啟行為必須詳細記錄至日誌。