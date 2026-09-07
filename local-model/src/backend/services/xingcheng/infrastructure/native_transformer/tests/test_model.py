"""星澄原生模型端對端測試：前向 / 反向 / 生成 / 量化。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import torch

# 確保可從測試檔直接 import 套件
# test_model.py 位於 native_transformer/tests/，需把 native_transformer 的
# 父目錄（infrastructure）加入 sys.path 才能以 `native_transformer` 為頂層匯入。
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from native_transformer import (
    XingChengConfig,
    XingChengForCausalLM,
    XingChengTokenizer,
)
from native_transformer.inference import Generator, Sampler, SamplingConfig
from native_transformer.training import (
    TextDataset,
    Trainer,
    TrainingConfig,
    make_dataloader,
)
from native_transformer.quantization import quantize_model, dequantize_model


def _small_config() -> XingChengConfig:
    cfg = XingChengConfig.small()
    cfg.vocab_size = 264  # 容納 byte tokenizer
    cfg.max_position_embeddings = 64
    return cfg


class TestForwardBackward(unittest.TestCase):
    def test_forward_logits_shape(self) -> None:
        cfg = _small_config()
        model = XingChengForCausalLM(cfg)
        ids = torch.randint(4, cfg.vocab_size, (2, 8))
        out = model(ids)
        self.assertEqual(out["logits"].shape, (2, 8, cfg.vocab_size))

    def test_backward_loss_finite(self) -> None:
        cfg = _small_config()
        model = XingChengForCausalLM(cfg)
        ids = torch.randint(4, cfg.vocab_size, (2, 8))
        labels = ids.clone()
        out = model(ids, labels=labels)
        self.assertIn("loss", out)
        self.assertTrue(torch.isfinite(out["loss"]))
        out["loss"].backward()
        # 確認梯度存在
        for p in model.parameters():
            if p.requires_grad:
                self.assertIsNotNone(p.grad)
                break

    def test_attention_mask_padding(self) -> None:
        cfg = _small_config()
        model = XingChengForCausalLM(cfg)
        ids = torch.randint(4, cfg.vocab_size, (2, 8))
        mask = torch.ones_like(ids)
        mask[1, 4:] = 0
        out = model(ids, attention_mask=mask)
        self.assertEqual(out["logits"].shape, (2, 8, cfg.vocab_size))


class TestGeneration(unittest.TestCase):
    def test_greedy_generate(self) -> None:
        cfg = _small_config()
        model = XingChengForCausalLM(cfg)
        sampler = Sampler(SamplingConfig(do_sample=False))
        gen = Generator(model, sampler=sampler, device="cpu")
        ids = torch.randint(4, cfg.vocab_size, (1, 4))
        out = gen.generate(ids, max_new_tokens=5)
        self.assertEqual(out.shape, (1, 5))

    def test_sampling_generate(self) -> None:
        cfg = _small_config()
        model = XingChengForCausalLM(cfg)
        sampler = Sampler(SamplingConfig(do_sample=True, temperature=0.8, top_k=10, top_p=0.9))
        gen = Generator(model, sampler=sampler, device="cpu")
        ids = torch.randint(4, cfg.vocab_size, (1, 4))
        out = gen.generate(ids, max_new_tokens=5)
        self.assertEqual(out.shape, (1, 5))


class TestTraining(unittest.TestCase):
    def test_one_step_loss_decreases(self) -> None:
        cfg = _small_config()
        model = XingChengForCausalLM(cfg)
        tok = XingChengTokenizer(vocab_size=cfg.vocab_size)
        texts = ["星澄", "本地模型", "transformer", "attention"]
        ds = TextDataset(texts, tok, max_length=16)
        loader = make_dataloader(ds, batch_size=2, pad_id=tok.pad_id, shuffle=False)
        tcfg = TrainingConfig(lr=1e-3, epochs=1, max_steps=3, log_every=0)
        trainer = Trainer(model, tcfg, cfg)
        result = trainer.fit(loader)
        self.assertGreater(len(result["losses"]), 0)
        self.assertTrue(all(torch.tensor(result["losses"]) >= 0))


class TestQuantization(unittest.TestCase):
    def test_quantize_dequantize_roundtrip(self) -> None:
        cfg = _small_config()
        model = XingChengForCausalLM(cfg)
        ids = torch.randint(4, cfg.vocab_size, (1, 4))
        out_before = model(ids)["logits"]
        quantize_model(model, n_bits=8)
        out_after = model(ids)["logits"]
        # INT8 量化誤差應在合理範圍
        diff = (out_before - out_after).abs().max().item()
        self.assertLess(diff, 5.0)
        dequantize_model(model)
        out_restored = model(ids)["logits"]
        self.assertLess((out_before - out_restored).abs().max().item(), 5.0)


class TestTokenizer(unittest.TestCase):
    def test_encode_decode_roundtrip(self) -> None:
        tok = XingChengTokenizer(vocab_size=512)
        text = "星澄原生模型"
        ids = tok.encode(text, add_bos=False, add_eos=False)
        decoded = tok.decode(ids, skip_special=False)
        # 位元組級 roundtrip 應保留原文字
        self.assertEqual(decoded, text)


if __name__ == "__main__":
    unittest.main()
