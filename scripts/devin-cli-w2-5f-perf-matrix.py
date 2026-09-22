"""W2-3 Phase 5F: Python/C++ dual-path performance matrix (per-layer + end-to-end).

Builds tiny XingCheng models (fixed seed/config), exports the C++ bundle,
and times both inference paths:
  * end-to-end forward latency (model() vs engine.logits)
  * end-to-end greedy generation latency (Generator vs engine.generate)
  * per-layer marginal cost (least-squares over num_hidden_layers {1,2,4})
  * per-module Python breakdown (forward hooks, separate pass)
  * MoE forward latency (gate path)

Output: JSON on stdout (format star-native-perf-matrix/v1).
Run: python scripts/devin-cli-w2-5f-perf-matrix.py > <evidence>.json
"""

from __future__ import annotations

import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

import torch

REPO = Path(__file__).resolve().parents[1]
SERVICE_ROOT = (
    REPO
    / "Standalone tools"
    / "local-model"
    / "src"
    / "backend"
    / "services"
)
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from xingcheng.infrastructure.native_transformer import cpp_runtime  # noqa: E402
from xingcheng.infrastructure.native_transformer.checkpoint import (  # noqa: E402
    save_checkpoint,
)
from xingcheng.infrastructure.native_transformer.config import (  # noqa: E402
    XingChengConfig,
)
from xingcheng.infrastructure.native_transformer.cpp_export import (  # noqa: E402
    export_checkpoint_for_cpp,
)
from xingcheng.infrastructure.native_transformer.inference.generate import (  # noqa: E402
    Generator,
)
from xingcheng.infrastructure.native_transformer.inference.sampler import (  # noqa: E402
    Sampler,
    SamplingConfig,
)
from xingcheng.infrastructure.native_transformer.modules.model import (  # noqa: E402
    XingChengForCausalLM,
)

WARMUP = 5
FORWARD_ITERS = 60
GENERATE_ITERS = 15
HOOK_ITERS = 20


def _config(num_layers: int, *, moe: bool = False) -> XingChengConfig:
    kwargs = {}
    if moe:
        kwargs = dict(use_moe=True, moe_num_experts=4, moe_top_k=2, moe_layer_interval=1)
    return XingChengConfig(
        vocab_size=64,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=num_layers,
        num_attention_heads=4,
        num_key_value_heads=2,
        max_position_embeddings=32,
        max_new_tokens=8,
        **kwargs,
    )


def _timed(fn, warmup: int, iters: int) -> dict:
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(iters):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return {
        "median_ms": round(statistics.median(samples), 4),
        "min_ms": round(min(samples), 4),
        "mean_ms": round(statistics.fmean(samples), 4),
    }


def _build(workdir: Path, num_layers: int, *, seed: int = 23, moe: bool = False):
    torch.manual_seed(seed)
    config = _config(num_layers, moe=moe)
    model = XingChengForCausalLM(config).eval()
    tag = f"l{num_layers}{'-moe' if moe else ''}"
    checkpoint = workdir / f"{tag}.pt"
    save_checkpoint(checkpoint, model, config=config)
    bundle = workdir / f"bundle-{tag}"
    export_checkpoint_for_cpp(checkpoint, bundle)
    return model, config, bundle


def _python_module_breakdown(model, ids, iters: int) -> dict:
    """Hook-timed cumulative ms per module type (separate pass)."""
    samples: dict[str, list[float]] = {}
    starts: dict[int, float] = {}

    def pre(mod, _args):
        starts[id(mod)] = time.perf_counter()

    def post(mod, _args, _out, _key=None):
        elapsed = (time.perf_counter() - starts.pop(id(mod), time.perf_counter())) * 1000.0
        samples.setdefault(_key, []).append(elapsed)

    handles = []
    named = [("embeddings", model.model.embeddings), ("final_norm", model.model.final_norm), ("lm_head", model.lm_head)]
    for layer in model.model.layers:
        named += [
            ("layer.input_norm", layer.input_norm),
            ("layer.attention", layer.attention),
            ("layer.post_attention_norm", layer.post_attention_norm),
            ("layer.mlp", layer.mlp),
        ]
    for key, module in named:
        handles.append(module.register_forward_pre_hook(pre))
        handles.append(module.register_forward_hook(lambda m, a, o, k=key: post(m, a, o, _key=k)))
    tensor = torch.tensor([ids], dtype=torch.long)
    with torch.no_grad():
        for _ in range(iters):
            model(tensor)
    for handle in handles:
        handle.remove()
    return {
        key: round(statistics.median(vals), 4)
        for key, vals in sorted(samples.items())
    }


def _slope(points: dict[int, float]) -> dict:
    xs = list(points)
    ys = [points[x] for x in xs]
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    slope = (n * sxy - sx * sy) / denom if denom else 0.0
    intercept = (sy - slope * sx) / n
    return {
        "per_layer_ms": round(slope, 4),
        "fixed_ms": round(intercept, 4),
    }


