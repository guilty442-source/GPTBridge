# `star-governed-tool-runtime-abi/v1` — 受管工具執行面協定規格

> 用途：P1 執行模組語言外移（`module-language-migration-order-20260922.md`）M1/M2
> 施工前置。凡以 C/C++/C# 重寫的 governed tool 執行面，必須逐項滿足本規格；parity
> 測試以本文件為判據清單。來源：`governance_rule/execution/tool_runtime/`
> （`governed_runtime.py`＋`governed_runtime_worker.py`＋
> `governed_runtime_maintenance.py`＋`governed_runtime_constants.py`）與
> `shared-layer/src/shared_layer/channel.py`／`store.py`。
>
> 邊界（§10.65 不變式）：fail-closed 判定、法典效力、稽核決策留 Python 治理主宰；
> 本規格描述的是**執行語義**——一個以他語言實作的工具行程如何對外呈現與
> Python `GovernedToolRuntime` 完全相同的線上行為。

## 0. 狀態

- 版本：`star-governed-tool-runtime-abi/v1`（2026-09-22 首版）
- 狀態：**規格草稿待核定**；第 5 節（transport token ABI）含一項必須先裁決的
  治理依賴（`issue_token` 演算法面），未裁決前 C++ 工具只能走「Python
  transport 代理」模式（§7 模式 B）。

## 1. 行程啟動契約（process bootstrap）

工具行程由 `ToolboxService.start_tool` 以受管方式啟動，以下環境變數為
**全部必要條件**，缺一/不合法 → `PERMISSION_DENIED` 立即退出：

| 變數 | 語義 | 校驗 |
| --- | --- | --- |
| `GPTBRIDGE_GOVERNANCE_PROJECT_ROOT` | 專案根 | resolve 後必須為 `E:/GPTBridge`；`tool_root` 必須在其下 |
| `GPTBRIDGE_TOOL_DIR` | 工具根 | resolve 後 `manifest.json` 存在且 `manifest.id == tool_id`；或命中 sealed identity bound-root／manifest-binding 規則 |
| `GPTBRIDGE_IPC_SESSION_TOKEN` | WS 閘門 token | `^[a-f0-9]{64}$`（lowercase hex，比對前 `.lower()`） |
| `GPTBRIDGE_IPC_PORT` | WS/HTTP 監聽埠 | int，1024–65535 |
| `GPTBRIDGE_SHUTDOWN_TOKEN` | `/shutdown` HMAC token | 可空字串→`/shutdown` 恆 403 |
| `GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP` | base64(JSON) 啟動包 | `format_version==1`，含 `launcher_key`(b64)、`integrity_manifest`、`identity_attestation`；讀取後自 env **pop**（不殘留） |

- `tool_id` 正則：`^[a-z0-9][a-z0-9_-]{1,63}$`。
- `workspace_instance_id`（服務端實效版本，maintenance mixin 覆寫後勝出）：
  `sha256("{tool_id}:{port}")[:16]`。
  ⚠️ 已知不一致：`governed_runtime.py` 另有一份 `sha256(normcase(root))[:24]`
  定義被 mixin 覆寫而**永不生效**——規格以 mixin 版為準，Python 側死碼列清理候選。

## 2. HTTP 閘門（`process_request`，先於 WS upgrade）

| 路徑 | 行為 |
| --- | --- |
| `/health` | 200 `application/json`：`health_snapshot()`（schema 見 §6），**不需 token** |
| `/metrics` | 200 JSON：`_collect_channel_metrics()`，不需 token |
| `/shutdown` | `X-GPTBridge-Shutdown-Token` 與 env token `hmac.compare_digest` 相等 → set `shutdown_event`、200 `OK`；否則/空 token → 403 `Forbidden` |
| 其他（WS upgrade） | query `token`（lowercase 後 compare_digest vs session token）**且** `instance == workspace_instance_id()` → `None`（放行 upgrade）；否則 403 |

- `origins=(None, "file://", "null")`；僅綁 `127.0.0.1`。

## 3. WebSocket 命令協定（`_drain_messages`）

訊息皆為 JSON `{command, payload}`（入）與 `{event, payload}`（出）。

### 3.1 入站命令

