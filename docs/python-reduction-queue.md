# Python 常駐收斂遷移佇列（A610 / registry `python_residency`）

**產生日期**：2026-09-27
**資料來源**：`governance_rule/execution/audit/architecture_registry.json`（`python_residency` disposition 為唯一權威）
**量測方式**：各 `physical_path` 下非測試 `.py` 檔（排除 `__pycache__`/venv/node_modules/runtime/dist/bin/obj/tests/data）

---

## 一、佇列現況（migrate-csharp）

| # | 元件 | Python 規模 | 狀態 | 前置需求 |
| --- | --- | --- | --- | --- |
| 1 | `bootstrap-entry` | 4 檔 / 1,089 行 | ✅ **已遷**（`e0d65d24`，C#14/.NET10 `GPTBridge.Bootstrap`，8/8 測試） | — |
| 2 | `information-channel-gateway` | registry 指 `channel_runtime.py`（65 行 shim）；實際為 `shared-layer/src/` 下 channel 系列模組 | ✅ **已收斂**（Python channel 模組全數退役並登 `retired_sources.json`；canonical = C++ `native/tool_runtime/channel_runtime*.cpp` + C# `GPTBridge.Channels`/`GPTBridge.ChannelHost`，C# 26/26、native 186/186 測試綠；registry 已改 `internal-service`） | — |
| 3 | `xingcheng-auto-repair-module` | `tasks/central_repair.py`，252 行 | ⏸ 待 host | 屬 main-system 內部 resident task；需 host 邊界決策（子進程或隨 main-system 整體遷移） |
| 4 | `system-rescue` | 9 檔 / 525 行（實質邏輯 `platform_packager.py` 13KB） | ✅ **活鏈路已驗證**（`GPTBridge.ToolHost` + `src-native/SystemRescue.Host.exe`；真實 IPC `start_tool`→選中原生 exe、`run_tool`→claim/execute/respond 經 PostgreSQL+proxy+C# host、新世代後端 `list_tools`→`running`（ExecutablePath 比對）、`stop_tool`→`stopped`；`force_close_tool` 同代碼進程內實測 **4,167ms < 5s 預算**，CPU 飽和下活鏈路 6.7s 逾時但進程全滅——見第五節環境注記；E2E wire-fixture 通過；Python `channel_runtime.py` 已退役，B38/E180 降級路徑關閉，由 `GPTBridge.ChannelHost` 接管） | 設計：`docs/csharp-tool-host-design.md`（P2 sidecar，E4 不變） |
| 5 | `investment-mobile` | 191 檔 / 30,384 行 | ⏸ 待 host | 同 #4；規模第二大，建議排最後 |
| 6 | `self-commit-service` / `integration-plane` / `recovery-plane` | `git_tiers/` 7 檔 / 2,313 行 | ✅ **已遷**（常駐 `GPTBridge.GitAutomation.exe --watch`：`82c50f18`；Python 退役收斂：`2d5d52b4`/`abb9679c`/`7b3ead04`，退役路徑已登 `retired_sources.json`） | — |
| 7 | `boot-core` | `src-core` 439 檔 / 97,245 行 | ⏸ 排序最末 | 主系統核心；依賴所有上述 host 基礎設施先就緒 |
| 8 | `main-system` | 504 檔 / 110,166 行（已排除 `.venv-*` 備份與 node_modules） | ⏸ 排序最末 | 終點工作；其餘全部遷完後才具備條件 |

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
     —— **已交付**：`shared-layer/csharp/GPTBridge.ToolHost`（設計
     `docs/csharp-tool-host-design.md`；`star-governed-transport-proxy/v1`
     P2 sidecar 形態，原生側不持有 token/傳輸庫）
   - resident service host（長駐型，承接 #6 git_tiers 與 #3 的 task）→ 解鎖 #3、#6
   - channel host（information-channel 邊界）→ 讓 #2 的已交付 C# 庫變成活路徑
3. `boot-core`/`main-system` 依定義排在所有 host 就緒之後——它們是遷移的終點而非起點。

## 四、治理邊界

- 本佇列只反映 registry 既有 disposition；**不新增法典條文、不改 sealed codex**。
- 任何 host 設計（進程模型、IPC 契約、attestation 傳遞）屬架構決策，需經治理程序而非直接實作。
- 遷移期 Python 路徑一律保留為 fallback（bootstrap-entry 先例：三處備援）。

## 五、已知環境限制（2026-09-27）

- **本機 CPU 長期 100% 飽和，導致後端重生循環與計時性失敗**。同一根因的三個表象：
  1. **A330 standby handover 失敗**：`app:hot-reload-backend` 三次均於
     `standby-readiness-failed`/`standby-unhealthy-after-activation` 終止；standby
     於 +4.5s 內部就緒（綁定 8767）但其 `/health` 整個 45s 窗口
     accept-but-never-respond——event loop 被啟動爆發期同步工作餓死。
  2. **後端 respawn 循環**：supervisor kill 後端後，連續數個世代各自
     `main_runtime_ready`（最快 +4s）但 `/health` 持續失敗 ~9 分鐘 →
     dead-grace 擊殺 → 重生；直至負載短暫回落才有一代存活（12:52 世代
     於 13:01 通過 health）。
  3. **`force_close_tool` 活鏈路 6,715ms 逾 5s 預算**：進程全滅
     （`remaining_process_ids: []`）但每輪清掃需 2 次全表枚舉，飽和下
     各 ~3s；同代碼進程內實測 4,167ms 過關。快照已為兩階段
     （name-only 全表 + 僅匹配取詳情），再降成本須弱化驗證語意，不取。
- 修復層面在 `boot_core_handover`/`server_lifecycle`/啟動工作排程，
  屬跨工具範圍的主系統子系統議題，另案處理。非本次工具 host 變更引入。
