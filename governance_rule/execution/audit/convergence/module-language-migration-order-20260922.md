# 執行模組語言外移順序（P1 §1.1 細項① 收尾）

> 清單：`python-execution-modules-20260922.json`（19 模組：7 stay-Python＋12 migrate）
> 本文件補齊欠缺的「外移順序」；依雙軌制（§10.65：shadow→primary→retire，每元件 runtime flag、
> parity 測試、fail-closed 回 Python）逐模組執行，不得跳階段。
> 排序原則：①爆炸半徑由小到大（standalone → shared → in-process → gate-critical → entry）
> ②依賴閘控（E3/E4 未完成前不碰啟動面）③契約可驗證性（A263/logits/檔案語意有明確 parity
> 判據者優先於契約模糊者）。

| 階段 | 模組（清單 component_id） | 實效規模（含依賴閉包） | 目標語言 | 排序理由 |
| --- | --- | --- | --- | --- |
| M1 | `system-rescue` | 10 files / 581 LOC | C++ | 獨立工具、按需啟動、零常駐影響——最安全的雙軌制試點 |
| M1 | `investment-mobile` | 19 files / 706 LOC | C# | 同上；業務型工具適 C# 承接 |
| M2 | `information-channel-gateway`（channel_runtime + mixins 閉包） | 6 files / 1083 LOC | C++ | A263 契約明確（heartbeat/outbox/cursor/無重複副作用），parity 判據可完整列舉；但全通道共用、爆炸半徑大 → 列 M2 非 M1 |
| M2 | `tokenizer` | 207 LOC wrapper（實效＝jieba 分詞引擎重實作） | C++ | 契約模糊（jieba dict/HMM/DAG 語意）——先產出分詞語意規格＋parity 語料庫才准施工 |
| M3 | `xingcheng-auto-repair-module`（central_repair + repair_* 閉包） | 15 files / 3568 LOC | C# | 修復執行屬自動化核心語域；治理判定仍留 Python（action allowlist 不變） |
| M3 | `xingcheng-auto-learning-module` | 1 entry / 682 LOC | C# | 同上（self-learning 觸發與執行面） |
| M3 | `language-review` | 23 files / 3145 LOC | C# | sub-sovereign 內部執行面；待主宰邊界穩定後遷移 |
| M4 | `self-commit-service`＋`integration-plane`＋`recovery-plane`（git_tiers 共源） | 72 files / 20238 LOC（三者同目錄共源） | C++ | commit gate 關鍵路徑＋治理鄰接——風險最高之一，列 M4；三者共源須作為單一遷移單元處理，不得分批 |
| M5 | `bootstrap-entry` | 6 files / 1088 LOC | C# | 行程入口——待 §10.65 E3（啟動面）完成後才可 shadow |
| M5 | `boot-core` | 418 files / 86045 LOC | C/C++/C#（依單元制映射） | 最後階段；受 §10.65 E4／C Sovereign 成熟度閘控，且需先完成單元制物理搬移才有可映射的遷移單元 |

## 各階段放行條件

- **M1→M2**：至少一模組完成 shadow→primary 觀察窗（≥1 個 release 週期）且 parity 零差異，證明雙軌機械可用。
- **M2→M3**：channel-runtime 的 A263 parity 套件（heartbeat deadline／outbox 不丟未確認事件／cursor 收斂）全綠。
- **M3→M4**：修復鏈在 C# 宿主下 action allowlist 與 fail-closed 語意逐項比對通過。
- **M4→M5**：git_tiers 遷移後連續 10 次受管 commit gate 全綠且 ≤30 s（A537 不迴歸）。
- **M5 內部**：boot-core 依單元制 unit 為單位分批，每批對應 codex `architecture_activation_states` 受管更新。

## 不變式

- Python 路徑在 retire 前不刪（§10.65）；每模組單一能力單一職責（混責先拆）；
- 按需使用不常駐：遷移後模組須驗證載入／卸載＋資源釋放；
- 整體淨占用不得因遷移上升（Python −100 MB 不得換他處 +150 MB）。

## 補充（2026-09-22）：M1 實效閉包修正＋ABI 規格

- 盤點 `system-rescue` 實效閉包後發現：其自身僅 87 LOC shim，**真正執行面為
  `GovernedToolRuntime` 框架**（worker/claim/WS 閘門/HTTP/健康快照，~1173 LOC）
  ——M1 的施工標的因此是「受管工具執行面協定」而非該 shim。
- 前置規格已產出：`governed-tool-runtime-abi-v1.md`（`star-governed-tool-runtime-abi/v1`），
  凍結 §1–§6 線上語義＋12 項 shadow parity 判據；`issue_token` 屬治理認證面，
  **未核定前 M1 採模式 B**（C++ 工具體＋Python transport 代理，不觸 token ABI）。
- `investment-mobile`（M1 C#）同受此規格約束；兩者共用同一 governed runtime 協定。

## 進度（2026-09-22）：M1 `system-rescue` shadow 原型落地

