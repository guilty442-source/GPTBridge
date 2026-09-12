from __future__ import annotations

import hashlib
import math
import random
import re
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Sequence


_BOS = "<|bos|>"
_EOS = "<|eos|>"
MIN_TRAINING_GROUNDING_COVERAGE = 0.45

_FACT_PATTERNS: dict[str, str] = {
    "numbers": r"(?<![A-Za-z])\d+(?:\.\d+)?%?",
    "dates": r"(?<!\d)(?:19|20)\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?",
    "money": r"(?:NT\$|US\$|\$|新台幣|美元)\s?\d[\d,.]*",
    "urls": r"https?://[^\s)\]>，。！？；]+",
    "emails": r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])",
}


FIRST_PARTY_CORPUS: dict[str, tuple[str, ...]] = {
    "conversation": (
        "我會先理解使用者最新命令，再依指定語言、格式、長度與限制直接回答。",
        "一般對話不會自動改成能力介紹；需要專家時才依意圖交給對應模型。",
        "使用者要求簡短回覆時會保持精簡，要求步驟或程式碼時才展開完整內容。",
    ),
    "reading": (
        "閱讀理解會先分段處理原文，再從相關段落擷取答案並保留引用位置。",
        "摘要與問答都以使用者提供的文件為證據；原文沒有答案時會明確說明資訊不足。",
        "多文件閱讀會分別保留標題、內容雜湊、章節結構與關鍵句，避免混淆來源。",
    ),
    "self_upgrade": (
        "自我升級會先產生可稽核的程式碼提案，再依序完成範圍、語法、測試、治理與備份檢查。",
        "執行中的模型不能直接覆寫原始碼；發布必須走可回復的版本化流程。",
        "星澄會自行設計與驗證候選變更，並保留來源雜湊及回復資訊。",
    ),
    "coding": (
        "程式設計專家會把需求轉成結構化規格，再產生可解析與可測試的程式碼。",
        "產生的程式碼預設不執行，必須先通過語法、範圍與治理檢查。",
        "無法確認的需求會保留為輸入條件，不會用危險操作填補缺漏。",
    ),
    "capabilities": (
        "我是星澄，一個在本機執行的生成式語言模型。我會先理解需求，再使用經授權的資料與工具完成工作。",
        "我能整理資料、進行推理與計算，也能協調投資分析；遇到缺少的事實時會清楚標示未知。",
        "我的文字由本機語言模型逐字生成，所有工具呼叫仍受治理規則與資料邊界約束。",
    ),
    "status": (
        "星澄的本機生成核心已就緒，推論不依賴外部模型服務。",
        "模型會保存受控的學習樣本，並以版本化統計檢查持續改善。",
        "自我訓練只接受通過品質檢查的內容，不會無條件學習未驗證的回覆。",
    ),
    "search": (
        "我會先辨識查詢目標，再從允許的公開來源取得資料並保留來源資訊。",
        "搜尋結果必須附帶可追溯來源；沒有足夠證據時，我會保留未知而不自行補造。",
        "外部協作只能經過受治理通道，搜尋資料不會直接改寫其他工具的資料庫。",
    ),
    "distribution": (
        "我會區分確定無配息與資料不足，並優先採用可驗證的官方公告。",
        "配息事件需要日期、幣別與來源證據，無法確認的欄位不會覆寫人工資料。",
        "重複查詢會使用有限期快取，新的可靠證據才能更新既有判定。",
    ),
    "quote": (
        "報價與基金淨值必須包含來源和觀測時間，我不會猜測即時價格。",
        "市場數值會先經識別與品質檢查，再交給投資分析工具使用。",
        "如果來源互相矛盾，我會呈現不確定性並保留原始資料。",
    ),
    "risk": (
        "風險分析會檢查集中度、幣別曝險、波動與資料缺口，結論以可重現計算為基礎。",
        "我會把已知事實、模型估計與需要人工判斷的事項分開呈現。",
        "缺少持股或市場資料時，我會先說明所需輸入，不會生成虛構結論。",
    ),
    "analysis": (
        "投資分析會整合持股、參數、行情與風險結果，並保留每項結論的證據。",
        "分析不是交易指令；我會揭露限制、資料時間與仍需確認的假設。",
        "所有計算由本機專家工具完成，生成文字只負責解釋可驗證的結果。",
    ),
    "calculation": (
        "我會保留輸入、公式、步驟與結果，缺少條件時先列出未知量。",
        "數值由本機數理工具計算，語言模型負責把結果組織成容易核對的說明。",
        "計算結果會接受範圍與格式檢查，避免把生成文字當成數值證據。",
    ),
    "reasoning": (
        "我會區分前提、推導步驟與結論，並檢查矛盾、隱含假設及證據是否足夠。",
        "推理過程以提供的事實為界，不會把可能性寫成已確認的事實。",
        "如果存在多種合理解釋，我會保留替代方案與需要補充的資訊。",
    ),
    "statistics": (
        "統計工作會保留樣本數、集中趨勢與離散程度，並標示資料限制。",
        "數值統計由本機工具重現計算，生成模型負責解釋結果而不修改原始值。",
        "遇到空值、異常值或樣本不足時，我會明確呈現資料品質問題。",
    ),
    "data_organization": (
        "資料會依欄位、缺漏值與指定分組整理，原始值不會被猜測或補造。",
        "整理結果會保留列數、欄位與轉換紀錄，方便追蹤每個變更。",
        "生成模型只描述整理結果，實際資料操作由可重現的本機工具執行。",
    ),
}


