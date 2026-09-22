/* audit_engine.h — 原生審計引擎（C++17；§1.1 權限核心 C/C++ 審計路徑）

P0「測試與審計 ≤30 s」細項①：審計以 C/C++ 實作、可嵌入提交閘門。

設計（shadow 模式，與 E1 相同）：
  - Python 受管工具產生 ``audit_checks_manifest.json``
    （``star-audit-manifest/v1``）——受保護源清單、禁用遺留路徑、
    污染掃描標的等**資料**仍由治理側決定；本引擎只執行、不決策。
  - 引擎支援的檢查 kind 以原生實作；不支援的 kind 一律回報
    ``delegated``（顯式移交 Python oracle，不靜默略過、不算失敗）。
  - 任何 manifest 解析失敗 / 檔案 I/O 異常 → fail-closed（exit≠0）。

純標準庫、無外部依賴、無網路、唯讀（不修改任何受檔檔案）。
*/
#ifndef GPTBRIDGE_AUDIT_ENGINE_H
#define GPTBRIDGE_AUDIT_ENGINE_H

#include <cstdint>
#include <string>
#include <vector>

namespace gptbridge {

enum class AuditStatus : std::int32_t {
    PASS = 0,
    FAIL = 1,
    DELEGATED = 2,   /* kind 未原生支援 → 移交 Python oracle */
};

struct AuditCheckResult {
    std::string id;
    std::string kind;
    AuditStatus status = AuditStatus::FAIL;
    std::string detail;   /* FAIL 時的人讀原因（有界） */
};

struct AuditReport {
    std::vector<AuditCheckResult> checks;
    int passed = 0;
    int failed = 0;
    int delegated = 0;
    bool manifest_ok = false;      /* manifest 解析失敗 → 整體 fail-closed */
    std::string manifest_error;
};

/* 單一檢查描述（manifest 解碼後內部表示） */
struct AuditCheck {
    std::string id;
    std::string kind;
    std::string path;            /* 相對 root */
    std::string glob;            /* glob-min-count 用 */
    std::vector<std::string> markers;  /* file-contains(-all) 用 */
    std::int64_t min_count = 0;
    std::string reason;          /* delegated 用 */
};

/* 執行一組檢查；root 為專案根（唯讀）。 */
AuditReport audit_run(const std::vector<AuditCheck>& checks,
                      const std::string& root);

/* 載入 manifest 檔；失敗時 out_error 填原因並回傳空清單＋false。 */
bool audit_load_manifest(const std::string& manifest_path,
                         std::vector<AuditCheck>* out_checks,
                         std::string* out_error);

/* 序列化報告為 JSON（供 native-report 匯流）。 */
std::string audit_report_json(const AuditReport& report);

}  // namespace gptbridge

#endif /* GPTBRIDGE_AUDIT_ENGINE_H */
