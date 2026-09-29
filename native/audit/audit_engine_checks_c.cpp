// check-kind executors, part c (unity-included by audit_engine.cpp)

AuditCheckResult check_glob_min_count(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    const fs::path g = fs::u8path(check.glob);
    const fs::path dir = fs::u8path(root) / g.parent_path();
    const std::string pattern = u8_bytes(g.filename());
    std::int64_t count = 0;
    if (fs::is_directory(dir, ec)) {
        for (const auto& entry : fs::directory_iterator(dir, ec)) {
            if (entry.is_regular_file(ec) &&
                wildcard_match(pattern, u8_bytes(entry.path().filename())))
                ++count;
        }
    }
    if (count >= check.min_count) { r.status = AuditStatus::PASS; }
    else {
        r.status = AuditStatus::FAIL;
        r.detail = "glob " + check.glob + " count " +
                   std::to_string(count) + " < " +
                   std::to_string(check.min_count);
    }
    return r;
}

AuditCheckResult check_glob_contains(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    /* 平層 glob：每個 marker 必須在「至少一個」命中檔案中出現
     * （union 語義——marker 可分散於不同檔，對齊 Python join-scan）。
     * 目錄缺席或檔案不可讀 → FAIL（fail-closed），optional 才豁免。*/
    const fs::path g = fs::u8path(check.glob);
    const fs::path dir = fs::u8path(root) / g.parent_path();
    const std::string pattern = u8_bytes(g.filename());
    if (!fs::is_directory(dir, ec)) {
        if (check.optional) {
            r.status = AuditStatus::PASS;
        } else {
            r.status = AuditStatus::FAIL;
            r.detail = "missing dir for glob: " + check.glob;
        }
        return r;
    }
    std::vector<bool> found(check.markers.size(), false);
    size_t found_count = 0;
    for (const auto& entry : fs::directory_iterator(dir, ec)) {
        if (found_count == check.markers.size()) break;
        if (!entry.is_regular_file(ec) ||
            !wildcard_match(pattern, u8_bytes(entry.path().filename())))
            continue;
        std::string content;
        if (!read_file(entry.path(), &content)) continue;
        const std::string haystack =
            check.ignore_case ? to_lower(content) : content;
        for (size_t i = 0; i < check.markers.size(); ++i) {
            if (found[i]) continue;
            const std::string needle =
                check.ignore_case ? to_lower(check.markers[i])
                                  : check.markers[i];
            if (haystack.find(needle) != std::string::npos) {
                found[i] = true;
                ++found_count;
            }
        }
    }
    if (found_count == check.markers.size()) {
        r.status = AuditStatus::PASS;
    } else {
        for (size_t i = 0; i < check.markers.size(); ++i)
            if (!found[i]) {
                r.detail = "marker absent from glob matches: " +
                           check.markers[i];
                break;
            }
        r.status = AuditStatus::FAIL;
    }
    return r;
}

AuditCheckResult check_glob_not_contains(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    /* 平層 glob（parent 目錄 + 檔名 pattern）：每個命中檔案都不得
     * 含任一 marker。目錄缺席 → FAIL（fail-closed），optional 才豁免。*/
    const fs::path g = fs::u8path(check.glob);
    const fs::path dir = fs::u8path(root) / g.parent_path();
    const std::string pattern = u8_bytes(g.filename());
    if (!fs::is_directory(dir, ec)) {
        if (check.optional) {
            r.status = AuditStatus::PASS;
        } else {
            r.status = AuditStatus::FAIL;
            r.detail = "missing dir for glob: " + check.glob;
        }
        return r;
    }
    std::string hit_path, hit_marker;
    for (const auto& entry : fs::directory_iterator(dir, ec)) {
        if (!entry.is_regular_file(ec) ||
            !wildcard_match(pattern, u8_bytes(entry.path().filename())))
            continue;
        std::string content;
        if (!read_file(entry.path(), &content)) continue;
        const std::string haystack =
            check.ignore_case ? to_lower(content) : content;
        for (const auto& m : check.markers) {
            const std::string needle =
                check.ignore_case ? to_lower(m) : m;
            if (haystack.find(needle) != std::string::npos) {
                hit_marker = m;
                std::error_code rec;
                hit_path = u8_bytes(
                    fs::relative(entry.path(), root, rec));
                if (rec) hit_path = u8_bytes(entry.path().filename());
                break;
            }
        }
        if (!hit_marker.empty()) break;
    }
    if (hit_marker.empty()) { r.status = AuditStatus::PASS; }
    else {
        r.status = AuditStatus::FAIL;
        r.detail = "forbidden marker '" + hit_marker + "' in " +
                   hit_path;
    }
    return r;
}

