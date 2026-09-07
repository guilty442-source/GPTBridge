"""Content Cleaner — 移除網頁中的無關內容。

對應需求 30：
  移除：Navigation、Footer、Sidebar、Ads、Cookie Banner、推薦文章、
        重複標題、追蹤碼、Script、Style、無關內容
  輸出：Clean Document
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .html_parser import ParsedPage


# 無關內容模式（標題/段落層級）
_NAV_PATTERNS = (
    re.compile(r"^(首頁|關於我們|聯絡我們|隱私權|條款|登入|註冊|搜尋|選單|導航)$"),
    re.compile(r"^(home|about|contact|privacy|terms|login|register|menu|navigation)$", re.I),
)

_COOKIE_PATTERNS = [
    re.compile(r"cookie|cookies|gdpr|consent|accept|同意|接受|拒絕", re.I),
]

_AD_PATTERNS = [
    re.compile(r"(?:sponsored|advertisement|廣告|贊助|推廣)", re.I),
]

_RECOMMEND_PATTERNS = [
    re.compile(r"(?:推薦文章|相關文章|你可能也喜歡|延伸閱讀|recommended|related posts)", re.I),
]

_TRACKING_PATTERNS = [
    re.compile(r"(?:utm_|tracking|analytics|gtag|pixel|fbclid|ref=)", re.I),
]


@dataclass
class CleanDocument:
    """清理後的文件。"""

    url: str
    title: str
    author: str | None = None
    published_time: str | None = None
    updated_time: str | None = None
    language: str | None = None
    text: str = ""
    paragraph_count: int = 0
    word_count: int = 0
    removed_count: int = 0

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "author": self.author,
            "published_time": self.published_time,
            "updated_time": self.updated_time,
            "language": self.language,
            "paragraph_count": self.paragraph_count,
            "word_count": self.word_count,
            "removed_count": self.removed_count,
        }


class ContentCleaner:
    """網頁內容清理器。"""

    def clean(self, page: ParsedPage) -> CleanDocument:
        removed = 0
        clean_paragraphs: list[str] = []

        for para in page.paragraphs:
            # 跳過導航
            if any(p.match(para.strip()) for p in _NAV_PATTERNS):
                removed += 1
                continue
            # 跳過 Cookie Banner
            if any(p.search(para) for p in _COOKIE_PATTERNS) and len(para) < 100:
                removed += 1
                continue
            # 跳過廣告
            if any(p.search(para) for p in _AD_PATTERNS) and len(para) < 80:
                removed += 1
                continue
            # 跳過推薦文章
            if any(p.search(para) for p in _RECOMMEND_PATTERNS) and len(para) < 80:
                removed += 1
                continue
            # 跳過追蹤碼
            if any(p.search(para) for p in _TRACKING_PATTERNS):
                removed += 1
                continue
            # 跳過過短段落（可能是 UI 文字）
            if len(para.strip()) < 15:
                removed += 1
                continue
            # 跳過重複段落
            if para in clean_paragraphs:
                removed += 1
                continue
            clean_paragraphs.append(para)

        # 移除重複標題（如果標題出現在正文開頭）
        if page.title and clean_paragraphs and clean_paragraphs[0].strip() == page.title.strip():
            clean_paragraphs.pop(0)
            removed += 1

        text = "\n\n".join(clean_paragraphs)
        return CleanDocument(
            url=page.url,
            title=page.title,
            author=page.author,
            published_time=page.published_time,
            updated_time=page.updated_time,
            language=page.language,
            text=text,
            paragraph_count=len(clean_paragraphs),
            word_count=len(text),
            removed_count=removed,
        )


__all__ = ["ContentCleaner", "CleanDocument"]
