from __future__ import annotations

import json
import socket
from typing import Any
from urllib.parse import urlparse


def normalize_remote_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("遠端橋接 URL 必須是 http:// 或 https:// 開頭。")
    hostname = str(parsed.hostname or "").casefold()
    if parsed.scheme != "https" and hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("非本機的遠端橋接 URL 必須使用 https://。")
    return raw.rstrip("/")


def local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        hostname = socket.gethostname()
        for item in socket.gethostbyname_ex(hostname)[2]:
            addresses.add(item)
    except OSError:
        pass

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            addresses.add(str(probe.getsockname()[0]))
        finally:
            probe.close()
    except OSError:
        pass

    return sorted(
        item
        for item in addresses
        if item and not item.startswith("127.") and not item.startswith("169.254.")
    )


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
