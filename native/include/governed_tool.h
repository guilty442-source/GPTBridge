/* governed_tool.h — M1 受管工具執行面 ABI 決策自由語義 C 原型
 * （`star-governed-tool-runtime-abi/v1`；module-language-migration-order M1）
 *
 * 對齊 `governance_rule/execution/tool_runtime/governed_runtime*.py` 的
 * 純判定語義（shadow 模式，Python 仍為權威）：
 *
 *  - §1 行程啟動校驗：tool_id 正則、session token ^[a-f0-9]{64}$、
 *    port 1024–65535、env 齊全 → 否則 PERMISSION_DENIED
 *  - §1 workspace_instance_id：sha256("{tool_id}:{port}")[:16]
 *    （以 governed_runtime_maintenance mixin 版為準）
 *  - §2 HTTP 閘門：/shutdown 以 hmac.compare_digest 語義比對
 *    （env token 空 → 恆 403）；WS upgrade 閘門＝token.lower() 比對
 *    ＋instance 相等
 *  - §3.1 命令前置校驗：command 非空 ∧ payload 為 dict ∧ request_id
 *    非空且 ≤256 ∧ payload.tool_id（缺省=自身）== 自身
 *  - §4 佇列 backoff：空轉 min(idle×1.5, 500ms)、notify → 50ms、
 *    拿到 request → 重置 250ms（由呼叫方重置）
 *  - §4 channel_health：consecutive_failures ≥ 3 → degraded；成功歸零
 *
 * transport（PG/SQLite store）、issue_token（§5 待裁決項）、網路與
 * 子程序一律不在本檔——模式 B：token/傳輸維持 Python 代理。純 C11、
 * 零 I/O、零副作用。
 */
#ifndef GPTBRIDGE_GOVERNED_TOOL_H
#define GPTBRIDGE_GOVERNED_TOOL_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GPTBRIDGE_GT_INSTANCE_ID_LEN 16
#define GPTBRIDGE_GT_IDLE_INITIAL_MS 250
#define GPTBRIDGE_GT_IDLE_MAX_MS 500
#define GPTBRIDGE_GT_IDLE_NOTIFY_MS 50
#define GPTBRIDGE_GT_HEALTH_DEGRADE_AT 3
#define GPTBRIDGE_GT_REQUEST_ID_MAX 256

/* §1：tool_id 正則 ^[a-z0-9][a-z0-9_-]{1,63}$ */
int gptbridge_gt_tool_id_valid(const char* tool_id);

/* §1：session token 規格——Python 先 str().strip().lower() 再
   fullmatch ^[a-f0-9]{64}$；故接受前後空白與大小寫 hex。 */
int gptbridge_gt_session_token_valid(const char* token);

/* §1：port ∈ [1024, 65535] */
int gptbridge_gt_port_valid(int64_t port);

/* §1：env 啟動閘門——全部必要條件注入後的合取判定：
   project_root_ok（resolve 後為專案根且 tool_root 在其下）、
   tool_dir_ok（manifest 存在且 id 相符或 sealed 規則命中——由呼叫方判定）、
   session_token（格式由本函式驗證）、port、bootstrap_ok。
   任一不成立 → 0（PERMISSION_DENIED 路徑）。 */
int gptbridge_gt_env_gate(int32_t project_root_ok,
                          int32_t tool_dir_ok,
                          const char* session_token,
                          int64_t port,
                          int32_t bootstrap_ok);

/* §1：workspace_instance_id = sha256("{tool_id}:{port}")[:16]。
   out 須 >= 17 bytes（16 hex + NUL）。回 1 成功。 */
int gptbridge_gt_workspace_instance_id(const char* tool_id,
                                       int64_t port,
                                       char* out);

/* §2 /shutdown：hmac.compare_digest 語義——env token 空或 NULL → 0；
   provided NULL → 0；長度不同 → 0；否則常數時間比較。 */
int gptbridge_gt_shutdown_gate(const char* env_token,
                               const char* provided_token);

/* §2 WS upgrade 閘門：provided_token 轉小寫後常數時間比對
   session_token，且 provided_instance == expected_instance。
   任一不符 → 0（403）。 */
int gptbridge_gt_ws_gate(const char* session_token,
                         const char* provided_token,
                         const char* expected_instance,
                         const char* provided_instance);

/* §3.1 命令前置校驗：
   command 非空 ∧ payload_is_dict ∧ request_id 非空 ∧ ≤256 ∧
   payload_tool_id 為 NULL/空（缺省=自身）或 == self_tool_id。
   通過 → 1；否則 0（PERMISSION_DENIED 形式）。 */
int gptbridge_gt_request_valid(const char* command,
                               int32_t payload_is_dict,
                               const char* request_id,
                               const char* payload_tool_id,
                               const char* self_tool_id);

/* §4 idle_poll 變數演化（governed_runtime_worker._worker）：
   notified（或拿到 request）→ 重置 250ms；逾時 →
   min(current × 1.5, 500ms)。current_ms <= 0 視為初值 250ms。 */
int64_t gptbridge_gt_idle_next_ms(int64_t current_ms, int32_t notified);

/* §4 當輪 wait_for 逾時值：notify_queue 非空 → 50ms；否則
   max(idle_poll_ms, 50ms)。 */
int64_t gptbridge_gt_wait_timeout_ms(int64_t idle_poll_ms,
                                     int32_t notify_pending);

/* §4 channel_health：consecutive_failures ≥ 3 → degraded(1)；
   ≤0（成功歸零）→ 0。 */
int gptbridge_gt_health_degraded(int32_t consecutive_failures);

#ifdef __cplusplus
}
#endif

#endif /* GPTBRIDGE_GOVERNED_TOOL_H */
