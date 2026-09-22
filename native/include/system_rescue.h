/* system_rescue.h — M1 語言外移：system-rescue 決策自由執行語義 C 原型
 * （module-language-migration-order-20260922 M1；§10.65 shadow 模式）
 *
 * 對應 Python
 * `Standalone tools/system-rescue/src/backend/services/system_rescue/
 *  integration/platform_packager.py` 的決策自由執行語義：
 *
 *  - _verify_tool_package 判定樹：PACKAGE_MISSING →
 *    PACKAGE_METADATA_MISSING → PACKAGE_METADATA_INVALID → STALE_PACKAGE
 *    → OK（事實由呼叫方注入：目錄／metadata 存在性、mtime）
 *  - _normalize_packager_report 錯誤碼映射：PROCESS_TIMEOUT →
 *    PACKAGER_TIMEOUT、PROCESS_LAUNCH_FAILED → PACKAGER_LAUNCH_FAILED、
 *    PROCESS_OUTPUT_INVALID → PACKAGER_OUTPUT_INVALID；其餘原樣透傳
 *  - verify_packaged_tool：PACKAGE_NOT_FOUND → sidecar 缺失即 OK →
 *    CHECKSUM_MISMATCH；SHA-256 由本檔實作（與 hashlib.sha256 等值）
 *  - verify_all_packages 聚合：results 全 ok 才 ok
 *  - _main 參數路由：--all/--tool/--verify/--deep/--package → op
 *
 * I/O（檔案系統、GovernedProcessAdapter 子程序、稽核）留在呼叫方
 * Python 治理路徑——本檔純 C11、無 I/O、無 SQL、零副作用。
 * shadow 模式：與 Python 並行比對，Python 為權威。
 */
#ifndef GPTBRIDGE_SYSTEM_RESCUE_H
#define GPTBRIDGE_SYSTEM_RESCUE_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* _verify_tool_package 判定碼 */
typedef enum {
    GPTBRIDGE_SR_PKG_OK = 0,
    GPTBRIDGE_SR_PKG_MISSING = 1,            /* PACKAGE_MISSING */
    GPTBRIDGE_SR_PKG_METADATA_MISSING = 2,   /* PACKAGE_METADATA_MISSING */
    GPTBRIDGE_SR_PKG_METADATA_INVALID = 3,   /* PACKAGE_METADATA_INVALID */
    GPTBRIDGE_SR_PKG_STALE = 4               /* STALE_PACKAGE */
} gptbridge_sr_pkg_verdict_t;

/* verify_packaged_tool 判定碼 */
typedef enum {
    GPTBRIDGE_SR_ARC_OK = 0,
    GPTBRIDGE_SR_ARC_NOT_FOUND = 1,           /* PACKAGE_NOT_FOUND */
    GPTBRIDGE_SR_ARC_CHECKSUM_MISMATCH = 2,   /* PACKAGE_CHECKSUM_MISMATCH */
    GPTBRIDGE_SR_ARC_CHECKSUM_UNREADABLE = 3  /* PACKAGE_CHECKSUM_UNREADABLE */
} gptbridge_sr_arc_verdict_t;

/* _main 參數路由結果（對齊 Python _main 的 elif 階梯） */
typedef enum {
    GPTBRIDGE_SR_OP_INVALID = 0,          /* INVALID_ARGS */
    GPTBRIDGE_SR_OP_VERIFY_ALL = 1,       /* verify_all_packages() */
    GPTBRIDGE_SR_OP_DEEP_VERIFY_TOOL = 2, /* deep_verify_tool_package */
    GPTBRIDGE_SR_OP_VERIFY_TOOL = 3,      /* _verify_tool_package */
    GPTBRIDGE_SR_OP_PACKAGE_TOOL = 4,     /* package_platform_tool */
    GPTBRIDGE_SR_OP_PACKAGE_ALL = 5       /* _run_packager_cli --all */
} gptbridge_sr_cli_op_t;

const char* gptbridge_sr_pkg_verdict_code(gptbridge_sr_pkg_verdict_t v);
const char* gptbridge_sr_arc_verdict_code(gptbridge_sr_arc_verdict_t v);
const char* gptbridge_sr_cli_op_name(gptbridge_sr_cli_op_t op);

/* _verify_tool_package 判定樹（事實注入）：
   !release_dir_exists → PACKAGE_MISSING
   !metadata_exists    → PACKAGE_METADATA_MISSING
   !metadata_valid     → PACKAGE_METADATA_INVALID（空／解析失敗）
   has_source_manifest && source_mtime > package_mtime → STALE_PACKAGE
   否則 OK。 */
gptbridge_sr_pkg_verdict_t gptbridge_sr_verify_tool_package(
    int32_t release_dir_exists,
    int32_t metadata_exists,
    int32_t metadata_valid,
    int32_t has_source_manifest,
    int64_t source_mtime,
    int64_t package_mtime);

/* _normalize_packager_report 錯誤碼映射。輸入為 adapter 回傳的
   error_code（可為 NULL）；輸出為規格化後的 error_code 字串——
   無 error_code 時回 NULL。字串為靜態儲存或原樣透傳指標。 */
const char* gptbridge_sr_normalize_packager_error(const char* error_code);

/* verify_all_packages 聚合：results 全 ok → 1；空清單 → 1
   （Python: all(...) of empty == True → "no packaged tools to verify"）。 */
int gptbridge_sr_all_ok(const int32_t* oks, int32_t count);

/* verify_packaged_tool 判定（sidecar 比對）：
   !file_exists → PACKAGE_NOT_FOUND
   sidecar_exists && expected_hex == NULL → CHECKSUM_UNREADABLE
     （Python 端空 sidecar → split()[0] IndexError → UNREADABLE；
      sidecar 讀取／解析失敗的事實由呼叫方以 NULL 注入）
   sidecar_exists && expected_hex != actual_hex → CHECKSUM_MISMATCH
   否則 OK（hex 比對不區分大小寫，Python .lower() 語義）。
   expected_hex/actual_hex 僅在 sidecar_exists 時使用。 */
gptbridge_sr_arc_verdict_t gptbridge_sr_verify_archive(
    int32_t file_exists,
    int32_t sidecar_exists,
    const char* expected_hex,
    const char* actual_hex);

/* SHA-256（FIPS 180-4）：out_hex 須 >= 65 bytes（64 hex + NUL）。
   與 hashlib.sha256(data).hexdigest() 等值。回 1 成功。 */
int gptbridge_sr_sha256_hex(const uint8_t* data, size_t len, char* out_hex);

/* _main 參數路由（Python elif 階梯逐序對齊）：
   all && verify            → VERIFY_ALL
   tool && verify && deep   → DEEP_VERIFY_TOOL
   tool && verify           → VERIFY_TOOL
   tool && package          → PACKAGE_TOOL
   all && package           → PACKAGE_ALL
   all                      → VERIFY_ALL
   其他                     → INVALID */
gptbridge_sr_cli_op_t gptbridge_sr_cli_dispatch(
    int32_t all_flag,
    const char* tool_id,
    int32_t verify_flag,
    int32_t deep_flag,
    int32_t package_flag);

#ifdef __cplusplus
}
#endif

#endif /* GPTBRIDGE_SYSTEM_RESCUE_H */
