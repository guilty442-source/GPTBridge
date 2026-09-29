// _binding_surfaces_b.h — pybind11 wrapper segments (B94 split of
// _binding.cpp): E2 transport / E3 startup-activation prototype
// surfaces (§10.65 shadow).  Included once by _binding.cpp.
#pragma once
// --- E2 transport/registration-surface prototype (§10.65 shadow) ---
// Thin holder over the pure-C request registry in native/core/ipc_registry.c.
// Python owns the struct by value; the C layer performs no I/O and no
// authority calls (governance gates stay on the Python path).

static const char* REQ_STATES[] = {
    "CREATED", "QUEUED", "RUNNING", "COMPLETED",
    "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED"};

class NativeIpcRegistry {
public:
    NativeIpcRegistry() {
        if (!gptbridge_ipc_registry_init(&reg_)) {
            throw std::runtime_error("ipc_registry init failed");
        }
    }
    bool create(const std::string& request_id, int32_t generation,
                int64_t now_ms) {
        return gptbridge_ipc_registry_create(
                   &reg_, request_id.c_str(), generation,
                   now_or_host(now_ms)) != 0;
    }
    bool set_status(const std::string& request_id, int status,
                    int64_t now_ms) {
        return gptbridge_ipc_registry_set_status(
                   &reg_, request_id.c_str(),
                   static_cast<gptbridge_req_status_t>(status),
                   now_or_host(now_ms)) == 1;
    }
    /* primary 模式：回傳區分碼（1 ok / 0 not-found / -1 terminal /
       -2 invalid-transition / -3 unknown-status），供 Python 映射
       既有 reason 字串。 */
    int set_status_rc(const std::string& request_id, int status,
                      int64_t now_ms) {
        return gptbridge_ipc_registry_set_status(
            &reg_, request_id.c_str(),
            static_cast<gptbridge_req_status_t>(status),
            now_or_host(now_ms));
    }
    bool merge_status(const std::string& request_id, int status,
                      int64_t now_ms) {
        return gptbridge_ipc_registry_merge_status(
                   &reg_, request_id.c_str(),
                   static_cast<gptbridge_req_status_t>(status),
                   now_or_host(now_ms)) != 0;
    }
    bool request_cancel(const std::string& request_id) {
        return gptbridge_ipc_registry_request_cancel(
                   &reg_, request_id.c_str()) != 0;
    }
    bool set_backend(const std::string& request_id,
                     const std::string& backend_id) {
        return gptbridge_ipc_registry_set_backend(
                   &reg_, request_id.c_str(), backend_id.c_str()) != 0;
    }
    bool cancel(const std::string& request_id, int64_t now_ms) {
        return gptbridge_ipc_registry_cancel(
                   &reg_, request_id.c_str(), now_or_host(now_ms)) != 0;
    }
    bool set_timeout(const std::string& request_id, int64_t timeout_ms) {
        return gptbridge_ipc_registry_set_timeout(
                   &reg_, request_id.c_str(), timeout_ms) != 0;
    }
    py::object find(const std::string& request_id) const {
        const gptbridge_ipc_request_t* r =
            gptbridge_ipc_registry_find(&reg_, request_id.c_str());
        if (r == nullptr) return py::none();
        py::dict out;
        out["request_id"] = r->request_id;
        out["backend_id"] = r->backend_id;
        out["backend_generation"] = r->backend_generation;
        out["status"] = REQ_STATES[r->status];
        out["cancelled"] = static_cast<bool>(r->cancelled);
        out["created_at_ms"] = r->created_at_ms;
        out["started_at_ms"] = r->started_at_ms;
        out["completed_at_ms"] = r->completed_at_ms;
        out["timeout_ms"] = r->timeout_ms;
        out["deadline_ms"] =
            gptbridge_ipc_registry_deadline_ms(&reg_, request_id.c_str());
        return out;
    }
    int count() const { return gptbridge_ipc_registry_count(&reg_); }
    bool transport_send(const std::string& payload, int64_t seq) {
        gptbridge_ipc_transport_msg_t msg{};
        std::snprintf(msg.payload, sizeof(msg.payload), "%s",
                      payload.c_str());
        msg.seq = static_cast<uint64_t>(seq);
        return gptbridge_ipc_transport_send(&msg) != 0;
    }
    py::object transport_recv() {
        gptbridge_ipc_transport_msg_t msg{};
        if (!gptbridge_ipc_transport_recv(&msg)) return py::none();
        py::dict out;
        out["payload"] = msg.payload;
        out["seq"] = static_cast<int64_t>(msg.seq);
        return out;
    }

private:
    gptbridge_ipc_registry_t reg_{};
};

