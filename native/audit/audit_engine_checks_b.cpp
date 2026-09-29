// check-kind executors, part b (unity-included by audit_engine.cpp)

AuditCheckResult check_json_key_value(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    /* markers 格式 "<dotted.path><op><literal>"：
     *   "="  等值（Bool/Number/String 型別比對）
     *   "!=" 不等值（路徑存在時值必須不同；路徑缺席視為 PASS）
     *   "^=" 字串前綴
     *   ">=" 數值下限
     * 路徑逐層走 object；段名可帶 [KEY] 選取 object 陣列中
     * id==KEY 的元素；缺鍵／型別不符／檔案不可讀 → FAIL。*/
    bool unreadable = false;
    const auto docp = cached_json(target, &unreadable);
    if (!docp) {
        if (unreadable && check.optional && !fs::exists(target, ec)) {
            r.status = AuditStatus::PASS;
            return r;
        }
        r.status = AuditStatus::FAIL;
        r.detail = (unreadable ? "unreadable: " : "invalid json: ")
            + check.path;
        return r;
    }
    const JsonValue& doc = *docp;
    for (const auto& marker : check.markers) {
        size_t op_pos = std::string::npos;
        std::string op;
        for (const char* cand : {"!=", "^=", ">=", "="}) {
            const size_t pos = marker.find(cand);
            if (pos != std::string::npos) {
                op_pos = pos; op = cand; break;
            }
        }
        if (op_pos == std::string::npos || op_pos == 0) {
            r.status = AuditStatus::FAIL;
            r.detail = "malformed json-key-value marker: " + marker;
            return r;
        }
        const std::string dotted = marker.substr(0, op_pos);
        const std::string literal = marker.substr(op_pos + op.size());
        const JsonValue* node = resolve_json_path(doc, dotted);
        bool ok = false;
        if (op == "!=") {
            /* 不等值斷言：路徑缺席 → 值不可能等於 literal → PASS；
             * 路徑存在 → 型別化比對，等值即 FAIL。 */
            if (node == nullptr) {
                ok = true;
            } else if (literal == "true" || literal == "false") {
                ok = !(node->type == JsonValue::Type::Bool &&
                       node->boolean == (literal == "true"));
            } else if (node->type == JsonValue::Type::Number) {
                try {
                    ok = node->number != std::stod(literal);
                } catch (...) { ok = false; }
            } else if (node->type == JsonValue::Type::String) {
                ok = node->string != literal;
            } else {
                ok = true;
            }
        } else {
            if (node == nullptr) {
                r.status = AuditStatus::FAIL;
                r.detail = "missing json path: " + dotted;
                return r;
            }
            if (op == "=") {
                if (literal == "true" || literal == "false") {
                    ok = node->type == JsonValue::Type::Bool &&
                         node->boolean == (literal == "true");
                } else if (node->type == JsonValue::Type::Number) {
                    try {
                        ok = node->number == std::stod(literal);
                    } catch (...) { ok = false; }
                } else if (node->type == JsonValue::Type::String) {
                    ok = node->string == literal;
                }
            } else if (op == "^=") {
                ok = node->type == JsonValue::Type::String &&
                     node->string.size() >= literal.size() &&
                     node->string.compare(
                         0, literal.size(), literal) == 0;
            } else { /* ">=" */
                if (node->type == JsonValue::Type::Number) {
                    try {
                        ok = node->number >= std::stod(literal);
                    } catch (...) { ok = false; }
                }
            }
        }
        if (!ok) {
            r.status = AuditStatus::FAIL;
            r.detail = "json path check failed: " + marker;
            return r;
        }
    }
    r.status = AuditStatus::PASS;
    return r;
}

AuditCheckResult check_json_key_absent(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    /* markers 為不得存在的 dotted 路徑（同 json-key-value 的路徑
     * 語法）；任一路徑可解析 → FAIL。缺席／null 皆視為不存在。*/
    bool unreadable = false;
    const auto docp = cached_json(target, &unreadable);
    if (!docp) {
        if (unreadable && check.optional && !fs::exists(target, ec)) {
            r.status = AuditStatus::PASS;
            return r;
        }
        r.status = AuditStatus::FAIL;
        r.detail = (unreadable ? "unreadable: " : "invalid json: ")
            + check.path;
        return r;
    }
    const JsonValue& doc = *docp;
    for (const auto& marker : check.markers) {
        const JsonValue* node = resolve_json_path(doc, marker);
        if (node != nullptr && node->type != JsonValue::Type::Null) {
            r.status = AuditStatus::FAIL;
            r.detail = "forbidden json key present: " + marker;
            return r;
        }
    }
    r.status = AuditStatus::PASS;
    return r;
}

