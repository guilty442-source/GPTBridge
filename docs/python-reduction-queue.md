# Python 常駐收斂遷移佇列（A610 / registry `python_residency`）

**產生日期**：2026-09-27
**資料來源**：`governance_rule/execution/audit/architecture_registry.json`（`python_residency` disposition 為唯一權威）
**量測方式**：各 `physical_path` 下非測試 `.py` 檔（排除 `__pycache__`/venv/node_modules/runtime/dist/bin/obj/tests/data）

---

## 一、佇列現況（migrate-csharp）

| # | 元件 | Python 規模 | 狀態 | 前置需求 |
| --- | --- | --- | --- | --- |
| 1 | `bootstrap-entry` | 4 檔 / 1,089 行 | ✅ **已遷**（`e0d65d24`，C#14/.NET10 `GPTBridge.Bootstrap`，8/8 測試） | — |
| 2 | `information-channel-gateway` | registry 指 `channel_runtime.py`（65 行 shim）；實際為 `shared-layer/src/` 下 channel 系列模組 | 🔄 **C# 庫已交付**（`shared-layer/csharp/GPTBridge.Channels`，16/16 測試，`91959d4b`） | **受管 C# host 進程 + IPC seam** — Python `channel_runtime` 仍為活路徑 |
| 3 | `xingcheng-auto-repair-module` | `tasks/central_repair.py`，252 行 | ⏸ 待 host | 屬 main-system 內部 resident task；需 host 邊界決策（子進程或隨 main-system 整體遷移） |
| 4 | `system-rescue` | 9 檔 / 525 行（實質邏輯 `platform_packager.py` 13KB） | ⏸ 待 host | **受管非 Python 工具 host**：manifest/channel/IPC 契約目前只以 Python tool 形式存在 |
| 5 | `investment-mobile` | 191 檔 / 30,384 行 | ⏸ 待 host | 同 #4；規模第二大，建議排最後 |
| 6 | `self-commit-service` / `integration-plane` / `recovery-plane` | `git_tiers/` 共用 64 檔 / 19,259 行 | ⏸ 待 host | 三元件共用同一路徑；resident 服務，需常駐 C# host（非 per-call 子進程） |
| 7 | `boot-core` | `src-core` 439 檔 / 97,245 行 | ⏸ 排序最末 | 主系統核心；依賴所有上述 host 基礎設施先就緒 |
| 8 | `main-system` | 全樹（量測含 venv-bak 殘留，實際待精算） | ⏸ 排序最末 | 終點工作；其餘全部遷完後才具備條件 |

## 二、已完成 / 非遷移目標

| 元件 | disposition | 狀態 |
| --- | --- | --- |
| `tokenizer` | `retire` | ✅ 已退役（`external_locator` 指向 git-history，零活消費者） |
| sovereigns ×5 | `retain-governance` | A610 允許角色（治理語義） |
| `xingcheng-auto-learning-module` / `model-training` | `retain-bounded` | A610 允許角色（JAX 訓練） |

## 三、排序理由

1. **可執行序由 host 邊界決定，不由檔案大小決定**：`system-rescue` 只有 525 行但卡在同一前置（工具 host）；`central_repair.py` 只有 252 行但卡在同一前置。
2. **下一步的實質解鎖點是「受管 C# host」**——一次性投資解鎖 #3–#6：
   - 工具 host（on-demand 進程型，對齊現有 tool manifest 契約）→ 解鎖 #4、#5
   - resident service host（長駐型，承接 #6 git_tiers 與 #3 的 task）→ 解鎖 #3、#6
   - channel host（information-channel 邊界）→ 讓 #2 的已交付 C# 庫變成活路徑
3. `boot-core`/`main-system` 依定義排在所有 host 就緒之後——它們是遷移的終點而非起點。

## 四、治理邊界

- 本佇列只反映 registry 既有 disposition；**不新增法典條文、不改 sealed codex**。
- 任何 host 設計（進程模型、IPC 契約、attestation 傳遞）屬架構決策，需經治理程序而非直接實作。
- 遷移期 Python 路徑一律保留為 fallback（bootstrap-entry 先例：三處備援）。
