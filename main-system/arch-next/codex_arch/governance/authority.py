"""authority — 法典完整性守衛。

法典實體檔受作業系統唯讀保護，權威完整性於載入與執行前驗證（A15）。
本守衛持有法碼檔之密封摘要（sealed digests），驗證時重算比對；
任何不符即 fail-closed 丟出 PermissionError，不揭露細節（A11）。
驗證過程唯讀，不寫入任何執行狀態。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping

_ARCH_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
GOVERNANCE_RULE_ROOT: Final[Path] = _ARCH_ROOT / "governance_rule"

SEALED_CODEX_DIGESTS: Final[Mapping[str, str]] = {
    "governance_rule/codex/__init__.py": (
        "f05a24d36c433be9a9ef57af4c3fa7cd93ceba11e57a7aecebe1116ac2bbd152"
    ),
    "governance_rule/codex/chinese.py": (
        "f885c4aeeace79ca308d8e786147e3d36f3f910cc9a9ce3ba79d132f9060ae9b"
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


@dataclass(frozen=True)
class IntegrityVerdict:
    ok: bool
    checked: tuple[str, ...] = ()
    mismatched: tuple[str, ...] = ()
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "checked": list(self.checked),
            "mismatched": list(self.mismatched),
            "reason": self.reason,
        }


class AuthorityGuard:
    """唯讀法典權威完整性守衛（fail-closed）。"""

    def __init__(
        self,
        *,
        rules_root: Path = GOVERNANCE_RULE_ROOT,
        sealed: Mapping[str, str] | None = None,
    ) -> None:
        self.rules_root = rules_root.resolve()
        self.sealed = dict(sealed or SEALED_CODEX_DIGESTS)

    def verify(self) -> IntegrityVerdict:
        checked: list[str] = []
        mismatched: list[str] = []
        for relative, expected in self.sealed.items():
            candidate = (self.rules_root / relative).resolve()
            if not _inside(candidate.parent, self.rules_root.resolve()) or not _inside(
                candidate, self.rules_root.resolve()
            ):
                mismatched.append(relative)
                continue
            checked.append(relative)
            try:
                if _sha256(candidate) != expected:
                    mismatched.append(relative)
            except (OSError, PermissionError):
                mismatched.append(relative)
        ok = not mismatched
        return IntegrityVerdict(
            ok=ok,
            checked=tuple(checked),
            mismatched=tuple(mismatched),
            reason="" if ok else "codex-integrity-mismatch",
        )


__all__ = [
    "AuthorityGuard",
    "GOVERNANCE_RULE_ROOT",
    "IntegrityVerdict",
    "SEALED_CODEX_DIGESTS",
    "_sha256",
]