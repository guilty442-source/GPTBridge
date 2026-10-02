// engine_json.h — B94 fragment of engine.cpp (InferenceError/JsonValue/JsonParser/json helpers).
// Included once by engine.cpp inside namespace xingcheng::inference.
#pragma once

namespace {

// R6: positions per KV page block.
constexpr int64_t kKvBlockTokens = 16;

class InferenceError : public std::runtime_error {
public:
    explicit InferenceError(const std::string& message) : std::runtime_error(message) {}
};

// ── Minimal JSON parser ────────────────────────────────────────────────
// The bundle format only needs standards-compliant JSON values. The parser is
// deliberately bounded by input size and recursion depth and fails closed.

struct JsonValue {
    enum class Type { Null, Bool, Number, String, Array, Object };
    Type type = Type::Null;
    bool boolean = false;
    double number = 0.0;
    std::string string;
    std::vector<JsonValue> array;
    std::map<std::string, JsonValue> object;
};

void append_utf8(std::string& out, uint32_t cp) {
    if (cp <= 0x7F) {
        out.push_back(static_cast<char>(cp));
    } else if (cp <= 0x7FF) {
        out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp <= 0xFFFF) {
        out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp <= 0x10FFFF) {
        out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
        throw InferenceError("JSON_INVALID_UNICODE");
    }
}

// Byte-level BPE decode can end mid-codepoint when generation stops on a
// token cap — sanitize so decode() always yields valid UTF-8 (U+FFFD for
// invalid bytes / truncated tails, matching errors='replace' semantics).
std::string sanitize_utf8(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    size_t i = 0;
    while (i < in.size()) {
        const unsigned char c = static_cast<unsigned char>(in[i]);
        if (c < 0x80) {
            out.push_back(in[i]);
            ++i;
            continue;
        }
        size_t width = 0;
        if ((c & 0xE0) == 0xC0) width = 2;
        else if ((c & 0xF0) == 0xE0) width = 3;
        else if ((c & 0xF8) == 0xF0) width = 4;
        bool valid = width > 0 && i + width <= in.size();
        if (valid) {
            for (size_t k = 1; k < width; ++k) {
                if ((static_cast<unsigned char>(in[i + k]) & 0xC0) != 0x80) {
                    valid = false;
                    break;
                }
            }
        }
        if (valid) {
            out.append(in, i, width);
            i += width;
        } else {
            out += "\xEF\xBF\xBD";
            // Truncated tail: consume the whole partial sequence so it
            // becomes one replacement char like Python's errors='replace'.
            if (width > 0) {
                i += 1;
                while (i < in.size() &&
                       (static_cast<unsigned char>(in[i]) & 0xC0) == 0x80) {
                    ++i;
                }
            } else {
                ++i;
            }
        }
    }
    return out;
}

class JsonParser {
public:
    explicit JsonParser(std::string_view input) : input_(input) {
        if (input_.size() > 64 * 1024 * 1024) {
            throw InferenceError("JSON_INPUT_TOO_LARGE");
        }
    }

    JsonValue parse() {
        JsonValue value = parse_value(0);
        skip_ws();
        if (pos_ != input_.size()) {
            throw InferenceError("JSON_TRAILING_CONTENT");
        }
        return value;
    }

private:
    std::string_view input_;
    size_t pos_ = 0;

    void skip_ws() {
        while (pos_ < input_.size() &&
               std::isspace(static_cast<unsigned char>(input_[pos_]))) {
            ++pos_;
        }
    }

    char peek() const {
        return pos_ < input_.size() ? input_[pos_] : '\0';
    }

    char take() {
        if (pos_ >= input_.size()) {
            throw InferenceError("JSON_UNEXPECTED_EOF");
        }
        return input_[pos_++];
    }

    void expect(char expected) {
        if (take() != expected) {
            throw InferenceError("JSON_UNEXPECTED_CHARACTER");
        }
    }

    bool consume(std::string_view literal) {
        if (input_.substr(pos_, literal.size()) == literal) {
            pos_ += literal.size();
            return true;
        }
        return false;
    }