def main() -> int:
    module = cpp_runtime.load_extension()
    seq8 = [1, 9, 10, 11, 12, 13, 14, 15]
    seq16 = list(range(1, 17))
    report: dict = {
        "format_version": "star-native-perf-matrix/v1",
        "environment": {
            "python": platform.python_version(),
            "torch": torch.__version__,
            "torch_num_threads": torch.get_num_threads(),
            "cpu_count": __import__("os").cpu_count(),
            "cpp_runtime_mode": __import__("os").environ.get("XINGCHENG_CPP_RUNTIME", "off"),
        },
        "config_base": {
            "hidden_size": 32, "intermediate_size": 64, "vocab_size": 64,
            "num_attention_heads": 4, "num_key_value_heads": 2,
        },
        "warmup": WARMUP,
        "forward_iters": FORWARD_ITERS,
        "generate_iters": GENERATE_ITERS,
    }

    forward_rows = []
    layer_points: dict[str, dict[int, float]] = {"python": {}, "cpp": {}}
    module_rows = []
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for layers in (1, 2, 4):
            model, _config, bundle = _build(workdir, layers)
            engine = module.NativeInferenceEngine()
            engine.load(str(bundle))
            for seq_label, ids in (("seq8", seq8), ("seq16", seq16)):
                tensor = torch.tensor([ids], dtype=torch.long)
                with torch.no_grad():
                    py = _timed(lambda: model(tensor), WARMUP, FORWARD_ITERS)
                cpp = _timed(lambda: engine.logits(ids), WARMUP, FORWARD_ITERS)
                row = {
                    "layers": layers,
                    "seq": len(ids),
                    "python": py,
                    "cpp": cpp,
                    "speedup_median": round(py["median_ms"] / cpp["median_ms"], 2)
                    if cpp["median_ms"] else None,
                }
                forward_rows.append(row)
                if seq_label == "seq8":
                    layer_points["python"][layers] = py["median_ms"]
                    layer_points["cpp"][layers] = cpp["median_ms"]
            if layers == 2:
                module_rows.append(_python_module_breakdown(model, seq8, HOOK_ITERS))
            engine.unload()

        # MoE forward latency (dense vs gate path, layers=2, seq8)
        model, _config, bundle = _build(workdir, 2, seed=29, moe=True)
        engine = module.NativeInferenceEngine()
        engine.load(str(bundle))
        tensor = torch.tensor([seq8], dtype=torch.long)
        with torch.no_grad():
            moe_py = _timed(lambda: model(tensor), WARMUP, FORWARD_ITERS)
        moe_cpp = _timed(lambda: engine.logits(seq8), WARMUP, FORWARD_ITERS)
        engine.unload()
        report["moe_forward"] = {
            "layers": 2, "seq": 8, "experts": 4, "top_k": 2,
            "python": moe_py, "cpp": moe_cpp,
            "speedup_median": round(moe_py["median_ms"] / moe_cpp["median_ms"], 2)
            if moe_cpp["median_ms"] else None,
        }

        # End-to-end greedy generation, layers=2, prompt 4, new 8
        model, _config, bundle = _build(workdir, 2)
        engine = module.NativeInferenceEngine()
        engine.load(str(bundle))
        prompt = [1, 9, 10, 11]
        prompt_tensor = torch.tensor([prompt], dtype=torch.long)
        py_sampling = SamplingConfig(do_sample=False, repetition_penalty=1.0)
        generator = Generator(
            model, sampler=Sampler(py_sampling), device=torch.device("cpu")
        )
        cpp_sampling = module.SamplingConfig()
        cpp_sampling.do_sample = False
        cpp_sampling.repetition_penalty = 1.0
        with torch.no_grad():
            gen_py = _timed(
                lambda: generator.generate(
                    prompt_tensor, max_new_tokens=8, use_cache=True
                ),
                WARMUP,
                GENERATE_ITERS,
            )
        gen_cpp = _timed(
            lambda: engine.generate(prompt, 8, cpp_sampling), WARMUP, GENERATE_ITERS
        )
        engine.unload()
        report["generate"] = {
            "layers": 2, "prompt_tokens": 4, "max_new_tokens": 8,
            "python": gen_py, "cpp": gen_cpp,
            "python_tokens_per_second": round(8 / max(1e-6, gen_py["min_ms"] / 1000.0), 1),
            "cpp_tokens_per_second": round(8 / max(1e-6, gen_cpp["min_ms"] / 1000.0), 1),
            "speedup_median": round(gen_py["median_ms"] / gen_cpp["median_ms"], 2)
            if gen_cpp["median_ms"] else None,
        }

    report["forward"] = forward_rows
    report["per_layer_scaling_seq8"] = {
        "python": _slope(layer_points["python"]),
        "cpp": _slope(layer_points["cpp"]),
    }
    report["python_module_breakdown_ms_per_call"] = module_rows[0] if module_rows else {}
    report["notes"] = (
        "per-layer cost derived by least-squares over num_hidden_layers={1,2,4} "
        "(slope=per-layer marginal ms, intercept=embed+final_norm+lm_head fixed ms); "
        "C++ per-module taps not available (binding TU frozen by concurrent P1-1 "
        "fp8 work) — per-module breakdown is Python-only."
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
