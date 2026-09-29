// _binding_adapters.h — pybind11 wrapper segments (B94 split of
// _binding.cpp): M1 system-rescue + governed-tool-runtime ABI adapters.
// Included once by _binding.cpp; not a public header.
#pragma once
// --- M1 system-rescue prototype adapters (stateless) -------------------
// Thin boundary: facts are injected by the Python caller; this layer
// performs no I/O and no process spawning.

static py::str sr_verify_tool_package_name(bool release_dir_exists,
                                           bool metadata_exists,
                                           bool metadata_valid,
                                           bool has_source_manifest,
                                           int64_t source_mtime,
                                           int64_t package_mtime) {
    return py::str(gptbridge_sr_pkg_verdict_code(
        gptbridge_sr_verify_tool_package(
            release_dir_exists ? 1 : 0, metadata_exists ? 1 : 0,
            metadata_valid ? 1 : 0, has_source_manifest ? 1 : 0,
            source_mtime, package_mtime)));
}

static py::object sr_normalize_packager_error(const py::object& error_code) {
    if (error_code.is_none()) return py::none();
    const std::string code = py::cast<std::string>(error_code);
    const char* out = gptbridge_sr_normalize_packager_error(code.c_str());
    if (out == nullptr) return py::none();
    return py::str(out);
}

static bool sr_all_ok(const py::iterable& oks) {
    std::vector<int32_t> values;
    for (const auto& item : oks) {
        values.push_back(py::cast<bool>(item) ? 1 : 0);
    }
    return gptbridge_sr_all_ok(values.data(),
                               static_cast<int32_t>(values.size())) != 0;
}

static py::str sr_verify_archive_name(bool file_exists,
                                      bool sidecar_exists,
                                      const py::object& expected_hex,
                                      const py::object& actual_hex) {
    std::string expected, actual;
    const char* pe = nullptr;
    const char* pa = nullptr;
    if (!expected_hex.is_none()) {
        expected = py::cast<std::string>(expected_hex);
        pe = expected.c_str();
    }
    if (!actual_hex.is_none()) {
        actual = py::cast<std::string>(actual_hex);
        pa = actual.c_str();
    }
    return py::str(gptbridge_sr_arc_verdict_code(gptbridge_sr_verify_archive(
        file_exists ? 1 : 0, sidecar_exists ? 1 : 0, pe, pa)));
}

static py::str sr_sha256_hex(const py::bytes& data) {
    // Zero-copy: hash the bytes object's buffer in place (A213 — borrowed
    // read-only view; the bytes object outlives the call, no ownership move).
    char hex[65];
    if (!gptbridge_sr_sha256_hex(
            reinterpret_cast<const uint8_t*>(PyBytes_AS_STRING(data.ptr())),
            static_cast<size_t>(PyBytes_GET_SIZE(data.ptr())),
            hex)) {
        throw std::runtime_error("sha256 failed");
    }
    return py::str(hex);
}

static py::str sr_cli_dispatch_name(bool all_flag,
                                    const py::object& tool_id,
                                    bool verify_flag,
                                    bool deep_flag,
                                    bool package_flag) {
    std::string tool;
    const char* pt = nullptr;
    if (!tool_id.is_none()) {
        tool = py::cast<std::string>(tool_id);
        pt = tool.c_str();
    }
    return py::str(gptbridge_sr_cli_op_name(gptbridge_sr_cli_dispatch(
        all_flag ? 1 : 0, pt, verify_flag ? 1 : 0, deep_flag ? 1 : 0,
        package_flag ? 1 : 0)));
}

// --- M1 governed-tool-runtime ABI adapters (star-governed-tool-runtime-abi/v1)
// Stateless decision subset; transport/token/network stay in Python.

static py::str gt_workspace_instance_id(const std::string& tool_id,
                                        int64_t port) {
    char out[GPTBRIDGE_GT_INSTANCE_ID_LEN + 1];
    if (!gptbridge_gt_workspace_instance_id(tool_id.c_str(), port, out)) {
        throw std::runtime_error("workspace_instance_id failed");
    }
    return py::str(out);
}

static bool gt_env_gate(bool project_root_ok, bool tool_dir_ok,
                        const std::string& session_token, int64_t port,
                        bool bootstrap_ok) {
    return gptbridge_gt_env_gate(
               project_root_ok ? 1 : 0, tool_dir_ok ? 1 : 0,
               session_token.c_str(), port, bootstrap_ok ? 1 : 0) != 0;
}

static bool gt_shutdown_gate(const py::object& env_token,
                             const py::object& provided_token) {
    std::string env, provided;
    const char* pe = nullptr;
    const char* pp2 = nullptr;
    if (!env_token.is_none()) {
        env = py::cast<std::string>(env_token);
        pe = env.c_str();
    }
    if (!provided_token.is_none()) {
        provided = py::cast<std::string>(provided_token);
        pp2 = provided.c_str();
    }
    return gptbridge_gt_shutdown_gate(pe, pp2) != 0;
}

static bool gt_ws_gate(const std::string& session_token,
                       const std::string& provided_token,
                       const std::string& expected_instance,
                       const std::string& provided_instance) {
    return gptbridge_gt_ws_gate(session_token.c_str(),
                                provided_token.c_str(),
                                expected_instance.c_str(),
                                provided_instance.c_str()) != 0;
}

static bool gt_request_valid(const std::string& command,
                             bool payload_is_dict,
                             const std::string& request_id,
                             const py::object& payload_tool_id,
                             const std::string& self_tool_id) {
    std::string tool;
    const char* pt = nullptr;
    if (!payload_tool_id.is_none()) {
        tool = py::cast<std::string>(payload_tool_id);
        pt = tool.c_str();
    }
    return gptbridge_gt_request_valid(command.c_str(),
                                      payload_is_dict ? 1 : 0,
                                      request_id.c_str(), pt,
                                      self_tool_id.c_str()) != 0;
}