    JsonValue parse_value(int depth) {
        if (depth > 128) {
            throw InferenceError("JSON_DEPTH_EXCEEDED");
        }
        skip_ws();
        const char ch = peek();
        if (ch == 'n') {
            if (!consume("null")) throw InferenceError("JSON_INVALID_LITERAL");
            return JsonValue{};
        }
        if (ch == 't') {
            if (!consume("true")) throw InferenceError("JSON_INVALID_LITERAL");
            JsonValue value;
            value.type = JsonValue::Type::Bool;
            value.boolean = true;
            return value;
        }
        if (ch == 'f') {
            if (!consume("false")) throw InferenceError("JSON_INVALID_LITERAL");
            JsonValue value;
            value.type = JsonValue::Type::Bool;
            return value;
        }
        if (ch == '"') {
            JsonValue value;
            value.type = JsonValue::Type::String;
            value.string = parse_string();
            return value;
        }
        if (ch == '[') {
            return parse_array(depth + 1);
        }
        if (ch == '{') {
            return parse_object(depth + 1);
        }
        return parse_number();
    }

    std::string parse_string() {
        expect('"');
        std::string out;
        while (true) {
            const char ch = take();
            if (ch == '"') {
                return out;
            }
            if (static_cast<unsigned char>(ch) < 0x20) {
                throw InferenceError("JSON_UNESCAPED_CONTROL");
            }
            if (ch != '\\') {
                out.push_back(ch);
                continue;
            }
            const char esc = take();
            switch (esc) {
                case '"': out.push_back('"'); break;
                case '\\': out.push_back('\\'); break;
                case '/': out.push_back('/'); break;
                case 'b': out.push_back('\b'); break;
                case 'f': out.push_back('\f'); break;
                case 'n': out.push_back('\n'); break;
                case 'r': out.push_back('\r'); break;
                case 't': out.push_back('\t'); break;
                case 'u': {
                    uint32_t cp = parse_hex4();
                    if (cp >= 0xD800 && cp <= 0xDBFF) {
                        if (take() != '\\' || take() != 'u') {
                            throw InferenceError("JSON_INVALID_SURROGATE");
                        }
                        const uint32_t low = parse_hex4();
                        if (low < 0xDC00 || low > 0xDFFF) {
                            throw InferenceError("JSON_INVALID_SURROGATE");
                        }
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00);
                    }
                    append_utf8(out, cp);
                    break;
                }
                default:
                    throw InferenceError("JSON_INVALID_ESCAPE");
            }
        }
    }

    uint32_t parse_hex4() {
        uint32_t value = 0;
        for (int i = 0; i < 4; ++i) {
            const char ch = take();
            value <<= 4;
            if (ch >= '0' && ch <= '9') value += static_cast<uint32_t>(ch - '0');
            else if (ch >= 'a' && ch <= 'f') value += static_cast<uint32_t>(ch - 'a' + 10);
            else if (ch >= 'A' && ch <= 'F') value += static_cast<uint32_t>(ch - 'A' + 10);
            else throw InferenceError("JSON_INVALID_UNICODE_ESCAPE");
        }
        return value;
    }

    JsonValue parse_number() {
        const size_t start = pos_;
        if (peek() == '-') ++pos_;
        if (!std::isdigit(static_cast<unsigned char>(peek()))) {
            throw InferenceError("JSON_INVALID_NUMBER");
        }
        if (peek() == '0') {
            ++pos_;
        } else {
            while (std::isdigit(static_cast<unsigned char>(peek()))) ++pos_;
        }
        if (peek() == '.') {
            ++pos_;
            if (!std::isdigit(static_cast<unsigned char>(peek()))) {
                throw InferenceError("JSON_INVALID_NUMBER");
            }
            while (std::isdigit(static_cast<unsigned char>(peek()))) ++pos_;
        }
        if (peek() == 'e' || peek() == 'E') {
            ++pos_;
            if (peek() == '+' || peek() == '-') ++pos_;
            if (!std::isdigit(static_cast<unsigned char>(peek()))) {
                throw InferenceError("JSON_INVALID_NUMBER");
            }
            while (std::isdigit(static_cast<unsigned char>(peek()))) ++pos_;
        }
        JsonValue value;
        value.type = JsonValue::Type::Number;
        value.number = std::stod(std::string(input_.substr(start, pos_ - start)));
        return value;
    }

