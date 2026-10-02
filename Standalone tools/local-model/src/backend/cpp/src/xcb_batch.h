// xcb_batch.h — XCB1 binary token-batch container (Xingcheng binary
// hot-data rule: token batches are never JSON).
//
// Same envelope convention as the XPA1 prefill artifact:
//
//   "XCB1" | u32 version=1 | u32 meta_len | meta JSON | u64 record_count
//   record x record_count:
//     u8  kind    1=pretrain(ids) 2=sft(ids+labels) 3=dpo(chosen+rejected)
//                 4=grpo(prompt_ids+completion_ids)
//     u8  flags   bit0 = vision grid present
//     u16 reserved = 0
//     u32 n_ids     + i32 ids[n_ids]
//     u32 n_labels  + i32 labels[n_labels]      (kind 1 writes 0)
//     kind 3: u32 n_rej + i32 rej_ids[n]
//             u32 n_rej_labels + i32 rej_labels[n]
//     flags&1: u32 patches | u32 dim | f32 grid[patches*dim]
//
// All integers little-endian. The meta block is JSON (configuration
// class: format tag, producer, tokenizer_sha256); the payload is
// binary only. Integrity comes from the dataset registry's file
// sha256 — the container itself carries no hash tail.
//
// Reader contract (fail-closed): unknown version/kind, truncated
// fields, or counts that exceed the remaining bytes throw
// XcbError. Callers sniff the 4-byte magic: XCB1 → binary reader,
// anything else → the caller's legacy path.

#pragma once

#include <cstdint>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace xcb {

inline constexpr char kMagic[4] = {'X', 'C', 'B', '1'};
inline constexpr uint32_t kVersion = 1;

enum class Kind : uint8_t {
    kPretrain = 1,
    kSft = 2,
    kDpo = 3,
    kGrpo = 4,
};

struct XcbError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

struct Record {
    Kind kind = Kind::kSft;
    std::vector<int32_t> ids;
    std::vector<int32_t> labels;
    std::vector<int32_t> rej_ids;       // kind 3 only
    std::vector<int32_t> rej_labels;
    std::vector<float> vision;          // flags&1: flat patches*dim
    uint32_t vision_patches = 0;
    uint32_t vision_dim = 0;
};

namespace detail {

inline void put_u32(std::string& out, uint32_t v) {
    char b[4] = {static_cast<char>(v & 0xff),
                 static_cast<char>((v >> 8) & 0xff),
                 static_cast<char>((v >> 16) & 0xff),
                 static_cast<char>((v >> 24) & 0xff)};
    out.append(b, 4);
}
inline void put_u64(std::string& out, uint64_t v) {
    char b[8];
    for (int i = 0; i < 8; ++i)
        b[i] = static_cast<char>((v >> (8 * i)) & 0xff);
    out.append(b, 8);
}
inline void put_i32v(std::string& out, const std::vector<int32_t>& v) {
    put_u32(out, static_cast<uint32_t>(v.size()));
    const size_t bytes = v.size() * 4;
    const size_t at = out.size();
    out.resize(at + bytes);
    std::memcpy(out.data() + at, v.data(), bytes);
}
inline void put_f32v(std::string& out, const std::vector<float>& v) {
    const size_t bytes = v.size() * 4;
    const size_t at = out.size();
    out.resize(at + bytes);
    std::memcpy(out.data() + at, v.data(), bytes);
}

inline void record_bytes(std::string& out, const Record& r) {
    const bool vision = !r.vision.empty();
    const uint8_t flags = vision ? 1 : 0;
    out.push_back(static_cast<char>(r.kind));
    out.push_back(static_cast<char>(flags));
    out.push_back(0);
    out.push_back(0);
    put_i32v(out, r.ids);
    put_i32v(out, r.labels);
    if (r.kind == Kind::kDpo) {
        put_i32v(out, r.rej_ids);
        put_i32v(out, r.rej_labels);
    }
    if (vision) {
        put_u32(out, r.vision_patches);
        put_u32(out, r.vision_dim);
        put_f32v(out, r.vision);
    }
}

}  // namespace detail

/// Streaming writer: emits the header on construction, one record per
/// add(), and back-patches record_count on close(). Written with
/// std::ostream seek/tell so corpora never materialize the whole
/// payload in memory.
class Writer {
  public:
    Writer(std::ostream& out, const std::string& meta_json)
        : out_(out) {
        std::string hdr;
        hdr.append(kMagic, 4);
        detail::put_u32(hdr, kVersion);
        detail::put_u32(hdr, static_cast<uint32_t>(meta_json.size()));
        hdr.append(meta_json);
        out_.write(hdr.data(), static_cast<std::streamsize>(hdr.size()));
        count_pos_ = out_.tellp();
        detail::put_u64(hdr, 0);  // placeholder, patched in close()
        out_.write(hdr.data() + hdr.size() - 8, 8);
        if (!out_) throw XcbError("XCB_WRITE_FAILED:header");
    }

