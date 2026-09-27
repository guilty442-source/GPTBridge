# 設計：受管 C# 工具 Host（`GPTBridge.ToolHost`）

> 目標：把 `migrate-csharp` 佇列中的「按需工具型」標的從 Python 受管 runtime
> 遷到 C#，作為後續 resident/channel host 的範式。
> 首個 workload：`system-rescue`（on-demand、headless、channel-only）。
> 協定依據：`governed-transport-proxy-v1.md`（規格已凍結）＋
> `star-governed-tool-runtime-abi/v1` §1–§4。
> E4 邊界不變：原生側**永不**持有 token 簽發能力、永不直連傳輸庫。

## 1. 部署形態

```
main-system ToolboxService.start_tool
  └─ spawn  <tool>/dist/<Tool>Host.exe          （runtime.native_entry 分支）
       ├─ GovernedEnvironment 驗證 env（fail-closed）
       ├─ spawn  python -B -s governance_rule/execution/tool_runtime/transport_proxy.py
       │        （P2 stdio sidecar；繼承受管 env；stdin EOF 即退）
       │        └─ SharedLayerChannel（token 發行/授權/傳輸庫 — Python 治理面）
       ├─ Worker loop：claim → executor → respond；request_cancelled 輪詢；
       │             notification_stamp 驅動喚醒
       └─ HttpListener 127.0.0.1:GPTBRIDGE_IPC_PORT
            ├─ GET /health   → health_snapshot（governance_ready/tool_id/instance）
            ├─ GET /metrics  → channel_health/queue/uptime
            ├─ /shutdown     → X-GPTBridge-Shutdown-Token HMAC 比對 → 關閉
            └─ WS /?token=&instance= → toolbox_cancel_tool_run（本地取消）
```

## 2. 環境契約（spawn 時由 `_source_runtime_environment` 注入）

| env | 用途 | host 驗證 |
| --- | --- | --- |
| `GPTBRIDGE_GOVERNANCE_PROJECT_ROOT` | 工作區根 | 必須存在且為目錄；fail-closed |
| `GPTBRIDGE_TOOL_DIR` | 工具根 | 必須在 root 內且 manifest.id 相符 |
| `GPTBRIDGE_GOVERNED_RUNTIME_TOOL_ID` | 受管身分 | 非空；作為 hello.tool_id |
| `GPTBRIDGE_IPC_SESSION_TOKEN` | WS 認證 | `^[a-f0-9]{64}$` |
| `GPTBRIDGE_IPC_PORT` | 監聽埠 | 1024–65535 |
| `GPTBRIDGE_SHUTDOWN_TOKEN` | 關閉權杖 | 非空時啟用 /shutdown |
| `GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP` | 治理引導 | **不讀取**——原樣傳給 sidecar |

`workspace_instance_id` = `sha256(normcase(root).replace("\\","/"))[:24]`
（對齊 `GovernedToolRuntime.workspace_instance_id` 與
`ToolboxService._workspace_instance_id`）。

## 3. Sidecar 協定（star-governed-transport-proxy/v1，P2 形態）

- spawn：`<root>/main-system/.venv/Scripts/python.exe -B -s <root>/governance_rule/execution/tool_runtime/transport_proxy.py`，
  cwd=tool root，stdin/stdout pipe（UTF-8 JSONL，單行 ≤2MiB），stderr→null。
- `hello`：`{"tool_id","workspace_instance_id","channels":{"system":"process"}}`。
  headless channel-only 工具只需 process 綁定（命令一律由 store claim 到達；
  main-system 的 `request_tool_execution` 走 `GovernedRequestClient` 直寫
  `gptbridge_transport.tool_request`，不經工具 WS）。
- process 側 op：`claim`/`respond`/`request_cancelled`/`progress`/
  `notify_for_request`/`notification_stamp`/`claim_pushed`/`acknowledge_push`。
- 回應以 `id` 對帳，允許 pipelining；`ok:false` → `ProxyError`（code 封閉集）。
- sidecar EOF/退出 → 所有未完成 op 視為 `PROXY_DISCONNECTED`，host 走
  fail-closed 關閉（不孤兒 claim——store lease 自然 reclaim）。

## 4. Worker 語意（對齊 `governed_runtime_worker.py`）

1. `claim`（system channel）→ request 或 null。
2. payload 非 dict 或缺 `_governed_command` → `respond` PERMISSION_DENIED 形狀。
3. 注入 `_governed_requester_actor`（claim 回傳的 `requester_actor`）。
4. executor 執行；每 100ms 輪詢 `request_cancelled`：命中 → 取消 executor、
   不回覆（Python `cancelled_during_execution → continue` 同義；requester 端
   `cancel_tool_execution` 已在 store 標記 cancelled）。
5. executor 回 `(event, result)` → `result["request_id"]=request_id` → `respond`。
6. 例外 → `respond` `{ok:false,error_code:"PERMISSION_DENIED",...}` 同形狀。
7. 空 claim → `notification_stamp` 輪詢喚醒（50ms 變動/250ms 靜默），
   idle backoff 0.25→0.5s；`channel_health` 以 `ok:false` 計失敗、
   `ok:true` 歸零（spec §6）。
8. `toolbox_run_local_cleanup`：actor != `governance/main-system` →
   PERMISSION_DENIED；相符時回 minimal `{ok:true, delegated:false}`——
   Python 版清掃語義保留在治理面，C# host 不實作檔案掃除（首輪範圍外，
   記錄於 health `local_cleanup.delegated`）。

## 5. HTTP/WS 伺服器（HttpListener + System.Net.WebSockets，無 NuGet 依賴）

