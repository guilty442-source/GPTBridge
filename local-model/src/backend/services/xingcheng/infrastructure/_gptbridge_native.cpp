#include <pybind11/pybind11.h>
#include <cstddef>
#include <cmath>

namespace py = pybind11;

// Mirror of StarTransformerRuntime._estimated_token_count
//  - CJK / wide characters (ord(c) > 0x7F) count as one token each
//  - Latin (and other ASCII) characters count as ~3.5 per token
long token_estimate(const std::string& text) {
    const std::size_t length = text.size();
    if (length == 0) {
        return 0L;
    }
    // In UTF-8 each code point is encoded by exactly one lead byte
    // (0b0xxxxxxx or 0b11xxxxxx); continuation bytes all start with 0b10.
    // A code point is "wide" (ord > 0x7F) iff its lead byte has the top bit set.
    std::size_t cjk_or_wide = 0;
    std::size_t code_points = 0;
    for (std::size_t i = 0; i < length; ++i) {
        const unsigned char byte = static_cast<unsigned char>(text[i]);
        const bool is_lead = (byte & 0xC0) != 0x80;
        if (is_lead) {
            ++code_points;
            if (byte >= 0x80) {
                ++cjk_or_wide;
            }
        }
    }
    const long latin = static_cast<long>(code_points) - static_cast<long>(cjk_or_wide);
    return static_cast<long>(std::ceil(static_cast<double>(cjk_or_wide) + (static_cast<double>(latin) / 3.5)));
}

PYBIND11_MODULE(_gptbridge_native, m) {
    m.doc() = "GPTBridge native helpers (hybrid Python/C++ architecture).";
    m.def("token_estimate", &token_estimate,
          py::arg("text"),
          "Estimate token count: CJK/wide chars count 1 token each, Latin ~3.5 per token (mirrors Python _estimated_token_count).");
}
