// dbg_int8.cpp — minimal KV-INT8 prefix-restore divergence probe.
#include "xingcheng_inference.hpp"
#include <cstdio>
#include <cstdlib>
#include <random>
#include <vector>
using namespace xingcheng::inference;

int main(int argc, char** argv) {
    const char* bundle = argc > 1 ? argv[1] : "xingcheng/runtime/models/cpp-bundles/final-1b54409abd2a8006";
    std::vector<int64_t> ids;
    {   // same seed-11 stream as cache-smoke
        std::mt19937_64 rng(11);
        std::uniform_int_distribution<int64_t> tok(3, 8191);
        for (int i = 0; i < 32; ++i) ids.push_back(tok(rng));
    }
    NativeInferenceEngine e;
    e.load(bundle);
    SamplingConfig sc;
    std::vector<int64_t> prompt(ids.begin(), ids.begin() + 16);
    auto g1 = e.generate(prompt, 6, sc);
    std::printf("g1:");
    for (auto t : g1) std::printf(" %lld", (long long)t);
    std::printf("\n%s\n", e.describe().c_str());
    auto g2 = e.generate(prompt, 6, sc);
    std::printf("g2:");
    for (auto t : g2) std::printf(" %lld", (long long)t);
    std::printf("\n%s\n", e.describe().c_str());
    auto g3 = e.generate(prompt, 6, sc);
    std::printf("g3:");
    for (auto t : g3) std::printf(" %lld", (long long)t);
    std::printf("\n");
    return 0;
}
