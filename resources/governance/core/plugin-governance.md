# [Level 1] Plugin Governance

## 1. Plugin Trust Boundary (G-028)
插件必須在嚴格隔離的沙盒環境中運行，並透過明確的清單（Manifest）聲明其權限。

## 2. Sandbox Enforcement (G-014)
插件系統必須強制執行沙盒隔離，確保插件崩潰不會影響主應用程式。

## 3. Timeout Protection
插件的加載與執行必須有超時保護，防止惡意或錯誤插件導致應用程式凍結。