- `native/core/system_rescue.c`＋`include/system_rescue.h`：platform_packager
  決策自由語義——`_verify_tool_package` 判定樹（MISSING→METADATA_MISSING→
  METADATA_INVALID→STALE→OK）、`_normalize_packager_report` 錯誤碼映射、
  `verify_packaged_tool` sidecar 判定（含 UNREADABLE）、`verify_all` 聚合、
  `_main` CLI 路由階梯、FIPS-180-4 SHA-256（hashlib 等值）。
- `native/core/governed_tool.c`＋`include/governed_tool.h`：
  `star-governed-tool-runtime-abi/v1` 判定子集（§9 parity 清單之純語義項）——
  env/tool_id/token/port 校驗、`workspace_instance_id`（sha256[:16] mixin 版）、
  `/shutdown`·WS 閘門（compare_digest 語義）、命令前置校驗、idle_poll 演化
  ＋wait_timeout、channel_health ≥3 degraded；§5 token ABI 未觸（模式 B）。
- 綁定：`sr_*`＋`gt_*` 薄綁定入 `_sovereign_native`；canonical build 註冊。
- 證據：native suites `system_rescue_suite` 6＋`governed_tool_suite` 10 全
  PASS；`main-system/tests/test_native_m1_shadow.py` 13 測試（Python 權威
  函式 vs C 逐項比對；`.pyd` 受 INT-10 鎖定期間 graceful skip，Python 側
  判定已對暫存樹實測）。`.pyd` rebuild 待 INT-10 窗口後執行。
- ~~收斂待辦~~ → **已收斂（2026-09-22）**：HTTP/WS 閘門雙實作擇一——
  保留 `governed_tool_ws.h/.cpp` 為主線，併入 http_gate/ws_codec 側的
  增量語義（`ws_validate_upgrade` 握手驗證含 Origin 允列／Version 13／
  Key 16-byte、`ws_pong`/`ws_close` 便捷封包、非最小長度編碼拒絕、
  1 MiB max_size 對齊 websockets 預設、declared-but-undelivered 回
  retry 非協定錯），suite 擴至 8/8 PASS；`http_gate.h`/`ws_codec.h`/
  `http_gate.cpp`/`ws_codec.cpp`/`suite_ws_http_gate.cpp` 退役刪除。
  parse_qs blank-drop parity（`token=&token=v` → "v"）併入時於
  `http_gate.cpp` 側修補後隨退役檔一併帶走——`route_request` 的
  query 抽取沿用 governed_tool_ws 本有的 blank-drop 語義，無回歸。
- 傳輸代理協定已定版：`star-governed-transport-proxy/v1`
  （`convergence/governed-transport-proxy-v1.md`）——模式 B 的線協定：
  JSONL 操作集（claim/respond/cancel/notify/request/response/hello）、
  錯誤碼閉集、P1 主系統常駐代理（目標）／P2 per-tool stdio sidecar
  （過渡）兩部署形態、6 項代理層 parity 判據；token 發行與傳輸庫
  仍 Python 管有（E4 邊界不變）。
- 傳輸代理兩側已落地（同日）：Python 端
  `governance_rule/execution/tool_runtime/transport_proxy.py`
  （`TransportProxyAgent`；P2 stdio sidecar＋`python -m` 入口、
  channel factory／authorizer 可注入、`shared-layer/tests/
  test_transport_proxy.py`）；原生端 `native/tool_runtime/
  transport_proxy_client.cpp`（零 I/O codec——encode/decode＋args
  builders＋`RequestWaiter` request_sync parity；`tool_runtime` 層已於
  `build_native.py` 宣告）。stdio/process 接線屬工具宿主職責。
- 線協定 interop 證據：`native/test_suites/driver_proxy_client.cpp`
  （codec CLI＋`sidecar` 模式）＋`proxy_wire_agent.py`（真實
  `TransportProxyAgent`＋echo-recording fake channels）＋
  `main-system/tests/test_native_proxy_wire.py`——C++ encode→真實
  agent→C++ decode 逐 op 驗證：hello 綁定、全部 process/submit
  操作、`_governed_command` 注入、`RequestWaiter` Completed＋
  request_id 剝離、錯誤碼閉集（CHANNEL_NOT_BOUND/BAD_ENVELOPE/
  PERMISSION_DENIED/未 hello）、丟棄語義。
- P2 sidecar 接線層已落地（同日）：`native/tool_runtime/
  sidecar_transport.cpp`（`ProxySidecar`——CreateProcess＋匿名管道、
  id 對帳同步 `call()`、讀取期限、`PROXY_DISCONNECTED/SPAWN_FAILED/
  TIMEOUT`；Windows-only，非 Windows fail-closed）；driver `sidecar`
  模式以真實 spawn 子行程端到端驗證，wire 測試 11/11 PASS。
