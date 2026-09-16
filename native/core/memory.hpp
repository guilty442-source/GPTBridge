// memory.hpp — shared native memory/buffer primitives (A204/A213/
// A214/A220; Memory & Buffer Architecture V1).
//
// Single owner of the four ownership classes used by every native
// domain (parser / vector / transformer).  Domains never build their
// own allocator or pool policy; this header is their only primitive.
//
//   BORROWED_READONLY        — caller owns; C++ reads only
//   BORROWED_MUTABLE         — caller owns; C++ may write in place
//   NATIVE_OWNED             — C++ owns; RAII frees, never escapes
//   CALLER_PROVIDED_OUTPUT   — caller allocated; C++ writes results
//
// Rules enforced here:
//   - header-only, noexcept, no exceptions cross any boundary
//   - every size/stride/shape computation is overflow-checked before
//     any allocation happens
//   - allocations require an explicit WorkspaceBudget check first
//   - the allocator and deallocator of any block live in the same
//     owner (RAII in C++; create/destroy pairs in the C ABI) —
//     cross-runtime free is structurally impossible
//   - memory.hpp depends on NOTHING else in native/ (no reverse
//     dependency from the memory domain into compute domains)
//
// Zero-copy is a policy decision, not a default: batching, buffer
// reuse and validated borrowed views come first; zero-copy only with
// profile evidence.
#ifndef GPTBRIDGE_NATIVE_MEMORY_HPP
#define GPTBRIDGE_NATIVE_MEMORY_HPP

#include <cstdint>
#include <cstddef>
#include <limits>
#include <memory>

namespace gptbridge_native_mem {

// ------------------------------------------------------------------
// Ownership classes
// ------------------------------------------------------------------
enum class BufferOwnership : uint8_t {
    BORROWED_READONLY = 0,
    BORROWED_MUTABLE = 1,
    NATIVE_OWNED = 2,
    CALLER_PROVIDED_OUTPUT = 3,
};

// ------------------------------------------------------------------
// Overflow-checked size math — every byte count passes through here
// before allocation or indexing.
// ------------------------------------------------------------------
inline bool checked_mul_i64(int64_t a, int64_t b, int64_t* out) noexcept {
    if (a < 0 || b < 0 || out == nullptr) {
        return false;
    }
    if (a != 0 && b > std::numeric_limits<int64_t>::max() / a) {
        return false;  // overflow
    }
    *out = a * b;
    return true;
}

// bytes = count * elem_size, guarded.  Returns false on overflow or
// negative input; *bytes_out is only written on success.
inline bool checked_bytes(
    int64_t count, int64_t elem_size, int64_t* bytes_out) noexcept {
    return checked_mul_i64(count, elem_size, bytes_out);
}

// Total element count for a shape (product of dims), guarded.
inline bool checked_shape_elems(
    const int64_t* dims, int64_t ndims, int64_t* elems_out) noexcept {
    if (elems_out == nullptr || ndims < 0) {
        return false;
    }
    if (ndims == 0) {
        *elems_out = 0;
        return true;
    }
    if (dims == nullptr) {
        return false;
    }
    int64_t total = 1;
    for (int64_t i = 0; i < ndims; ++i) {
        if (!checked_mul_i64(total, dims[i], &total)) {
            return false;
        }
    }
    *elems_out = total;
    return true;
}

// ------------------------------------------------------------------
// Borrowed views — the default exchange shape.  A view never owns;
// construction validates pointer/length once.
// ------------------------------------------------------------------
template <typename T>
struct ConstView {
    const T* data;
    int64_t len;
    BufferOwnership ownership = BufferOwnership::BORROWED_READONLY;

    bool valid() const noexcept {
        return data != nullptr && len >= 0;
    }
};

template <typename T>
struct MutView {
    T* data;
    int64_t len;
    BufferOwnership ownership = BufferOwnership::BORROWED_MUTABLE;

    bool valid() const noexcept {
        return data != nullptr && len >= 0;
    }
};

// Output slot supplied by the caller (pre-allocated); C++ only writes.
template <typename T>
struct OutView {
    T* data;
    int64_t len;
    BufferOwnership ownership = BufferOwnership::CALLER_PROVIDED_OUTPUT;

