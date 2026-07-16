# AI投資管家 Android

這是可安裝的 Android 原生程式，不使用瀏覽器或 WebView。介面以 React
Native 原生元件建立，與桌面版共用同一個投資資料庫及本地 AI 工作佇列。

## 能力邊界

- Android：即時總覽、持股、警示、行動計畫，以及排入本地 AI 命令。
- 電腦端：檔案匯入、持股修改、刪除、交易帳本、安全設定及資料備份。
- 前景狀態每 2 秒與電腦同步；手機不另建第二份投資帳本。
- 配對工作階段保存在 Android Keystore 支援的 SecureStore。
- 外部網路僅接受 HTTPS；HTTP 只允許私人區網。

## 開發與 APK

```powershell
npm.cmd install
npm.cmd run type-check
npx.cmd expo prebuild --platform android
npx.cmd expo run:android --variant release
```

若本機沒有 Java 及 Android SDK，可使用 EAS 的 `preview` 設定建立 APK：

```powershell
npx.cmd eas build --platform android --profile preview
```

首次配對前，先在電腦版投資管家明確啟用區網手機同步，再將電腦端顯示的
`http://私人IP:連接埠` 與一次性配對碼輸入 Android App。