// --- E3 startup/activation-surface prototypes (§10.65 shadow) ---
// Thin holders over the pure-C state machines in native/core/.  The C
// layer performs no I/O, no SQL and no process spawning — persistence,
// transport probes and toolbox calls stay on the Python governed path.

class NativeRuntimeStateRegistry {
public:
    NativeRuntimeStateRegistry() { gptbridge_rs_init(&reg_); }
    bool set_runtime_state(const std::string& module_id,
                           const std::string& state,
                           const py::object& health,
                           const py::object& release_id,
                           const py::object& error,
                           const std::string& now_str) {
        const uint8_t st = gptbridge_rs_runtime_from_name(state.c_str());
        if (st == GPTBRIDGE_RT_UNKNOWN) return false;
        std::string h, r, e;
        const char* ph = nullptr;
        const char* pr = nullptr;
        const char* pe = nullptr;
        if (!health.is_none()) { h = py::cast<std::string>(health); ph = h.c_str(); }
        if (!release_id.is_none()) { r = py::cast<std::string>(release_id); pr = r.c_str(); }
        if (!error.is_none()) { e = py::cast<std::string>(error); pe = e.c_str(); }
        return gptbridge_rs_set_runtime(
                   &reg_, module_id.c_str(), st, ph, pr, pe,
                   now_str.c_str()) != 0;
    }
    bool set_capability_state(const std::string& module_id,
                              const std::string& state,
                              const std::string& now_str) {
        const uint8_t st = gptbridge_rs_capability_from_name(state.c_str());
        if (st == GPTBRIDGE_CAP_UNKNOWN) return false;
        return gptbridge_rs_set_capability(
                   &reg_, module_id.c_str(), st, now_str.c_str()) != 0;
    }
    bool heartbeat(const std::string& module_id,
                   const std::string& now_str, int64_t now_ms) {
        return gptbridge_rs_heartbeat(
                   &reg_, module_id.c_str(), now_str.c_str(),
                   now_or_host(now_ms)) != 0;
    }
    int is_stale(const std::string& module_id, int64_t now_ms,
                 int64_t stale_after_ms) {
        return gptbridge_rs_is_stale(
            &reg_, module_id.c_str(), now_or_host(now_ms),
            stale_after_ms);
    }
    bool record_error(const std::string& module_id,
                      const std::string& error,
                      const std::string& now_str) {
        return gptbridge_rs_record_error(
                   &reg_, module_id.c_str(), error.c_str(),
                   now_str.c_str()) != 0;
    }
    py::object get(const std::string& module_id) const {
        const gptbridge_rs_record_t* r =
            gptbridge_rs_find(&reg_, module_id.c_str());
        if (r == nullptr) return py::none();
        py::dict out;
        out["module_id"] = r->module_id;
        out["runtime_state"] = gptbridge_rs_runtime_name(r->runtime_state);
        out["capability_state"] =
            gptbridge_rs_capability_name(r->capability_state);
        out["health"] = r->health;
        out["release_id"] = r->release_id;
        out["last_heartbeat"] = r->last_heartbeat;
        out["last_error"] = r->last_error;
        out["recovery_attempts"] = r->recovery_attempts;
        out["updated_at"] = r->updated_at;
        return out;
    }
    py::dict aggregate() const {
        int32_t by_rt[8], by_cap[5], failed_n = 0;
        char failed[GPTBRIDGE_RS_MAX_MODULES][GPTBRIDGE_RS_ID_MAX];
        const int32_t total = gptbridge_rs_aggregate(
            &reg_, by_rt, by_cap, failed, GPTBRIDGE_RS_MAX_MODULES,
            &failed_n);
        py::dict rt, cap;
        for (uint8_t s = 1; s <= 7; ++s) {
            if (by_rt[s] > 0) rt[gptbridge_rs_runtime_name(s)] = by_rt[s];
        }
        for (uint8_t s = 1; s <= 4; ++s) {
            if (by_cap[s] > 0) {
                cap[gptbridge_rs_capability_name(s)] = by_cap[s];
            }
        }
        py::list fl;
        const int32_t shown = failed_n < GPTBRIDGE_RS_MAX_MODULES
                                  ? failed_n : GPTBRIDGE_RS_MAX_MODULES;
        for (int32_t i = 0; i < shown; ++i) fl.append(failed[i]);
        py::dict out;
        out["schema"] = "star-runtime-state/v1";
        out["module_count"] = total;
        out["by_runtime_state"] = rt;
        out["by_capability_state"] = cap;
        out["failed_modules"] = fl;
        return out;
    }
    int count() const { return reg_.count; }
    /* primary 模式：持久化回放——逐欄位寫入不經轉移語意（對齊 Python
       _load）。dict 缺欄位以安全預設填充。 */
    bool restore(const py::dict& rec) {
        gptbridge_rs_record_t r{};
        _copy_str(r.module_id, sizeof(r.module_id), rec, "module_id");
        if (r.module_id[0] == '\0') return false;
        _copy_str(r.health, sizeof(r.health), rec, "health");
        _copy_str(r.release_id, sizeof(r.release_id), rec, "release_id");
        _copy_str(r.last_heartbeat, sizeof(r.last_heartbeat), rec,
                  "last_heartbeat");
        _copy_str(r.last_error, sizeof(r.last_error), rec, "last_error");
        _copy_str(r.updated_at, sizeof(r.updated_at), rec, "updated_at");
        r.runtime_state = gptbridge_rs_runtime_from_name(
            _get_str(rec, "runtime_state").c_str());
        r.capability_state = gptbridge_rs_capability_from_name(
            _get_str(rec, "capability_state").c_str());
        r.recovery_attempts = rec.contains("recovery_attempts")
                                  ? py::cast<int32_t>(rec["recovery_attempts"])
                                  : 0;
        r.last_heartbeat_ms = rec.contains("last_heartbeat_ms")
                                  ? py::cast<int64_t>(rec["last_heartbeat_ms"])
                                  : 0;
        return gptbridge_rs_restore(&reg_, &r) != 0;
    }
    py::list modules() const {
        py::list out;
        for (int32_t i = 0; i < GPTBRIDGE_RS_MAX_MODULES; ++i) {
            const gptbridge_rs_record_t* r = &reg_.records[i];
            if (!r->in_use) continue;
            py::dict d;
            d["module_id"] = r->module_id;
            d["runtime_state"] = gptbridge_rs_runtime_name(r->runtime_state);
            d["capability_state"] =
                gptbridge_rs_capability_name(r->capability_state);
            d["health"] = r->health;
            d["release_id"] = r->release_id;
            d["last_heartbeat"] = r->last_heartbeat;
            d["last_heartbeat_ms"] = r->last_heartbeat_ms;
            d["last_error"] = r->last_error;
            d["recovery_attempts"] = r->recovery_attempts;
            d["updated_at"] = r->updated_at;
            out.append(d);
        }
        return out;
    }

private:
    static std::string _get_str(const py::dict& d, const char* key) {
        if (!d.contains(key) || d[key].is_none()) return "";
        return py::cast<std::string>(d[key]);
    }
    static void _copy_str(char* dst, size_t cap, const py::dict& d,
                          const char* key) {
        const std::string v = _get_str(d, key);
        std::snprintf(dst, cap, "%s", v.c_str());
    }
    gptbridge_rs_registry_t reg_{};
};

