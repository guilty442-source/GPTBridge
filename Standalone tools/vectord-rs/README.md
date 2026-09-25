# vectord-rs

GPTBridge 受管 Rust 向量引擎——codex A610 指定之 target-primary 語意索引
（接管 Qdrant，後者於 cutover 完成後退休）。只保存可重建的衍生資料；
正式資料權威與 scope/revision/tombstone 裁決留在 PostgreSQL。

## 建置

需要 Rust 工具鏈（codex 鎖定 1.98.1，`rustup` stable）：

```powershell
cargo build --release --locked
Copy-Item target\release\vectord.exe bin\vectord.exe
```

`bin/`、`target/`、`runtime/` 均為 gitignore 產物。

## 執行

```powershell
.\bin\vectord.exe --bind 127.0.0.1:8092 --store-dir runtime
```

非 loopback 綁定位址直接拒絕啟動（fail-closed）。快照每 2 秒於 dirty
時自動落盤，重啟自動載入；無快照時以空索引啟動（由 PostgreSQL 正式
資料經受管重放重建）。

## 由誰啟動

1. `startup_core` 的 `qdrant-start` phase：預設（`VECTOR_BACKEND` 非
   `qdrant`）改探測/拉起 vectord。
2. `RustVectorRuntime.initialize()` 惰性 ensure：probe → build →
   spawn，與 `searchd` 的受管模式相同。

## 測試

```powershell
cargo test
```

涵蓋 scope 過濾、tombstone 刪除、快照往返、維度不符 fail-closed、
alias 解析。

## 契約

見 `CONTRACT.md`（`vectord/v1`）。Python 側適配層：
`main-system/src-core/core_system/rag/rust_vector_runtime.py`
（`RustVectorRuntime` / `VectordClient`）。