    bool valid() const noexcept {
        return data != nullptr && len >= 0;
    }
};

template <typename T>
inline ConstView<T> borrow_const(const T* data, int64_t len) noexcept {
    return ConstView<T>{data, len, BufferOwnership::BORROWED_READONLY};
}

template <typename T>
inline MutView<T> borrow_mut(T* data, int64_t len) noexcept {
    return MutView<T>{data, len, BufferOwnership::BORROWED_MUTABLE};
}

template <typename T>
inline OutView<T> caller_output(T* data, int64_t len) noexcept {
    return OutView<T>{data, len, BufferOwnership::CALLER_PROVIDED_OUTPUT};
}

// ------------------------------------------------------------------
// Workspace budget — checked before every native allocation.
// ------------------------------------------------------------------
struct WorkspaceBudget {
    int64_t max_bytes;

    bool admits(int64_t bytes) const noexcept {
        return bytes >= 0 && bytes <= max_bytes;
    }
};

// ------------------------------------------------------------------
// NativeBuffer — the only NATIVE_OWNED shape.  RAII, move-only,
// frees in the same owner that allocated.  No cross-runtime free.
// ------------------------------------------------------------------
template <typename T>
class NativeBuffer {
public:
    NativeBuffer() noexcept = default;

    // Returns null buffer when the request overflows or exceeds the
    // budget — allocation never happens before validation passes.
    static NativeBuffer allocate(
        int64_t count, const WorkspaceBudget& budget) noexcept {
        int64_t bytes = 0;
        if (!checked_bytes(count, static_cast<int64_t>(sizeof(T)), &bytes) ||
            !budget.admits(bytes)) {
            return NativeBuffer{};
        }
        // try/catch stays inside C++; nothing crosses the boundary.
        try {
            return NativeBuffer(new T[static_cast<size_t>(count)](), count, bytes);
        } catch (...) {
            return NativeBuffer{};
        }
    }

    NativeBuffer(const NativeBuffer&) = delete;
    NativeBuffer& operator=(const NativeBuffer&) = delete;
    NativeBuffer(NativeBuffer&&) noexcept = default;
    NativeBuffer& operator=(NativeBuffer&&) noexcept = default;

    T* data() noexcept { return ptr_.get(); }
    const T* data() const noexcept { return ptr_.get(); }
    int64_t size() const noexcept { return len_; }
    int64_t bytes() const noexcept { return bytes_; }
    explicit operator bool() const noexcept { return ptr_ != nullptr; }

    MutView<T> view() noexcept {
        return MutView<T>{ptr_.get(), len_, BufferOwnership::NATIVE_OWNED};
    }

private:
    NativeBuffer(T* ptr, int64_t len, int64_t bytes) noexcept
        : ptr_(ptr), len_(len), bytes_(bytes) {}

    std::unique_ptr<T[]> ptr_;
    int64_t len_ = 0;
    int64_t bytes_ = 0;
};

// ------------------------------------------------------------------
// Per-capability memory metrics — the five required counters.
// ------------------------------------------------------------------
struct CapabilityMemory {
    int64_t boundary_copy_bytes = 0;   // bytes copied across the boundary
    int64_t allocation_count = 0;      // number of native allocations
    int64_t allocated_bytes = 0;       // total bytes ever allocated
    int64_t peak_workspace = 0;        // high-water of live workspace
    int64_t retained_capacity = 0;     // capacity kept alive between calls

    void record_copy(int64_t bytes) noexcept {
        if (bytes > 0) {
            boundary_copy_bytes += bytes;
        }
    }

    void record_alloc(int64_t bytes) noexcept {
        if (bytes <= 0) {
            return;
        }
        ++allocation_count;
        allocated_bytes += bytes;
        live_ += bytes;
        if (live_ > peak_workspace) {
            peak_workspace = live_;
        }
    }

    void record_free(int64_t bytes) noexcept {
        live_ -= bytes;
        if (live_ < 0) {
            live_ = 0;
        }
    }

    void record_retain(int64_t bytes) noexcept {
        if (bytes >= 0) {
            retained_capacity = bytes;
        }
    }

private:
    int64_t live_ = 0;
};

}  // namespace gptbridge_native_mem

#endif  // GPTBRIDGE_NATIVE_MEMORY_HPP