AuditCheckResult check_glob_absent(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    /* 遞迴掃描 check.path 子樹（空字串 = 專案根）：檔名命中 check.glob
     * wildcard 即 FAIL。exclude 目錄名與 dotdir 於任意深度略過，
     * 對齊 Python os.walk + dirnames 修剪語義。*/
    const fs::path base = check.path.empty()
        ? fs::u8path(root)
        : fs::u8path(root) / fs::u8path(check.path);
    if (!fs::is_directory(base, ec)) {
        r.status = AuditStatus::PASS;   /* 無子樹 → 無命中 */
        return r;
    }
    auto excluded = [&](const fs::path& p) {
        const std::string name = u8_bytes(p.filename());
        if (!name.empty() && name[0] == '.') return true;
        for (const auto& ex : check.exclude)
            if (name == ex) return true;
        return false;
    };
    std::string hit;
    std::error_code iec;
    fs::recursive_directory_iterator it(
        base, fs::directory_options::skip_permission_denied, iec);
    const fs::recursive_directory_iterator dend;
    while (!iec && it != dend) {
        std::error_code sec;
        if (it->is_directory(sec)) {
            if (excluded(it->path())) it.disable_recursion_pending();
        } else if (it->is_regular_file(sec)) {
            if (wildcard_match(check.glob,
                               u8_bytes(it->path().filename()))) {
                std::error_code rec;
                hit = u8_bytes(fs::relative(it->path(), base, rec));
                if (rec) hit = u8_bytes(it->path().filename());
                break;
            }
        }
        it.increment(iec);
    }
    if (hit.empty()) { r.status = AuditStatus::PASS; }
    else {
        r.status = AuditStatus::FAIL;
        r.detail = "forbidden file present: " + hit;
    }
    return r;
}

AuditCheckResult check_tree_not_contains(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    /* glob-not-contains 的遞迴版：check.path 子樹（空字串=專案根）
     * 內檔名命中 check.glob 的每個檔案都不得含任一 marker。
     * exclude 目錄名與 dotdir 於任意深度略過（同 glob-absent）。
     * 子樹缺席 → FAIL（fail-closed），optional 才豁免。*/
    const fs::path base = check.path.empty()
        ? fs::u8path(root)
        : fs::u8path(root) / fs::u8path(check.path);
    if (!fs::is_directory(base, ec)) {
        if (check.optional) {
            r.status = AuditStatus::PASS;
        } else {
            r.status = AuditStatus::FAIL;
            r.detail = "missing subtree: " + check.path;
        }
        return r;
    }
    auto excluded = [&](const fs::path& p) {
        const std::string name = u8_bytes(p.filename());
        if (!name.empty() && name[0] == '.') return true;
        for (const auto& ex : check.exclude)
            if (name == ex) return true;
        return false;
    };
    std::string hit_path, hit_marker;
    std::error_code iec;
    fs::recursive_directory_iterator it(
        base, fs::directory_options::skip_permission_denied, iec);
    const fs::recursive_directory_iterator dend;
    while (!iec && it != dend && hit_marker.empty()) {
        std::error_code sec;
        if (it->is_directory(sec)) {
            if (excluded(it->path())) it.disable_recursion_pending();
        } else if (it->is_regular_file(sec)
                   && wildcard_match(
                       check.glob, u8_bytes(it->path().filename()))) {
            std::string content;
            if (read_file(it->path(), &content)) {
                const std::string haystack =
                    check.ignore_case ? to_lower(content) : content;
                for (const auto& m : check.markers) {
                    const std::string needle =
                        check.ignore_case ? to_lower(m) : m;
                    if (haystack.find(needle) != std::string::npos) {
                        hit_marker = m;
                        std::error_code rec;
                        hit_path = u8_bytes(
                            fs::relative(it->path(), base, rec));
                        if (rec)
                            hit_path =
                                u8_bytes(it->path().filename());
                        break;
                    }
                }
            }
        }
        it.increment(iec);
    }
    if (hit_marker.empty()) { r.status = AuditStatus::PASS; }
    else {
        r.status = AuditStatus::FAIL;
        r.detail = "forbidden marker in " + hit_path + ": " + hit_marker;
    }
    return r;
}

