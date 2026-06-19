# AI Provider 治理規則

## 一、獨立性原則

**一個 AI = 一組獨立設定**。

每個 Provider (ChatGPT / Gemini) 必須擁有獨立的：
- URL (Conversation URL)
- Session (Browser Context)
- 實體 Browser 實例
- Health State (健康狀態)
- Retry State (重試計數)

## 二、變更權限
禁止在 Provider 實作內部寫死 URL，必須允許透過 `System Settings` 修改。