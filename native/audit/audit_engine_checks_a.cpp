// check-kind executors, part a (unity-included by audit_engine.cpp)

AuditCheckResult check_delegated(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    r.status = AuditStatus::DELEGATED;
    r.detail = check.reason;
    return r;
}

AuditCheckResult check_fail(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    /* 匯出期已確認的違規（如 self-health 非法 test_targets）——
     * 確定性 FAIL，reason 攜帶人讀原因。 */
    r.status = AuditStatus::FAIL;
    r.detail = check.reason.empty() ? "declared fail row" : check.reason;
    return r;
}

AuditCheckResult check_file_exists(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    if (fs::is_regular_file(target, ec)) { r.status = AuditStatus::PASS; }
    else { r.status = AuditStatus::FAIL; r.detail = "missing: " + check.path; }
    return r;
}

AuditCheckResult check_file_not_exists(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    if (!fs::exists(target, ec) && !ec) { r.status = AuditStatus::PASS; }
    else { r.status = AuditStatus::FAIL; r.detail = "forbidden path present: " + check.path; }
    return r;
}

AuditCheckResult check_file_readonly(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    if (!fs::is_regular_file(target, ec)) {
        r.status = AuditStatus::FAIL; r.detail = "missing: " + check.path;
    } else if (is_readonly(target)) {
        r.status = AuditStatus::PASS;
    } else {
        r.status = AuditStatus::FAIL; r.detail = "not read-only: " + check.path;
    }
    return r;
}

AuditCheckResult check_dir_exists(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    /* 語義對齊 Python：必須為實體目錄且非 symlink */
    if (fs::is_symlink(target, ec)) {
        r.status = AuditStatus::FAIL;
        r.detail = "must be a physical directory (symlink): " + check.path;
    } else if (fs::is_directory(target, ec)) {
        r.status = AuditStatus::PASS;
    } else {
        r.status = AuditStatus::FAIL; r.detail = "missing dir: " + check.path;
    }
    return r;
}

AuditCheckResult check_file_contains(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    const auto text = cached_text(target);
    if (!text) {
        if (check.optional && !fs::exists(target, ec)) {
            r.status = AuditStatus::PASS;
            return r;
        }
        r.status = AuditStatus::FAIL; r.detail = "unreadable: " + check.path;
        return r;
    }
    const std::string& content = *text;
    for (const auto& m : check.markers) {
        if (content.find(m) == std::string::npos) {
            r.status = AuditStatus::FAIL;
            r.detail = "missing marker: " + m;
            return r;
        }
    }
    r.status = AuditStatus::PASS;
    return r;
}

AuditCheckResult check_file_not_contains(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    const auto text = cached_text(target);
    if (!text) {
        if (check.optional && !fs::exists(target, ec)) {
            r.status = AuditStatus::PASS;   /* 條件式禁標檢查：缺席即略過 */
            return r;
        }
        r.status = AuditStatus::FAIL; r.detail = "unreadable: " + check.path;
        return r;
    }
    const auto lower = check.ignore_case ? cached_lower(target) : nullptr;
    const std::string& haystack = check.ignore_case ? *lower : *text;
    for (const auto& m : check.markers) {
        const std::string needle =
            check.ignore_case ? to_lower(m) : m;
        if (haystack.find(needle) != std::string::npos) {
            r.status = AuditStatus::FAIL;
            r.detail = "forbidden marker present: " + m;
            return r;
        }
    }
    r.status = AuditStatus::PASS;
    return r;
}

AuditCheckResult check_file_not_contains_unless(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    /* 條件式禁標：markers 任一命中時，檔案必須同時持有至少一個
     * unless 解禁標記，否則 FAIL。
     * 對齊 Python 複合條件「含 A 且不含 B → 錯」。 */
    const auto text = cached_text(target);
    if (!text) {
        if (check.optional && !fs::exists(target, ec)) {
            r.status = AuditStatus::PASS;
            return r;
        }
        r.status = AuditStatus::FAIL; r.detail = "unreadable: " + check.path;
        return r;
    }
    const auto lower = check.ignore_case ? cached_lower(target) : nullptr;
    const std::string& haystack = check.ignore_case ? *lower : *text;
    for (const auto& m : check.markers) {
        const std::string needle =
            check.ignore_case ? to_lower(m) : m;
        if (haystack.find(needle) != std::string::npos) {
            bool relieved = false;
            for (const auto& u : check.unless) {
                const std::string un =
                    check.ignore_case ? to_lower(u) : u;
                if (haystack.find(un) != std::string::npos) {
                    relieved = true;
                    break;
                }
            }
            if (!relieved) {
                r.status = AuditStatus::FAIL;
                r.detail = "forbidden marker present: " + m;
                return r;
            }
        }
    }
    r.status = AuditStatus::PASS;
    return r;
}

AuditCheckResult check_json_has_keys(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    bool unreadable = false;
    const auto docp = cached_json(target, &unreadable);
    if (!docp) {
        r.status = AuditStatus::FAIL;
        r.detail = (unreadable ? "unreadable: " : "invalid json: ")
            + check.path;
        return r;
    }
    const JsonValue& doc = *docp;
    if (doc.type != JsonValue::Type::Object) {
        r.status = AuditStatus::FAIL;
        r.detail = "json root is not an object: " + check.path;
        return r;
    }
    for (const auto& key : check.markers) {
        if (doc.get(key) == nullptr) {
            r.status = AuditStatus::FAIL;
            r.detail = "missing key: " + key;
            return r;
        }
    }
    r.status = AuditStatus::PASS;
    return r;
}
