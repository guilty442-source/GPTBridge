from __future__ import annotations

import re
from typing import Any, Iterable


STAR_ACCOUNT_WORDS = {
    "actor",
    "actress",
    "artist",
    "band",
    "dancer",
    "idol",
    "jpop",
    "kpop",
    "model",
    "performer",
    "singer",
    "trainee",
}

STAR_ACCOUNT_PHRASES = {
    "k pop",
    "j pop",
    "c pop",
    "偶像",
    "女團",
    "女星",
    "明星",
    "歌手",
    "演員",
    "演藝",
    "男團",
    "男星",
    "練習生",
    "舞者",
    "藝人",
    "藝能",
    "韓團",
    "日團",
    "模特",
    "模特兒",
}

NON_STAR_ACCOUNT_WORDS = {
    "agency",
    "cafe",
    "clinic",
    "company",
    "corp",
    "corporation",
    "fanclub",
    "fanpage",
    "hospital",
    "hotel",
    "inc",
    "ltd",
    "magazine",
    "mall",
    "market",
    "media",
    "news",
    "newspaper",
    "outlet",
    "restaurant",
    "shop",
    "store",
    "tourism",
    "travel",
    "wholesale",
}

NON_STAR_ACCOUNT_PHRASES = {
    "不動產",
    "代購",
    "企業",
    "公司",
    "商店",
    "商場",
    "客服",
    "工作機會",
    "批發",
    "折扣",
    "招募",
    "新聞",
    "旅遊",
    "旅行社",
    "日報",
    "活動企劃",
    "物流",
    "房仲",
    "房產",
    "品牌",
    "團購",
    "媒體",
    "官方客服",
    "購物",
    "醫院",
    "診所",
    "餐廳",
    "飯店",
    "電商",
    "雜誌",
    "優惠",
    "零售",
}


def is_star_candidate_account(
    account: dict[str, Any],
    filter_terms: Iterable[str] = (),
) -> tuple[bool, str]:
    if account.get("verified") is True:
        return True, "已認證帳號"

    text = " ".join(
        [
            str(account.get("handle", "")),
            str(account.get("display_name", "")),
            str(account.get("context_text", "")),
        ]
    ).casefold()
    normalized = re.sub(r"[_\-.]+", " ", text)
    words = set(re.findall(r"[a-z0-9]+", normalized))
    handle = str(account.get("handle", "")).strip().lstrip("@").casefold()

    for raw_term in filter_terms:
        term = str(raw_term).strip().casefold()
        if not term:
            continue
        if term.startswith("@"):
            if handle == term[1:].lstrip("@"):
                return False, f"自訂篩選帳號：{raw_term}"
            continue
        if term in text or term in normalized:
            return False, f"自訂篩選關鍵字：{raw_term}"

    matched_word = sorted(words.intersection(NON_STAR_ACCOUNT_WORDS))
    if matched_word:
        return False, f"明顯非明星帳號關鍵字：{matched_word[0]}"

    for phrase in NON_STAR_ACCOUNT_PHRASES:
        if phrase in normalized:
            return False, f"明顯非明星帳號關鍵字：{phrase}"

    matched_star_word = sorted(words.intersection(STAR_ACCOUNT_WORDS))
    if matched_star_word:
        return True, "偶像明星線索"

    for phrase in STAR_ACCOUNT_PHRASES:
        if phrase in normalized:
            return True, "偶像明星線索"
    return True, ""
