/* strings.h — zh-TW UI string table (UTF-8; compile with /utf-8).
 * Mirrors the retired React surface and the egui tool_file_sorter
 * strings 1:1 so the operator sees identical wording. */
#ifndef GPTBRIDGE_FSUI_STRINGS_H
#define GPTBRIDGE_FSUI_STRINGS_H

namespace fsui {
namespace tr {

inline constexpr const char* kKicker        = "SYS // SMART FILE WORKSPACE";
inline constexpr const char* kTitle         = "自動化檔案管理";
inline constexpr const char* kConnected     = "系統已連線";
inline constexpr const char* kConnecting    = "正在連線";
inline constexpr const char* kWorkspaceLbl  = "目前工作區";
inline constexpr const char* kNoWorkspace   = "尚未選擇要整理的位置";

inline constexpr const char* kSecWorkspace  = "工作區與自動分類";
inline constexpr const char* kWorkspaceHint = "選定工作區後，自動分類會持續監看新檔案，不需要額外設定開關。";
inline constexpr const char* kTargetLabel   = "目標資料夾";
inline constexpr const char* kTargetHint    = "貼上要管理的資料夾路徑";
inline constexpr const char* kBtnBrowse     = "瀏覽…";
inline constexpr const char* kBrowseTitle   = "選擇目標資料夾";
inline constexpr const char* kTargetBad     = "目標資料夾不存在或無法存取";
inline constexpr const char* kAutoInFlow    = "自動分類已納入流程";
inline constexpr const char* kWaitWorkspace = "等待選擇工作區";
inline constexpr const char* kAutoOrganize  = "啟用背景自動分類";
inline constexpr const char* kDupTrash      = "自動將完全重複檔移至 Windows 資源回收筒";
inline constexpr const char* kDupTrashHint  = "預設關閉。只處理 SHA-256 完全相同、連續兩輪保持不變的副本；保留最早檔案，不會永久刪除。";

inline constexpr const char* kSecSort       = "安全整理工作流程";
inline constexpr const char* kSortHint      = "手動整理只會先建立 dry-run 預覽；必須勾選確認後，才能套用同一個 plan_id。";
inline constexpr const char* kBtnPreview    = "建立預覽（Dry run）";
inline constexpr const char* kBtnUndo       = "復原上一次";
inline constexpr const char* kBtnHistory    = "整理歷史";
inline constexpr const char* kPlanPending   = "待套用計畫：%d 個動作";
inline constexpr const char* kPlanConfirm   = "我已檢查此計畫，確認套用 plan ";
inline constexpr const char* kPlanEmpty     = "此計畫沒有可套用的搬移動作";
inline constexpr const char* kBtnApply      = "確認並套用";
inline constexpr const char* kHistoryEmpty  = "本次視窗尚無操作記錄。";
inline constexpr const char* kHistoryTitle  = "最近操作";
inline constexpr const char* kSuccess       = "成功";
inline constexpr const char* kFailure       = "失敗";

inline constexpr const char* kSecKeywords   = "關鍵字分類規則";
inline constexpr const char* kKeywordHint   = "輸入關鍵字，並從目前目標資料夾既有的第一層子資料夾中選擇目的地；分類不會離開此目標。";
inline constexpr const char* kKeywordLbl    = "關鍵字";
inline constexpr const char* kKeywordHintTxt= "idol, live, report";
inline constexpr const char* kDestLabel     = "分類目的地（目標內的第一層子資料夾）";
inline constexpr const char* kDestEmpty     = "請先掃描並選擇目的地";
inline constexpr const char* kBtnScan       = "掃描";
inline constexpr const char* kAutoScan      = "自動掃描目的地資料夾";
inline constexpr const char* kBtnListRules  = "列出規則";
inline constexpr const char* kBtnUpsert     = "新增/更新規則";
inline constexpr const char* kModifyLbl     = "修改既有關鍵字";
inline constexpr const char* kCurKeyword    = "目前關鍵字";
inline constexpr const char* kNewKeyword    = "新關鍵字";
inline constexpr const char* kBtnModify     = "修改";

inline constexpr const char* kSecCleanup    = "清理掃描";
inline constexpr const char* kCleanupHint   = "清理掃描屬於唯讀分析，不會搬移或刪除任何檔案；掃描完成後再逐一檢視候選項目。";
inline constexpr const char* kImgIssues     = "圖片問題";
inline constexpr const char* kSimilarImgs   = "相似圖片";
inline constexpr const char* kVidIssues     = "影片問題";
inline constexpr const char* kSimilarVids   = "相似影片";
inline constexpr const char* kThresholdLbl  = "相似門檻";
inline constexpr const char* kSpeedLbl      = "分析速度";
inline constexpr const char* kTempLbl       = "溫度";
inline constexpr const char* kTopPLbl       = "Top-p";
inline constexpr const char* kCtxLbl        = "上下文";
inline constexpr const char* kMaxTokLbl     = "輸出上限";
inline constexpr const char* kParallel      = "平行分析";
inline constexpr const char* kBtnCleanup    = "開始清理掃描";
inline constexpr const char* kBtnStopScan   = "■ 停止掃描";
inline constexpr const char* kCandidates    = "候選項目（%d）";
inline constexpr const char* kBtnReveal     = "顯示位置";
inline constexpr const char* kScanOutput    = "掃描輸出";

inline constexpr const char* kConfirmTitle  = "確認操作";
inline constexpr const char* kConfirmUndo   = "確定要復原最近一次已完成的檔案整理？";
inline constexpr const char* kConfirmDupTrash = "啟用後，系統只會將 SHA-256 完全相同且連續兩輪未變動的額外副本移至 Windows 資源回收筒。確定啟用嗎？";
inline constexpr const char* kConfirmMigrate  = "舊版規則中有超出目前資料夾邊界的項目，原始資料已完整保留在隔離紀錄。確定要接受安全遷移結果並啟用自動分類嗎？";

/* ---- run labels / status messages (same as egui surface) ---- */
inline constexpr const char* kMsgReady        = "自動化檔案管理已就緒";
inline constexpr const char* kMsgCleanupReady = "清理掃描已併入此工具";
inline constexpr const char* kMsgNoBackend    = "後端尚未接收工具指令";
inline constexpr const char* kMsgDisconnected = "後端連線中斷，請重新送出指令。";
inline constexpr const char* kMsgBackendErr   = "後端處理失敗。";
inline constexpr const char* kMsgRunFailed    = "工具執行失敗";
inline constexpr const char* kMsgSelectTarget = "正在切換掃描目標...";
inline constexpr const char* kMsgTargetDone   = "掃描目標已更新";
inline constexpr const char* kMsgProfiles     = "正在讀取自動分類設定...";
inline constexpr const char* kMsgProfilesDone = "自動分類設定已更新";
inline constexpr const char* kMsgScanFolders  = "正在掃描目的地資料夾...";
inline constexpr const char* kMsgFoldersDone  = "目的地資料夾已更新";
inline constexpr const char* kMsgScanningFld  = "正在掃描可用目的地資料夾...";
inline constexpr const char* kMsgNoFolders    = "尚未找到可用目的地資料夾";
inline constexpr const char* kMsgPreview      = "正在建立安全預覽...";
inline constexpr const char* kMsgPreviewDone  = "預覽計畫已建立";
inline constexpr const char* kMsgNoPlanId     = "後端未回傳可套用的 plan_id；沒有執行任何搬移";
inline constexpr const char* kMsgApply        = "正在套用已確認的整理計畫...";
inline constexpr const char* kMsgApplyDone    = "整理計畫已安全套用";
inline constexpr const char* kMsgUndo         = "正在復原最近一次整理...";
inline constexpr const char* kMsgUndoDone     = "最近一次整理已復原";
inline constexpr const char* kMsgHistory      = "正在讀取整理歷史...";
inline constexpr const char* kMsgHistoryDone  = "整理歷史已更新";
inline constexpr const char* kMsgListKw       = "正在讀取關鍵字規則...";
inline constexpr const char* kMsgListKwDone   = "關鍵字規則已讀取";
inline constexpr const char* kMsgUpsertKw     = "正在新增或更新關鍵字...";
inline constexpr const char* kMsgUpsertDone   = "關鍵字規則已更新";
inline constexpr const char* kMsgModKw        = "正在修改關鍵字...";
inline constexpr const char* kMsgModKwDone    = "關鍵字規則已修改";
inline constexpr const char* kMsgCleanupRun   = "正在執行清理掃描...";
inline constexpr const char* kMsgCleanupDone  = "清理掃描完成";
inline constexpr const char* kMsgCleanupStop  = "正在停止清理掃描...";
inline constexpr const char* kMsgCleanupStopped = "清理掃描已停止";
inline constexpr const char* kMsgCleanupBad   = "清理掃描回傳格式無法辨識";
inline constexpr const char* kMsgScanStarted  = "正在掃描；完成後顯示完整結果";
inline constexpr const char* kMsgScanning     = "掃描中";

inline constexpr const char* kProfileOff      = "此資料夾尚未啟用背景自動分類";
inline constexpr const char* kProfileClosed   = "此資料夾的自動分類設定為關閉";
inline constexpr const char* kProfileOn       = "背景自動分類已啟用；即使關閉此工具視窗仍會持續監看";
inline constexpr const char* kProfileOffDone  = "自動分類已關閉";
inline constexpr const char* kProfileEnabling = "正在啟用背景自動分類...";
inline constexpr const char* kProfileDisabling= "正在關閉背景自動分類...";
inline constexpr const char* kProfileChanging = "正在變更自動分類設定...";
inline constexpr const char* kProfileChanged  = "自動分類設定已更新";
inline constexpr const char* kProfileNeedConn = "後端連線後才能變更自動分類設定";
inline constexpr const char* kDupTrashOn      = "完全重複檔自動回收已啟用";
inline constexpr const char* kDupTrashOff     = "完全重複檔自動回收已關閉";
inline constexpr const char* kDupTrashOffInit = "完全重複檔自動回收尚未啟用";
inline constexpr const char* kDupTrashEnabling= "正在啟用完全重複檔自動回收...";
inline constexpr const char* kDupTrashDisabling = "正在關閉完全重複檔自動回收...";
inline constexpr const char* kDupTrashChanging= "正在變更重複檔回收設定...";
inline constexpr const char* kDupTrashChanged = "重複檔回收設定已更新";
inline constexpr const char* kDupTrashNeedConn= "後端連線後才能變更重複檔回收設定";
inline constexpr const char* kNeedTarget      = "請先選擇目標資料夾";
inline constexpr const char* kMigrateReview0  = "舊規則已完整保留並隔離；確認後才能重新啟用自動分類";

} // namespace tr
} // namespace fsui
#endif /* GPTBRIDGE_FSUI_STRINGS_H */
