// _binding_surfaces_a.h — pybind11 wrapper segments (B94 split of
// _binding.cpp): E1 execution-surface prototypes (§10.65 shadow).
// Included once by _binding.cpp; not a public header.
#pragma once
// _binding.cpp): E1 execution / E2 transport / E3 startup-activation
// Thin holders over the pure-C state machines in native/core/. Python owns
// the structs by value; the C layer performs no I/O and no authority calls.

// Caller-supplied ms timestamps keep the C core deterministic; a 0 means
// "stamp now" so bindings stay ergonomic for callers without a clock.
static int64_t now_or_host(int64_t now_ms) {
    if (now_ms > 0) return now_ms;
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

static const char* WD_STATES[] = {
    "unknown", "connected", "degraded", "disconnected", "starting"};

class NativeWatchdog {
public:
    NativeWatchdog(int64_t min_interval_ms, int64_t max_interval_ms,
                   int32_t dead_threshold, int32_t retry_grace) {
        if (!gptbridge_wd_init(&wd_, min_interval_ms, max_interval_ms,
                               dead_threshold, retry_grace)) {
            throw std::invalid_argument("watchdog init failed");
        }
    }
    py::object probe(bool backend_process_alive, bool backend_http_healthy,
                     bool frontend_connected, int64_t now_ms) {
        gptbridge_wd_probe_t p{backend_process_alive ? 1 : 0,
                               backend_http_healthy ? 1 : 0,
                               frontend_connected ? 1 : 0};
        gptbridge_wd_event_t ev{};
        int repair = gptbridge_wd_probe(&wd_, &p, now_ms, &ev);
        if (ev.from_state == ev.to_state && !repair) {
            return py::none();
        }
        py::dict out;
        out["from_state"] = WD_STATES[ev.from_state];
        out["to_state"] = WD_STATES[ev.to_state];
        out["trigger_repair"] = static_cast<bool>(ev.trigger_repair);
        out["at_ms"] = ev.at_ms;
        out["repair_fired"] = static_cast<bool>(repair);
        return out;
    }
    int64_t next_interval_ms() { return gptbridge_wd_next_interval_ms(&wd_); }
    std::string state() const { return WD_STATES[wd_.state]; }
    int32_t consecutive_dead() const { return wd_.consecutive_dead; }
    int64_t probe_count() const { return wd_.probe_count; }

private:
    gptbridge_wd_t wd_{};
};

static void sched_count_tick(void* ctx) {
    ++(*static_cast<int64_t*>(ctx));
}

class NativeScheduler {
public:
    NativeScheduler() {
        if (!gptbridge_sched_init(&sched_)) {
            throw std::runtime_error("scheduler init failed");
        }
    }
    bool register_job(const std::string& name, int64_t interval_ms,
                      int64_t timeout_ms, int64_t now_ms,
                      bool run_immediately, bool pausable) {
        if (sched_.count >= GPTBRIDGE_SCHED_MAX_JOBS) return false;
        counters_[sched_.count] = 0;
        counter_index_[name] = sched_.count;
        return gptbridge_sched_register(
                   &sched_, name.c_str(), interval_ms, timeout_ms, now_ms,
                   run_immediately ? 1 : 0, pausable ? 1 : 0,
                   &sched_count_tick, &counters_[sched_.count]) != 0;
    }
    bool unregister_job(const std::string& name) {
        auto it = counter_index_.find(name);
        if (it == counter_index_.end()) return false;
        int32_t slot = it->second;
        if (gptbridge_sched_unregister(&sched_, name.c_str()) == 0) {
            return false;
        }
        counters_[slot] = 0;
        /* shift-remove moved later jobs down one slot — rebind ctx pointers
           and the name→slot index from the authoritative job order. */
        counter_index_.clear();
        for (int32_t i = 0; i < sched_.count; ++i) {
            sched_.jobs[i].ctx = &counters_[i];
            counter_index_[sched_.jobs[i].name] = i;
        }
        return true;
    }
    int tick(int64_t now_ms, bool paused) {
        return gptbridge_sched_tick(&sched_, now_ms, paused ? 1 : 0);
    }
    /* primary 模式：回傳到期 job 名單（不執行 fn——Python 跑 coroutine
       後以 record() 回填）。starvation 界內建於 C 層。 */
    py::list collect_due(int64_t now_ms, bool paused) {
        char names[GPTBRIDGE_SCHED_MAX_JOBS][GPTBRIDGE_SCHED_NAME_MAX];
        const int n = gptbridge_sched_collect_due(
            &sched_, now_ms, paused ? 1 : 0, names,
            GPTBRIDGE_SCHED_MAX_JOBS);
        py::list out;
        for (int32_t i = 0; i < n; ++i) out.append(names[i]);
        return out;
    }
    bool record(const std::string& name, int64_t started_ms,
                int64_t duration_ms, bool error) {
        return gptbridge_sched_record(&sched_, name.c_str(), started_ms,
                                      duration_ms, error ? 1 : 0) != 0;
    }
    int64_t min_due_ms() const {
        return gptbridge_sched_min_due_ms(&sched_);
    }
    int job_count() const { return gptbridge_sched_job_count(&sched_); }
    py::object job_stats(const std::string& name) const {
        const gptbridge_sched_job_t* j =
            gptbridge_sched_find(&sched_, name.c_str());
        if (j == nullptr) return py::none();
        py::dict out;
        out["run_count"] = j->run_count;
        out["error_count"] = j->error_count;
        out["paused_count"] = j->paused_count;
        out["last_run_ms"] = j->last_run_ms;
        out["last_duration_ms"] = j->last_duration_ms;
        out["next_due_ms"] = j->next_due_ms;
        out["enabled"] = static_cast<bool>(j->enabled);
        return out;
    }

private:
    gptbridge_sched_t sched_{};
    int64_t counters_[GPTBRIDGE_SCHED_MAX_JOBS] = {};
    std::unordered_map<std::string, int32_t> counter_index_{};
};

class NativeOutbox {
public:
    NativeOutbox() {
        if (!gptbridge_ob_init(&reg_)) {
            throw std::runtime_error("outbox init failed");
        }
    }
    bool register_session(const std::string& sid) {
        return gptbridge_ob_register(&reg_, sid.c_str()) != 0;
    }
    bool unregister_session(const std::string& sid) {
        return gptbridge_ob_unregister(&reg_, sid.c_str()) != 0;
    }
    py::dict hello(const std::string& sid, int64_t cursor,
                   bool generation_matches, int64_t latest_sequence) {
        int32_t reset = 0;
        int64_t effective = gptbridge_ob_hello(
            &reg_, sid.c_str(), cursor, generation_matches ? 1 : 0,
            latest_sequence, &reset);
        py::dict out;
        out["effective_cursor"] = effective;
        out["reset"] = static_cast<bool>(reset);
        return out;
    }
    bool ack(const std::string& sid, int64_t cursor) {
        return gptbridge_ob_ack(&reg_, sid.c_str(), cursor) != 0;
    }
    bool resync(const std::string& sid, int64_t cursor) {
        return gptbridge_ob_resync(&reg_, sid.c_str(), cursor) != 0;
    }
    py::dict drain_plan(const std::string& sid, int64_t now_ms,
                        int64_t retry_ms) {
        int64_t start_after = 0, limit = 0;
        int rc = gptbridge_ob_drain_plan(&reg_, sid.c_str(), now_ms, retry_ms,
                                       &start_after, &limit);
        py::dict out;
        out["has_work"] = rc != 0;
        out["start_after"] = start_after;
        out["limit"] = limit;
        return out;
    }
    bool mark_sent(const std::string& sid, int64_t seq, int64_t now_ms) {
        return gptbridge_ob_mark_sent(&reg_, sid.c_str(), seq, now_ms) != 0;
    }
    int64_t next_retry_deadline(int64_t retry_ms) const {
        int64_t deadline = 0;
        gptbridge_ob_next_retry_deadline(&reg_, retry_ms, &deadline);
        return deadline;
    }
    int64_t prune_floor(int64_t latest_sequence) const {
        return gptbridge_ob_prune_floor(&reg_, latest_sequence);
    }
    py::object session(const std::string& sid) const {
        const gptbridge_ob_session_t* s =
            gptbridge_ob_find(&reg_, sid.c_str());
        if (s == nullptr) return py::none();
        py::dict out;
        out["acked"] = s->acked;
        out["sent_upto"] = s->sent_upto;
        out["last_attempt_ms"] = s->last_attempt_ms;
        return out;
    }

private:
    gptbridge_ob_registry_t reg_{};
};

class NativeMaintenance {
public:
    NativeMaintenance(int64_t tick_interval_ms, int64_t max_job_age_ms,
                      int32_t max_retry_attempts, int64_t retry_backoff_ms,
                      int64_t current_generation) {
        if (!gptbridge_mt_init(&mt_, tick_interval_ms, max_job_age_ms,
                               max_retry_attempts, retry_backoff_ms,
                               current_generation)) {
            throw std::invalid_argument("maintenance init failed");
        }
    }
    bool admit(const std::string& job_id, const std::string& action_id,
               int risk_class, int priority, int64_t generation,
               bool system_idle, bool authorized, int64_t now_ms,
               bool system_blocked) {
        gptbridge_mt_job_t job{};
        std::snprintf(job.job_id, GPTBRIDGE_MT_ID_MAX, "%s", job_id.c_str());
        std::snprintf(job.action_id, GPTBRIDGE_MT_ID_MAX, "%s",
                      action_id.c_str());
        job.risk_class = static_cast<gptbridge_mt_class_t>(risk_class);
        job.priority = priority;
        job.status = GPTBRIDGE_MT_PLANNED;
        job.generation = generation;
        job.scheduled_at_ms = now_ms;
        job.next_attempt_ms = now_ms;
        return gptbridge_mt_admit(&mt_, &job, system_idle ? 1 : 0,
                                  authorized ? 1 : 0,
                                  system_blocked ? 1 : 0, now_ms) != 0;
    }
    py::object next_due(int64_t now_ms) {
        gptbridge_mt_job_t* j = gptbridge_mt_next_due(&mt_, now_ms);
        if (j == nullptr) return py::none();
        py::dict out;
        out["job_id"] = j->job_id;
        out["action_id"] = j->action_id;
        out["status"] = static_cast<int>(j->status);
        out["attempt_count"] = j->attempt_count;
        return out;
    }
    bool complete(const std::string& job_id) {
        return gptbridge_mt_complete(&mt_, job_id.c_str()) != 0;
    }
    bool fail(const std::string& job_id, int64_t now_ms) {
        return gptbridge_mt_fail(&mt_, job_id.c_str(), now_ms) != 0;
    }
    bool cancel(const std::string& job_id) {
        return gptbridge_mt_cancel(&mt_, job_id.c_str()) != 0;
    }
    bool requeue(const std::string& job_id, int64_t now_ms) {
        return gptbridge_mt_requeue(&mt_, job_id.c_str(),
                                    now_or_host(now_ms)) != 0;
    }
    int job_count() const { return mt_.count; }
    /* Non-terminal slots (QUEUED/DEFERRED/RUNNING) — mirrors the Python
       scheduler's len(_queue)+len(_running).  job_count() is the raw table
       size: terminal slots linger until recycled, so it overstates live
       work (P4 shadow desynced-queue metric was counting terminal slots). */
    int live_count() const {
        int n = 0;
        for (int32_t i = 0; i < mt_.count; ++i) {
            const gptbridge_mt_status_t st = mt_.jobs[i].status;
            if (st == GPTBRIDGE_MT_QUEUED || st == GPTBRIDGE_MT_DEFERRED ||
                st == GPTBRIDGE_MT_RUNNING)
                ++n;
        }
        return n;
    }
    void set_generation(int64_t generation) {
        mt_.current_generation = generation;
    }
    /* TTL probe cache (same-tick shared probe result, failure cached too).
       get returns None on miss/expiry, else the stored ok flag. */
    py::object cache_get(int64_t now_ms, int64_t ttl_ms) {
        int32_t ok = 0;
        if (!gptbridge_mt_cache_get(&cache_, now_ms, ttl_ms,
                                    nullptr, &ok)) {
            return py::none();
        }
        return py::bool_(ok != 0);
    }
    void cache_set(int64_t now_ms, bool probe_ok) {
        gptbridge_mt_cache_set(&cache_, now_ms, probe_ok ? 1 : 0, nullptr);
    }

private:
    gptbridge_mt_t mt_{};
    gptbridge_mt_cache_t cache_{};
};
