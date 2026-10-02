// engine_state.h — NativeStateManager / DeltaNet state snapshot support
// (architecture-convergence §23/§27).
//
// The engine owns the raw state; this header provides the serialized
// per-slot image of lin_states_ so the capability layer can checkpoint,
// compact and resume long-horizon tasks without unbounded prompt growth.
//
// Blob layout (little-endian, self-describing):
//   u32 magic 'DLTS' | u32 version=1 | i64 slot | i64 layers |
//   per layer: u8 present | i64 conv_len | conv_len*f64 |
//              i64 s_len | s_len*f64 | i64 tokens
//
// Binding to a model hash + generation is the caller's envelope
// (star-delta-state-snapshot/v1): the blob itself is raw state and is
// never accepted across generations by the manager contract.

bool NativeInferenceEngine::has_delta_state() const {
    return bundle_ != nullptr && bundle_->config().has_linear_layers();
}

int64_t NativeInferenceEngine::delta_state_bytes(int64_t slot) const {
    if (!has_delta_state()) return 0;
    if (slot < 0 || slot >= static_cast<int64_t>(lin_states_.size())) {
        return 0;
    }
    int64_t bytes = 4 + 4 + 8 + 8;   // magic + version + slot + layers
    for (const LinLayerState& st :
         lin_states_[static_cast<size_t>(slot)]) {
        bytes += 1 + 8 + 8 + 8;      // present + lens + tokens
        bytes += static_cast<int64_t>(st.conv_tail.size() +
                                      st.s.size()) *
                 static_cast<int64_t>(sizeof(double));
    }
    return bytes;
}

namespace {
constexpr uint32_t kDeltaStateMagic = 0x53544C44u;   // 'DLTS'
constexpr uint32_t kDeltaStateVersion = 1;

void ds_put_u8(std::vector<char>& o, uint8_t v) {
    o.push_back(static_cast<char>(v));
}
void ds_put_u32(std::vector<char>& o, uint32_t v) {
    for (int i = 0; i < 4; ++i) o.push_back(static_cast<char>(v >> (8 * i)));
}
void ds_put_i64(std::vector<char>& o, int64_t v) {
    for (int i = 0; i < 8; ++i) o.push_back(static_cast<char>(v >> (8 * i)));
}
void ds_put_f64v(std::vector<char>& o, const std::vector<double>& v) {
    ds_put_i64(o, static_cast<int64_t>(v.size()));
    if (!v.empty()) {
        const char* p = reinterpret_cast<const char*>(v.data());
        o.insert(o.end(), p, p + v.size() * sizeof(double));
    }
}

struct DsCur {
    const char* p;
    const char* end;
    bool ok = true;
    uint8_t u8() {
        if (end - p < 1) { ok = false; return 0; }
        return static_cast<uint8_t>(*p++);
    }
    uint32_t u32() {
        if (end - p < 4) { ok = false; return 0; }
        uint32_t v = 0;
        for (int i = 0; i < 4; ++i)
            v |= static_cast<uint32_t>(static_cast<uint8_t>(p[i])) << (8 * i);
        p += 4;
        return v;
    }
    int64_t i64() {
        if (end - p < 8) { ok = false; return 0; }
        uint64_t v = 0;
        for (int i = 0; i < 8; ++i)
            v |= static_cast<uint64_t>(static_cast<uint8_t>(p[i])) << (8 * i);
        p += 8;
        return static_cast<int64_t>(v);
    }
    bool f64v(std::vector<double>& out) {
        int64_t n = i64();
        if (!ok || n < 0 || n > (end - p) / 8) { ok = false; return false; }
        out.resize(static_cast<size_t>(n));
        if (n > 0) std::memcpy(out.data(), p, static_cast<size_t>(n) * 8);
        p += n * 8;
        return true;
    }
};
}  // namespace

bool NativeInferenceEngine::delta_state_save(
    int64_t slot, std::vector<char>& out) const {
    if (!has_delta_state()) return false;
    if (slot < 0 || slot >= static_cast<int64_t>(lin_states_.size())) {
        return false;
    }
    const auto& states = lin_states_[static_cast<size_t>(slot)];
    out.clear();
    out.reserve(static_cast<size_t>(delta_state_bytes(slot)));
    ds_put_u32(out, kDeltaStateMagic);
    ds_put_u32(out, kDeltaStateVersion);
    ds_put_i64(out, slot);
    ds_put_i64(out, static_cast<int64_t>(states.size()));
    for (const LinLayerState& st : states) {
        ds_put_u8(out, st.tokens > 0 || !st.s.empty() ? 1 : 0);
        ds_put_f64v(out, st.conv_tail);
        ds_put_f64v(out, st.s);
        ds_put_i64(out, st.tokens);
    }
    return true;
}

bool NativeInferenceEngine::delta_state_restore(
    int64_t slot, const char* data, int64_t len) {
    if (!has_delta_state() || data == nullptr || len <= 0) return false;
    if (slot < 0 || slot >= static_cast<int64_t>(lin_states_.size())) {
        return false;
    }
    DsCur c{data, data + len};
    if (c.u32() != kDeltaStateMagic) return false;
    if (c.u32() != kDeltaStateVersion) return false;
    const int64_t src_slot = c.i64();
    const int64_t layers = c.i64();
    auto& states = lin_states_[static_cast<size_t>(slot)];
    if (!c.ok || src_slot < 0 ||
        layers != static_cast<int64_t>(states.size())) {
        return false;   // STATE_MODEL_MISMATCH territory — wrong layer count
    }
    // Expected per-layer geometry: a state blob whose per-layer sizes do
    // not match this model's DeltaNet geometry is a different model's
    // state — fail closed rather than corrupt the recurrence.
    const ModelConfig& cfg = bundle_->config();
    const int64_t conv_dim =
        2 * cfg.linear_num_key_heads * cfg.linear_key_head_dim +
        cfg.linear_num_value_heads * cfg.linear_value_head_dim;
    const int64_t exp_conv =
        (cfg.linear_conv_kernel_dim - 1) * conv_dim;
    const int64_t exp_s = cfg.linear_num_value_heads *
        cfg.linear_key_head_dim * cfg.linear_value_head_dim;
    std::vector<LinLayerState> decoded(static_cast<size_t>(layers));
    for (int64_t l = 0; l < layers; ++l) {
        const uint8_t present = c.u8();
        LinLayerState& st = decoded[static_cast<size_t>(l)];
        if (!c.f64v(st.conv_tail) || !c.f64v(st.s)) return false;
        st.tokens = c.i64();
        if (!c.ok) return false;
        if (!present) {
            st.conv_tail.clear();
            st.s.clear();
            st.tokens = 0;
            continue;
        }
        if (!cfg.is_linear_layer(l)) return false;   // state on dense layer
        if (static_cast<int64_t>(st.conv_tail.size()) != exp_conv ||
            static_cast<int64_t>(st.s.size()) != exp_s) {
            return false;   // STATE_MODEL_MISMATCH geometry
        }
    }
    if (!c.ok || c.p != c.end) return false;   // trailing bytes rejected
    states = std::move(decoded);
    return true;
}