class NativeActivationBroker {
public:
    NativeActivationBroker(double cooldown_s, double min_backoff_s,
                           double max_backoff_s) {
        if (!gptbridge_act_init(&broker_, cooldown_s, min_backoff_s,
                                max_backoff_s)) {
            throw std::runtime_error("activation broker init failed");
        }
    }
    std::string ensure(const py::dict& inputs) {
        gptbridge_act_inputs_t in{};
        in.pending = _flag(inputs, "pending");
        in.maintenance_ready = _flag(inputs, "maintenance_ready");
        in.shutting_down = _flag(inputs, "shutting_down");
        in.admission_hold = _flag(inputs, "admission_hold");
        in.liveness_known = _flag(inputs, "liveness_known");
        in.owner_active = _flag(inputs, "owner_active");
        in.regulation_active = _flag(inputs, "regulation_active");
        in.now_monotonic = inputs.contains("now_monotonic")
                               ? py::cast<double>(inputs["now_monotonic"])
                               : 0.0;
        return gptbridge_act_decision_name(
            gptbridge_act_ensure(&broker_, &in));
    }
    std::string on_start_result(bool ok, double now_monotonic) {
        return gptbridge_act_decision_name(gptbridge_act_on_start_result(
            &broker_, ok ? 1 : 0, now_monotonic));
    }
    std::string on_release_result(bool ok, double now_monotonic) {
        return gptbridge_act_decision_name(gptbridge_act_on_release_result(
            &broker_, ok ? 1 : 0, now_monotonic));
    }
    void note_explicit_stop(double now_monotonic, double wall_time) {
        gptbridge_act_note_explicit_stop(&broker_, now_monotonic, wall_time);
    }
    /* 狀態回放／shadow resync：權威 Python 狀態逐欄位寫入（rs_restore
       先例）。回傳 bool——無效輸入 fail-closed 不寫入。 */
    bool restore(const py::dict& state) {
        return gptbridge_act_restore(
                   &broker_,
                   _num(state, "next_attempt_at"),
                   _num(state, "next_release_at"),
                   _num(state, "backoff_seconds"),
                   static_cast<int32_t>(_num(state, "attempts")),
                   _flag(state, "broker_started_owner"),
                   _num(state, "explicit_stop_at")) != 0;
    }
    py::dict status() const {
        py::dict out;
        out["attempts"] = broker_.attempts;
        out["backoff_seconds"] = broker_.backoff_s;
        out["next_attempt_at"] = broker_.next_attempt_at;
        out["next_release_at"] = broker_.next_release_at;
        out["broker_started_owner"] =
            static_cast<bool>(broker_.broker_started_owner);
        out["explicit_stop_at"] = broker_.explicit_stop_at;
        return out;
    }
    static double poll_interval(bool pending, double idle_s,
                                double pending_s) {
        return gptbridge_act_poll_interval(pending ? 1 : 0, idle_s,
                                           pending_s);
    }
    static bool state_write_due(bool fingerprint_changed, double now,
                                double last_write_at, double heartbeat_s) {
        return gptbridge_act_state_write_due(
                   fingerprint_changed ? 1 : 0, now, last_write_at,
                   heartbeat_s) != 0;
    }

private:
    static int32_t _flag(const py::dict& d, const char* key) {
        return d.contains(key) && py::cast<bool>(d[key]) ? 1 : 0;
    }
    static double _num(const py::dict& d, const char* key) {
        if (!d.contains(key) || d[key].is_none()) return 0.0;
        return py::cast<double>(d[key]);
    }
    gptbridge_act_broker_t broker_{};
};
