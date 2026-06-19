# 中文化治理規則

## 一、結構鏡像

中文化語系檔必須存放於 `resources/localization/`，並鏡像 `src-ui` 的目錄結構。
例如：`resources/localization/src-ui/dashboard/zh-TW.json`

## 二、字串提取規範

1. **禁止硬編碼**：禁止在 `.tsx` 或 `.py` 中直接寫入中文字串。
2. **標籤化調用**：
   - 前端必須使用 `t("key")`。
   - 後端回報訊息優先引用配置檔中的 `display_names`。

## 三、驗證流程

正式變更必須經過以下流程：

1. **Audit**：掃描受影響區域。
2. **Analysis**：分析對高風險模組的影響。
3. **Approval**：批准後才可動工。
4. **Verification**：
   - 真正執行 `npm.cmd run build`。
   - 真正執行 `npm.cmd run dev`。
   - 真正於 UI 進行功能點擊驗證。