- `/health`：`health_snapshot` — `ok/role/sovereign_id/authority/scope/duty/
  subordinate_to/version/tool_id/runtime_scope:"independent-tool"/
  governance_ready:true/workspace_instance_id/channels/channel_routes/
  channel_health` + executor `health()` 擴充欄位。
  `_check_source_runtime_ready` 只要求 `ok/governance_ready/tool_id/
  workspace_instance_id` — 全部提供。
- `/metrics`：channel_health、waiter 數、uptime、last_notification。
- `/shutdown`：`X-GPTBridge-Shutdown-Token` 與 env token HMAC compare →
  403 或觸發關閉 200。
- WS `/?token=&instance=`：token 需與 `GPTBRIDGE_IPC_SESSION_TOKEN` 一致
  （HMAC compare）、instance 與 workspace_instance_id 一致，否則 403。
  訊息 `{command,payload}`：`toolbox_cancel_tool_run` → 本地取消
  in-flight request 並回 `toolbox_cancel_tool_run_result`；其他命令 →
  `*_result` PERMISSION_DENIED（v1 不做 WS 入站再入列——submit 綁定屬
  後續 UI 工具階段，記錄於設計限制）。
- WS 實作：`HttpListenerContext.AcceptWebSocketAsync`，text frame JSON，
  close frame 優雅退出。

## 6. main-system 接入（最小 diff）

- manifest 新增選配鍵 `runtime.native_entry`（如
  `"dist/SystemRescue.Host.exe"`）。`runtime.type` 維持 `"python"`——
  Python 路徑永遠是降級路徑。
- `ToolPathResolver.resolve_special_unpacked_entry`：先嘗試
  `runtime.native_entry`（root 內、`.exe`、存在）；命中即回傳——
  spawn 層以副檔名分流。找不到 → 沿用 Python `.py` 驗證（fallback）。
- `_spawn_tool_process`：`source_entry.suffix == ".exe"` → 直接
  `create_subprocess_exec(exe, *args, env=source_environment)`（無 python
  前置參數），env/日誌/重試語意不變。
- `tool_process_registry.running_source_runtime_process_ids`：entry 為
  `.exe` 時放寬 process-name 白名單（比對 cmdline 含 entry 路徑即可），
  讓 `_external_runtime_active`/關閉掃描對原生進程同樣生效。
- `_source_launch_requested`/`use_source_runtime` 路徑不變（native entry
  屬 source-runtime 家族，status/journal/回退語意一致）。

## 7. 首個 workload：system-rescue

`SystemRescue.Host.exe`（`src-native/SystemRescue.Host.csproj` → publish 至
`dist/`）實作 `IGovernedCommandExecutor`：

- `system_health_check` → `system_health_check_result`
  `{ok:true,tool_id,authority:"main-system-central-packaging-only",resident:false}`
- `system_rescue_status` → `system_rescue_status_result`
  `{ok:true,tool_id,authority:"main-system",channels:["system"],
   audit_records_owner:true,runtime_logs_owner:true}`
- 其他 → PERMISSION_DENIED（同 Python executor）。
- `health()`：`{service_ready:true,resident:false,authority:...,tool_id}`。

Python `src/channel_runtime.py` 保留為降級路徑直到接線驗收完成。

## 8. 故障/逾時語意

| 情境 | 行為 |
| --- | --- |
| env 缺/不合法 | 啟動即 PERMISSION_DENIED 非零退出（fail-closed） |
| sidecar spawn 失敗 | 重試 2 次（同 Python spawn 語意），仍敗 → 退出非零 |
| hello 遭拒 | 立即退出（proxy 亦退出），main-system 見 SOURCE_RUNTIME_EXITED |
| sidecar EOF/崩潰 | 未完成 op → PROXY_DISCONNECTED；worker 關閉；進程退出 |
| claim/respond TRANSPORT_ERROR | channel_health 計失敗，退避後重試（不殺 worker） |
| /shutdown 合法權杖 | 設 shutdown_event：worker 排空、server 關閉、stdin 關閉讓 sidecar 退 |
| main-system 強殺 | 進程死亡→sidecar stdin EOF→sidecar 退；store lease 自然回收 |
| executor 逾時 | v1 由 store lease/deadline 管；executor 層不另設超時 |

## 9. 測試/稽核契約

- xunit：`GPTBridge.ToolHost.Tests` — env 驗證 fail-closed、JSONL codec、
  worker loop（假 sidecar：claim→execute→respond、cancel poll、
  PERMISSION_DENIED 包裝、健康計數）、HTTP /health//metrics//shutdown、
  WS 認證與 cancel 命令。
- Python contract test：`test_native_tool_host_spawn.py` 釘住
  resolver native 分支、spawn 分流、process-registry 比對放寬。
- audit：`check_tool_host_native_boundary` — C# 原始碼不得含
  token/HMAC 簽章原語（`HMACSHA`/`launcher_key`/`integrity_manifest`），
  `transport_proxy` ops 只走定義白名單，manifest `native_entry` 指向
  tool-root 內 `.exe`。
- 驗收：以假 proxy + 假 executor 的端到端 loop 測試，及（若環境允許）
  真 sidecar + 真 PostgreSQL transport 的 claim/respond smoke。

## 10. 已知限制（v1，後續階段）

- 無 WS 入站命令再入列（需 submit 綁定＋authorizer 註冊）——僅影響
  `has_custom_ui` 工具；headless 目標不受影響。
- `toolbox_run_local_cleanup`/`tool_self_repair` 委派回 Python 治理面
  或不實作（headless 目標 manifest 可自行關閉）。
- AI channel（submit/process）與 push 語義已於 host 庫支援但未啟用。
- resident 型 host（git_tiers/常駐服務）共享同一 ToolHost 庫，
  但生命周期由 supervisor 而非 WS/HTTP 邊界持有——屬下一設計。
