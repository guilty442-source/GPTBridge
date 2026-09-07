"""URL 安全驗證 / SSRF 防護。

對應需求：安全限制。網路資料一律視為不可信輸入。
預設禁止：
  - file://
  - ftp://
  - localhost 非必要存取
  - 127.0.0.1 任意探測
  - 私有網段任意探測
只有明確授權的本地服務才能存取（例如本機 SearXNG）。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse


# 允許的 URL scheme
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# 預設禁止的 scheme
_FORBIDDEN_SCHEMES = frozenset({
    "file", "ftp", "ftps", "gopher", "dict", "ldap", "ldaps",
    "sftp", "tftp", "telnet", "ssh", "smb", "nfs", "nntp",
    "rtsp", "ws", "wss", "data", "blob", "javascript", "vbscript",
})

# 私有 / 保留網段（RFC 1918 + RFC 4193 + loopback + link-local + 其他）
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),        # loopback IPv4
    ipaddress.ip_network("10.0.0.0/8"),         # private
    ipaddress.ip_network("172.16.0.0/12"),      # private
    ipaddress.ip_network("192.168.0.0/16"),     # private
    ipaddress.ip_network("169.254.0.0/16"),     # link-local
    ipaddress.ip_network("0.0.0.0/8"),          # current network
    ipaddress.ip_network("100.64.0.0/10"),      # CGNAT
    ipaddress.ip_network("::1/128"),            # loopback IPv6
    ipaddress.ip_network("fc00::/7"),           # unique local IPv6
    ipaddress.ip_network("fe80::/10"),          # link-local IPv6
]

# 明確授權的本地服務（例如本機 SearXNG）
_DEFAULT_ALLOWED_LOCAL = frozenset({
    "127.0.0.1:8080",   # 常見 SearXNG 連接埠
    "127.0.0.1:8888",
    "localhost:8080",
    "localhost:8888",
})


class URLSafetyError(Exception):
    """URL 安全檢查失敗。"""


@dataclass
class URLSafetyConfig:
    """URL 安全策略設定。"""

    allowed_schemes: frozenset[str] = _ALLOWED_SCHEMES
    forbidden_schemes: frozenset[str] = _FORBIDDEN_SCHEMES
    block_private_networks: bool = True
    block_localhost: bool = True
    block_metadata_endpoints: bool = True  # 169.254.169.254 (cloud metadata)
    allowed_local_endpoints: set[str] = field(default_factory=lambda: set(_DEFAULT_ALLOWED_LOCAL))
    max_redirects: int = 5
    allow_redirect_to_private: bool = False


class URLSafetyChecker:
    """URL 安全驗證器。"""

    def __init__(self, config: URLSafetyConfig | None = None) -> None:
        self.config = config or URLSafetyConfig()

    def validate(self, url: str, *, allow_local: bool = False) -> str:
        """驗證 URL 安全性；回傳正規化後的 URL，不安全則拋 URLSafetyError。"""
        if not url or not isinstance(url, str):
            raise URLSafetyError("URL 為空或非字串")

        parsed = urlparse(url.strip())
        scheme = (parsed.scheme or "").lower()

        # scheme 檢查
        if scheme in self.config.forbidden_schemes:
            raise URLSafetyError(f"禁止的 URL scheme: {scheme}://")
        if scheme not in self.config.allowed_schemes:
            raise URLSafetyError(f"不允許的 URL scheme: {scheme}://")

        hostname = parsed.hostname or ""
        if not hostname:
            raise URLSafetyError("URL 缺少 hostname")

        port = parsed.port
        netloc_key = f"{hostname.lower()}:{port}" if port else hostname.lower()

        # 授權本地服務
        is_allowed_local = netloc_key in self.config.allowed_local_endpoints
        if is_allowed_local and allow_local:
            return url

        # localhost / 私有網段檢查
        if self.config.block_localhost:
            if hostname.lower() in ("localhost", "0.0.0.0"):
                if not (is_allowed_local and allow_local):
                    raise URLSafetyError(f"禁止存取 localhost: {hostname}")

        # IP 位址檢查
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            ip = None

        if ip is not None:
            if self.config.block_metadata_endpoints and str(ip) == "169.254.169.254":
                raise URLSafetyError("禁止存取雲端 metadata 端點")
            if self.config.block_private_networks:
                for network in _PRIVATE_NETWORKS:
                    if ip in network:
                        if not (is_allowed_local and allow_local):
                            raise URLSafetyError(
                                f"禁止存取私有/保留網段: {ip} (matches {network})"
                            )

        # DNS 解析後再次檢查（防止 DNS rebinding 到內網）
        if self.config.block_private_networks and ip is None:
            try:
                resolved = socket.getaddrinfo(hostname, None)
                for family, _, _, _, sockaddr in resolved:
                    addr = sockaddr[0]
                    try:
                        resolved_ip = ipaddress.ip_address(addr)
                    except ValueError:
                        continue
                    for network in _PRIVATE_NETWORKS:
                        if resolved_ip in network:
                            if not (is_allowed_local and allow_local):
                                raise URLSafetyError(
                                    f"DNS 解析到私有網段: {hostname} → {resolved_ip}"
                                )
            except socket.gaierror:
                pass  # 無法解析，讓 fetcher 處理

        return url

    def is_safe(self, url: str, *, allow_local: bool = False) -> bool:
        try:
            self.validate(url, allow_local=allow_local)
            return True
        except URLSafetyError:
            return False


__all__ = ["URLSafetyChecker", "URLSafetyError", "URLSafetyConfig"]