    void add(const Record& r) {
        if (closed_) throw XcbError("XCB_WRITE_AFTER_CLOSE");
        std::string rec;
        detail::record_bytes(rec, r);
        out_.write(rec.data(), static_cast<std::streamsize>(rec.size()));
        if (!out_) throw XcbError("XCB_WRITE_FAILED:record");
        ++count_;
    }

    uint64_t close() {
        if (closed_) return count_;
        closed_ = true;
        const std::streampos end = out_.tellp();
        std::string cnt;
        detail::put_u64(cnt, count_);
        out_.seekp(count_pos_);
        out_.write(cnt.data(), 8);
        out_.seekp(end);
        if (!out_) throw XcbError("XCB_WRITE_FAILED:count");
        return count_;
    }

    ~Writer() {
        if (!closed_) {
            try { close(); } catch (...) { /* destructor must not throw */ }
        }
    }

  private:
    std::ostream& out_;
    std::streampos count_pos_ = 0;
    uint64_t count_ = 0;
    bool closed_ = false;
};

/// Bounds-checked sequential reader over a mapped/loaded byte view.
/// view() is non-owning — the caller keeps the buffer alive.
class Reader {
  public:
    Reader(const char* data, size_t size) : d_(data), n_(size) {
        if (n_ < 4 || std::memcmp(d_, kMagic, 4) != 0)
            throw XcbError("XCB_MAGIC");
        pos_ = 4;
        const uint32_t ver = u32();
        if (ver != kVersion) throw XcbError("XCB_VERSION");
        meta_len_ = u32();
        need(meta_len_);
        meta_.assign(d_ + pos_, meta_len_);
        pos_ += meta_len_;
        count_ = u64();
    }

    const std::string& meta() const { return meta_; }
    uint64_t count() const { return count_; }
    bool done() const { return read_ >= count_; }
    /// Bytes consumed so far; after done() the caller may compare with
    /// the file size to reject trailing garbage.
    size_t consumed() const { return pos_; }

    Record next() {
        if (read_ >= count_) throw XcbError("XCB_RECORD_OVERFLOW");
        ++read_;
        Record r;
        need(4);
        r.kind = static_cast<Kind>(
            static_cast<uint8_t>(d_[pos_]));
        const uint8_t flags = static_cast<uint8_t>(d_[pos_ + 1]);
        if (d_[pos_ + 2] != 0 || d_[pos_ + 3] != 0)
            throw XcbError("XCB_RECORD_RESERVED");
        if (flags & ~static_cast<uint8_t>(1))
            throw XcbError("XCB_RECORD_FLAGS");
        pos_ += 4;
        if (r.kind != Kind::kPretrain && r.kind != Kind::kSft &&
            r.kind != Kind::kDpo && r.kind != Kind::kGrpo)
            throw XcbError("XCB_RECORD_KIND");
        r.ids = i32v();
        r.labels = i32v();
        if (r.kind == Kind::kDpo) {
            r.rej_ids = i32v();
            r.rej_labels = i32v();
        }
        if (flags & 1) {
            if (r.kind == Kind::kDpo)
                throw XcbError("XCB_DPO_VISION");
            r.vision_patches = u32();
            r.vision_dim = u32();
            const uint64_t cells =
                static_cast<uint64_t>(r.vision_patches) * r.vision_dim;
            if (cells > (static_cast<uint64_t>(n_) - pos_) / 4)
                throw XcbError("XCB_VISION_TRUNCATED");
            r.vision.resize(static_cast<size_t>(cells));
            std::memcpy(r.vision.data(), d_ + pos_,
                        static_cast<size_t>(cells) * 4);
            pos_ += static_cast<size_t>(cells) * 4;
        }
        return r;
    }

  private:
    void need(size_t bytes) const {
        if (bytes > n_ - pos_) throw XcbError("XCB_TRUNCATED");
    }
    uint32_t u32() {
        need(4);
        uint32_t v = 0;
        std::memcpy(&v, d_ + pos_, 4);
        pos_ += 4;
        return v;  // LE on every supported host (x86/ARM64 LE)
    }
    uint64_t u64() {
        need(8);
        uint64_t v = 0;
        std::memcpy(&v, d_ + pos_, 8);
        pos_ += 8;
        return v;
    }
    std::vector<int32_t> i32v() {
        const uint32_t n = u32();
        if (n > (n_ - pos_) / 4) throw XcbError("XCB_IDS_TRUNCATED");
        std::vector<int32_t> v(n);
        std::memcpy(v.data(), d_ + pos_, static_cast<size_t>(n) * 4);
        pos_ += static_cast<size_t>(n) * 4;
        return v;
    }

    const char* d_;
    size_t n_;
    size_t pos_ = 0;
    uint32_t meta_len_ = 0;
    std::string meta_;
    uint64_t count_ = 0;
    uint64_t read_ = 0;
};

/// Sniff helper: true when the first bytes of `path` are the XCB1
/// magic. Returns false for unreadable/short files so callers fall
/// back to their legacy format path.
inline bool is_xcb_file(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    char magic[4] = {};
    if (!f.read(magic, 4)) return false;
    return std::memcmp(magic, kMagic, 4) == 0;
}

}  // namespace xcb
