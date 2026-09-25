import os
import sys

sys.path.insert(0, r"E:\GPTBridge\shared-layer\src")
sys.path.insert(
    0, r"E:\GPTBridge\Standalone tools\local-model\src\backend\services"
)

import torch

from xingcheng.infrastructure.native_transformer.checkpoint import (
    load_checkpoint,
)
from xingcheng.infrastructure.native_transformer.cpp_runtime import (
    ensure_bundle,
    load_engine,
)

CKPT = (
    r"Standalone tools\local-model\xingcheng\runtime\models\jobs"
    r"\sft-294m-moe16s1-v19-20260925\final.pt"
)

os.environ.setdefault("XINGCHENG_CPP_CUDA", "0")

bundle = ensure_bundle(CKPT)
print("bundle:", bundle["output_dir"])
engine = load_engine(bundle["output_dir"])

loaded = load_checkpoint(CKPT, map_location="cpu")
model = loaded["model"].eval()
tok = loaded["tokenizer"]

torch.manual_seed(0)
text = "<|im_start|>user\n什麼是光合作用？<|im_end|>\n<|im_start|>assistant\n"
ids = tok.encode(text)
print("tokens:", len(ids))

with torch.no_grad():
    t_logits = model(input_ids=torch.tensor([ids]))["logits"][0, -1].tolist()
c_logits = engine.logits(ids)

diffs = [abs(a - b) for a, b in zip(t_logits, c_logits)]
t_arg = max(range(len(t_logits)), key=lambda i: t_logits[i])
c_arg = max(range(len(c_logits)), key=lambda i: c_logits[i])
print("max logit diff:", max(diffs))
print("argmax torch:", t_arg, "cpp:", c_arg, "equal:", t_arg == c_arg)