AuditCheckResult check_json_array_min_count(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    /* items 指定陣列路徑（空 → 文件根即陣列）；每個 object 元素以
     * markers 逐條作「欄位<op>literal」謂詞過濾（op 同
     * json-key-value：!= ^= >= =；欄位缺席時 != 視為成立）。
     * 全部謂詞成立者計 1；計數 >= min_count → PASS。 */
    bool unreadable = false;
    const auto docp = cached_json(target, &unreadable);
    if (!docp) {
        if (unreadable && check.optional && !fs::exists(target, ec)) {
            r.status = AuditStatus::PASS;
            return r;
        }
        r.status = AuditStatus::FAIL;
        r.detail = (unreadable ? "unreadable: " : "invalid json: ")
            + check.path;
        return r;
    }
    const JsonValue& doc = *docp;
    const JsonValue* arr =
        check.items.empty() ? &doc : resolve_json_path(doc, check.items);
    if (arr == nullptr || arr->type != JsonValue::Type::Array) {
        r.status = AuditStatus::FAIL;
        r.detail = "missing json array: " +
            (check.items.empty() ? std::string("<root>") : check.items);
        return r;
    }
    /* Marker predicates are element-invariant: path/op/literal are parsed
     * once per check instead of per array element (the previous version
     * re-ran find×4 + two substr allocations + stod for every row of the
     * mirror tables — the dominant audit cost). */
    struct Pred {
        std::string path;
        std::string op;
        std::string literal;
        bool literal_bool = false;
        bool literal_is_bool = false;
        double literal_num = 0.0;
        bool literal_num_ok = false;
    };
    std::vector<Pred> preds;
    preds.reserve(check.markers.size());
    for (const auto& marker : check.markers) {
        Pred p;
        size_t op_pos = std::string::npos;
        for (const char* cand : {"!=", "^=", ">=", "="}) {
            const size_t pos = marker.find(cand);
            if (pos != std::string::npos) {
                op_pos = pos; p.op = cand; break;
            }
        }
        if (op_pos == std::string::npos || op_pos == 0) {
            r.status = AuditStatus::FAIL;
            r.detail = "malformed array predicate: " + marker;
            return r;
        }
        p.path = marker.substr(0, op_pos);
        p.literal = marker.substr(op_pos + p.op.size());
        if (p.literal == "true" || p.literal == "false") {
            p.literal_is_bool = true;
            p.literal_bool = p.literal == "true";
        }
        try { p.literal_num = std::stod(p.literal);
              p.literal_num_ok = true; }
        catch (...) { }
        preds.push_back(std::move(p));
    }
    std::int64_t count = 0;
    for (const auto& elem : arr->array) {
        if (elem.type != JsonValue::Type::Object) continue;
        bool match = true;
        for (const auto& p : preds) {
            const JsonValue* fv = resolve_json_path(elem, p.path);
            bool ok;
            if (p.op == "!=") {
                if (fv == nullptr ||
                    fv->type == JsonValue::Type::Null) {
                    ok = true;
                } else if (p.literal_is_bool) {
                    ok = !(fv->type == JsonValue::Type::Bool &&
                           fv->boolean == p.literal_bool);
                } else if (fv->type == JsonValue::Type::Number) {
                    ok = p.literal_num_ok &&
                         fv->number != p.literal_num;
                } else if (fv->type == JsonValue::Type::String) {
                    ok = fv->string != p.literal;
                } else {
                    ok = true;
                }
            } else if (p.op == "^=") {
                ok = fv != nullptr &&
                     fv->type == JsonValue::Type::String &&
                     fv->string.size() >= p.literal.size() &&
                     fv->string.compare(
                         0, p.literal.size(), p.literal) == 0;
            } else if (p.op == ">=") {
                ok = fv != nullptr &&
                     fv->type == JsonValue::Type::Number &&
                     p.literal_num_ok &&
                     fv->number >= p.literal_num;
            } else { /* "=" */
                if (fv == nullptr) {
                    ok = false;
                } else if (p.literal_is_bool) {
                    ok = fv->type == JsonValue::Type::Bool &&
                         fv->boolean == p.literal_bool;
                } else if (fv->type == JsonValue::Type::Number) {
                    ok = p.literal_num_ok &&
                         fv->number == p.literal_num;
                } else {
                    ok = fv->type == JsonValue::Type::String &&
                         fv->string == p.literal;
                }
            }
            if (!ok) { match = false; break; }
        }
        if (match) ++count;
    }
    if (count >= check.min_count) {
        r.status = AuditStatus::PASS;
    } else {
        r.status = AuditStatus::FAIL;
        r.detail = "array " +
            (check.items.empty() ? std::string("<root>") : check.items) +
            " matched " + std::to_string(count) +
            " < min_count " + std::to_string(check.min_count);
    }
    return r;
}

AuditCheckResult check_text_no_pollution(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    const auto text = cached_text(target);
    if (!text) {
        r.status = AuditStatus::FAIL; r.detail = "unreadable: " + check.path;
        return r;
    }
    if (contains_pollution(*text)) {
        r.status = AuditStatus::FAIL; r.detail = "pollution: " + check.path;
    } else {
        r.status = AuditStatus::PASS;
    }
    return r;
}

AuditCheckResult check_json_parses(const AuditCheck& check,
    const std::string& root,
    const fs::path& target,
                                   std::error_code& ec) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    bool unreadable = false;
    r.status = cached_json(target, &unreadable)
        ? AuditStatus::PASS : AuditStatus::FAIL;
    if (r.status == AuditStatus::FAIL)
        r.detail = (unreadable ? "unreadable: " : "invalid json: ")
            + check.path;
    return r;
}
