# [Level 1] Security Governance

## 1. Preload Security Boundary (G-104)
`preload.js` 是 Electron 應用程式中唯一的橋接權威。Renderer 進程禁止直接存取 Node.js API。

## 2. IPC Typed Governance (G-101)
所有 IPC 通訊必須是強型別的、經過驗證的、具備權限範圍的，並記錄所有操作。

## 3. Unsafe Runtime Detection
禁止在運行時使用不受限制的 `eval`、檔案系統存取或 IPC 變異。

## 4. Security Boundary Governance (G-041)
Bridge/Preload 必須嚴格限制對 Node.js 環境的暴露。