- **C++ 工具體骨架已落地**（同日）：`native/include/tool_host.h`＋
  `tool_runtime/tool_host.cpp`（`ToolHost`，winsock2、Windows-only、
  非 Windows fail-closed）——組合 governed_tool C 判定＋
  governed_tool_ws 閘門＋transport_proxy_client codec＋
  `ProxySidecar`/注入式 `ProxyCallFn` 成可執行行程：§1 env 載入
  （含 manifest.id 比對、tool_root 深度規則、bootstrap pop-after-read、
  wsid 計算）、§2 僅 127.0.0.1 監聽＋route 階梯＋握手驗證、§3 命令
  受理（tool_id `or` 語義 falsy→self/truthy 異形→DENIED、waiter 先
  登記再 submit、COMMAND_RECEIVED、訊息級 frame 重組）、§4
  claim→execute→respond 迴圈（idle backoff、100ms cancel 輪詢→旗標
  ＋cancellation→不 respond、channel_health degraded、
  `waiters[request_id]` 結果回推）；submit 側走獨立
  `proxy_submit_call`/第二 sidecar 綁定（submit actor/authorizer）。
  `suite_tool_host.cpp` **3/3 PASS**：真實 loopback socket 端到端——
  /health/metrics/shutdown 閘門、WS upgrade 正負路徑、命令→
  COMMAND_RECEIVED→claim→execute→respond→waiter `_result` 推送、
  異形 DENIED、cancel 執行中旗標傳遞不回應（13d00404 修 WS 結果
  投遞＋/shutdown 關閉時 accept 解除阻塞）。
- 未做（M1 殘項）：shadow→primary 觀察窗、parity 零差異證據、
  runtime flag、load/unload 資源釋放驗收。
- 已補（同日）：live P2 Python sidecar smoke（`python -m
  …transport_proxy` 真實子行程 ping/hello fail-closed 端到端，
  `suite_tool_host.cpp::live_p2_sidecar_smoke`）；通知喚醒路徑
  （`claim_loop` 空轉期以 `notification_stamp` 探針 250ms 輪詢
  process 通道——寫入戳變化即中斷退避即刻重取，對齊 Python
  `_listen_for_notifications`；`notify_stamp_wake` 測試 PASS）。

- M1 `investment-mobile` C# shadow（同日補）：`native/test_suites/csharp_investment`
  （net10.0）重實作決策自由語義——`InvestmentMobileService.owns/handle/_status`、
  requester 允列、`ChannelClient`（snapshot/submit_instruction 填充/send 路由/
  未連線錯誤）、`MarketDataClient`/`DatabaseClient`/`ExternalAPIClient` 斷線與
  通道包裝、use-cases、presenters；`InvestmentMobileShadow.exe` 輸出 51 案例
  JSON 矩陣，`test_native_m1_investment_shadow.py` 8 測試以真實 Python 函式
  ＋同型 stub 逐鍵比對全 PASS。transport/token/網路/DB 仍留 Python（模式 B）。

## 進度（2026-09-22）：M2 `information-channel-gateway` 決定性核心 C 原型

- `native/core/a263_channel_core.c`＋`include/a263_channel_core.h`：
  `channel_runtime.py`＋`connection_mixin.py`＋`heartbeat_mixin.py`＋
  `transactional_outbox.py` 的決定性（零 I/O）語義——ChannelState 名稱、
  generation 遞增、reconnect 上限→DEAD＋指數退避、heartbeat deadline
  （嚴格大於）、ack cursor 單調、resync 雙游標收斂、outbox fetch_after
  視窗與 sequence/idempotency_key、backpressure、message batch 依
  priority 穩定排序（Python sort 穩定語義）、snapshot cursor>=0。
- `native/test_suites/suite_a263_channel_core.cpp`：11 案全 PASS；
  Python 側等值語義 25/25 逐項比對（shadow parity）。
- transport／WebSocket／SQLite store／token 簽發仍 Python 管有
  （A177/E4 邊界不變）；本核心只覆蓋協定中的純語義層。
- 已補（同日）：A263 parity 放行套件——`suite_a263_channel_core.cpp`
  增三項情境測試並輸出 `a263_parity_matrix.json`（heartbeat deadline
  sweep 8 列、outbox append→限窗取送→resync replay 不丟未確認、ack
  亂序單調＋resync 雙游標收斂）；`main-system/tests/
  test_native_m2_a263_parity.py` 以真實 Python 物件
  （`TransactionalOutbox`、bare `A263Channel._handle_control/
  _handle_resync`、`HeartbeatMixin` 實例屬性＋實跑 `_heartbeat_loop`
  活體驗證）逐鍵重播比對——native 14/14 PASS、pytest 4/4 PASS，
  M2→M3 放行門檻之三項 parity 判據落地。
- 未做（M2 殘項）：C++ 非同步執行面（send/receive loop、state 機轉移、
  outbox 事件持有與回放）、ChannelTransport 實作面對接、雙軌 flag 與
  觀察窗。
- 證據：`convergence/a263-channel-core-20260922.json`。
