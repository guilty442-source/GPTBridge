# star-governed-transport-proxy/v1 — 受管傳輸代理協定（M1/M2 前置）

> 定位：`star-governed-tool-runtime-abi/v1` §5/§7-B 的實作協定。
> 原生執行模組（C/C++/C#）不持有 token 發行能力、不直連傳輸庫；
> 一切 channel 操作經由本協定向 **Python 傳輸代理**請求，代理以
> `SharedLayerChannel` 代為執行。token 發行、路由授權、傳輸庫存取
> 全部留在 Python 治理面（E4 邊界不變）。
> 狀態：**規格凍結供 shadow 施工**；尚未有任何 primary 實作。

## 1. 部署形態（兩形態同協定）

| 形態 | 說明 | 適用 |
| --- | --- | --- |
| **P1 主系統常駐代理**（目標） | 主系統 Python 常駐行程內的代理端點，為全部原生工具服務；工具經 loopback 連線，以 `hello` 綁定身分 | 正式部署；不新增每工具 Python 行程 |
| **P2 per-tool stdio sidecar**（過渡／開發） | 原生工具 spawn `python -m governance_rule.execution.tool_runtime.transport_proxy` 子行程，stdio JSONL；代理繼承工具行程的受管 env | shadow 開發、主系統代理未上線前 |

兩形態**線協定完全相同**（§3），僅連線建立與身分綁定方式不同（§4）。
sidecar 為按需行程——隨工具啟動、stdin EOF 即退出，不構成常駐 Python。

## 2. 操作集（對映 `SharedLayerChannel`）

| op | 對映方法 | 說明 | 側 |
| --- | --- | --- | --- |
| `hello` | — | 綁定工具身分（tool_id/instance/通道清單）；首個必須、其它 op 之前 | 控制 |
| `ping` | — | 活性探測；回 `{"pong":true}` | 控制 |
| `claim` | `claim(channel, timeout, lease_seconds)` | 取待處理請求（SKIP-LOCKED／lease 語義在 store，代理只轉發） | process |
| `respond` | `respond(request_id, result)` | 回覆已 claim 請求；僅 `claimed` 態成功 | process |
| `request_cancelled` | `request_cancelled(request_id)` | 輪詢取消旗標（100ms 窗由呼叫方實作） | process |
| `notify_for_request` | `notify_for_request(request_id)` | 執行中進度/事件通知 | process |
| `notification_stamp` | `notification_stamp(channel)` | 本地 store 的通知戳（無 LISTEN/NOTIFY 時輪詢用） | process |
| `claim_pushed` | `claim_pushed(request_id, worker_id)` | push 模式下認領推送請求 | process |
| `acknowledge_push` | `acknowledge_push(request_id, worker_id)` | push 確認 | process |
| `request` | `request(target_tool_id, command, payload, timeout_seconds)` | submit 側送請求；回 `request_id`，**不阻塞等 result** | submit |
| `response` | `response(request_id, timeout_seconds)` | 取回覆（代理內可阻塞至 timeout；原生側以短輪詢亦可） | submit |
| `cancel` | `cancel(request_id)` | submit 側取消 | submit |

- 代理**不得**新增語義：租約、重取、重試、reclaim、actor 授權檢查
  一律沿用 `SharedLayerChannel`／store 現行行為。
- 每個 op 的 token 發行、capability/action/data_scope 逐項由代理內部
  依 ABI §5 表執行——**原生側永遠不見 token**。

## 3. 線協定（JSONL，UTF-8，一行一訊息）

請求：

```json
{"v":1,"id":"<uuid4>","op":"claim","args":{"channel":"system","timeout":0.5,"lease_seconds":300}}
```

回應：

```json
{"v":1,"id":"<uuid4>","ok":true,"result":{...}}
{"v":1,"id":"<uuid4>","ok":false,"error":{"code":"PERMISSION_DENIED","message":"..."}}
```

- `v` 恆為 `1`；非整數或缺 `id`/`op` → 連線層丟棄並記錄（fail-closed 不回應）。
- `id` 為呼叫方產生之相關性 id；代理原樣回帶。原生側以 `id` 多工對帳。
- 單連線可併發多個未完成請求（pipelining）；代理須以 request `id` 對帳，
  不得假設 FIFO 完成順序。
- 訊息大小上限：單行 ≤ `MAX_OUTPUT_CHARACTERS`（2 MiB，同 runtime 上限）。
- 連線中斷：所有未完成請求視為失敗（`PROXY_DISCONNECTED`），
  原生側不得假設已送達；已 claim 之 request 的 lease 由 store 自然過期 reclaim。

### hello

```json
{"v":1,"id":"...","op":"hello","args":{
  "tool_id":"system-rescue",
  "workspace_instance_id":"<instance>",
  "channels":{"system":"process","ai":"submit"},
  "bootstrap":{ ...GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP 內容... }
}}
→ {"ok":true,"result":{"agent":"star-governed-transport-proxy","v":1,
   "channels":{"system":"process","ai":"submit"}}}
```