| command | 處理 |
| --- | --- |
| `toolbox_cancel_tool_run` | `channel.cancel(tool_id, request_id)`（執行緒池）∨ 工具自備 `cancellation(request_id)` → 回 `toolbox_cancel_tool_run_result {ok, cancelled, tool_id, request_id}` |
| 其他 | 前置校驗：`command` 非空、`payload` 為 dict、`payload.request_id` 非空且 ≤256、`payload.tool_id`(缺省=自身) == 自身 `tool_id` → 失敗一律 `{command}_result {ok:false, error_code/message:"PERMISSION_DENIED"}` |

### 3.2 受理流程（合法命令）

1. `queued_payload = payload ∪ {"_governed_command": command}`；`waiters[request_id] = ws`
2. `channel.request(tool_id, request_id, queued_payload)`（submit 入傳輸層）
3. 回 `COMMAND_RECEIVED {command, status:"processing"}`
4. 後續由 worker（§4）執行並透過同一 ws 推送 `{command}_result {result ∪ {request_id}}`

### 3.3 錯誤語義

任何例外 → `{"event": f"{command}_result"|"error", "payload": {ok:false, tool_id, request_id, error_code:"PERMISSION_DENIED", message:"PERMISSION_DENIED"}}`。
連線中斷（`ConnectionClosed`）為生命週期事件非故障。

## 4. Worker／佇列語義（`_worker`）

- 對每個 processing channel（預設 `["system"]`；`channel_modes` 可增 `ai` 於 submit/process 兩模式，ai+process 才入 processing）依序 `channel.claim()`：
  - 傳輸層語義（store）：`FOR UPDATE SKIP LOCKED` 取 `status='queued'` 且
    `next_retry_at<=now()`、`deadline_at>now()`，`ORDER BY priority_value, created_at, request_id LIMIT 1`；
    claim 後 `status='claimed'`、`lease_until=now()+300s`、`attempt_count+1`，
    同事務先 `reclaim_expired`（lease 過期重排回 queued）。
- claim 失敗（例外）→ `_record_channel_health(ok=false)`、`sleep 0.5` 重試（不死迴圈）。
- 無 request：通知佇列等待 `max(idle_poll,0.05)`（有通知時 0.05）；逾時 →
  `idle_poll = min(idle_poll×1.5, 0.5)`（初值 0.25）；拿到 request → 重置 0.25。
- 通知加速：PG `pg_notify('tool_request_<channel>')` listener；本地 degraded
  transport 以 `notification_stamp()`（檔案 mtime 對）50ms/250ms 輪詢；
  JSON payload 的 `request_id` 記入 `_notified_request_ids`（界 128，溢出清空）。
- 執行：`payload.pop("_governed_command")` → `command`；`payload["_governed_requester_actor"]=request.requester_actor`；
  - `command == "toolbox_run_local_cleanup"`：僅 `requester_actor == "governance/main-system"` 放行 → `_run_local_cleanup()`（工具本地維護，§8 delegated）。
  - 否則 `executor(command, payload, request_id)`；**執行中每 100ms** 輪詢
    `channel.request_cancelled(request_id)` → 命中即呼叫工具 `cancellation(request_id)`、
    `execution_task.cancel()`、彈出 waiter、`continue`（**不 respond**）。
- respond：`channel.respond(request_id, result)`（`status='completed'`+response，僅
  `status='claimed'` 才成功）；成功 → `channel_health(ok=true)`；失敗 → 紀錄 fail 並
  以 PERMISSION_DENIED 形式取代 result 後仍嘗試 emit waiter。

## 5. Transport token ABI（**待裁決項**）

每次 channel 操作先 `authentication.issue_token(...)` 取得一次性 token：

| 操作 | capability | action | target_tool_id | target/data_scope/resource_path |
| --- | --- | --- | --- | --- |
| request/cancel/response/push（submit 側） | `{ch}-channel-request-submit` | request / cancel-request / consume-response | 對方 tool_id | `shared-layer-{ch}-request:{op_tool}` / `shared-layer-{ch}-request` / 通道 DB 路徑 |
| claim/respond/cancelled/progress/notify（process 側） | `{ch}-channel-request-process` | claim / respond | None（自身） | 同上 |

- `op_tool = target_tool_id or 自身`；`data_scope`/`resource_path` 恆如上。
- 通道：`{"system","ai"}`；`tool_id=="main-system"` 拒絕。
- **待裁決**：`issue_token` 之演算法（launcher_key HMAC＋integrity manifest 綁定）
  屬治理認證面——C++ 工具直接實作即觸及 E4 邊界。選項：
  a) token 發行維持 Python：C++ 工具經由本機 IPC stub 向主系統請領 token（transport 代理模式，§7-B）；
  b) 將 `issue_token` 定為穩定 ABI（演算法＋輸入逐項凍結）供 C++ 實作——需治理核定後才可施工。
