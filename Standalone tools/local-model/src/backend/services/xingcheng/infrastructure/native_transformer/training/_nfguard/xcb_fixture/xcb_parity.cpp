// xcb_parity.cpp — emit a fixed XCB1 stream for byte-parity diff vs
// the Rust xcorpus writer (same meta, same records).
#include "xcb_batch.h"
#include <fstream>
#include <iostream>
int main(int argc, char** argv) {
    if (argc < 2) return 2;
    std::ofstream f(argv[1], std::ios::binary | std::ios::trunc);
    if (!f) return 3;
    const std::string meta = "{\"format\":\"parity/v1\"}";
    {
        xcb::Writer w(f, meta);
        xcb::Record r;
        r.kind = xcb::Kind::kPretrain;
        r.ids = {5, -2, 7, 300};
        w.add(r);
        w.close();
    }
    f.close();
    if (!f) return 4;
    // round-trip readback check
    std::ifstream in(argv[1], std::ios::binary);
    std::string blob((std::istreambuf_iterator<char>(in)),
                     std::istreambuf_iterator<char>());
    xcb::Reader rd(blob.data(), blob.size());
    if (rd.count() != 1) return 5;
    xcb::Record a = rd.next();
    if (a.ids.size() != 4 || a.ids[1] != -2) return 6;
    if (rd.consumed() != blob.size()) return 8;  // no trailing bytes
    std::cout << "parity-emit ok\n";
    return 0;
}
