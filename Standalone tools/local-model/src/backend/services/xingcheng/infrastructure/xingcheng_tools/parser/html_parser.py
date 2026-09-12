"""HTML Parser — 將 HTML 轉換成結構化文字。

對應需求 29-30：
  - DOM Parse → Main Content Detection → Metadata Extraction → Structured Text
  - 抽取：標題、正文、段落、標題層級、表格、列表、作者、日期、Canonical URL、語言
  - Parser 不得直接把整個 HTML 原文送入模型
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedPage:
    """解析後的網頁結構。"""

    url: str
    canonical_url: str | None = None
    title: str = ""
    author: str | None = None
    published_time: str | None = None
    updated_time: str | None = None
    language: str | None = None
    description: str | None = None
    headings: list[dict[str, Any]] = field(default_factory=list)  # [{level, text, position}]
    paragraphs: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    lists: list[str] = field(default_factory=list)
    main_text: str = ""
    word_count: int = 0
    parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "author": self.author,
            "published_time": self.published_time,
            "updated_time": self.updated_time,
            "language": self.language,
            "description": self.description,
            "heading_count": len(self.headings),
            "paragraph_count": len(self.paragraphs),
            "table_count": len(self.tables),
            "list_count": len(self.lists),
            "word_count": self.word_count,
            "main_text_length": len(self.main_text),
            "parse_error": self.parse_error,
        }


class HTMLParser:
    """HTML 解析器。

    優先使用 BeautifulSoup（若已安裝），否則退回內建 regex 解析。
    不依賴外部瀏覽器引擎。
    """

    def __init__(self) -> None:
        self._use_bs4 = self._check_bs4()

    @staticmethod
    def _check_bs4() -> bool:
        try:
            import bs4  # noqa: F401
            return True
        except ImportError:
            return False

    def parse(self, html: str, url: str) -> ParsedPage:
        if self._use_bs4:
            try:
                return self._parse_bs4(html, url)
            except Exception as exc:
                # BS4 失敗時退回 regex
                page = self._parse_regex(html, url)
                page.parse_error = f"BS4 parse failed, fallback to regex: {exc}"
                return page
        return self._parse_regex(html, url)

    # ── BeautifulSoup 路徑 ──────────────────────────────────────
    def _parse_bs4(self, html: str, url: str) -> ParsedPage:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        # 移除不需要的標籤
        for tag in soup.find_all(["script", "style", "noscript", "iframe", "svg"]):
            tag.decompose()

        page = ParsedPage(url=url)

        # 標題
        if soup.title:
            page.title = soup.title.get_text(strip=True)
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            page.title = page.title or og_title["content"]

        # Canonical
        canonical = soup.find("link", rel="canonical")
        if canonical and canonical.get("href"):
            page.canonical_url = canonical["href"]

        # 作者
        author = soup.find("meta", attrs={"name": "author"}) or \
                 soup.find("meta", property="article:author")
        if author and author.get("content"):
            page.author = author["content"]

        # 發布時間
        published = soup.find("meta", property="article:published_time") or \
                    soup.find("time", attrs={"datetime": True})
        if published:
            page.published_time = published.get("content") or published.get("datetime")

        # 更新時間
        updated = soup.find("meta", property="article:modified_time")
        if updated and updated.get("content"):
            page.updated_time = updated["content"]

        # 語言
        if soup.html and soup.html.get("lang"):
            page.language = soup.html["lang"]

        # 描述
        desc = soup.find("meta", attrs={"name": "description"}) or \
               soup.find("meta", property="og:description")
        if desc and desc.get("content"):
            page.description = desc["content"]

        # 主要內容偵測
        main = self._detect_main_content(soup)
        if main is None:
            main = soup

        # 標題層級
        for h in main.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
            level = int(h.name[1])
            text = h.get_text(strip=True)
            if text:
                page.headings.append({"level": level, "text": text})

        # 段落
        for p in main.find_all("p"):
            text = p.get_text(strip=True)
            if text and len(text) > 10:
                page.paragraphs.append(text)

        # 表格
        for table in main.find_all("table"):
            text = table.get_text(separator=" | ", strip=True)
            if text and len(text) > 10:
                page.tables.append(text)

        # 列表
        for lst in main.find_all(["ul", "ol"]):
            text = lst.get_text(separator="\n", strip=True)
            if text and len(text) > 5:
                page.lists.append(text)

        page.main_text = "\n\n".join(page.paragraphs)
        page.word_count = len(page.main_text)
        return page

    @staticmethod
    def _detect_main_content(soup: Any) -> Any:
        """偵測主要內容區域。"""
        # 常見 main content 容器
        for selector in ["main", "article", "[role='main']", ".post-content",
                         ".article-content", ".entry-content", "#content", ".content"]:
            try:
                found = soup.select_one(selector)
                if found:
                    return found
            except Exception:
                continue
        return None

    # ── Regex fallback 路徑 ─────────────────────────────────────
    def _parse_regex(self, html: str, url: str) -> ParsedPage:
        page = ParsedPage(url=url)

        # 標題
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if title_match:
            page.title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()

        # Canonical
        canonical_match = re.search(
            r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
            html, re.IGNORECASE
        )
        if canonical_match:
            page.canonical_url = canonical_match.group(1)

        # 作者
        author_match = re.search(
            r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']*)["\']',
            html, re.IGNORECASE
        )
        if author_match:
            page.author = author_match.group(1)

        # 發布時間
        published_match = re.search(
            r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']*)["\']',
            html, re.IGNORECASE
        )
        if published_match:
            page.published_time = published_match.group(1)

        # 語言
        lang_match = re.search(r'<html[^>]+lang=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if lang_match:
            page.language = lang_match.group(1)

        # 移除 script/style
        cleaned = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", "", html,
                         flags=re.IGNORECASE | re.DOTALL)
        # 移除所有標籤
        text = re.sub(r"<[^>]+>", " ", cleaned)
        # 正規化空白
        text = re.sub(r"\s+", " ", text).strip()
        # 分段
        paragraphs = [p.strip() for p in text.split("\n") if len(p.strip()) > 20]
        page.paragraphs = paragraphs
        page.main_text = "\n\n".join(paragraphs)
        page.word_count = len(page.main_text)
        return page


__all__ = ["HTMLParser", "ParsedPage"]
