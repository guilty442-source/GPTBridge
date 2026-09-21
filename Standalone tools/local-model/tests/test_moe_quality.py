"""Phase 9B: MoE router, dispatch/combine, health and gradient checks."""

from __future__ import annotations

import sys
from pathlib import Path

import torch
import torch.nn as nn
from torch.profiler import ProfilerActivity, profile

_ROOT = Path(__file__).resolve().parents[1]
_INFRA = _ROOT / "src" / "backend" / "services" / "xingcheng" / "infrastructure"
if str(_INFRA) not in sys.path:
    sys.path.insert(0, str(_INFRA))

from native_transformer.config import XingChengConfig  # noqa: E402
from native_transformer.modules.moe import XingChengMoE  # noqa: E402
from native_transformer.modules.model import XingChengForCausalLM  # noqa: E402


class _ConstantExpert(nn.Module):
    def __init__(self, value: float) -> None:
        super().__init__()
        self.value = nn.Parameter(torch.tensor(float(value)))

    def forward(self, hidden: torch.Tensor) -> torch.Tensor:
        return torch.ones_like(hidden) * self.value


def _config(experts: int = 4) -> XingChengConfig:
    config = XingChengConfig.small_moe(num_experts=experts, top_k=2)
    config.vocab_size = 264
    config.max_position_embeddings = 32
    return config


def test_moe_supports_top2_and_four_or_eight_experts() -> None:
    for experts in (4, 8):
        config = _config(experts)
        moe = XingChengMoE(config)
        assert moe.num_experts == experts
        assert moe.top_k == 2
        assert len(moe.experts) == experts


def test_router_computes_logits_in_fp32_and_reports_health() -> None:
    torch.manual_seed(7)
    moe = XingChengMoE(_config(4)).eval()
    output, aux_loss = moe(torch.randn(8, 64, moe.hidden_size))
    metrics = moe.metrics()
    assert output.shape == (8, 64, moe.hidden_size)
    assert torch.isfinite(aux_loss)
    assert metrics["router_dtype"] == "torch.float32"
    assert len(metrics["expert_load"]) == 4
    assert abs(sum(metrics["expert_load"]) - 1.0) < 1e-6
    assert metrics["router_entropy"] > 0.0
    assert metrics["utilized_experts"] >= 2
    assert isinstance(metrics["is_collapsed"], bool)


def test_top2_dispatch_and_combine_preserves_weighted_expert_output() -> None:
    config = _config(4)
    moe = XingChengMoE(config).eval()
    moe.experts = nn.ModuleList([_ConstantExpert(i + 1) for i in range(4)])
    with torch.no_grad():
        moe.router.weight.zero_()
        moe.router.weight[0, 0] = 2.0
        moe.router.weight[1, 0] = 1.0
    hidden = torch.zeros(1, 1, config.hidden_size)
    hidden[0, 0, 0] = 1.0
    output, _ = moe(hidden)
    probs = torch.softmax(torch.tensor([2.0, 1.0, 0.0, 0.0]), dim=0)
    expected = probs[:2] / probs[:2].sum()
    assert torch.allclose(output[0, 0, 0], expected[0] + 2 * expected[1])
    assert torch.allclose(output, torch.full_like(output, output[0, 0, 0].item()))


def test_all_expected_experts_receive_gradients() -> None:
    torch.manual_seed(11)
    moe = XingChengMoE(_config(4))
    hidden = torch.randn(8, 64, moe.hidden_size, requires_grad=True)
    output, aux_loss = moe(hidden)
    (output.square().mean() + aux_loss).backward()
    assert moe.router.weight.grad is not None
    assert float(moe.router.weight.grad.abs().sum()) > 0.0
    for expert in moe.experts:
        gradients = [parameter.grad for parameter in expert.parameters() if parameter.requires_grad]
        assert gradients and all(gradient is not None for gradient in gradients)
        assert sum(float(gradient.abs().sum()) for gradient in gradients) > 0.0


def test_router_health_detects_balanced_noncollapsed_routing() -> None:
    torch.manual_seed(19)
    moe = XingChengMoE(_config(8)).eval()
    with torch.no_grad():
        moe.router.weight.normal_(mean=0.0, std=0.03)
    _output, _aux = moe(torch.randn(16, 64, moe.hidden_size))
    metrics = moe.metrics()
    loads = metrics["expert_load"]
    assert len(loads) == 8
    assert max(loads) < 0.5
    assert metrics["utilized_experts"] >= 4
    assert metrics["router_entropy"] > 0.0
    assert metrics["is_collapsed"] is False


def test_dense_path_remains_available_and_finite() -> None:
    config = XingChengConfig.small()
    config.vocab_size = 264
    config.max_position_embeddings = 32
    model = XingChengForCausalLM(config)
    ids = torch.randint(4, config.vocab_size, (2, 8))
    output = model(ids, labels=ids)
    assert "aux_loss" not in output
    assert torch.isfinite(output["loss"])
    output["loss"].backward()
    assert all(parameter.grad is not None for parameter in model.parameters() if parameter.requires_grad)


def test_dense_and_moe_parameter_and_runtime_evidence_are_observable() -> None:
    dense_config = XingChengConfig.small()
    dense_config.vocab_size = 264
    dense_config.max_position_embeddings = 32
    dense = XingChengForCausalLM(dense_config)
    moe = XingChengForCausalLM(_config(4))
    assert moe.num_parameters(False) > dense.num_parameters(False)
    ids = torch.randint(4, 264, (1, 8))
    with torch.inference_mode():
        dense_out = dense(ids)["logits"]
        moe_out = moe(ids)["logits"]
    assert dense_out.shape == moe_out.shape
    assert torch.isfinite(dense_out).all()
    assert torch.isfinite(moe_out).all()


def test_dense_and_moe_report_measured_cpu_flops() -> None:
    ids = torch.randint(4, 264, (1, 4))
    dense_config = XingChengConfig.small()
    dense_config.vocab_size = 264
    dense_config.max_position_embeddings = 32
    models = (XingChengForCausalLM(dense_config), XingChengForCausalLM(_config(4)))
    measured: list[int] = []
    for model in models:
        with profile(
            activities=[ProfilerActivity.CPU],
            record_shapes=False,
            with_flops=True,
        ) as profiler:
            with torch.inference_mode():
                model(ids)
        flops = sum(int(getattr(event, "flops", 0) or 0) for event in profiler.key_averages())
        measured.append(flops)
    assert measured[0] > 0