class StarAutoregressiveLanguageModel:
    """Compact first-party statistical LM with bounded autoregressive decoding.

    It learns weighted token n-gram transitions from local, reviewed text.  This is
    intentionally small enough to run without third-party weights while still
    being a genuine language model: every next token is sampled from a learned
    conditional probability distribution.
    """

    MODEL_TYPE = "weighted-backoff-token-ngram"

    def __init__(
        self,
        *,
        order: int = 4,
        corpus: Mapping[str, Sequence[str]] | None = None,
        learned_examples: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        if order < 2 or order > 8:
            raise ValueError("language model order must be between 2 and 8")
        self.order = order
        self._counts: dict[tuple[str, ...], Counter[str]] = defaultdict(Counter)
        self._vocabulary: set[str] = set()
        self._example_hashes: set[str] = set()
        self._examples: list[dict[str, str]] = []
        self._base_example_count = 0
        self._learned_example_count = 0
        for intent, texts in (corpus or FIRST_PARTY_CORPUS).items():
            for target in texts:
                if self.learn(
                    str(intent),
                    str(target),
                    input_text=str(target),
                    weight=3,
                    source="first-party",
                ):
                    self._base_example_count += 1
        for example in learned_examples:
            self.learn(
                str(example.get("intent") or "capabilities"),
                str(example.get("target_text") or ""),
                input_text=str(example.get("input_text") or ""),
                weight=max(1, min(5, round(float(example.get("quality_score") or 0.8) * 5))),
                source="self-training",
            )

    @staticmethod
    def tokenize(text: str) -> list[str]:
        # Han text has no mandatory whitespace.  Treating a complete Chinese
        # sentence as one token makes an n-gram model a sentence selector rather
        # than a language model.  Character tokens keep the tokenizer fully
        # first-party while allowing the decoder to learn and compose Chinese.
        return re.findall(
            r"[\u3400-\u9fff]|[A-Za-z]+(?:[-_][A-Za-z]+)*|\d+(?:\.\d+)?%?|[^\s]",
            str(text or "").strip(),
        )

    @staticmethod
    def _fact_values(text: str) -> dict[str, set[str]]:
        return {
            name: {
                re.sub(r"[\s,]", "", match.casefold())
                for match in re.findall(pattern, str(text or ""), flags=re.IGNORECASE)
            }
            for name, pattern in _FACT_PATTERNS.items()
        }

    @staticmethod
    def detokenize(tokens: Sequence[str]) -> str:
        output = ""
        previous = ""
        for token in tokens:
            if token in {_BOS, _EOS} or token.startswith("<|intent:"):
                continue
            needs_space = bool(
                output
                and re.fullmatch(r"[A-Za-z0-9_.-]+", token)
                and re.fullmatch(r"[A-Za-z0-9_.-]+", previous)
            )
            output += (" " if needs_space else "") + token
            previous = token
        return output.strip()

    @staticmethod
    def _intent_token(intent: str) -> str:
        normalized = re.sub(r"[^a-z_]", "", str(intent).casefold()) or "capabilities"
        return f"<|intent:{normalized}|>"

    def _sequence(self, intent: str, target: str) -> list[str]:
        return [*([_BOS] * (self.order - 1)), self._intent_token(intent), *self.tokenize(target), _EOS]

    def _accumulate(
        self,
        counts: dict[tuple[str, ...], Counter[str]],
        sequence: Sequence[str],
        *,
        weight: int,
    ) -> None:
        for index in range(self.order, len(sequence)):
            next_token = sequence[index]
            for width in range(self.order):
                context = tuple(sequence[index - width:index]) if width else ()
                counts[context][next_token] += weight

    def learn(
        self,
        intent: str,
        target: str,
        *,
        input_text: str = "",
        weight: int = 1,
        source: str = "self",
    ) -> bool:
        normalized = str(target or "").strip()
        normalized_input = str(input_text or normalized).strip()
        tokens = self.tokenize(normalized)
        if len(tokens) < 3 or len(tokens) > 360:
            return False
        digest = hashlib.sha256(
            f"{intent}\0{normalized_input}\0{normalized}".encode("utf-8")
        ).hexdigest()
        if digest in self._example_hashes:
            return False
        self._example_hashes.add(digest)
        self._examples.append(
            {
                "intent": str(intent),
                "input_text": normalized_input,
                "target_text": normalized,
                "source": str(source),
            }
        )
        sequence = self._sequence(intent, normalized)
        self._accumulate(self._counts, sequence, weight=max(1, min(12, int(weight))))
        self._vocabulary.update(token for token in sequence if token not in {_BOS, _EOS})
        if source != "first-party":
            self._learned_example_count += 1
        return True

    @staticmethod
    def _seed(prompt: str, intent: str) -> int:
        digest = hashlib.sha256(
            f"{intent}\0{prompt}".encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:8], "big")

    def _distribution(
        self,
        history: Sequence[str],
        transient: Mapping[tuple[str, ...], Counter[str]],
    ) -> Counter[str]:
        distribution: Counter[str] = Counter()
        maximum = min(self.order - 1, len(history))
        for width in range(maximum, -1, -1):
            context = tuple(history[-width:]) if width else ()
            persistent = self._counts.get(context, {})
            contextual = transient.get(context, {})
            if not persistent and not contextual:
                continue
            for token, count in persistent.items():
                distribution[token] += count
            for token, count in contextual.items():
                distribution[token] += count * 32
            return distribution
        return distribution

    @staticmethod
    def _sample(
        distribution: Counter[str],
        *,
        randomizer: random.Random,
        temperature: float,
        top_k: int,
    ) -> str:
        candidates = distribution.most_common(max(1, top_k))
        if not candidates:
            return _EOS
        if temperature <= 0:
            return candidates[0][0]
        exponent = 1.0 / max(0.1, min(2.0, temperature))
        weighted = [(token, math.pow(max(1, count), exponent)) for token, count in candidates]
        threshold = randomizer.random() * sum(weight for _, weight in weighted)
        cumulative = 0.0
        for token, weight in weighted:
            cumulative += weight
            if cumulative >= threshold:
                return token
        return weighted[-1][0]

    def generate(
        self,
        *,
        intent: str,
        prompt: str,
        grounding: str,
        max_tokens: int = 180,
        temperature: float = 0.55,
        top_k: int = 4,
    ) -> dict[str, Any]:
        transient: dict[tuple[str, ...], Counter[str]] = defaultdict(Counter)
        prompt_tokens = set(self.tokenize(prompt))
        conditioned_examples: list[tuple[float, dict[str, str]]] = []
        for example in self._examples:
            if example["intent"] != str(intent):
                continue
            example_tokens = set(self.tokenize(example["input_text"]))
            similarity = (
                2 * len(prompt_tokens & example_tokens)
                / (len(prompt_tokens) + len(example_tokens))
                if prompt_tokens and example_tokens
                else 0.0
            )
            if similarity > 0:
                conditioned_examples.append((similarity, example))
        conditioned_examples.sort(key=lambda item: -item[0])
        for similarity, example in conditioned_examples[:6]:
            self._accumulate(
                transient,
                self._sequence(intent, example["target_text"]),
                weight=max(1, round(2 + similarity * 10)),
            )
        normalized_grounding = str(grounding or "").strip()
        if normalized_grounding:
            self._accumulate(
                transient,
                self._sequence(intent, normalized_grounding),
                weight=12,
            )
        history = [*([_BOS] * (self.order - 1)), self._intent_token(intent)]
        output: list[str] = []
        randomizer = random.Random(
            self._seed(prompt, intent)
        )
        repeated: Counter[tuple[str, str, str]] = Counter()
        for _ in range(max(16, min(360, int(max_tokens)))):
            distribution = self._distribution(history, transient)
            token = self._sample(
                distribution,
                randomizer=randomizer,
                temperature=temperature,
                top_k=top_k,
            )
            if token == _EOS:
                break
            if len(output) >= 2:
                trigram = (output[-2], output[-1], token)
                repeated[trigram] += 1
                if repeated[trigram] > 2:
                    break
            output.append(token)
            history.append(token)
        text = self.detokenize(output)
        grounding_facts = self._fact_values(normalized_grounding)
        output_facts = self._fact_values(text)
        missing_facts = {
            name: sorted(values - output_facts[name])
            for name, values in grounding_facts.items()
            if values - output_facts[name]
        }
        unsupported_facts = {
            name: sorted(values - grounding_facts[name])
            for name, values in output_facts.items()
            if values - grounding_facts[name]
        }
        facts_preserved = not missing_facts and not unsupported_facts
        fallback_used = len(text) < 8 or not facts_preserved
        if fallback_used:
            text = normalized_grounding
            grounding_facts = self._fact_values(normalized_grounding)
            output_facts = self._fact_values(text)
            missing_facts = {}
            unsupported_facts = {}
            facts_preserved = grounding_facts == output_facts
        if text and text[-1] not in "。！？.!?":
            text += "。"
        return {
            "text": text,
            "token_count": len(self.tokenize(text)),
            "decoder": "autoregressive-probabilistic-decoder",
            "model_type": self.MODEL_TYPE,
            "order": self.order,
            "temperature": temperature,
            "top_k": top_k,
            "grounding_fallback_used": fallback_used,
            "facts_preserved": facts_preserved,
            "facts_supported": not unsupported_facts,
            "missing_facts": missing_facts,
            "unsupported_facts": unsupported_facts,
            "prompt_conditioned_example_count": min(6, len(conditioned_examples)),
        }

    def metrics(self) -> dict[str, Any]:
        transition_count = sum(sum(counter.values()) for counter in self._counts.values())
        return {
            "model_type": self.MODEL_TYPE,
            "order": self.order,
            "vocabulary_size": len(self._vocabulary),
            "context_count": len(self._counts),
            "weighted_transition_count": transition_count,
            "base_example_count": self._base_example_count,
            "learned_example_count": self._learned_example_count,
            "prompt_conditioned_example_count": len(self._examples),
            "third_party_weights_used": False,
        }


__all__ = [
    "FIRST_PARTY_CORPUS",
    "MIN_TRAINING_GROUNDING_COVERAGE",
    "StarAutoregressiveLanguageModel",
]
