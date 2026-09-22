/* jsonlite.h — 共用極簡 JSON 讀寫（純標準庫，無第三方依賴）。
 *
 * 來源：自 native/audit/audit_engine.cpp 抽出（P0-9），供審計引擎與
 * governed transport proxy 客戶端共用。語義不變：
 *  - JsonValue：Null/Bool/Number/String/Array/Object；物件保留鍵序
 *  - JsonParser：嚴格解析，任何異形輸入 throw JsonError（fail-closed）
 *  - json_escape/json_serialize：輸出側（字串逸出、值序列化）
 *
 * 依賴最小化政策（藍圖 §1.1）：不引入 nlohmann/RapidJSON。
 */
#ifndef GPTBRIDGE_JSONLITE_H
#define GPTBRIDGE_JSONLITE_H

#include <cctype>
#include <string>
#include <utility>
#include <vector>

namespace gptbridge {
namespace jsonlite {

struct JsonValue {
    enum class Type { Null, Bool, Number, String, Array, Object } type = Type::Null;
    bool boolean = false;
    double number = 0.0;
    std::string string;
    std::vector<JsonValue> array;
    std::vector<std::pair<std::string, JsonValue>> object;

    const JsonValue* get(const std::string& key) const {
        if (type != Type::Object) return nullptr;
        for (const auto& kv : object)
            if (kv.first == key) return &kv.second;
        return nullptr;
    }
};

struct JsonError {};

class JsonParser {
public:
    explicit JsonParser(const std::string& text) : p_(text.data()), end_(p_ + text.size()) {}

    JsonValue parse() {
        skip_ws();
        JsonValue v = value();
        skip_ws();
        if (p_ != end_) throw JsonError{};
        return v;
    }

private:
    const char* p_;
    const char* end_;

    void skip_ws() {
        while (p_ < end_ && (*p_ == ' ' || *p_ == '\t' || *p_ == '\n' || *p_ == '\r')) ++p_;
    }
    char peek() { return p_ < end_ ? *p_ : '\0'; }
    char take() { if (p_ >= end_) throw JsonError{}; return *p_++; }
    void expect(char c) { if (take() != c) throw JsonError{}; }

    JsonValue value() {
        skip_ws();
        switch (peek()) {
            case '{': return object_value();
            case '[': return array_value();
            case '"': { JsonValue v; v.type = JsonValue::Type::String; v.string = string_value(); return v; }
            case 't': literal("true"); { JsonValue v; v.type = JsonValue::Type::Bool; v.boolean = true; return v; }
            case 'f': literal("false"); { JsonValue v; v.type = JsonValue::Type::Bool; v.boolean = false; return v; }
            case 'n': literal("null"); return JsonValue{};
            default: return number_value();
        }
    }

    void literal(const char* word) {
        while (*word) { if (take() != *word++) throw JsonError{}; }
    }

    JsonValue object_value() {
        JsonValue v; v.type = JsonValue::Type::Object;
        expect('{');
        skip_ws();
        if (peek() == '}') { ++p_; return v; }
        for (;;) {
            skip_ws();
            expect('"');
            --p_;
            std::string key = string_value();
            skip_ws();
            expect(':');
            v.object.emplace_back(std::move(key), value());
            skip_ws();
            char c = take();
            if (c == '}') return v;
            if (c != ',') throw JsonError{};
        }
    }

    JsonValue array_value() {
        JsonValue v; v.type = JsonValue::Type::Array;
        expect('[');
        skip_ws();
        if (peek() == ']') { ++p_; return v; }
        for (;;) {
            v.array.push_back(value());
            skip_ws();
            char c = take();
            if (c == ']') return v;
            if (c != ',') throw JsonError{};
        }
    }

