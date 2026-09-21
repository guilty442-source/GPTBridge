"""Phase 2T: independent next-token prediction correctness matrix NT-1..NT-12."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
import torch
import torch.nn.functional as F

_ROOT = Path(__file__).resolve().parents[1]
_INFRA = _ROOT / "src" / "backend" / "services" / "xingcheng" / "infrastructure"
if str(_INFRA) not in sys.path:
    sys.path.insert(0, str(_INFRA))

from native_transformer import (  # noqa: E402
    XingChengConfig,
    XingChengForCausalLM,
    XingChengTokenizer,
    load_checkpoint,
    save_checkpoint,
)
from native_transformer.modules.model import _causal_lm_loss  # noqa: E402
from native_transformer.training.data import collate_batch  # noqa: E402
from native_transformer.training.pretrain import pack_blocks  # noqa: E402
from native_transformer.training.sft import encode_sft_example  # noqa: E402


def _config() -> XingChengConfig:
    config = XingChengConfig.small()
    config.vocab_size = 264
    config.max_position_embeddings = 32
    return config


def _model() -> XingChengForCausalLM:
    torch.manual_seed(123)
    return XingChengForCausalLM(_config()).eval()


def _shifted(sequence: list[int], pad_id: int) -> tuple[torch.Tensor, torch.Tensor]:
    input_ids = torch.tensor([sequence[:-1]], dtype=torch.long)
    labels = torch.tensor([sequence[1:]], dtype=torch.long)
    assert input_ids.shape == labels.shape
    assert input_ids.dtype is torch.long
    assert labels[0, -1].item() == 2
    return input_ids, labels


def test_nt1_sequence_shift() -> None:
    input_ids, labels = _shifted([1, 10, 11, 12, 13, 2], 0)
    assert labels[0, :-1].tolist() == input_ids[0, 1:].tolist()
    assert labels[0, -1].item() == 2


def test_nt2_input_target_ids_are_batched_long_tensors() -> None:
    input_ids, labels = _shifted([1, 10, 11, 12, 2], 0)
    assert input_ids.ndim == labels.ndim == 2
    assert input_ids.shape == (1, 4)
    assert input_ids.dtype == labels.dtype == torch.long
    assert torch.equal(labels[:, :-1], input_ids[:, 1:])


def test_nt3_attention_mask_tracks_padding() -> None:
    input_ids, attention = collate_batch([[1, 10, 2], [1, 11]], pad_id=0)
    assert input_ids.shape == attention.shape == (2, 3)
    assert attention.tolist() == [[1, 1, 1], [1, 1, 0]]
    assert input_ids[1, 2].item() == 0


def test_nt4_causal_mask_blocks_future_tokens_with_and_without_mask() -> None:
    model = _model()
    ids = torch.tensor([[4, 10, 11, 12, 13]], dtype=torch.long)
    changed = ids.clone()
    changed[0, -1] = 14
    with torch.inference_mode():
        plain_a = model(ids)["logits"]
        plain_b = model(changed)["logits"]
        mask = torch.ones_like(ids)
        masked_a = model(ids, attention_mask=mask)["logits"]
        masked_b = model(changed, attention_mask=mask)["logits"]
    assert torch.allclose(plain_a[:, :-1], plain_b[:, :-1], atol=1e-6)
    assert torch.allclose(masked_a[:, :-1], masked_b[:, :-1], atol=1e-6)


def test_nt5_eos_is_preserved_at_packed_document_boundaries() -> None:
    packed = pack_blocks(
        torch.tensor([1, 10, 2, 1, 11, 2], dtype=torch.int64).numpy(), 3
    )
    assert packed.tolist() == [[1, 10, 2], [1, 11, 2]]
    assert all(row[-1] == 2 for row in packed.tolist())


def test_nt6_vocabulary_range_and_model_tokenizer_alignment() -> None:
    config = _config()
    tokenizer = XingChengTokenizer.from_config(config)
    ids = tokenizer.encode("NTP", add_bos=True, add_eos=True)
    assert len(tokenizer) == config.vocab_size
    assert all(0 <= token < config.vocab_size for token in ids)
    with pytest.raises(IndexError):
        _model()(torch.tensor([[config.vocab_size]], dtype=torch.long))


def test_nt7_pad_id_is_ignored_by_loss() -> None:
    logits = torch.tensor([[[2.0, 0.0], [0.0, 2.0], [8.0, -8.0]]])
    labels = torch.tensor([[0, 1, 0]], dtype=torch.long)
    actual = _causal_lm_loss(logits, labels, pad_token_id=0)
    expected = F.cross_entropy(logits[:, :-1].reshape(-1, 2), labels[:, 1:].reshape(-1), ignore_index=0)
    assert torch.allclose(actual, expected)


def test_nt8_sft_masks_prompt_and_padding_only() -> None:
    tokenizer = XingChengTokenizer(vocab_size=264)
    input_ids, labels = encode_sft_example(
        tokenizer, "prompt", "completion", max_length=64, pad_id=tokenizer.pad_id
    )
    prefix = tokenizer.encode("prompt", add_bos=True, add_eos=False)
    assert labels[: len(prefix)] == [tokenizer.pad_id] * len(prefix)
    assert any(label != tokenizer.pad_id for label in labels[len(prefix):])
    assert len(input_ids) == len(labels)


def test_nt9_cross_entropy_matches_manual_fixed_logits() -> None:
    logits = torch.tensor([[[2.0, 0.0], [0.0, 2.0], [1.0, 0.0]]])
    labels = torch.tensor([[0, 1, 0]], dtype=torch.long)
    actual = _causal_lm_loss(logits, labels, pad_token_id=0)
    manual = -torch.log_softmax(logits[0, 0], dim=-1)[1]
    assert torch.allclose(actual, manual)


def test_nt10_loss_reduction_uses_valid_target_tokens_only() -> None:
    logits = torch.tensor([[[3.0, 0.0], [0.0, 3.0], [0.0, 3.0], [9.0, -9.0]]])
    labels = torch.tensor([[0, 1, 0, 0]], dtype=torch.long)
    actual = _causal_lm_loss(logits, labels, pad_token_id=0)
    valid = labels[:, 1:].reshape(-1) != 0
    shifted = logits[:, :-1].reshape(-1, 2)[valid]
    targets = labels[:, 1:].reshape(-1)[valid]
    expected = F.cross_entropy(shifted, targets, reduction="mean")
    assert torch.allclose(actual, expected)


def test_nt11_target_changes_do_not_change_logits() -> None:
    model = _model()
    ids = torch.tensor([[4, 10, 11, 12]], dtype=torch.long)
    labels_a = torch.tensor([[0, 10, 11, 2]], dtype=torch.long)
    labels_b = torch.tensor([[0, 12, 13, 2]], dtype=torch.long)
    with torch.inference_mode():
        logits_a = model(ids, labels=labels_a)["logits"]
        logits_b = model(ids, labels=labels_b)["logits"]
    assert torch.equal(logits_a, logits_b)


def test_nt12_checkpoint_tokenizer_artifact_is_identical(tmp_path: Path) -> None:
    config = _config()
    model = XingChengForCausalLM(config)
    tokenizer = XingChengTokenizer.from_config(config)
    target = tmp_path / "ntp-checkpoint.pt"
    info = save_checkpoint(target, model, tokenizer=tokenizer)
    loaded = load_checkpoint(target)
    assert loaded["tokenizer"].state_dict() == tokenizer.state_dict()
    assert loaded["state_sha256"] == info["state_sha256"]
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    assert target.with_name(target.name + ".sha256").read_text(encoding="ascii").strip() == digest
    text = "星澄 NTP"
    assert loaded["tokenizer"].decode(
        loaded["tokenizer"].encode(text, add_bos=False, add_eos=False), skip_special=False
    ) == text