- 傳輸層：central PG（`gptbridge_transport.tool_request`，RLS 身分綁定）或
  本地 degraded SQLite store（`LocalSharedLayerStore`，含 `notification_stamp`）。
  C++ 工具不應直連傳輸庫——建議一律走 §7-B 代理，傳輸層維持 Python 管有。
  **代理線協定已定版**：`star-governed-transport-proxy/v1`
  （`convergence/governed-transport-proxy-v1.md`，2026-09-22）——
  操作集、JSONL envelope、hello 綁定、錯誤碼閉集、P1 常駐代理／P2 stdio
  sidecar 兩部署形態、shadow parity 清單。

## 6. 健康快照 schema（`/health`）

固定欄位：`ok:true, role:"governed-tool-runtime", sovereign_id,
authority:"information-management-delivery-channels-and-channel-health-and-automatic-cleanup-repair-backup",
scope:"all-owned-channel-delivery-health-and-local-maintenance-duties",
duty:[5 項], subordinate_to:["system","maintenance"], version, tool_id,
runtime_scope:"independent-tool", governance_ready:true,
workspace_instance_id, channels:[sorted], channel_routes:{ch:"{ch}-channel/{tool}"},
channel_health:{ch:{last_ok,last_request_at,consecutive_failures,degraded}},
_self_repair(啟用時), _local_cleanup(啟用時)`＋工具 `health_callback()` 注入欄位。

channel_health：`consecutive_failures>=3 → degraded:true`；成功歸零。

## 7. 遷移模式

- **模式 A（全 C++）**：本規格 §1–§6 全實作＋`issue_token` ABI 核定後直接實作。
  適 M2 `information-channel-gateway` 完成之後。
- **模式 B（C++ 工具體＋Python transport 代理）**：C++ 行程實作 §1–§4（HTTP/WS/命令/
  佇列），channel claim/respond/cancel/token 經受管本機 IPC 向主系統 Python 代理
  請求——不觸 token ABI 即可 shadow。**建議 M1 `system-rescue` 採用**。
- 雙軌制不變式照 §10.65：shadow 並行比對 → primary（≥2 release 觀察窗）→ retire；
  每模組一個 runtime flag；fail-closed 回 Python 不刪路徑。

## 8. 工具本地維持 delegated 的語義（不在本期 ABI）

- `_run_local_self_repair`（tool-local DB 修復）／`_run_local_cleanup`＋
  `dead_code_scan`／`idle_cleanup`：工具本地維護鉤子，C++ 版以 IPC 回 Python
  維護宿主或暫標 delegated，非線上語義阻斷項。
- `GovernedCliExecutor`（子行程沙盒 executor）：另屬 CLI 執行器面，獨立規格。

## 9. Parity 驗收清單（shadow 必驗）

1. 缺任一 env／token 格式錯／port 越界／bootstrap 壞 → 立即退出碼≠0（PERMISSION_DENIED 路徑）。
2. `/health` 快照欄位與 Python 逐鍵一致（動態值除外）。
3. WS 閘門：錯 token/錯 instance → 403；正確 → upgrade 成功。
4. `/shutdown` 錯 token → 403；對 → 200 且行程退出（shutdown_event 傳導至所有迴圈）。
5. 合法命令 → `COMMAND_RECEIVED` → `{cmd}_result` 同 request_id；payload 異形 → PERMISSION_DENIED 形式。
6. `toolbox_cancel_tool_run` 對執行中 request：100ms 輪詢窗內命中→executor 取消、
   **不產生 respond**、waiter 無 `_result`。
7. 佇列空轉 backoff 序列：0.25→0.375→0.5 封頂；notify 命中→即刻 0.05 重取。
8. claim 傳輸語義：同 request 不被兩 worker 同時取走（SKIP LOCKED）；lease 300s；
   過期 reclaim 回 queued。
9. respond 僅於 `status='claimed'` 成功；重複 respond 為 false。
10. channel_health：連續 3 次 claim 失敗 → degraded；成功歸零。
11. `toolbox_run_local_cleanup`：非 `governance/main-system` actor → PERMISSION_DENIED。
12. ai channel：僅 `channel_modes{"ai":"process"}` 入 processing；非法 channel/mode → PERMISSION_DENIED。