    std::string string_value() {
        std::string out;
        expect('"');
        for (;;) {
            char c = take();
            if (c == '"') return out;
            if (c == '\\') {
                char e = take();
                switch (e) {
                    case '"': out += '"'; break;
                    case '\\': out += '\\'; break;
                    case '/': out += '/'; break;
                    case 'b': out += '\b'; break;
                    case 'f': out += '\f'; break;
                    case 'n': out += '\n'; break;
                    case 'r': out += '\r'; break;
                    case 't': out += '\t'; break;
                    case 'u': {
                        /* \uXXXX → UTF-8 (BMP + surrogate pairs) */
                        unsigned cp = hex4();
                        if (cp >= 0xD800 && cp <= 0xDBFF) {
                            if (take() == '\\' && take() == 'u') {
                                unsigned lo = hex4();
                                if (lo >= 0xDC00 && lo <= 0xDFFF) {
                                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                                }
                            } else throw JsonError{};
                        }
                        append_utf8(out, cp);
                        break;
                    }
                    default: throw JsonError{};
                }
            } else {
                out += c;
            }
        }
    }

    unsigned hex4() {
        unsigned v = 0;
        for (int i = 0; i < 4; ++i) {
            char c = take();
            v <<= 4;
            if (c >= '0' && c <= '9') v += c - '0';
            else if (c >= 'a' && c <= 'f') v += c - 'a' + 10;
            else if (c >= 'A' && c <= 'F') v += c - 'A' + 10;
            else throw JsonError{};
        }
        return v;
    }

    static void append_utf8(std::string& out, unsigned cp) {
        if (cp < 0x80) out += static_cast<char>(cp);
        else if (cp < 0x800) {
            out += static_cast<char>(0xC0 | (cp >> 6));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        } else if (cp < 0x10000) {
            out += static_cast<char>(0xE0 | (cp >> 12));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        } else {
            out += static_cast<char>(0xF0 | (cp >> 18));
            out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        }
    }

    JsonValue number_value() {
        const char* start = p_;
        if (peek() == '-') ++p_;
        while (p_ < end_ && (std::isdigit(static_cast<unsigned char>(*p_)) ||
               *p_ == '.' || *p_ == 'e' || *p_ == 'E' || *p_ == '+' || *p_ == '-'))
            ++p_;
        if (p_ == start) throw JsonError{};
        JsonValue v;
        v.type = JsonValue::Type::Number;
        try { v.number = std::stod(std::string(start, p_)); }
        catch (...) { throw JsonError{}; }
        return v;
    }
};

/* ------------------------------------------------------------------
 * Output side: string escaping and value serialization.
 * ------------------------------------------------------------------ */

inline std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (unsigned char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
    return out;
}

inline std::string json_serialize(const JsonValue& v) {
    switch (v.type) {
        case JsonValue::Type::Null: return "null";
        case JsonValue::Type::Bool: return v.boolean ? "true" : "false";
        case JsonValue::Type::Number: {
            if (v.number == static_cast<double>(static_cast<long long>(v.number)) &&
                v.number >= -9.0e15 && v.number <= 9.0e15) {
                char buf[24];
                std::snprintf(buf, sizeof(buf), "%lld",
                              static_cast<long long>(v.number));
                return buf;
            }
            char buf[32];
            std::snprintf(buf, sizeof(buf), "%.17g", v.number);
            return buf;
        }
        case JsonValue::Type::String:
            return "\"" + json_escape(v.string) + "\"";
        case JsonValue::Type::Array: {
            std::string out = "[";
            for (size_t i = 0; i < v.array.size(); ++i) {
                if (i) out += ',';
                out += json_serialize(v.array[i]);
            }
            return out + ']';
        }
        case JsonValue::Type::Object: {
            std::string out = "{";
            for (size_t i = 0; i < v.object.size(); ++i) {
                if (i) out += ',';
                out += "\"" + json_escape(v.object[i].first) + "\":";
                out += json_serialize(v.object[i].second);
            }
            return out + '}';
        }
    }
    return "null";
}

} // namespace jsonlite
} // namespace gptbridge

#endif /* GPTBRIDGE_JSONLITE_H */