- P1（常駐代理）：連線為 loopback TCP／具名管道；`hello` 前連線無權限，
  hello 驗證 `tool_id` 存在於工具註冊表、`workspace_instance_id` 與
  註冊 instance 一致、bootstrap 必要欄位齊全——任一不符 →
  `PERMISSION_DENIED` 並關閉連線。
- P2（stdio sidecar）：行程擁有關係即信任邊界；仍須 hello 以建立
  代理內的 channel 例項；hello 失敗 → 代理立即以非零碼退出。
- `channels` 宣告對齊 ABI：`process`=入站 claim、`submit`=出站 request。
  代理依宣告建構對應 `SharedLayerChannel`；未宣告的通道 op →
  `CHANNEL_NOT_BOUND`。
- bootstrap 內容：P1 由工具於 hello 傳入（loopback＋instance 綁定）；
  P2 由 sidecar 自繼承 env 讀取，`hello.args.bootstrap` 可省略。

## 4. 錯誤碼（`error.code` 列舉，封閉集）

| code | 語義 |
| --- | --- |
| `PERMISSION_DENIED` | 身分／路由／capability 授權失敗（代理轉發治理判定，原樣不透譯） |
| `CHANNEL_NOT_BOUND` | 對未宣告通道執行 op，或通道 mode 不符（submit 通道收 claim 等） |
| `REQUEST_NOT_FOUND` | respond/response/cancel 指向不存在或已終態的 request_id |
| `NOT_CLAIMED` | respond 時 request 非 `claimed` 態（含重複 respond → ok:false） |
| `TIMEOUT` | claim/response 於 timeout 內無結果（正常控制流，非故障） |
| `TRANSPORT_ERROR` | 傳輸庫例外；`message` 帶摘要，**不帶** SQL/內部路徑 |
| `PROXY_SHUTDOWN` | 代理關閉中；原生側應停止新請求並走自身關閉流程 |
| `PROXY_DISCONNECTED` | 連線中斷後原生側對未完成請求的本地合成錯誤（非代理發出） |
| `BAD_ENVELOPE` | 協定違規（版本/欄位/大小）；連線層亦可能直接靜默丟棄 |

fail-closed 規則：原生側收到 `ok:false`、非預期 JSON、或連線中斷時，
**一律不得自行重試超過 store 語義**——claim 迴圈的重試/lease 由
store 決定，代理與原生側只負責如實轉發與如實失敗。

## 5. 提交側語義補充

- `request` 回 `{request_id, queued:true}`；回覆以 `response(request_id,
  timeout)` 取得。原生側可自行選擇阻塞等（代理內阻塞）或短輪詢。
- `response` 逾時回 `TIMEOUT`（request 仍存活，可再問）；request 被
  對方 `PERMISSION_DENIED` 時回覆為 `{"ok":false,"error_code":...}`
  的正常 result——**不屬於 `error.code` 層**（兩層錯誤不混淆：
  transport 層 vs 對端業務層）。
- `GovernedRequestClient` 之路由授權（如
  `authorize_investment_mobile_route`）由代理於 `request` 時執行——
  原生側不實作路由政策，送錯路由 → `PERMISSION_DENIED`。

## 6. 與 runtime ABI 的接縫

- 原生工具體仍依 `star-governed-tool-runtime-abi/v1` §1–§4 實作
  HTTP/WS/命令/佇列；佇列 worker 的「取件／回覆／取消查詢／通知」
  改經本代理 op 取代直接呼叫 `SharedLayerChannel`。
- 代理只代行 §5 表的 token＋傳輸動作；命令 envelope、`COMMAND_RECEIVED`、
  `PERMISSION_DENIED` 包裝、心跳、lease 觀測全在原生側，parity 清單
  （ABI §9）逐項不變。
- 健康快照 `channel_health.consecutive_failures`：原生側以代理回應
  `ok:false`（`TRANSPORT_ERROR`/`PROXY_*`）計失敗、`ok:true` 歸零——
  與 Python 版 channel 例外計數同義。

## 7. Parity 驗收（shadow 必驗，疊加於 ABI §9）

1. hello 綁定：錯 instance／未註冊 tool_id → `PERMISSION_DENIED`＋連線關閉。
2. claim/respond 與 Python `SharedLayerChannel` 同事實集同結果
   （含 lease 過期 reclaim、重複 respond → `NOT_CLAIMED`）。
3. submit 側：`request`+`response` 對 live xingcheng stub 端到端；
   `TIMEOUT` 後同 request 仍可取。
4. 連線中斷注入：未完成請求全部 `PROXY_DISCONNECTED`，原生側
   `channel_health` 計失敗，重連後 hello 重新綁定可續。
5. 代理死亡（P2）：stdin EOF／行程退出 → 原生工具走 fail-closed
   關閉路徑，不孤兒 claim。
6. `BAD_ENVELOPE`／大小上限／非 JSON 行：丟棄不崩潰，連線保持。
