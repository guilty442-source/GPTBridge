from __future__ import annotations

import re


IGNORED_HANDLES = {
    "about",
    "accounts",
    "direct",
    "explore",
    "home",
    "i",
    "intent",
    "messages",
    "notifications",
    "privacy",
    "reel",
    "reels",
    "search",
    "settings",
    "share",
    "stories",
    "terms",
    "tos",
}

HANDLE_PATTERNS = {
    "instagram": re.compile(r"^[A-Za-z0-9._]{1,30}$"),
    "x": re.compile(r"^[A-Za-z0-9_]{1,15}$"),
}
INSTAGRAM_WEB_APP_ID = "936619743392459"
INSTAGRAM_API_PAGE_SIZE = 50