AuditCheckResult check_py_bucket_budget(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    /* Python-minimization ratchet (native replacement for the retired
     * pytest gate).  check.path = baseline JSON carrying the embedded
     * "measurement" recipe: scan_roots / exclude_dirs /
     * exclude_file_substr / rules (ordered first-match substring map)
     * / fallback_bucket, plus the zero_targets + allowed_zones budgets.
     * Any bucket measuring above its budget -> FAIL (only-tighten). */
    std::string content;
    if (!read_file(target, &content)) {
        r.status = AuditStatus::FAIL;
        r.detail = "unreadable: " + check.path;
        return r;
    }
    JsonValue doc;
    try { doc = JsonParser(content).parse(); }
    catch (const JsonError&) {
        r.status = AuditStatus::FAIL;
        r.detail = "invalid json: " + check.path;
        return r;
    }
    const JsonValue* meas = doc.get("measurement");
    const JsonValue* zero = doc.get("zero_targets");
    const JsonValue* allowed = doc.get("allowed_zones");
    if (meas == nullptr || meas->type != JsonValue::Type::Object ||
        zero == nullptr || zero->type != JsonValue::Type::Object ||
        allowed == nullptr || allowed->type != JsonValue::Type::Object) {
        r.status = AuditStatus::FAIL;
        r.detail = "baseline lacks measurement/budgets: " + check.path;
        return r;
    }
    auto str_list = [](const JsonValue* node) {
        std::vector<std::string> out;
        if (node != nullptr && node->type == JsonValue::Type::Array)
            for (const auto& e : node->array)
                if (e.type == JsonValue::Type::String)
                    out.push_back(e.string);
        return out;
    };
    const std::vector<std::string> roots =
        str_list(meas->get("scan_roots"));
    const std::vector<std::string> ex_dirs =
        str_list(meas->get("exclude_dirs"));
    const std::vector<std::string> ex_sub =
        str_list(meas->get("exclude_file_substr"));
    const JsonValue* rules = meas->get("rules");
    if (roots.empty() || rules == nullptr ||
        rules->type != JsonValue::Type::Object) {
        r.status = AuditStatus::FAIL;
        r.detail = "baseline measurement recipe incomplete";
        return r;
    }
    const JsonValue* fbv = meas->get("fallback_bucket");
    const std::string fallback =
        (fbv != nullptr && fbv->type == JsonValue::Type::String)
            ? fbv->string : "GENERAL_APP";

    std::map<std::string, std::pair<long long, long long>> actual;
    auto count_file = [&](const fs::path& fp) {
        /* 單一檔案歸類計數：exclude_file_substr 與 rules first-match
         * 語義與目錄掃描一致。 */
        if (fp.extension() != ".py") return;
        std::error_code rec;
        std::string rel = u8_bytes(fs::relative(fp, fs::u8path(root), rec));
        if (rec) return;
        for (auto& ch : rel)
            if (ch == '\\') ch = '/';
        rel = to_lower(rel);
        for (const auto& sub : ex_sub)
            if (rel.find(to_lower(sub)) != std::string::npos) return;
        std::string bucket = fallback;
        for (const auto& kv : rules->object) {
            bool hit = false;
            if (kv.second.type == JsonValue::Type::Array) {
                for (const auto& pv : kv.second.array) {
                    if (pv.type == JsonValue::Type::String &&
                        rel.find(pv.string) != std::string::npos) {
                        hit = true; break;
                    }
                }
            }
            if (hit) { bucket = kv.first; break; }
        }
        std::string fsrc;
        if (!read_file(fp, &fsrc)) return;
        const long long loc =
            static_cast<long long>(
                std::count(fsrc.begin(), fsrc.end(), '\n')) +
            ((!fsrc.empty() && fsrc.back() != '\n') ? 1 : 0);
        auto& slot = actual[bucket];
        slot.first += 1;
        slot.second += loc;
    };
    for (const auto& rr : roots) {
        const fs::path base = fs::u8path(root) / fs::u8path(rr);
        if (fs::is_regular_file(base, ec)) {
            /* 檔案級 root（如 main-system/run.py）直接計量。 */
            count_file(base);
            continue;
        }
        if (!fs::is_directory(base, ec)) continue;
        std::error_code iec;
        fs::recursive_directory_iterator it(
            base, fs::directory_options::skip_permission_denied, iec);
        const fs::recursive_directory_iterator dend;
        while (!iec && it != dend) {
            std::error_code sec;
            if (it->is_directory(sec)) {
                const std::string dn = u8_bytes(it->path().filename());
                for (const auto& ex : ex_dirs)
                    if (dn == ex) {
                        it.disable_recursion_pending();
                        break;
                    }
            } else if (it->is_regular_file(sec)) {
                count_file(it->path());
            }
            it.increment(iec);
        }
    }
    std::string viol;
    auto check_budget = [&](const JsonValue& budgets) {
        for (const auto& kv : budgets.object) {
            if (kv.second.type != JsonValue::Type::Object) continue;
            const JsonValue* bf = kv.second.get("files");
            const JsonValue* bl = kv.second.get("loc");
            const long long bf_v =
                (bf && bf->type == JsonValue::Type::Number)
                    ? (long long)bf->number : -1;
            const long long bl_v =
                (bl && bl->type == JsonValue::Type::Number)
                    ? (long long)bl->number : -1;
            const auto got = actual.find(kv.first);
            const long long af =
                got == actual.end() ? 0 : got->second.first;
            const long long al =
                got == actual.end() ? 0 : got->second.second;
            if (af > bf_v)
                viol += " " + kv.first + " files " +
                        std::to_string(af) + ">" +
                        std::to_string(bf_v) + ";";
            if (al > bl_v)
                viol += " " + kv.first + " loc " +
                        std::to_string(al) + ">" +
                        std::to_string(bl_v) + ";";
        }
    };
    check_budget(*zero);
    check_budget(*allowed);
    if (viol.empty()) {
        r.status = AuditStatus::PASS;
    } else {
        r.status = AuditStatus::FAIL;
        r.detail = "python-minimization ratchet violated:" + viol;
    }
    return r;
}
