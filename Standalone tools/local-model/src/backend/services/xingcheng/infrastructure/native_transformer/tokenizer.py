"""星澄簡易 Tokenizer。

第一版使用位元組對 (byte-pair) 字元 tokenizer，確保完全本地、無外部依賴。
未來可替換為 BPE / SentencePiece / Tiktoken 等正規 tokenizer，介面保持一致。

設計：
  - pad=0, bos=1, eos=2, unk=3
  - 4~255：單位元組 token（UTF-8 byte）
  - 256+：保留給未來 BPE 合併規則
"""

from __future__ import annotations

from typing import Iterable, List, Sequence

PAD_ID = 0
BOS_ID = 1
EOS_ID = 2
UNK_ID = 3
BYTE_BASE = 4


class XingChengTokenizer:
    """位元組級 tokenizer；可完全本地執行。"""

    def __init__(self, vocab_size: int = 8_192) -> None:
        if vocab_size < 256 + BYTE_BASE:
            raise ValueError("vocab_size 至少需 260 以容納位元組 token")
        self.vocab_size = int(vocab_size)
        self.pad_id = PAD_ID
        self.bos_id = BOS_ID
        self.eos_id = EOS_ID
        self.unk_id = UNK_ID

    # ── 編碼 ────────────────────────────────────────────────────
    def encode(
        self,
        text: str,
        *,
        add_bos: bool = True,
        add_eos: bool = False,
        max_length: int | None = None,
    ) -> List[int]:
        ids: List[int] = []
        if add_bos:
            ids.append(self.bos_id)
        for byte in text.encode("utf-8"):
            ids.append(BYTE_BASE + byte)
        if add_eos:
            ids.append(self.eos_id)
        if max_length is not None and len(ids) > max_length:
            ids = ids[:max_length]
            if ids and ids[-1] != self.eos_id and add_eos:
                ids[-1] = self.eos_id
        return ids

    def encode_batch(
        self,
        texts: Sequence[str],
        *,
        add_bos: bool = True,
        add_eos: bool = True,
        max_length: int | None = None,
    ) -> List[List[int]]:
        return [
            self.encode(t, add_bos=add_bos, add_eos=add_eos, max_length=max_length)
            for t in texts
        ]

    # ── 解碼 ────────────────────────────────────────────────────
    def decode(self, ids: Iterable[int], *, skip_special: bool = True) -> str:
        special = {self.pad_id, self.bos_id, self.eos_id, self.unk_id}
        bytes_: List[int] = []
        for token in ids:
            if skip_special and token in special:
                continue
            if BYTE_BASE <= token < BYTE_BASE + 256:
                bytes_.append(token - BYTE_BASE)
            else:
                # 未實作的 BPE token 以空白佔位
                bytes_.append(0x20)
        return bytes(bytes_).decode("utf-8", errors="replace")

    # ── 狀態 ────────────────────────────────────────────────────
    def __len__(self) -> int:
        return self.vocab_size

    def state_dict(self) -> dict:
        return {"vocab_size": self.vocab_size}

    @classmethod
    def from_state(cls, state: dict) -> "XingChengTokenizer":
        return cls(vocab_size=int(state["vocab_size"]))