    JsonValue parse_array(int depth) {
        expect('[');
        JsonValue value;
        value.type = JsonValue::Type::Array;
        skip_ws();
        if (peek() == ']') {
            ++pos_;
            return value;
        }
        while (true) {
            value.array.push_back(parse_value(depth));
            skip_ws();
            const char ch = take();
            if (ch == ']') return value;
            if (ch != ',') throw InferenceError("JSON_ARRAY_SEPARATOR_EXPECTED");
        }
    }

    JsonValue parse_object(int depth) {
        expect('{');
        JsonValue value;
        value.type = JsonValue::Type::Object;
        skip_ws();
        if (peek() == '}') {
            ++pos_;
            return value;
        }
        while (true) {
            skip_ws();
            if (peek() != '"') throw InferenceError("JSON_OBJECT_KEY_EXPECTED");
            std::string key = parse_string();
            skip_ws();
            expect(':');
            value.object.emplace(std::move(key), parse_value(depth));
            skip_ws();
            const char ch = take();
            if (ch == '}') return value;
            if (ch != ',') throw InferenceError("JSON_OBJECT_SEPARATOR_EXPECTED");
        }
    }
};

const JsonValue& json_field(const JsonValue& object, const char* name) {
    if (object.type != JsonValue::Type::Object) {
        throw InferenceError("JSON_OBJECT_EXPECTED");
    }
    const auto it = object.object.find(name);
    if (it == object.object.end()) {
        throw InferenceError(std::string("JSON_FIELD_MISSING:") + name);
    }
    return it->second;
}

const JsonValue* json_optional(const JsonValue& object, const char* name) {
    if (object.type != JsonValue::Type::Object) return nullptr;
    const auto it = object.object.find(name);
    return it == object.object.end() ? nullptr : &it->second;
}

std::string json_string(const JsonValue& object, const char* name) {
    const JsonValue& value = json_field(object, name);
    if (value.type != JsonValue::Type::String) {
        throw InferenceError(std::string("JSON_STRING_EXPECTED:") + name);
    }
    return value.string;
}

int64_t json_int(const JsonValue& object, const char* name) {
    const JsonValue& value = json_field(object, name);
    if (value.type != JsonValue::Type::Number ||
        value.number < static_cast<double>(std::numeric_limits<int64_t>::min()) ||
        value.number > static_cast<double>(std::numeric_limits<int64_t>::max()) ||
        std::floor(value.number) != value.number) {
        throw InferenceError(std::string("JSON_INT_EXPECTED:") + name);
    }
    return static_cast<int64_t>(value.number);
}

double json_number(const JsonValue& object, const char* name) {
    const JsonValue& value = json_field(object, name);
    if (value.type != JsonValue::Type::Number) {
        throw InferenceError(std::string("JSON_NUMBER_EXPECTED:") + name);
    }
    return value.number;
}

bool json_bool(const JsonValue& object, const char* name) {
    const JsonValue& value = json_field(object, name);
    if (value.type != JsonValue::Type::Bool) {
        throw InferenceError(std::string("JSON_BOOL_EXPECTED:") + name);
    }
    return value.boolean;
}

std::vector<int64_t> json_shape(const JsonValue& value) {
    if (value.type != JsonValue::Type::Array) {
        throw InferenceError("TENSOR_SHAPE_EXPECTED");
    }
    std::vector<int64_t> shape;
    for (const JsonValue& dim : value.array) {
        if (dim.type != JsonValue::Type::Number || dim.number <= 0 ||
            std::floor(dim.number) != dim.number ||
            dim.number > static_cast<double>(std::numeric_limits<int64_t>::max())) {
            throw InferenceError("TENSOR_SHAPE_INVALID");
        }
        shape.push_back(static_cast<int64_t>(dim.number));
    }
    return shape;
}
