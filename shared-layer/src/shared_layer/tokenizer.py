"""Tokenizer wrapper integrating jieba with POS tagging and custom dictionary support."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

import jieba
import jieba.posseg as pseg


@dataclass(frozen=True)
class Token:
    """A single token with its text and part-of-speech tag."""
    text: str
    pos: str
    start: int
    end: int


class TokenizerWrapper:
    """
    Wrapper around jieba tokenizer with POS tagging and custom dictionary support.

    Provides a clean interface for Chinese text tokenization with part-of-speech
    tagging, supporting custom dictionary loading for domain-specific terms.
    """

    def __init__(
        self,
        dict_path: Optional[str | Path] = None,
        user_dict_paths: Optional[Iterable[str | Path]] = None,
        hmm: bool = True,
    ) -> None:
        """
        Initialize the tokenizer wrapper.

        Args:
            dict_path: Optional path to a custom jieba dictionary file.
            user_dict_paths: Optional iterable of paths to user dictionary files.
            hmm: Whether to use HMM model for unknown words (default: True).
        """
        self._hmm = hmm
        self._user_dict_paths: list[Path] = []

        if dict_path:
            jieba.set_dictionary(str(dict_path))

        if user_dict_paths:
            for path in user_dict_paths:
                self.load_user_dict(path)

    def load_user_dict(self, path: str | Path) -> None:
        """
        Load a user dictionary for custom terms.

        Args:
            path: Path to the user dictionary file.
                  Format: one term per line, optionally with frequency and POS tag.
                  e.g., "自定义词 10 n" or just "自定义词"
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"User dictionary not found: {path}")
        jieba.load_userdict(str(path))
        self._user_dict_paths.append(path)

    def add_word(
        self,
        word: str,
        freq: Optional[int] = None,
        tag: Optional[str] = None,
    ) -> None:
        """
        Add a single word to the tokenizer's dictionary dynamically.

        Args:
            word: The word to add.
            freq: Optional frequency (higher = more likely to be segmented).
            tag: Optional POS tag (e.g., 'n', 'v', 'eng').
        """
        jieba.add_word(word, freq=freq, tag=tag)

    def del_word(self, word: str) -> None:
        """
        Remove a word from the tokenizer's dictionary.

        Args:
            word: The word to remove.
        """
        jieba.del_word(word)

    def suggest_freq(self, segment: tuple[str, ...], tune: bool = True) -> None:
        """
        Suggest frequency for a word segment to adjust segmentation.

        Args:
            segment: Tuple of words forming the segment.
            tune: Whether to tune the frequency automatically.
        """
        jieba.suggest_freq(segment, tune=tune)

    def tokenize(self, text: str) -> list[Token]:
        """
        Tokenize text with POS tagging.

        Args:
            text: Input text to tokenize.

        Returns:
            List of Token objects with text, POS tag, and position info.
        """
        if not text:
            return []

        tokens: list[Token] = []
        offset = 0

        for word, pos in pseg.cut(text, HMM=self._hmm):
            start = text.find(word, offset)
            if start == -1:
                start = offset
            end = start + len(word)
            tokens.append(Token(text=word, pos=pos, start=start, end=end))
            offset = end

        return tokens

    def cut(self, text: str) -> list[str]:
        """
        Simple tokenization without POS tags.

        Args:
            text: Input text to tokenize.

        Returns:
            List of token strings.
        """
        if not text:
            return []
        return list(jieba.cut(text, HMM=self._hmm))

    def cut_iter(self, text: str) -> Iterator[str]:
        """
        Tokenize text as an iterator (memory efficient for long texts).

        Args:
            text: Input text to tokenize.

        Yields:
            Token strings one at a time.
        """
        if not text:
            return
        yield from jieba.cut(text, HMM=self._hmm)

    def tokenize_iter(self, text: str) -> Iterator[Token]:
        """
        Tokenize text with POS tags as an iterator.

        Args:
            text: Input text to tokenize.

        Yields:
            Token objects one at a time.
        """
        if not text:
            return
        offset = 0
        for word, pos in pseg.cut(text, HMM=self._hmm):
            start = text.find(word, offset)
            if start == -1:
                start = offset
            end = start + len(word)
            yield Token(text=word, pos=pos, start=start, end=end)
            offset = end

    @property
    def loaded_user_dicts(self) -> list[Path]:
        """Return list of loaded user dictionary paths."""
        return self._user_dict_paths.copy()

    def reset_user_dicts(self) -> None:
        """Clear all loaded user dictionaries and reload jieba default."""
        jieba.initialize()
        self._user_dict_paths.clear()


def create_tokenizer(
    dict_path: Optional[str | Path] = None,
    user_dict_paths: Optional[Iterable[str | Path]] = None,
    hmm: bool = True,
) -> TokenizerWrapper:
    """
    Factory function to create a TokenizerWrapper instance.

    Args:
        dict_path: Optional path to a custom jieba dictionary file.
        user_dict_paths: Optional iterable of paths to user dictionary files.
        hmm: Whether to use HMM model for unknown words.

    Returns:
        Configured TokenizerWrapper instance.
    """
    return TokenizerWrapper(dict_path=dict_path, user_dict_paths=user_dict_paths, hmm=hmm)