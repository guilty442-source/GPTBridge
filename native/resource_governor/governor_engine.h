/* governor_engine.h — 引擎介面（列舉 / 系統 / 動作）。
 *
 * 由 resource_governor.h 依 A185（≤500 effective 行）拆分而來；內容原樣
 * 平移，語義不變。測試以假實作注入，零 OS 依賴；Windows 實作在
 * governor_engine_win32.cpp。
 */
#pragma once

#include <cstdint>
#include <expected>
#include <span>
#include <string>
#include <vector>

namespace gptbridge {
namespace governor {

struct ProcKey {
    int pid = 0;
    std::int64_t create_ms = 0;
    bool operator<(const ProcKey& other) const {
        if (pid != other.pid) return pid < other.pid;
        return create_ms < other.create_ms;
    }
    bool operator==(const ProcKey& other) const {
        return pid == other.pid && create_ms == other.create_ms;
    }
};

struct ProcSample {
    int pid = 0;
    std::string name;
    std::string exe;
    std::string cmdline;
    std::string username;
    double cpu_percore = 0.0;
    double rss_mb = 0.0;
    double io_read_mb = 0.0;
    double io_write_mb = 0.0;
    std::int64_t create_ms = 0;
};

struct SysInfo {
    double cpu_load_machine = 0.0;
    double mem_used_pct = 0.0;
    double mem_avail_mb = 0.0;
    double total_ram_mb = 0.0;
    long long total_ram_bytes = 0;
    int logical = 1;
    /* 使用者無輸入秒數（GetLastInputInfo）；<0 = 未知（fail-closed：
     * 視同使用中，閒置全速不啟動）。 */
    double user_idle_s = -1.0;
};

class IEngine {
 public:
    virtual ~IEngine() = default;
    virtual std::vector<ProcSample> enumerate() = 0;
    virtual SysInfo system() = 0;
    virtual bool set_priority(int pid, int prio_class) = 0;
    virtual std::expected<std::vector<int>, std::string> get_affinity(int pid) = 0;
    virtual bool set_affinity(int pid, std::span<const int> cpus) = 0;
    virtual bool trim(int pid) = 0;
    virtual bool background(int pid, bool enable) = 0;
    virtual bool ecoqos(int pid, bool enable) = 0;
    virtual bool cpu_limit(const ProcKey& key, int pid, double percent,
                           long long job_memory_bytes, int job_process_limit) = 0;
    virtual bool cpu_limit_clear(const ProcKey& key) = 0;
    virtual int foreground_pid() = 0;
    virtual double responsiveness() = 0;
};

}  // namespace governor
}  // namespace gptbridge
