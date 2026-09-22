// pybind11 surface for the formal C++ inference layer.
//
// This binding is intentionally thin: all model/runtime behavior lives in
// xingcheng::inference; primitive math is delegated by that layer to the
// public C ABI. Python remains responsible for training/export and can keep
// using the PyTorch path as the compatibility fallback.

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "xingcheng_inference.hpp"

namespace py = pybind11;
using xingcheng::inference::NativeInferenceEngine;
using xingcheng::inference::SamplingConfig;

#if defined(XINGCHENG_CUDA)
extern "C" int xcuda_bf16_available();
#if defined(XINGCHENG_CUDA_KERNELS)
extern "C" int xcuda_matmul_bf16(
    const double* a, long long m, long long k,
    const double* b, long long n, double* out);
#endif
#endif

PYBIND11_MODULE(_xingcheng_inference, m) {
    m.doc() = "Xingcheng formal C++ inference runtime over the public C ABI.";

    py::class_<SamplingConfig>(m, "SamplingConfig")
        .def(py::init<>())
        .def_readwrite("do_sample", &SamplingConfig::do_sample)
        .def_readwrite("temperature", &SamplingConfig::temperature)
        .def_readwrite("top_k", &SamplingConfig::top_k)
        .def_readwrite("top_p", &SamplingConfig::top_p)
        .def_readwrite("repetition_penalty", &SamplingConfig::repetition_penalty)
        .def_readwrite("seed", &SamplingConfig::seed);

    py::class_<NativeInferenceEngine>(m, "NativeInferenceEngine")
        .def(py::init<>())
        .def("load", &NativeInferenceEngine::load, py::arg("bundle_dir"))
        .def("unload", &NativeInferenceEngine::unload)
        .def("loaded", &NativeInferenceEngine::loaded)
        .def("cuda_active", &NativeInferenceEngine::cuda_active)
        .def(
            "encode",
            &NativeInferenceEngine::encode,
            py::arg("text"),
            py::arg("add_bos") = true,
            py::arg("add_eos") = false,
            py::arg("max_length") = 0)
        .def(
            "decode",
            &NativeInferenceEngine::decode,
            py::arg("ids"),
            py::arg("skip_special") = true)
        .def("logits", &NativeInferenceEngine::logits, py::arg("input_ids"))
        .def(
            "sequence_nll",
            &NativeInferenceEngine::sequence_nll,
            py::arg("input_ids"))
        .def(
            "layer_metrics",
            &NativeInferenceEngine::layer_metrics,
            py::arg("input_ids"))
        .def(
            "module_metrics",
            &NativeInferenceEngine::module_metrics,
            py::arg("input_ids"))
        .def(
            "generate",
            &NativeInferenceEngine::generate,
            py::arg("prompt_ids"),
            py::arg("max_new_tokens"),
            py::arg("sampling"))
        .def(
            "generate_batch",
            &NativeInferenceEngine::generate_batch,
            py::arg("prompts"),
            py::arg("max_new_tokens"),
            py::arg("sampling"))
        .def(
            "generate_text",
            &NativeInferenceEngine::generate_text,
            py::arg("prompt"),
            py::arg("max_new_tokens"),
            py::arg("sampling"))
        .def("memory_bytes", &NativeInferenceEngine::memory_bytes)
        .def("kv_memory_bytes", &NativeInferenceEngine::kv_memory_bytes)
        .def("set_kv_memory_limit", &NativeInferenceEngine::set_kv_memory_limit)
        .def(
            "set_prefix_cache_limit",
            &NativeInferenceEngine::set_prefix_cache_limit,
            py::arg("max_entries"),
            py::arg("max_bytes"))
        .def("describe", &NativeInferenceEngine::describe);

    m.def(
        "parse_generated_output",
        &xingcheng::inference::parse_generated_output,
        py::arg("text"),
        py::arg("max_json_bytes") = 64 * 1024);

#if defined(XINGCHENG_CUDA)
    m.def("_cuda_bf16_available", &xcuda_bf16_available);
#if defined(XINGCHENG_CUDA_KERNELS)
    m.def(
        "_probe_matmul_bf16",
        [](const std::vector<double>& a, int64_t m_rows, int64_t k,
           const std::vector<double>& b, int64_t n) {
            std::vector<double> out(
                static_cast<size_t>(m_rows * n));
            const int rc = xcuda_matmul_bf16(
                a.data(), m_rows, k, b.data(), n, out.data());
            if (rc != 0) {
                throw std::runtime_error(
                    "bf16 matmul rc=" + std::to_string(rc));
            }
            return out;
        });
#endif
#endif
}
